"""Minervini SEPA screener dashboard (Streamlit).

Reads the files the GitHub Action writes to data/. Only the Chart tab calls
Twelve Data (1 credit per symbol, cached), using the key in Streamlit secrets.
"""
from __future__ import annotations

import hmac
import json
import math
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

import config as C
from screener.td_client import values_to_frame

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
    if entered and hmac.compare_digest(entered, pw):
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
    if wl.empty:
        st.info("Watchlist is empty. Either the weekly scan hasn't run yet, or no stock passed the Trend Template with a VCP base this week.")
        return
    st.caption(
        "Checked after every US close against the pivot set by the weekly scan. "
        f"**Breakout** = close above pivot on ≥ {C.BREAKOUT_VOL_MULT:g}× average volume. "
        f"**Extended** = buying at today's close would put the stop more than {C.MAX_ENTRY_RISK:.0%} away. "
        f"**Ready** = within {C.VCP_READY_WITHIN:.0%} below pivot. "
        "**Risk at entry** = stop distance if you buy now (or at the pivot, if it hasn't broken out)."
    )
    df = wl.copy()
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
               "breakout_vol_ratio", "rs_rating", "RS line", "Fell vs SPY", "EPS", "Trend", "sector",
               "depths", "updated"]]
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
            "eps_accel", "eps_note", "surprise_streak", "next_report", "industry"]
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
                        f"Next report: {row.get('next_report') or '—'}")


def tab_sectors(mk: dict) -> None:
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


def tab_sizing(wl: pd.DataFrame) -> None:
    st.caption("Shares = (account × risk per trade) ÷ (entry − stop). Default entry is the pivot, "
               "or today's close if the stock is already above it.")
    syms = ["(manual)"] + (wl["symbol"].tolist() if not wl.empty else [])
    c0, c1, c2 = st.columns(3)
    sym = c0.selectbox("Symbol", syms)
    acct = c1.number_input("Account size ($)", min_value=0.0, value=10_000.0, step=1_000.0)
    risk = c2.number_input("Risk per trade (%)", min_value=0.1, max_value=5.0, value=1.0, step=0.25)
    entry_d, stop_d = 100.0, 93.0
    if sym != "(manual)":
        r = wl[wl["symbol"] == sym].iloc[0]
        entry_d, stop_d = max(float(r["pivot"]), float(r["close"])), float(r["stop"])
    c3, c4 = st.columns(2)
    entry = c3.number_input("Entry ($)", min_value=0.01, value=round(entry_d, 2), key=f"e_{sym}")
    stop = c4.number_input("Stop ($)", min_value=0.0, value=round(stop_d, 2), key=f"s_{sym}")
    if stop >= entry:
        st.error("Stop must be below entry.")
        return
    per_share = entry - stop
    risk_usd = acct * risk / 100
    shares = math.floor(risk_usd / per_share)
    value = shares * entry
    m = st.columns(4)
    m[0].metric("Shares", f"{shares:,}")
    m[1].metric("Position value", money(value))
    m[2].metric("Share of account", f"{value / acct:.0%}" if acct else "—")
    m[3].metric("Max loss at stop", money(shares * per_share))
    stop_pct = per_share / entry
    if stop_pct > 0.10:
        st.error(f"Stop is {stop_pct:.1%} below entry. Minervini's absolute limit is 10%; wait for a tighter setup.")
    elif stop_pct > C.MAX_STOP_PCT:
        st.warning(f"Stop is {stop_pct:.1%} below entry, wider than the {C.MAX_STOP_PCT:.0%} default.")
    if acct and value > acct:
        st.warning("Position is larger than the account, so this needs margin. Lower the risk % or skip the trade.")
    elif acct and value / acct > 0.25:
        st.info("Position is over 25% of the account. Minervini caps single positions around 20–25%.")


# ---------------------------------------------------------------------------
def main() -> None:
    password_gate()
    mk, meta = data("market.json"), data("run_meta.json")
    sc, wl, sig = data("screen.csv"), data("watchlist.csv"), data("signals_log.csv")
    header(mk, meta)
    market_strip(mk)
    t1, t2, t3, t4, t5 = st.tabs(["Signals", "Screener", "Chart", "Sectors", "Position size"])
    with t1:
        tab_signals(wl, sig)
    with t2:
        tab_screener(sc)
    with t3:
        tab_chart(sc, wl)
    with t4:
        tab_sectors(mk)
    with t5:
        tab_sizing(wl)
    st.divider()
    st.caption("Screens a published method (Minervini's SEPA). It is not financial advice. "
               "VCP detection is a heuristic, so confirm every setup on the chart.")


main()
