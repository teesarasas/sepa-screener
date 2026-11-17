"""Minervini SEPA screener dashboard (Streamlit).

Reads the files the GitHub Action writes to data/. Only the Chart tab calls
Twelve Data (1 credit per symbol, cached), using the key in Streamlit secrets.
"""
from __future__ import annotations

import hmac
import json
import math
from datetime import date, datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

import config as C
from screener import journal as J
from screener.td_client import values_to_frame

# Defaults for settings added after the first release. If config.py on GitHub
# is older than app.py (e.g. a partial upload), the app still runs.
_DEFAULTS = {
    "MAX_ENTRY_RISK": 0.08, "VCP_MAX_CHASE": 0.10, "EPS_MIN_BASE": 0.05,
    "MAX_CORRECTION_MULT": 2.5, "MIN_MARKET_CORRECTION": 0.03, "RS_LINE_NEW_HIGH_TOL": 0.01,
    "EXCLUDE_SYMBOLS": {"CDEV": "renamed PR", "OAS": "merged into CHRD"},
    "EARNINGS_WARN_DAYS": 7,
}
_STALE_CONFIG = [k for k in _DEFAULTS if not hasattr(C, k)]
for _k in _STALE_CONFIG:
    setattr(C, _k, _DEFAULTS[_k])

st.set_page_config(page_title="SEPA Screener", page_icon="📈", layout="wide")

STATUS_ORDER = ["Breakout", "Ready", "Breakout (weak volume)", "Forming", "Extended", "Failed"]
STATUS_LABEL = {
    "Breakout": "🟢 Breakout",
    "Ready": "🟡 Ready",
    "Breakout (weak volume)": "🟠 Breakout, weak volume",
    "Forming": "⚪ Forming",
    "Extended": "🔵 Extended",
    "Failed": "🔴 Failed",
}
CRITERIA = {
    "c1": "Price above 150- and 200-day MA",
    "c2": "150-day MA above 200-day MA",
    "c3": "200-day MA rising ≥ 1 month",
    "c4": "50-day MA above 150- and 200-day MA",
    "c5": "Price above 50-day MA",
    "c6": f"≥ {C.MIN_ABOVE_52W_LOW:.0%} above 52-week low",
    "c7": f"Within {C.MAX_BELOW_52W_HIGH:.0%} of 52-week high",
    "c8": f"RS rating ≥ {C.MIN_RS_RATING}",
}
# validated categorical slots (light / dark) for the three moving averages
MA_COLORS = {
    "light": {"SMA50": "#2a78d6", "SMA150": "#eb6834", "SMA200": "#6250d6"},
    "dark": {"SMA50": "#3987e5", "SMA150": "#d95926", "SMA200": "#9085e9"},
}


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------
def secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except Exception:  # no secrets file locally
        return default


