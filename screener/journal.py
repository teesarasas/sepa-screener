"""Trading journal: Supabase storage + position/performance maths.

Tables (see setup/supabase_trades.sql):
  sepa_stock_trade       one row per position
  sepa_stock_trade_exit  one row per sell (partial or full)
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd
import requests

TRADES = "sepa_stock_trade"
EXITS = "sepa_stock_trade_exit"


class JournalError(RuntimeError):
    pass


class SupabaseJournal:
    """Minimal PostgREST client (no extra dependency)."""

    def __init__(self, url: str, key: str):
        if not url or not key:
            raise JournalError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in secrets")
        self.base = url.rstrip("/") + "/rest/v1/"
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _req(self, method: str, table: str, params: dict | None = None, body=None, prefer: str | None = None):
        headers = dict(self.h)
        if prefer:
            headers["Prefer"] = prefer
        r = requests.request(method, self.base + table, headers=headers, params=params, json=body, timeout=20)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise JournalError(f"Supabase {r.status_code}: {msg}")
        return r.json() if r.text else None

    # -- reads
    def trades(self) -> pd.DataFrame:
        return pd.DataFrame(self._req("GET", TRADES, {"select": "*", "order": "entry_date.asc"}) or [])

    def exits(self) -> pd.DataFrame:
        return pd.DataFrame(self._req("GET", EXITS, {"select": "*", "order": "exit_date.asc"}) or [])

    # -- writes
    def add_trade(self, row: dict) -> dict:
        return self._req("POST", TRADES, body=row, prefer="return=representation")[0]

    def add_exit(self, row: dict) -> dict:
        return self._req("POST", EXITS, body=row, prefer="return=representation")[0]

    def update_trade(self, trade_id: int, fields: dict) -> None:
        self._req("PATCH", TRADES, {"id": f"eq.{trade_id}"}, fields, prefer="return=minimal")

    def delete_trade(self, trade_id: int) -> None:
        self._req("DELETE", TRADES, {"id": f"eq.{trade_id}"}, prefer="return=minimal")


# ---------------------------------------------------------------------------
# maths
# ---------------------------------------------------------------------------
def _f(x) -> float:
    try:
        x = float(x)
        return x if math.isfinite(x) else math.nan
    except (TypeError, ValueError):
        return math.nan


def enrich(trades: pd.DataFrame, exits: pd.DataFrame) -> pd.DataFrame:
    """Per-trade totals: shares sold/left, realized P&L, average exit, R per share."""
    if trades.empty:
        return trades
    t = trades.copy()
    for c in ("entry_price", "shares", "initial_stop", "current_stop"):
        t[c] = pd.to_numeric(t[c], errors="coerce")
    t["risk_per_share"] = t["entry_price"] - t["initial_stop"]
    t["risk_dollars"] = t["risk_per_share"] * t["shares"]
    if exits.empty:
        t["shares_sold"] = 0.0
        t["realized_pnl"] = 0.0
        t["avg_exit"] = math.nan
        t["last_exit_date"] = None
    else:
        e = exits.copy()
        e["exit_price"] = pd.to_numeric(e["exit_price"], errors="coerce")
        e["shares"] = pd.to_numeric(e["shares"], errors="coerce")
        e = e.merge(t[["id", "entry_price"]], left_on="trade_id", right_on="id", how="inner", suffixes=("", "_t"))
        e["pnl"] = (e["exit_price"] - e["entry_price"]) * e["shares"]
        e["proceeds"] = e["exit_price"] * e["shares"]
        g = e.groupby("trade_id").agg(shares_sold=("shares", "sum"), realized_pnl=("pnl", "sum"),
                                      proceeds=("proceeds", "sum"), last_exit_date=("exit_date", "max"))
        g["avg_exit"] = g["proceeds"] / g["shares_sold"]
        t = t.merge(g[["shares_sold", "realized_pnl", "avg_exit", "last_exit_date"]],
                    left_on="id", right_index=True, how="left")
        t["shares_sold"] = t["shares_sold"].fillna(0.0)
        t["realized_pnl"] = t["realized_pnl"].fillna(0.0)
    t["shares_left"] = (t["shares"] - t["shares_sold"]).clip(lower=0)
    return t


def suggested_back_stop(entry: float, rps: float, r_now: float) -> float:
    """At 3R lock in +1R, at 4R +2R, and so on."""
    locked = max(1, math.floor(r_now) - 2)
    return entry + locked * rps


def position_advice(row: dict, price: float, warn_days: int | None = None,
                    below_50: bool | None = None) -> tuple[str, str]:
    """(stage label, next action) for an open position, following Minervini's
    'never let a good gain turn into a loss' rules used in this project."""
    entry, rps = _f(row["entry_price"]), _f(row["risk_per_share"])
    stop = _f(row.get("current_stop"))
    if math.isnan(stop):
        stop = _f(row["initial_stop"])
    if math.isnan(price) or rps <= 0:
        return "—", "Price unavailable."
    r_now = (price - entry) / rps
    half_sold = _f(row.get("shares_sold")) > 0
    two_r, three_r = entry + 2 * rps, entry + 3 * rps
    notes = []
    if warn_days is not None:
        notes.append(f"Earnings in {warn_days} trading day(s): decide whether your cushion can absorb a gap.")
    if below_50:
        notes.append("Closed below the 50-day line: a sell signal if volume was heavy.")
    extra = (" " + " ".join(notes)) if notes else ""

    if price <= stop:
        return "🔴 Stop hit", f"Price is at or below your stop (${stop:,.2f}). Sell per your rules." + extra
    if r_now < 2:
        return (f"⚪ {r_now:+.1f}R",
                f"Hold. Stop ${stop:,.2f}. At 2R (${two_r:,.2f}) sell half and raise the stop to breakeven "
                f"(${entry:,.2f})." + extra)
    if r_now < 3:
        if not half_sold or stop < entry:
            return (f"🟡 {r_now:+.1f}R",
                    f"2R reached: sell half (if not done) and raise the stop to breakeven ${entry:,.2f}. "
                    f"At 3R (${three_r:,.2f}) set a back stop." + extra)
        return (f"🟢 {r_now:+.1f}R", f"Stop at breakeven or better. At 3R (${three_r:,.2f}) set a back stop." + extra)
    back = suggested_back_stop(entry, rps, r_now)
    if stop < back - 1e-9:
        return (f"🟢 {r_now:+.1f}R",
                f"Raise the back stop to ${back:,.2f} (locks in {(back - entry) / rps:.0f}R). "
                "Consider selling more into strength." + extra)
    return (f"🟢 {r_now:+.1f}R",
            f"Back stop ${stop:,.2f} is in place. Raise it as the stock climbs; sell into strength or on a "
            "sell signal." + extra)


def performance(closed: pd.DataFrame) -> dict:
    """Minervini's core stats on closed trades."""
    if closed.empty:
        return {}
    c = closed.copy()
    c["ret"] = c["realized_pnl"] / (c["entry_price"] * c["shares"])
    c["r_mult"] = c["realized_pnl"] / c["risk_dollars"]
    wins, losses = c[c["realized_pnl"] > 0], c[c["realized_pnl"] <= 0]
    avg_gain = wins["ret"].mean() if len(wins) else math.nan
    avg_loss = losses["ret"].mean() if len(losses) else math.nan
    return {
        "trades": len(c),
        "win_rate": len(wins) / len(c),
        "avg_gain": avg_gain,
        "avg_loss": avg_loss,
        "gain_loss_ratio": (avg_gain / abs(avg_loss)) if len(wins) and len(losses) and avg_loss != 0 else math.nan,
        "avg_r": c["r_mult"].mean(),
        "expectancy_r": c["r_mult"].mean(),
        "total_pnl": c["realized_pnl"].sum(),
        "best": c.loc[c["ret"].idxmax(), "symbol"] + f" {c['ret'].max():+.1%}",
        "worst": c.loc[c["ret"].idxmin(), "symbol"] + f" {c['ret'].min():+.1%}",
    }


def by_setup(closed: pd.DataFrame, column: str) -> pd.DataFrame:
    if closed.empty or column not in closed:
        return pd.DataFrame()
    c = closed.copy()
    c["r_mult"] = c["realized_pnl"] / c["risk_dollars"]
    c[column] = c[column].fillna("not recorded").astype(str)
    g = c.groupby(column).agg(trades=("id", "count"),
                              win_rate=("realized_pnl", lambda s: (s > 0).mean()),
                              avg_r=("r_mult", "mean"),
                              pnl=("realized_pnl", "sum")).reset_index()
    return g.sort_values("trades", ascending=False)


def today_iso() -> str:
    return date.today().isoformat()
