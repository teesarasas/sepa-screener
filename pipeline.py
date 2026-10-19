"""Data pipeline. Run by GitHub Actions; can also be run by hand.

    python pipeline.py weekly            # full screen of the liquid universe
    python pipeline.py weekly --rebuild  # also re-check all US stocks for liquidity
    python pipeline.py daily             # check the watchlist against its pivots
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from datetime import date, datetime, timezone

import pandas as pd

import config as C
from screener import fundamentals as F
from screener import market as M
from screener.indicators import EMPTY_VCP, detect_vcp, entry_status, rs_raw, rs_rating, trend_template
from screener.td_client import TwelveDataClient

log = logging.getLogger("pipeline")

UNIVERSE_PATH = C.DATA_DIR / "universe.csv"
SCREEN_PATH = C.DATA_DIR / "screen.csv"
WATCH_PATH = C.DATA_DIR / "watchlist.csv"
MARKET_PATH = C.DATA_DIR / "market.json"
META_PATH = C.DATA_DIR / "run_meta.json"
SIGNALS_PATH = C.DATA_DIR / "signals_log.csv"

MARKET_SYMBOLS = list(C.BENCHMARKS) + list(C.SECTOR_ETFS)
WATCH_STATUSES = {"Forming", "Ready", "Breakout", "Breakout (weak volume)", "Extended"}
SYMBOL_OK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default


def save_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def clean(o):
        if isinstance(o, float) and not math.isfinite(o):
            return None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o

    path.write_text(json.dumps(clean(data), indent=1, default=str))


def fetch_prices(client, symbols: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    size = max(1, min(C.BATCH_SIZE, client.credits_per_min))
    batches = [symbols[i:i + size] for i in range(0, len(symbols), size)]
    t0 = time.time()
    for k, batch in enumerate(batches, 1):
        got = client.time_series(batch, outputsize=C.HISTORY_BARS)
        out.update(got)
        if k % 5 == 0 or k == len(batches):
            mins = (time.time() - t0) / 60
            log.info("prices: batch %d/%d, %d symbols ok, %.1f min", k, len(batches), len(out), mins)
    missing = len(symbols) - len(out)
    if missing:
        log.info("prices: %d symbols returned no data", missing)
    return out


def last_bar_date(prices: dict[str, pd.DataFrame]) -> str | None:
    spy = prices.get("SPY")
    if spy is not None and len(spy):
        return spy.index[-1].strftime("%Y-%m-%d")
    dates = [df.index[-1] for df in prices.values() if len(df)]
    return max(dates).strftime("%Y-%m-%d") if dates else None


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def drop_duplicate_tickers(client, prices: dict[str, pd.DataFrame], symbols: list[str]) -> set[str]:
    """Old tickers of renamed or merged companies (e.g. CDEV -> PR) can still
    be listed and return the same price history as the new ticker. Group
    symbols with identical recent bars and keep one per group: the one whose
    company profile has a sector, then the shorter / alphabetically first."""
    groups: dict[tuple, list[str]] = {}
    for sym in symbols:
        df = prices.get(sym)
        if df is None or len(df) < 30:
            continue
        tail = df.tail(30)
        key = (tail.index[-1],
               tuple(tail["close"].round(4)),
               tuple(tail["volume"].fillna(-1).round(0)))
        groups.setdefault(key, []).append(sym)
    dupes = [g for g in groups.values() if len(g) > 1]
    if not dupes:
        return set()
    profs = F.profiles_for(client, sorted({s for g in dupes for s in g}))
    dropped: set[str] = set()
    for g in dupes:
        keep = sorted(g, key=lambda s: (not profs.get(s, {}).get("sector"), len(s), s))[0]
        gone = [s for s in g if s != keep]
        dropped.update(gone)
        log.info("duplicate price history: keeping %s, dropping %s", keep, ", ".join(gone))
    return dropped


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------
def universe_candidates(client, rebuild: bool, meta: dict) -> tuple[pd.DataFrame, bool]:
    built = meta.get("universe_built")
    stale = (not built) or (date.today() - date.fromisoformat(built)).days >= C.UNIVERSE_REBUILD_DAYS
    if UNIVERSE_PATH.exists() and not (rebuild or stale):
        return pd.read_csv(UNIVERSE_PATH), False

    log.info("rebuilding universe from all US common stocks")
    raw = client.stocks()
    if raw.empty:
        raise RuntimeError("/stocks returned nothing; check the API key")
    raw = raw[raw["exchange"].isin(C.EXCHANGES)]
    raw = raw[raw["symbol"].astype(str).str.match(SYMBOL_OK)]
    raw = raw.drop_duplicates("symbol")[["symbol", "name", "exchange"]]
    log.info("universe candidates: %d", len(raw))
    return raw.reset_index(drop=True), True


# ---------------------------------------------------------------------------
# weekly
# ---------------------------------------------------------------------------
def run_weekly(client, rebuild: bool = False) -> None:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta = load_json(META_PATH, {})
    cands, rebuilding = universe_candidates(client, rebuild, meta)
    names = cands.set_index("symbol")[["name", "exchange"]].to_dict("index")

    syms = cands["symbol"].tolist()
    prices = fetch_prices(client, syms + [s for s in MARKET_SYMBOLS if s not in names])
    as_of = last_bar_date(prices)

    # --- trend template + liquidity -------------------------------------
    rows = []
    for sym in syms:
        df = prices.get(sym)
        if df is None or len(df) < 60:
            continue
        tt = trend_template(df)
        if not tt["liquid"]:
            continue
        tt["symbol"] = sym
        tt["rs_raw"] = rs_raw(df["close"])
        rows.append(tt)
    screen = pd.DataFrame(rows)
    if screen.empty:
        raise RuntimeError("no liquid stocks scored; aborting without overwriting data")
    dropped = drop_duplicate_tickers(client, prices, screen["symbol"].tolist())
    if dropped:
        screen = screen[~screen["symbol"].isin(dropped)]
    log.info("liquid stocks scored: %d", len(screen))

    if rebuilding:
        liquid = cands[cands["symbol"].isin(screen["symbol"])]
        liquid.to_csv(UNIVERSE_PATH, index=False)
        meta["universe_built"] = date.today().isoformat()
        log.info("universe saved: %d liquid stocks", len(liquid))

    screen["rs_rating"] = rs_rating(screen["rs_raw"])
    screen["c8"] = screen["rs_rating"] >= C.MIN_RS_RATING
    crit = [f"c{i}" for i in range(1, 9)]
    screen[crit] = screen[crit].fillna(False).astype(bool)
    screen["tt_score"] = screen[crit].sum(axis=1)
    screen["tt_pass"] = screen["tt_score"] == 8
    log.info("Trend Template passes: %d", int(screen["tt_pass"].sum()))

    # --- VCP on Trend Template passes -----------------------------------
    vcp_rows = []
    for sym in screen.loc[screen["tt_pass"], "symbol"]:
        v = detect_vcp(prices[sym])
        v["symbol"] = sym
        vcp_rows.append(v)
    vcp = pd.DataFrame(vcp_rows) if vcp_rows else pd.DataFrame(columns=["symbol", *EMPTY_VCP])
    screen = screen.merge(vcp, on="symbol", how="left")
    screen["vcp"] = screen["vcp"].fillna(False).astype(bool)

    # --- fundamentals for passes: VCP setups first, then by RS ------------
    passes = screen[screen["tt_pass"]].sort_values(["vcp", "rs_rating"], ascending=[False, False])
    earn, prof, spent = F.refresh(client, passes["symbol"].tolist(), C.FUNDAMENTAL_CREDIT_BUDGET)
    log.info("fundamentals: %d credits", spent)

    eps_cache = {s: F.metrics_for(earn.get(s)) for s in screen["symbol"]}
    eps = pd.DataFrame([{"symbol": s, **m} for s, m in eps_cache.items()])
    screen = screen.merge(eps, on="symbol", how="left")
    screen["eps_pass"] = [F.eps_pass(m) if m else None for m in eps_cache.values()]
    screen["sector"] = [F.normalize_sector(prof.get(s, {}).get("sector")) for s in screen["symbol"]]
    screen["industry"] = [prof.get(s, {}).get("industry", "") for s in screen["symbol"]]
    screen["name"] = [names.get(s, {}).get("name", "") for s in screen["symbol"]]
    screen["exchange"] = [names.get(s, {}).get("exchange", "") for s in screen["symbol"]]
    screen["as_of"] = as_of

    screen = screen.sort_values(["tt_pass", "vcp", "rs_rating"], ascending=False)
    screen.to_csv(SCREEN_PATH, index=False, float_format="%.6g")

    # --- watchlist --------------------------------------------------------
    old = pd.read_csv(WATCH_PATH) if WATCH_PATH.exists() else pd.DataFrame()
    wl = screen[screen["vcp"] & screen["vcp_status"].isin(WATCH_STATUSES)].copy()
    for col in ("eps_note", "eps_pass"):
        if col not in wl:
            wl[col] = None
    wl = wl[["symbol", "name", "sector", "rs_rating", "pivot", "pivot_date", "stop", "risk_pct",
             "close", "dist_to_pivot", "entry_risk", "vcp_status", "breakout_date", "breakout_vol_ratio",
             "contractions", "depths", "eps_pass", "eps_note"]].copy()
    wl["added"] = as_of
    if not old.empty and "added" in old:
        keep_added = old.set_index("symbol")["added"].to_dict()
        wl["added"] = [keep_added.get(s, as_of) for s in wl["symbol"]]
    wl["tt_ok"] = True
    wl["updated"] = as_of
    _log_new_breakouts(old, wl, as_of)
    wl.to_csv(WATCH_PATH, index=False, float_format="%.6g")
    log.info("watchlist: %d names", len(wl))

    # --- market context ---------------------------------------------------
    mk = M.build(prices)
    passers = screen[screen["tt_pass"]]
    mk["breadth"] = {
        "liquid": int(len(screen)),
        "tt_pass": int(screen["tt_pass"].sum()),
        "pct_above_200": float((screen["close"] > screen["sma200"]).mean()),
        "pct_above_50": float((screen["close"] > screen["sma50"]).mean()),
        "vcp_setups": int(len(wl)),
        "tt_pass_by_sector": passers["sector"].replace("", "Unknown").value_counts().to_dict(),
    }
    mk["as_of"] = as_of
    mk["weekly_scan_at"] = now_utc()
    mk["daily_check_at"] = None
    save_json(MARKET_PATH, mk)

    meta.update(last_weekly=now_utc(), last_weekly_credits=client.credits_used, as_of=as_of)
    save_json(META_PATH, meta)
    log.info("weekly done: %d credits", client.credits_used)


# ---------------------------------------------------------------------------
# daily
# ---------------------------------------------------------------------------
def run_daily(client) -> None:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wl = pd.read_csv(WATCH_PATH) if WATCH_PATH.exists() else pd.DataFrame()
    syms = wl["symbol"].tolist() if not wl.empty else []
    prices = fetch_prices(client, syms + MARKET_SYMBOLS)
    as_of = last_bar_date(prices)

    if not wl.empty:
        old = wl.copy()
        if "entry_risk" not in wl:
            wl["entry_risk"] = float("nan")
        for col in ("vcp_status", "breakout_date", "updated"):
            wl[col] = wl[col].astype(object)
        for i, r in wl.iterrows():
            df = prices.get(r["symbol"])
            if df is None or not len(df):
                continue
            pdate = r.get("pivot_date") if isinstance(r.get("pivot_date"), str) else None
            st = entry_status(df, float(r["pivot"]), float(r["stop"]), pdate)
            tt = trend_template(df)
            wl.at[i, "close"] = float(df["close"].iloc[-1])
            wl.at[i, "dist_to_pivot"] = st["dist_to_pivot"]
            wl.at[i, "entry_risk"] = st["entry_risk"]
            wl.at[i, "vcp_status"] = st["vcp_status"]
            wl.at[i, "breakout_date"] = st["breakout_date"]
            wl.at[i, "breakout_vol_ratio"] = st["breakout_vol_ratio"]
            wl.at[i, "tt_ok"] = bool(all(tt.get(f"c{k}") for k in range(1, 8)))
            wl.at[i, "updated"] = df.index[-1].strftime("%Y-%m-%d")
        _log_new_breakouts(old, wl, as_of)
        wl.to_csv(WATCH_PATH, index=False, float_format="%.6g")

    mk = load_json(MARKET_PATH, {})
    fresh = M.build(prices)
    mk["indexes"], mk["sectors"] = fresh["indexes"], fresh["sectors"]
    mk["daily_as_of"] = as_of
    mk["daily_check_at"] = now_utc()
    save_json(MARKET_PATH, mk)

    meta = load_json(META_PATH, {})
    meta.update(last_daily=now_utc(), last_daily_credits=client.credits_used)
    save_json(META_PATH, meta)
    log.info("daily done: %d symbols, %d credits", len(syms), client.credits_used)


def _log_new_breakouts(old: pd.DataFrame, new: pd.DataFrame, as_of: str | None) -> None:
    prev = old.set_index("symbol")["vcp_status"].to_dict() if not old.empty else {}
    hits = new[new["vcp_status"].astype(str).str.startswith("Breakout")]
    hits = hits[[not str(prev.get(s, "")).startswith("Breakout") for s in hits["symbol"]]]
    if hits.empty:
        return
    rec = hits[["symbol", "vcp_status", "pivot", "close", "breakout_vol_ratio", "stop"]].copy()
    rec.insert(0, "date", as_of)
    rec.to_csv(SIGNALS_PATH, mode="a", header=not SIGNALS_PATH.exists(), index=False, float_format="%.6g")
    log.info("new breakouts: %s", ", ".join(rec["symbol"]))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["weekly", "daily"])
    p.add_argument("--rebuild", action="store_true", help="re-check all US stocks for liquidity")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = TwelveDataClient(C.API_KEY, C.CREDITS_PER_MIN)
    if a.mode == "weekly":
        run_weekly(client, a.rebuild)
    else:
        run_daily(client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