def password_gate() -> None:
    pw = secret("APP_PASSWORD")
    if not pw or st.session_state.get("auth_ok"):
        return
    st.title("SEPA Screener")
    entered = st.text_input("Password", type="password")
    # compare as UTF-8 bytes: compare_digest rejects non-ASCII text (e.g. Thai characters)
    if entered and hmac.compare_digest(entered.encode("utf-8"), pw.encode("utf-8")):
        st.session_state["auth_ok"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password")
    st.stop()


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def _mtime(path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_csv(path_str: str, mtime: float) -> pd.DataFrame:
    try:
        return pd.read_csv(path_str)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()


@st.cache_data(show_spinner=False)
def load_json(path_str: str, mtime: float) -> dict:
    try:
        with open(path_str) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def data(name: str):
    p = C.DATA_DIR / name
    if name.endswith(".json"):
        return load_json(str(p), _mtime(p))
    return load_csv(str(p), _mtime(p))


@st.cache_data(ttl=6 * 3600, show_spinner="Loading chart data…")
def fetch_bars(symbol: str, api_key: str) -> pd.DataFrame | None:
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={"symbol": symbol, "interval": "1day", "outputsize": 300, "apikey": api_key},
        timeout=30,
    )
    payload = r.json()
    if payload.get("status") == "error":
        raise RuntimeError(payload.get("message", "Twelve Data error"))
    return values_to_frame(payload.get("values", []))


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------
def num(x) -> float:
    try:
        x = float(x)
        return x if math.isfinite(x) else math.nan
    except (TypeError, ValueError):
        return math.nan


def pct(x, digits=1) -> str:
    x = num(x)
    return "—" if math.isnan(x) else f"{x * 100:+.{digits}f}%"


def money(x) -> str:
    x = num(x)
    return "—" if math.isnan(x) else f"${x:,.2f}"


def eps_label(passed, note) -> str:
    note = note if isinstance(note, str) else ""
    if passed is True or passed == "True":
        return "✓"
    if "turnaround" in note:
        return "turnaround"
    if "too small" in note:
        return "✗ tiny base"
    if passed is False or passed == "False":
        return "✗"
    return "pending"


def truthy(x) -> bool:
    return x is True or x == "True" or (isinstance(x, (int, float)) and not isinstance(x, bool) and x == 1)


def corr_label(ratio) -> str:
    r = num(ratio)
    if math.isnan(r):
        return "—"
    mark = "✓" if r <= C.MAX_CORRECTION_MULT else "✗"
    return f"{mark} {r:.1f}×"


def rs_line_label(high, leads) -> str:
    if truthy(leads):
        return "★ leads price"
    if truthy(high):
        return "✓ new high"
    if high is None or (isinstance(high, float) and math.isnan(high)):
        return "—"
    return "✗"


def trading_days_until(d) -> int | None:
    """Trading days from today until date d (weekends skipped; US holidays not)."""
    if not isinstance(d, str) or not d:
        return None
    try:
        target = date.fromisoformat(d[:10])
    except ValueError:
        return None
    return int(np.busday_count(date.today(), target))


def earnings_label(d) -> str:
    n = trading_days_until(d)
    if n is None or n < 0:
        return "—"
    when = date.fromisoformat(d[:10]).strftime("%b %d")
    if n <= C.EARNINGS_WARN_DAYS:
        return f"⚠️ {when} ({'today' if n == 0 else f'{n}d'})"
    return when


def soon_reporting(df: pd.DataFrame) -> list[str]:
    if "next_report" not in df:
        return []
    out = []
    for sym, d in zip(df["symbol"], df["next_report"]):
        n = trading_days_until(d)
        if n is not None and 0 <= n <= C.EARNINGS_WARN_DAYS:
            out.append(f"{sym} ({'today' if n == 0 else f'{n}d'})")
    return out


GLOSSARY = {
    "signals": f"""
**Statuses**
- 🟢 **Breakout**: closed above the pivot on ≥ {C.BREAKOUT_VOL_MULT:g}× average volume, and buying now keeps the stop within {C.MAX_ENTRY_RISK:.0%}. The buy signal.
- 🟠 **Breakout, weak volume**: above the pivot but volume didn't confirm. Many fall back; treat with suspicion.
- 🟡 **Ready**: within {C.VCP_READY_WITHIN:.0%} *below* the pivot. Not a buy yet; set a buy-stop order at the pivot.
- ⚪ **Forming**: base still developing.
- 🔵 **Extended**: already broke out and ran too far. Buying now would put the stop more than {C.MAX_ENTRY_RISK:.0%} away. Don't chase; wait for a pullback or a new base.
- 🔴 **Failed**: closed below the stop. Setup is broken.

**Columns**
- **Pivot**: the buy point, i.e. the top of the last, tightest contraction in the base.
- **Stop**: where the setup is proven wrong (low of the last contraction, max {C.MAX_STOP_PCT:.0%} below the pivot).
- **Risk at entry**: how far the stop is if you buy now (or at the pivot, if not broken out). Keep it under {C.MAX_ENTRY_RISK:.0%}.
- **RS**: 1–99 rank of price performance vs all liquid US stocks (last 3 months weighted double). 90+ = leader.
- **RS line**: stock ÷ S&P 500. ✓ = at a 52-week high. ★ = at a high *before* the price is, the earliest leader sign.
- **Fell vs SPY**: stock's deepest drop since the S&P 500's high ÷ the index's drop. ✓ = {C.MAX_CORRECTION_MULT:g}× or less (held up). "—" = market isn't really correcting.
- **EPS**: ✓ = latest quarter's earnings up ≥ {C.MIN_EPS_GROWTH:.0%} year on year. *turnaround* = loss to profit. *tiny base* = year-ago EPS too small for a % to mean anything.
- **Earnings**: next report date. ⚠️ = within {C.EARNINGS_WARN_DAYS} trading days. A report can gap a stock 10–20% either way; avoid new buys before it, and don't hold through it without a profit cushion.
- **Trend**: whether the Trend Template (price above rising 50/150/200-day lines) still holds today.

**The best setups have all of these:** 🟢 Breakout · RS 90+ · RS line ✓/★ · EPS ✓ · leading sector · no ⚠️ earnings · market banner green.
""",
    "screener": f"""
- **TT**: how many of the 8 Trend Template criteria pass. Only 8/8 is Stage 2. Tick *Show each criterion* to see which failed.
- **RS**, **RS line**, **Fell vs SPY**: see the Signals tab panel. The two toggles keep only stocks that held up or have the RS line at a new high, i.e. the leaders.
- **Above 52w low / Below 52w high**: Minervini wants ≥ {C.MIN_ABOVE_52W_LOW:.0%} above the low and within {C.MAX_BELOW_52W_HIGH:.0%} of the high.
- **200MA rising (mo)**: months the 200-day line has been rising. 4–5+ is ideal.
- **VCP / vs pivot / Risk at entry**: the base and buy point (VCP stocks only).
- **EPS YoY / EPS accel. / Beats in a row**: earnings growth vs the same quarter last year, whether it's speeding up, and how many quarters in a row it beat estimates.
- **Next report**: ⚠️ = within {C.EARNINGS_WARN_DAYS} trading days.
- Fundamentals are fetched only for 8/8 passes. Sales growth and margins aren't on the Grow plan; check them on Finviz for your final picks.
""",
    "chart": """
- **Candles**: green = closed up, red = closed down. **Lines**: blue 50-day, orange 150-day, purple 200-day moving average. In Stage 2 the price is above all three and they're stacked 50 > 150 > 200.
- **Dashed grey line**: pivot (buy point). **Dotted red line**: stop.
- **Volume panel**: grey line is the 50-day average. A real breakout has volume well above it; the right side of a good base has volume well *below* it ("dry-up").
- Confirm the base by eye: each pullback should be smaller than the one before, and the last one tight and quiet.
""",
    "sectors": """
- **Table**: SPDR sector ETFs ranked by the same weighted performance used for stock RS. Prefer stocks from the top 2–3 sectors; avoid sectors below their 200-day line.
- **Where the leaders are**: how many stocks in each sector pass all 8 Trend Template criteria. Leadership usually clusters in a few groups.
""",
    "journal": """
- **Log a trade** when you buy. The setup at entry (signal, RS, RS line, EPS, sector, market state) is filled in from the dashboard automatically, so later you can see which kinds of setups actually make you money.
- **R** = your initial risk per share (entry − initial stop). **+2R** means the stock has gained twice what you risked.
- **Exit plan** (Minervini's "never let a good gain turn into a loss"): at **2R** sell half and raise the stop to breakeven; at **3R** set a back stop that locks in +1R, then +2R at 4R, and so on. Sell everything if the stop is hit.
- **Record a sell** for every partial or full exit. The trade closes automatically when no shares are left.
- **Stops only move up.** The form won't let you lower one.
- **Performance** uses closed trades only. Win rate, average gain vs average loss, and average R are the numbers Minervini tracks; they tell you whether to trade bigger or smaller.
""",
    "sizing": """
- **Shares** = (account × risk per trade) ÷ (entry − stop). This keeps every loss the same size in dollars, whatever the stock's price or stop distance.
- **Risk per trade**: Minervini uses roughly 1–2.5% of the account. Start at the low end, and use quarter-size positions while the market banner isn't green.
- **Position size**: Full, Half or Quarter. Defaults to a quarter-size "pilot" position unless the market banner is green.
- **Warnings**: stop more than 10% away = don't take the trade. Positions are capped at 25% of the account.
- **Take-profit plan**: targets in R (risk per share): sell half at **2R** and move the stop to breakeven; at **3R** set a back stop at +1R; then raise it 1R for each R gained. Once you have 5+ closed trades in the Journal, your own average winner is shown as a target too.
""",
}


def how_to_read(key: str) -> None:
    with st.expander("How to read this", icon="ℹ️"):
        st.markdown(GLOSSARY[key])


def theme_type() -> str:
    try:
        t = st.context.theme.type
        return t if t in ("light", "dark") else "light"
    except Exception:
        return "light"


def days_old(d: str | None) -> int | None:
    if not d:
        return None
    try:
        return (date.today() - date.fromisoformat(str(d)[:10])).days
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------
def header(mk: dict, meta: dict) -> None:
    st.title("SEPA Screener")
    as_of = mk.get("daily_as_of") or mk.get("as_of")
    bits = [f"Prices as of **{as_of or '—'}** (US close)"]
    if mk.get("weekly_scan_at"):
        bits.append(f"weekly scan {mk['weekly_scan_at']}")
    if mk.get("daily_check_at"):
        bits.append(f"daily check {mk['daily_check_at']}")
    st.caption(" · ".join(bits))
    age = days_old(as_of)
    if age is not None and age > 5:
        st.warning(f"Data is {age} days old. Check the GitHub Actions run history.", icon="⚠️")


def market_state(mk: dict) -> str:
    trend = {s: v.get("trend") for s, v in (mk.get("indexes") or {}).items()}
    lead = [s for s in ("SPY", "QQQ") if s in trend]
    if not lead:
        return "unknown"
    if all(trend[s] == "Uptrend" for s in lead):
        return "uptrend"
    if any(trend[s] == "Downtrend" for s in lead):
        return "downtrend"
    return "correction"


def market_strip(mk: dict) -> None:
    idx = mk.get("indexes", {})
    br = mk.get("breadth", {})
    if not idx:
        st.info("No market data yet. Run the weekly scan once (see README).")
        return
    trend = {s: v.get("trend") for s, v in idx.items()}
    lead = [s for s in ("SPY", "QQQ") if s in trend]
    lagging = [s for s, t in trend.items() if t != "Uptrend"]
    if lead and all(trend[s] == "Uptrend" for s in lead):
        note = f" {', '.join(lagging)} still lagging." if lagging else ""
        st.success("**Market: uptrend.** S&P 500 and Nasdaq above rising 50- and 200-day MAs. "
                   f"Breakouts have the odds on their side.{note}", icon="✅")
    elif any(trend[s] == "Downtrend" for s in lead):
        down = ", ".join(s for s in lead if trend[s] == "Downtrend")
        st.error(f"**Market: downtrend.** {down} below its 200-day MA. Minervini stays mostly in cash here; "
                 "most breakouts fail.", icon="⛔")
    else:
        below50 = ", ".join(lagging) or "Indexes"
        st.warning(f"**Market: correction / under pressure.** {below50} not in a clean uptrend "
                   "(below the 50-day MA or 200-day). Only small pilot positions in true leaders "
                   "(RS line at a new high, held up vs SPY, volume-confirmed breakout), or wait for the "
                   "indexes to reclaim their 50-day lines.", icon="⚠️")

    icon = {"Uptrend": "✓", "Under pressure": "~", "Downtrend": "✗"}
    cols = st.columns(6)
    for col, (sym, v) in zip(cols, idx.items()):
        col.metric(f"{sym} · {icon.get(v.get('trend'), '')} {v.get('trend', '')}", money(v.get("close")),
                   pct(v.get("chg_1w")) + " 1w",
                   help=f"{v.get('name', sym)} · above 50-day: {v.get('above_50')} · above 200-day: {v.get('above_200')}")
    corr = mk.get("correction") or {}
    if corr.get("since") and num(corr.get("spy_depth")) >= C.MIN_MARKET_CORRECTION:
        st.caption(f"S&P 500 has fallen up to **{num(corr['spy_depth']):.1%}** since its high on {corr['since']}. "
                   f"Leaders are the stocks that fell no more than {C.MAX_CORRECTION_MULT:g}× that "
                   "and whose RS line is at a new high.")
    k = len(idx)
    cols[k].metric("Trend Template passes", br.get("tt_pass", "—"), help=f"Out of {br.get('liquid', '—')} liquid stocks")
    cols[k + 1].metric("Stocks above 200-day", pct(br.get("pct_above_200"), 0).lstrip("+"),
                       help="Breadth of the liquid universe")
    cols[k + 2].metric("VCP setups", br.get("vcp_setups", "—"), help="On the watchlist this week")


def tab_signals(wl: pd.DataFrame, signals: pd.DataFrame) -> None:
    how_to_read("signals")
    if wl.empty:
        st.info("Watchlist is empty. Either the weekly scan hasn't run yet, or no stock passed the Trend Template with a VCP base this week.")
        return
    st.caption("Checked after every US close against the pivot set by the weekly scan.")
    soon = soon_reporting(wl)
    if soon:
        st.warning(f"**Earnings within {C.EARNINGS_WARN_DAYS} trading days:** {', '.join(soon)}. "
                   "Avoid new buys before the report; if you hold one, decide now whether your profit "
                   "cushion is big enough to hold through it.", icon="📅")
    df = wl.copy()
    df["Earnings"] = [earnings_label(d) for d in df.get("next_report", [None] * len(df))]
    df["_order"] = df["vcp_status"].map({s: i for i, s in enumerate(STATUS_ORDER)}).fillna(99)
    df = df.sort_values(["_order", "dist_to_pivot"], ascending=[True, False])
    df["Status"] = df["vcp_status"].map(STATUS_LABEL).fillna(df["vcp_status"])
    df["Trend"] = df["tt_ok"].map({True: "✓ intact", False: "✗ broken"})
    df["EPS"] = [eps_label(p, n) for p, n in zip(df["eps_pass"], df.get("eps_note", [None] * len(df)))]
    if "entry_risk" not in df:
        df["entry_risk"] = df["risk_pct"]
    df["sector"] = df["sector"].fillna("—").replace("", "—")
    df["Fell vs SPY"] = [corr_label(r) for r in df.get("corr_ratio", [math.nan] * len(df))]
    df["RS line"] = [rs_line_label(h, l) for h, l in zip(df.get("rs_line_high", [None] * len(df)),
                                                        df.get("rs_line_leads", [None] * len(df)))]
    show = df[["symbol", "name", "Status", "close", "pivot", "dist_to_pivot", "stop", "entry_risk",
               "breakout_vol_ratio", "rs_rating", "RS line", "Fell vs SPY", "EPS", "Earnings", "Trend",
               "sector", "depths", "updated"]]
    st.dataframe(
        show, hide_index=True, width="stretch",
        column_config={
            "symbol": st.column_config.TextColumn("Symbol", pinned=True),
            "name": "Name",
            "close": st.column_config.NumberColumn("Close", format="$%.2f"),
            "pivot": st.column_config.NumberColumn("Pivot", format="$%.2f"),
            "dist_to_pivot": st.column_config.NumberColumn("vs pivot", format="percent"),
            "stop": st.column_config.NumberColumn("Stop", format="$%.2f"),
            "entry_risk": st.column_config.NumberColumn(
                "Risk at entry", format="percent",
                help=f"Distance to the stop if bought at max(close, pivot). Above {C.MAX_ENTRY_RISK:.0%} = too far."),
            "breakout_vol_ratio": st.column_config.NumberColumn("Breakout vol ×", format="%.1f×"),
            "rs_rating": st.column_config.NumberColumn("RS", format="%d"),
            "sector": "Sector", "depths": "Contractions", "updated": "Last bar",
            "RS line": st.column_config.TextColumn(
                "RS line", help="Stock ÷ S&P 500. ★ = RS line at a 52-week high before the price is (classic leader tell)."),
            "Fell vs SPY": st.column_config.TextColumn(
                "Fell vs SPY", help=f"Stock's deepest fall since the S&P 500's high ÷ the S&P 500's fall. "
                                    f"✓ = {C.MAX_CORRECTION_MULT:g}× or less (held up)."),
        },
    )
    if not signals.empty:
        st.subheader("Breakout log")
        st.dataframe(signals.sort_values("date", ascending=False).head(30), hide_index=True,
                     width="stretch",
                     column_config={
                         "date": "Date", "symbol": "Symbol", "vcp_status": "Signal",
                         "pivot": st.column_config.NumberColumn("Pivot", format="$%.2f"),
                         "close": st.column_config.NumberColumn("Close", format="$%.2f"),
                         "stop": st.column_config.NumberColumn("Stop", format="$%.2f"),
                         "breakout_vol_ratio": st.column_config.NumberColumn("Volume ×", format="%.1f×"),
                     })


def tab_screener(sc: pd.DataFrame) -> None:
    how_to_read("screener")
    if sc.empty:
        st.info("No screen yet. Run the weekly scan once (see README).")
        return
    for col in ("eps_g0", "eps_g1", "eps_g2", "eps_accel", "eps_pass", "sector", "vcp_status",
                "surprise_streak", "next_report", "eps_note", "entry_risk"):
        if col not in sc:
            sc[col] = None
    sc["sector"] = sc["sector"].fillna("")
    f1, f2, f3, f4, f5 = st.columns([1.3, 1, 1.3, 2, 1])
    tt = f1.selectbox("Trend Template", ["All 8 pass", "7 of 8 (near misses)", "Any"], index=0)
    min_rs = f2.number_input("Min RS", 1, 99, C.MIN_RS_RATING, step=5)
    eps = f3.selectbox("EPS", ["Any", f"Growth ≥ {C.MIN_EPS_GROWTH:.0%}", "Growth + accelerating"], index=0)
    sectors = sorted(s for s in sc["sector"].dropna().unique() if s)
    pick = f4.multiselect("Sector", sectors, placeholder="All sectors")
    vcp_only = f5.toggle("VCP only", value=False)
    g1, g2, _ = st.columns([1.3, 1.3, 3])
    held_only = g1.toggle(f"Held up vs SPY (≤ {C.MAX_CORRECTION_MULT:g}×)", value=False,
                          help="Fell no more than this multiple of the S&P 500 since its high")
    rsl_only = g2.toggle("RS line at new high", value=False)

    d = sc.copy()
    if tt == "All 8 pass":
        d = d[d["tt_pass"]]
    elif tt.startswith("7"):
        d = d[d["tt_score"] == 7]
    d = d[d["rs_rating"] >= min_rs]
    if eps != "Any":
        d = d[d["eps_pass"] == True]  # noqa: E712
        if eps.startswith("Growth +"):
            d = d[d["eps_accel"] == True]  # noqa: E712
    if pick:
        d = d[d["sector"].isin(pick)]
    if held_only and "held_up" in d:
        d = d[d["held_up"].map(truthy)]
    if rsl_only and "rs_line_high" in d:
        d = d[d["rs_line_high"].map(truthy)]
    if vcp_only:
        d = d[d["vcp"] == True]  # noqa: E712

    st.caption(f"**{len(d)}** stocks · fundamentals are fetched for Trend Template passes only; "
               "sales growth and margins aren't on the Grow plan, so check those by hand.")
    d = d.assign(VCP=d["vcp_status"].map(STATUS_LABEL).where(d["vcp"] == True, ""))  # noqa: E712
    detail = st.toggle("Show each Trend Template criterion", value=False)
    cols = ["symbol", "name", "sector", "close", "rs_rating", "tt_score", "pct_above_low",
            "pct_below_high", "sma200_months_rising", "VCP", "dist_to_pivot", "entry_risk", "eps_g0", "eps_g1",
            "eps_accel", "eps_note", "surprise_streak", "Next report", "industry"]
    d = d.assign(**{"Next report": [earnings_label(x) for x in d["next_report"]]})
    if "corr_ratio" in d:
        d = d.assign(**{"RS line": [rs_line_label(h, l) for h, l in zip(d["rs_line_high"], d["rs_line_leads"])],
                        "Fell vs SPY": [corr_label(r) for r in d["corr_ratio"]]})
        cols[cols.index("rs_rating") + 1:cols.index("rs_rating") + 1] = ["RS line", "corr_depth", "Fell vs SPY"]
    if detail:
        cols[6:6] = list(CRITERIA)
    cols = [c for c in cols if c in d]
    cfg = {
        "symbol": st.column_config.TextColumn("Symbol", pinned=True),
        "name": "Name", "sector": "Sector", "industry": "Industry",
        "close": st.column_config.NumberColumn("Close", format="$%.2f"),
        "rs_rating": st.column_config.ProgressColumn("RS", min_value=1, max_value=99, format="%d"),
        "tt_score": st.column_config.NumberColumn("TT", format="%d/8"),
        "pct_above_low": st.column_config.NumberColumn("Above 52w low", format="percent"),
        "pct_below_high": st.column_config.NumberColumn("Below 52w high", format="percent"),
        "sma200_months_rising": st.column_config.NumberColumn("200MA rising (mo)", format="%d"),
        "dist_to_pivot": st.column_config.NumberColumn("vs pivot", format="percent"),
        "eps_g0": st.column_config.NumberColumn("EPS YoY (last Q)", format="percent"),
        "eps_g1": st.column_config.NumberColumn("EPS YoY (prior Q)", format="percent"),
        "eps_accel": st.column_config.CheckboxColumn("EPS accel."),
        "eps_note": "EPS note",
        "entry_risk": st.column_config.NumberColumn("Risk at entry", format="percent"),
        "corr_depth": st.column_config.NumberColumn("Max fall since SPY high", format="percent"),
        "surprise_streak": st.column_config.NumberColumn("Beats in a row", format="%d"),
        "next_report": "Next report",
        "Next report": "Next report",
    }
    for k, label in CRITERIA.items():
        cfg[k] = st.column_config.CheckboxColumn(k.upper(), help=label)
    st.dataframe(d[cols], hide_index=True, width="stretch", column_config=cfg, height=560)
    st.download_button("Download CSV", d.to_csv(index=False), file_name="sepa_screen.csv", mime="text/csv")


def price_chart(df: pd.DataFrame, row: dict | None) -> go.Figure:
    colors = MA_COLORS[theme_type()]
    c = df["close"]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.76, 0.24], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=c, name="Price",
        increasing_line_color="#1a9e5a", decreasing_line_color="#d64545", showlegend=False,
    ), row=1, col=1)
    for n in (50, 150, 200):
        key = f"SMA{n}"
        fig.add_trace(go.Scatter(x=df.index, y=c.rolling(n).mean(), name=f"{n}-day MA",
                                 line=dict(color=colors[key], width=2), hoverinfo="skip"), row=1, col=1)
    if row:
        pv, sp = num(row.get("pivot")), num(row.get("stop"))
        if not math.isnan(pv):
            fig.add_hline(y=pv, line=dict(color="#7a7974", width=1.5, dash="dash"), row=1, col=1,
                          annotation_text=f"Pivot {pv:,.2f}", annotation_position="top left")
        if not math.isnan(sp):
            fig.add_hline(y=sp, line=dict(color="#d64545", width=1.5, dash="dot"), row=1, col=1,
                          annotation_text=f"Stop {sp:,.2f}", annotation_position="bottom left")
    up = df["close"] >= df["open"]
    fig.add_trace(go.Bar(x=df.index, y=df["volume"], name="Volume", showlegend=False,
                         marker_color=["#1a9e5a" if u else "#d64545" for u in up], opacity=0.55),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["volume"].rolling(50).mean(), name="50-day avg volume",
                             line=dict(color="#7a7974", width=1.5), hoverinfo="skip"), row=2, col=1)
    fig.update_layout(
        height=620, margin=dict(l=8, r=8, t=30, b=8), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return fig


def tab_chart(sc: pd.DataFrame, wl: pd.DataFrame) -> None:
    how_to_read("chart")
    wl_syms = wl["symbol"].tolist() if not wl.empty else []
    tt_syms = sc.loc[sc["tt_pass"] == True, "symbol"].tolist() if not sc.empty else []  # noqa: E712
    options = wl_syms + [s for s in tt_syms if s not in wl_syms]
    c1, c2 = st.columns([1, 2])
    pick = c1.selectbox("Watchlist and Trend Template passes", options, index=0 if options else None,
                        placeholder="No symbols yet")
    typed = c2.text_input("…or any ticker", placeholder="e.g. NVDA").strip().upper()
    sym = typed or pick
    if not sym:
        return

    row = None
    if not sc.empty and sym in set(sc["symbol"]):
        row = sc[sc["symbol"] == sym].iloc[0].to_dict()
    if not wl.empty and sym in set(wl["symbol"]):
        row = {**(row or {}), **wl[wl["symbol"] == sym].iloc[0].to_dict()}

    key = secret("TWELVE_DATA_API_KEY")
    left, right = st.columns([2.6, 1])
    with left:
        if not key:
            st.info("Add TWELVE_DATA_API_KEY to the app's secrets to show charts (1 credit per ticker, cached 6 hours).")
        else:
            try:
                bars = fetch_bars(sym, key)
            except Exception as e:
                st.error(f"Couldn't load {sym}: {e}")
                bars = None
            if bars is not None and len(bars):
                st.plotly_chart(price_chart(bars, row), width="stretch", theme="streamlit")
        st.caption(f"[TradingView](https://www.tradingview.com/symbols/{sym}/) · "
                   f"[Finviz](https://finviz.com/quote.ashx?t={sym}) — check sales growth and margins here.")
    with right:
        st.subheader(sym)
        if not row:
            st.caption("Not in this week's liquid universe, so no criteria scores.")
            return
        if row.get("name"):
            st.caption(f"{row.get('name')} · {row.get('sector') or 'sector n/a'}")
        st.markdown("**Trend Template**")
        for k, label in CRITERIA.items():
            ok = bool(row.get(k))
            st.markdown(f"{'✅' if ok else '❌'} {label}")
        st.caption(f"RS {num(row.get('rs_rating')):.0f} · {pct(row.get('pct_above_low'), 0)} above 52w low · "
                   f"{pct(-num(row.get('pct_below_high')), 0)} from 52w high")
        st.markdown("**Setup**")
        if row.get("vcp") in (True, "True"):
            st.markdown(f"{STATUS_LABEL.get(row.get('vcp_status'), row.get('vcp_status'))}  \n"
                        f"Contractions: {row.get('depths')}  \n"
                        f"Pivot {money(row.get('pivot'))} · stop {money(row.get('stop'))} "
                        f"({pct(-num(row.get('risk_pct')))} from pivot, "
                        f"{pct(-num(row.get('entry_risk')))} if bought now)  \n"
                        f"Volume dry-up: {'yes' if row.get('vol_dryup') in (True, 'True') else 'no'} · "
                        f"tight last 2 weeks: {'yes' if row.get('tight') in (True, 'True') else 'no'}")
        else:
            reason = row.get("vcp_reason")
            st.caption(f"No VCP detected{': ' + reason if isinstance(reason, str) and reason else ''}.")
        st.markdown("**Leadership**")
        st.markdown(f"RS line: {rs_line_label(row.get('rs_line_high'), row.get('rs_line_leads'))}  \n"
                    f"Fell {pct(-num(row.get('corr_depth')), 0)} since the S&P 500's high "
                    f"({corr_label(row.get('corr_ratio'))} the index's fall)")
        st.markdown("**Earnings (EPS)**")
        g0 = num(row.get("eps_g0"))
        note = row.get("eps_note") if isinstance(row.get("eps_note"), str) else ""
        if note:
            st.caption(f"Note: {note}. Growth % not shown when the year-ago quarter is negative or under "
                       f"${C.EPS_MIN_BASE:.2f}.")
        if math.isnan(g0) and not note:
            st.caption("Not fetched yet (only Trend Template passes, within the weekly credit budget).")
        else:
            st.markdown(f"Last Q YoY {pct(row.get('eps_g0'), 0)} · prior {pct(row.get('eps_g1'), 0)} · "
                        f"before {pct(row.get('eps_g2'), 0)}  \n"
                        f"Accelerating: {'yes' if row.get('eps_accel') in (True, 'True') else 'no'} · "
                        f"beats in a row: {num(row.get('surprise_streak')):.0f}  \n"
                        f"Next report: {earnings_label(row.get('next_report'))}")
        n = trading_days_until(row.get("next_report"))
        if n is not None and 0 <= n <= C.EARNINGS_WARN_DAYS:
            st.warning(f"Reports earnings in {n} trading day(s). Avoid buying before the report; "
                       "if you hold it, decide whether your profit cushion can absorb a gap.", icon="📅")


def tab_sectors(mk: dict) -> None:
    how_to_read("sectors")
    secs = pd.DataFrame(mk.get("sectors", []))
    if secs.empty:
        st.info("No sector data yet.")
        return
    st.caption("SPDR sector ETFs ranked by the same weighted 3/6/9/12-month performance used for stock RS.")
    secs["Trend"] = [("✓ above 50 & 200" if a50 and a200 else "~ above 200 only" if a200 else "✗ below 200")
                     for a50, a200 in zip(secs["above_50"], secs["above_200"])]
    st.dataframe(secs[["rank", "sector", "etf", "perf_1m", "perf_3m", "perf_6m", "Trend"]], hide_index=True,
                 width="stretch",
                 column_config={
                     "rank": st.column_config.NumberColumn("#", format="%d"),
                     "sector": "Sector", "etf": "ETF",
                     "perf_1m": st.column_config.NumberColumn("1 month", format="percent"),
                     "perf_3m": st.column_config.NumberColumn("3 months", format="percent"),
                     "perf_6m": st.column_config.NumberColumn("6 months", format="percent"),
                 })
    by = mk.get("breadth", {}).get("tt_pass_by_sector", {})
    if by:
        s = pd.Series(by).sort_values()
        st.subheader("Where the leaders are")
        st.caption("Trend Template passes per sector (sector from company profile).")
        fig = go.Figure(go.Bar(
            x=s.values, y=s.index, orientation="h", text=s.values, textposition="outside",
            marker=dict(color=MA_COLORS[theme_type()]["SMA50"], cornerradius=4),
            hovertemplate="%{y}: %{x} stocks<extra></extra>",
        ))
        fig.update_layout(height=40 + 30 * len(s), margin=dict(l=8, r=30, t=8, b=8),
                          xaxis_title="Stocks passing", yaxis_title=None, bargap=0.35)
        st.plotly_chart(fig, width="stretch", theme="streamlit")


SIZE_FRACTIONS = {"Full": 1.0, "Half": 0.5, "Quarter (pilot)": 0.25}


def _journal_avg_gain(min_trades: int = 5) -> tuple[float, int] | None:
    """Average winning trade % from the journal, once there are enough closed trades."""
    url, key = secret("SUPABASE_URL"), secret("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    try:
        trades, exits = load_journal(url, key)
    except Exception:
        return None
    t = J.enrich(trades, exits)
    if t.empty:
        return None
    closed = t[t["status"] == "closed"]
    perf = J.performance(closed)
    if not perf or perf["trades"] < min_trades or math.isnan(perf["avg_gain"]):
        return None
    return perf["avg_gain"], perf["trades"]


def tab_sizing(wl: pd.DataFrame, mk: dict | None = None) -> None:
    how_to_read("sizing")
    state = market_state(mk or {})
    syms = ["(manual)"] + (wl["symbol"].tolist() if not wl.empty else [])
    c0, c1, c2 = st.columns(3)
    sym = c0.selectbox("Symbol", syms)
    acct = c1.number_input("Account size ($)", min_value=0.0, value=10_000.0, step=1_000.0)
    risk = c2.number_input("Risk per trade (%)", min_value=0.1, max_value=5.0, value=1.0, step=0.25,
                           help="Share of the account you lose if the stop is hit. Minervini: roughly 1–2.5%.")
    entry_d, stop_d = 100.0, None
    if sym != "(manual)":
        r = wl[wl["symbol"] == sym].iloc[0]
        entry_d, stop_d = max(float(r["pivot"]), float(r["close"])), float(r["stop"])
    c3, c4, c5 = st.columns(3)
    entry = c3.number_input("Entry ($)", min_value=0.01, value=round(entry_d, 2), key=f"e_{sym}")
    if stop_d is None:
        stop_d = entry * 0.93
    stop = c4.number_input("Stop ($)", min_value=0.0, value=round(stop_d, 2), key=f"s_{sym}",
                           help="From the setup when you pick a watchlist stock; otherwise 7% below entry. "
                                "Size always depends on the stop: no stop, no position size.")
    default_size = "Full" if state == "uptrend" else "Quarter (pilot)"
    size_pick = c5.selectbox("Position size", list(SIZE_FRACTIONS),
                             index=list(SIZE_FRACTIONS).index(default_size),
                             help="Minervini starts with a quarter-size pilot position and only scales up after "
                                  "wins. Defaults to Quarter unless the market banner is green.")
    if stop >= entry:
        st.error("Stop must be below entry.")
        return

    per_share = entry - stop
    frac = SIZE_FRACTIONS[size_pick]
    risk_usd = acct * risk / 100 * frac
    shares = math.floor(risk_usd / per_share) if per_share > 0 else 0
    capped = False
    cap = math.floor(acct * 0.25 / entry) if acct else shares
    if shares > cap:
        shares, capped = cap, True
    value = shares * entry
    actual_risk = shares * per_share
    stop_pct = per_share / entry

    st.markdown("#### Position")
    m = st.columns(4)
    m[0].metric("Shares", f"{shares:,}")
    m[1].metric("Position value", money(value), f"{value / acct:.0%} of account" if acct else None,
                delta_color="off")
    m[2].metric("Max loss at stop", money(actual_risk), f"{actual_risk / acct:.2%} of account" if acct else None,
                delta_color="off")
    m[3].metric("1R (risk per share)", money(per_share), f"stop {stop_pct:.1%} below entry", delta_color="off")
    if frac < 1:
        st.caption(f"{size_pick} size: risking {risk * frac:.2f}% of the account instead of {risk:.2f}%"
                   + (f" because the market is in a {state}." if state in ("correction", "downtrend") else "."))
    if capped:
        st.info("Capped at 25% of the account: the stop is so tight that full risk would need an oversized "
                "position. Minervini keeps single positions around 20–25%.")
    if stop_pct > 0.10:
        st.error(f"Stop is {stop_pct:.1%} below entry. Minervini's absolute limit is 10%; wait for a tighter setup.")
        return
    if stop_pct > C.MAX_STOP_PCT:
        st.warning(f"Stop is {stop_pct:.1%} below entry, wider than the {C.MAX_STOP_PCT:.0%} default.")
    if shares == 0:
        st.warning("Position rounds down to 0 shares. Raise the account size or risk %, or pick a cheaper stock.")
        return

    # ---- take-profit plan ---------------------------------------------------
    st.markdown("#### Take-profit plan")
    st.caption("SEPA has no single fixed target. Profits are taken in stages measured in R (your risk per "
               "share), and the rest rides a rising stop, so a big winner isn't capped.")
    half = shares // 2
    rest = shares - half
    t2, t3, t4 = entry + 2 * per_share, entry + 3 * per_share, entry + 4 * per_share
    rows = [
        {"Stage": "Stop (if wrong)", "Price": stop, "Move": -stop_pct, "Action": f"Sell all {shares:,} shares",
         "P&L": -actual_risk},
        {"Stage": "Target 1: 2R", "Price": t2, "Move": t2 / entry - 1,
         "Action": f"Sell half ({half:,} sh), raise stop on the rest to breakeven ${entry:,.2f}",
         "P&L": half * 2 * per_share},
        {"Stage": "Target 2: 3R", "Price": t3, "Move": t3 / entry - 1,
         "Action": f"Set a back stop at +1R (${entry + per_share:,.2f}) on the {rest:,} shares left",
         "P&L": None},
        {"Stage": "4R and up", "Price": t4, "Move": t4 / entry - 1,
         "Action": "Raise the back stop 1R for each R gained; sell into strength or on a sell signal",
         "P&L": None},
    ]
    avg = _journal_avg_gain()
    if avg:
        g, n = avg
        rows.insert(2, {"Stage": "Your average winner", "Price": entry * (1 + g), "Move": g,
                        "Action": f"Average gain of your {n} closed trades: a realistic first target for you",
                        "P&L": shares * entry * g})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Price": st.column_config.NumberColumn(format="$%.2f"),
        "Move": st.column_config.NumberColumn("vs entry", format="percent"),
        "P&L": st.column_config.NumberColumn("P&L at this stage", format="$%.2f"),
    })

    worst = -actual_risk
    plan_min = half * 2 * per_share + rest * 1 * per_share       # half at 2R, rest stopped at +1R
    plan_3r = half * 2 * per_share + rest * 3 * per_share        # half at 2R, rest sold at 3R
    o = st.columns(3)
    o[0].metric("If stopped out", money(worst), f"{worst / acct:+.2%} of account" if acct else None)
    o[1].metric("Plan, minimum win", money(plan_min),
                f"{plan_min / acct:+.2%}" if acct else None,
                help="Half sold at 2R, the rest stopped at the +1R back stop.")
    o[2].metric("Plan, rest sold at 3R", money(plan_3r), f"{plan_3r / acct:+.2%}" if acct else None,
                help="Half at 2R, the rest at 3R. Bigger winners ride the back stop further.")
    st.caption(f"Reward-to-risk: the minimum win is {plan_min / actual_risk:.1f}× the loss. Minervini aims for "
               "average gains at least 2–3× average losses, so a 33–40% win rate can still make money.")


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
def get_journal() -> J.SupabaseJournal | None:
    url, key = secret("SUPABASE_URL"), secret("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    return J.SupabaseJournal(url, key)


@st.cache_data(ttl=60, show_spinner=False)
def load_journal(url: str, _key: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    j = J.SupabaseJournal(url, _key)
    return j.trades(), j.exits()


def _rs_line_code(row: dict | None) -> str | None:
    if not row:
        return None
    if truthy(row.get("rs_line_leads")):
        return "leads"
    if truthy(row.get("rs_line_high")):
        return "high"
    if row.get("rs_line_high") is None or (isinstance(row.get("rs_line_high"), float) and math.isnan(row["rs_line_high"])):
        return None
    return "no"


def _clean(v):
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


def tab_journal(sc: pd.DataFrame, wl: pd.DataFrame, mk: dict) -> None:
    how_to_read("journal")
    j = get_journal()
    if j is None:
        st.info("Connect your Supabase project to use the journal: add **SUPABASE_URL** and "
                "**SUPABASE_SERVICE_KEY** to the app's secrets (Manage app → Settings → Secrets).")
        return
    if not secret("APP_PASSWORD"):
        st.warning("The journal can change your database, but this app has no APP_PASSWORD. "
                   "Add one in the app's secrets, and keep the app's sharing set to private.", icon="🔒")
    try:
        trades, exits = load_journal(secret("SUPABASE_URL"), secret("SUPABASE_SERVICE_KEY"))
    except J.JournalError as e:
        st.error(f"Couldn't read the journal: {e}")
        return
    t = J.enrich(trades, exits)
    open_t = t[t["status"] == "open"] if not t.empty else t
    closed = t[t["status"] == "closed"] if not t.empty else t

    def refresh():
        load_journal.clear()
        st.rerun()

    lookup = {}
    for frame in (sc, wl):
        if not frame.empty:
            for r in frame.to_dict("records"):
                lookup.setdefault(r["symbol"], {}).update(r)

    # ---- open positions -----------------------------------------------------
    st.subheader(f"Open positions ({len(open_t)})")
    if open_t.empty:
        st.caption("No open trades. Log one below when you buy.")
    else:
        key = secret("TWELVE_DATA_API_KEY")
        rows, actions = [], []
        for r in open_t.to_dict("records"):
            sym = r["symbol"]
            price, below50 = num(lookup.get(sym, {}).get("close")), None
            if key:
                try:
                    bars = fetch_bars(sym, key)
                    if bars is not None and len(bars):
                        price = float(bars["close"].iloc[-1])
                        if len(bars) >= 50:
                            below50 = bool(bars["close"].iloc[-1] < bars["close"].rolling(50).mean().iloc[-1])
                except Exception:
                    pass
            n = trading_days_until(lookup.get(sym, {}).get("next_report"))
            warn = n if n is not None and 0 <= n <= C.EARNINGS_WARN_DAYS else None
            stage, action = J.position_advice(r, price, warn, below50)
            stop = num(r.get("current_stop"))
            stop = num(r["initial_stop"]) if math.isnan(stop) else stop
            rows.append({
                "Symbol": sym, "Entered": r["entry_date"], "Entry": r["entry_price"], "Shares left": r["shares_left"],
                "Stop": stop, "Price": price,
                "Open P&L": (price - r["entry_price"]) * r["shares_left"] if not math.isnan(price) else None,
                "Return": price / r["entry_price"] - 1 if not math.isnan(price) else None,
                "Realized": r["realized_pnl"], "Stage": stage,
            })
            actions.append((sym, stage, action))
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "Entry": st.column_config.NumberColumn(format="$%.2f"),
            "Stop": st.column_config.NumberColumn(format="$%.2f"),
            "Price": st.column_config.NumberColumn(format="$%.2f", help="Latest daily close"),
            "Open P&L": st.column_config.NumberColumn(format="$%.2f"),
            "Realized": st.column_config.NumberColumn(format="$%.2f", help="From partial sells so far"),
            "Return": st.column_config.NumberColumn(format="percent"),
            "Shares left": st.column_config.NumberColumn(format="%g"),
        })
        st.markdown("**Next actions**")
        for sym, stage, action in actions:
            st.markdown(f"- **{sym}** {stage}: {action}")

    # ---- forms --------------------------------------------------------------
    f_new, f_sell, f_stop, f_del = st.tabs(["Log a new trade", "Record a sell", "Raise a stop", "Delete"])

    with f_new:
        sym = st.text_input("Symbol", key="j_sym").strip().upper()
        info = lookup.get(sym, {}) if sym else {}
        if sym and info:
            st.caption(f"From the dashboard: {STATUS_LABEL.get(info.get('vcp_status'), info.get('vcp_status') or 'no setup')}"
                       f" · pivot {money(info.get('pivot'))} · stop {money(info.get('stop'))} · RS {num(info.get('rs_rating')):.0f}")
        elif sym:
            st.caption("Not on this week's screen, so no setup details will be recorded.")
        with st.form("j_new", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            d = c1.date_input("Entry date", value=date.today())
            price_default = num(info.get("close")) if info else math.nan
            ep = c2.number_input("Entry price", min_value=0.0, step=0.01,
                                 value=round(price_default, 2) if not math.isnan(price_default) else 0.0)
            sh = c3.number_input("Shares", min_value=0.0, step=1.0)
            stop_default = num(info.get("stop")) if info else math.nan
            sp = c4.number_input("Initial stop", min_value=0.0, step=0.01,
                                 value=round(stop_default, 2) if not math.isnan(stop_default) else 0.0)
            notes = st.text_input("Notes (why you took it)")
            if st.form_submit_button("Save trade", type="primary"):
                if not sym:
                    st.error("Enter a symbol.")
                elif ep <= 0 or sh <= 0 or sp <= 0:
                    st.error("Entry price, shares and stop must all be above zero.")
                elif sp >= ep:
                    st.error("The stop must be below the entry price.")
                else:
                    risk = (ep - sp) / ep
                    row = {
                        "symbol": sym, "entry_date": d.isoformat(), "entry_price": ep, "shares": sh,
                        "initial_stop": sp, "current_stop": sp, "status": "open", "notes": notes or None,
                        "signal_at_entry": _clean(info.get("vcp_status")) if info else None,
                        "pivot": _clean(num(info.get("pivot"))) if info else None,
                        "rs_rating": int(num(info.get("rs_rating"))) if info and not math.isnan(num(info.get("rs_rating"))) else None,
                        "rs_line": _rs_line_code(info) if info else None,
                        "eps_pass": bool(truthy(info.get("eps_pass"))) if info and info.get("eps_pass") is not None
                                    and not (isinstance(info.get("eps_pass"), float) and math.isnan(info.get("eps_pass"))) else None,
                        "market_state": market_state(mk),
                        "sector": _clean(info.get("sector")) or None if info else None,
                    }
                    try:
                        j.add_trade(row)
                        if risk > 0.10:
                            st.warning(f"Saved, but the stop is {risk:.1%} below entry, beyond Minervini's 10% limit.")
                        refresh()
                    except J.JournalError as e:
                        st.error(str(e))

    with f_sell:
        if open_t.empty:
            st.caption("No open trades.")
        else:
            opts = {f"{r['symbol']} · {r['shares_left']:g} shares left · entry ${r['entry_price']:,.2f} (#{r['id']})": r
                    for r in open_t.to_dict("records")}
            with st.form("j_sell", clear_on_submit=True):
                pick = st.selectbox("Trade", list(opts))
                r = opts[pick]
                c1, c2, c3 = st.columns(3)
                d = c1.date_input("Exit date", value=date.today())
                xp = c2.number_input("Exit price", min_value=0.0, step=0.01)
                xs = c3.number_input("Shares sold", min_value=0.0, step=1.0, value=float(r["shares_left"]))
                reason = st.selectbox("Reason", ["2R: sold half", "Back stop hit", "Initial stop hit",
                                                 "Sold into strength", "Sell signal", "Before earnings", "Other"])
                if st.form_submit_button("Save sell", type="primary"):
                    r = opts[pick]
                    if xp <= 0 or xs <= 0:
                        st.error("Exit price and shares must be above zero.")
                    elif xs > r["shares_left"] + 1e-9:
                        st.error(f"Only {r['shares_left']:g} shares are left in this trade.")
                    else:
                        try:
                            j.add_exit({"trade_id": int(r["id"]), "exit_date": d.isoformat(), "exit_price": xp,
                                        "shares": xs, "reason": reason})
                            if r["shares_left"] - xs <= 1e-9:
                                j.update_trade(int(r["id"]), {"status": "closed"})
                            refresh()
                        except J.JournalError as e:
                            st.error(str(e))

    with f_stop:
        if open_t.empty:
            st.caption("No open trades.")
        else:
            opts = {f"{r['symbol']} · stop ${num(r['current_stop']) if not math.isnan(num(r['current_stop'])) else num(r['initial_stop']):,.2f} (#{r['id']})": r
                    for r in open_t.to_dict("records")}
            with st.form("j_stop"):
                pick = st.selectbox("Trade", list(opts))
                new_stop = st.number_input("New stop", min_value=0.0, step=0.01)
                if st.form_submit_button("Update stop", type="primary"):
                    r = opts[pick]
                    cur = num(r["current_stop"])
                    cur = num(r["initial_stop"]) if math.isnan(cur) else cur
                    if new_stop <= cur:
                        st.error(f"Stops only move up. Current stop is ${cur:,.2f}.")
                    else:
                        try:
                            j.update_trade(int(r["id"]), {"current_stop": new_stop})
                            refresh()
                        except J.JournalError as e:
                            st.error(str(e))

    with f_del:
        if t.empty:
            st.caption("Nothing to delete.")
        else:
            opts = {f"{r['symbol']} · {r['entry_date']} · {r['status']} (#{r['id']})": r for r in t.to_dict("records")}
            with st.form("j_del"):
                pick = st.selectbox("Trade", list(opts))
                sure = st.checkbox("Yes, permanently delete this trade and its sells")
                if st.form_submit_button("Delete"):
                    if not sure:
                        st.error("Tick the box to confirm.")
                    else:
                        try:
                            j.delete_trade(int(opts[pick]["id"]))
                            refresh()
                        except J.JournalError as e:
                            st.error(str(e))

    # ---- performance --------------------------------------------------------
    st.subheader("Performance (closed trades)")
    perf = J.performance(closed)
    if not perf:
        st.caption("Shows once you've closed your first trade.")
        return
    m = st.columns(6)
    m[0].metric("Trades", perf["trades"])
    m[1].metric("Win rate", f"{perf['win_rate']:.0%}")
    m[2].metric("Avg gain", pct(perf["avg_gain"]))
    m[3].metric("Avg loss", pct(perf["avg_loss"]))
    m[4].metric("Avg R", f"{perf['avg_r']:+.2f}R", help="Average result in multiples of initial risk. Above 0 = an edge.")
    m[5].metric("Total P&L", money(perf["total_pnl"]))
    ratio = perf["gain_loss_ratio"]
    st.caption(f"Gain/loss ratio {ratio:.1f}× · best {perf['best']} · worst {perf['worst']}. "
               "Minervini aims for average gains at least 2–3× average losses."
               if not math.isnan(ratio) else f"Best {perf['best']} · worst {perf['worst']}.")

    if not exits.empty:
        e = exits.merge(trades[["id", "entry_price"]], left_on="trade_id", right_on="id")
        e["pnl"] = (pd.to_numeric(e["exit_price"]) - pd.to_numeric(e["entry_price"])) * pd.to_numeric(e["shares"])
        curve = e.groupby("exit_date")["pnl"].sum().sort_index().cumsum()
        fig = go.Figure(go.Scatter(x=curve.index, y=curve.values, mode="lines+markers",
                                   line=dict(color=MA_COLORS[theme_type()]["SMA50"], width=2),
                                   hovertemplate="%{x}: $%{y:,.2f}<extra></extra>"))
        fig.update_layout(height=260, margin=dict(l=8, r=8, t=8, b=8), yaxis_title="Realized P&L ($)")
        st.plotly_chart(fig, width="stretch", theme="streamlit")

    st.markdown("**Results by setup at entry**")
    cols = st.columns(2)
    for i, (col, label) in enumerate([("rs_line", "RS line"), ("eps_pass", "EPS ✓"),
                                      ("market_state", "Market"), ("signal_at_entry", "Signal at entry")]):
        tbl = J.by_setup(closed, col)
        if not tbl.empty:
            cols[i % 2].dataframe(tbl.rename(columns={col: label}), hide_index=True, width="stretch",
                                  column_config={"win_rate": st.column_config.NumberColumn("Win rate", format="percent"),
                                                 "avg_r": st.column_config.NumberColumn("Avg R", format="%+.2f"),
                                                 "pnl": st.column_config.NumberColumn("P&L", format="$%.2f"),
                                                 "trades": "Trades"})
    with st.expander("Closed trades"):
        show = closed.assign(ret=closed["realized_pnl"] / (closed["entry_price"] * closed["shares"]),
                             r=closed["realized_pnl"] / closed["risk_dollars"])
        want = ["symbol", "entry_date", "last_exit_date", "entry_price", "avg_exit", "shares",
                "realized_pnl", "ret", "r", "rs_line", "eps_pass", "market_state", "notes"]
        st.dataframe(show[[c for c in want if c in show]],
                     hide_index=True, width="stretch", column_config={
                         "entry_price": st.column_config.NumberColumn("Entry", format="$%.2f"),
                         "avg_exit": st.column_config.NumberColumn("Avg exit", format="$%.2f"),
                         "realized_pnl": st.column_config.NumberColumn("P&L", format="$%.2f"),
                         "ret": st.column_config.NumberColumn("Return", format="percent"),
                         "r": st.column_config.NumberColumn("R", format="%+.2f"),
                     })


# ---------------------------------------------------------------------------
def main() -> None:
    password_gate()
    if _STALE_CONFIG:
        st.warning("config.py on GitHub is older than app.py, so defaults are used for: "
                   f"{', '.join(_STALE_CONFIG)}. Upload the latest config.py.", icon="⚠️")
    mk, meta = data("market.json"), data("run_meta.json")
    sc, wl, sig = data("screen.csv"), data("watchlist.csv"), data("signals_log.csv")
    # hide old/renamed tickers even in data written before they were excluded
    sc, wl, sig = [d[~d["symbol"].isin(C.EXCLUDE_SYMBOLS)] if "symbol" in d else d for d in (sc, wl, sig)]
    if not wl.empty and not sc.empty and "next_report" in sc:
        nr = sc.set_index("symbol")["next_report"].to_dict()
        wl = wl.copy()
        if "next_report" not in wl:
            wl["next_report"] = None
        wl["next_report"] = [r if isinstance(r, str) and r else nr.get(sym)
                             for sym, r in zip(wl["symbol"], wl["next_report"])]
    header(mk, meta)
    market_strip(mk)
    t1, t2, t3, t4, t5, t6 = st.tabs(["Signals", "Screener", "Chart", "Sectors", "Position size", "Journal"])
    with t1:
        tab_signals(wl, sig)
    with t2:
        tab_screener(sc)
    with t3:
        tab_chart(sc, wl)
    with t4:
        tab_sectors(mk)
    with t5:
        tab_sizing(wl, mk)
    with t6:
        tab_journal(sc, wl, mk)
    st.divider()
    st.caption("Screens a published method (Minervini's SEPA). It is not financial advice. "
               "VCP detection is a heuristic, so confirm every setup on the chart.")


main()
