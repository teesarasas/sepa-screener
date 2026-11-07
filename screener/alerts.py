"""Telegram alerts, sent by the GitHub Action after each run.

Each alert is sent once: a small state file (data/alerts_state.json) remembers
what was already sent, so the same breakout or 2R level doesn't repeat daily.
"""
from __future__ import annotations

import html
import logging
import math
import os

import numpy as np
import pandas as pd
import requests

import config as C
from screener import journal as J

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.info("Telegram not configured; alert not sent:\n%s", text)
        return False
    for chunk in _chunks(text):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=20)
            if r.status_code != 200:
                log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
                return False
        except requests.RequestException as e:
            log.warning("Telegram unreachable: %s", e)
            return False
    return True


def _chunks(text: str, limit: int = 3900):
    lines, buf = text.split("\n"), ""
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def esc(x) -> str:
    return html.escape(str(x))


def _f(x) -> float:
    try:
        x = float(x)
        return x if math.isfinite(x) else math.nan
    except (TypeError, ValueError):
        return math.nan


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


MARKET_LINE = {
    "uptrend": "🟢 Market: <b>uptrend</b>. Breakouts have the odds on their side.",
    "correction": "🟡 Market: <b>correction</b>. Pilot positions in true leaders only.",
    "downtrend": "🔴 Market: <b>downtrend</b>. Mostly cash; most breakouts fail.",
    "unknown": "Market state unavailable.",
}


# ---------------------------------------------------------------------------
# daily
# ---------------------------------------------------------------------------
def watchlist_alerts(old: pd.DataFrame, new: pd.DataFrame, state: dict) -> list[str]:
    """New volume-confirmed breakouts, and setups that failed."""
    out = []
    if new.empty:
        return out
    prev = old.set_index("symbol")["vcp_status"].to_dict() if not old.empty else {}
    for r in new.to_dict("records"):
        sym, status = r["symbol"], str(r.get("vcp_status"))
        key = f"wl:{sym}:{r.get('pivot_date')}:{status}"
        if status == "Breakout" and prev.get(sym) != "Breakout" and key not in state:
            lead = "★ RS line leads" if str(r.get("rs_line_leads")) == "True" else (
                "RS line ✓" if str(r.get("rs_line_high")) == "True" else "RS line ✗")
            eps = "EPS ✓" if str(r.get("eps_pass")) == "True" else "EPS ✗"
            out.append(f"🟢 <b>{esc(sym)}</b> breakout: close ${_f(r['close']):,.2f} over pivot "
                       f"${_f(r['pivot']):,.2f} on {_f(r.get('breakout_vol_ratio')):.1f}× volume. "
                       f"Stop ${_f(r['stop']):,.2f}, risk at entry {_f(r.get('entry_risk')):.1%}. "
                       f"RS {_f(r.get('rs_rating')):.0f} · {lead} · {eps}")
            state[key] = True
    return out


def position_alerts(trades: pd.DataFrame, exits: pd.DataFrame, prices: dict[str, pd.DataFrame],
                    next_reports: dict[str, str], state: dict, today) -> list[str]:
    """Stop hit, 2R / 3R / new-R levels, earnings soon, close below the 50-day line."""
    out = []
    t = J.enrich(trades, exits)
    if t.empty:
        return out
    for r in t[t["status"] == "open"].to_dict("records"):
        sym, tid = r["symbol"], int(r["id"])
        df = prices.get(sym)
        if df is None or not len(df):
            continue
        price = float(df["close"].iloc[-1])
        entry, rps = _f(r["entry_price"]), _f(r["risk_per_share"])
        stop = _f(r.get("current_stop"))
        stop = _f(r["initial_stop"]) if math.isnan(stop) else stop
        if rps <= 0:
            continue
        r_now = (price - entry) / rps
        bar = df.index[-1].strftime("%Y-%m-%d")

        if price <= stop:
            k = f"t{tid}:stop:{stop:.4f}"
            if k not in state:
                out.append(f"🔴 <b>{esc(sym)}</b> closed at ${price:,.2f}, at or below your stop ${stop:,.2f}. "
                           "Sell per your rules, then record the sell in the Journal.")
                state[k] = True
            continue

        level = int(math.floor(r_now))
        if level >= 2:
            k = f"t{tid}:R{level}"
            if k not in state:
                _, action = J.position_advice(r, price)
                out.append(f"🟡 <b>{esc(sym)}</b> reached +{r_now:.1f}R (${price:,.2f}, "
                           f"{price / entry - 1:+.1%}). {esc(action)}")
                for lower in range(2, level + 1):  # don't re-send lower levels later
                    state[f"t{tid}:R{lower}"] = True

        nr = next_reports.get(sym)
        if isinstance(nr, str) and nr:
            try:  # same count as the dashboard's earnings column
                n = int(np.busday_count(today, pd.Timestamp(nr[:10]).date()))
            except ValueError:
                n = -1
            if 0 <= n <= 3:
                k = f"t{tid}:earn:{nr[:10]}"
                if k not in state:
                    out.append(f"📅 <b>{esc(sym)}</b> reports earnings in {n} trading day(s) ({nr[:10]}). "
                               f"You're at {r_now:+.1f}R. Decide now whether to hold through it.")
                    state[k] = True

        if len(df) >= 50:
            sma50 = df["close"].rolling(50).mean().iloc[-1]
            vol_ratio = df["volume"].iloc[-1] / df["volume"].rolling(50).mean().shift(1).iloc[-1]
            if price < sma50 and df["close"].iloc[-2] >= df["close"].rolling(50).mean().iloc[-2]:
                k = f"t{tid}:below50:{bar}"
                if k not in state:
                    heavy = "on heavy volume" if vol_ratio >= 1.4 else "on light volume"
                    out.append(f"⚠️ <b>{esc(sym)}</b> closed below its 50-day line {heavy} "
                               f"({vol_ratio:.1f}× avg). Minervini treats this as a sell signal when volume is heavy.")
                    state[k] = True
    return out


def market_alert(mk: dict, state: dict) -> list[str]:
    now = market_state(mk)
    before = state.get("market")
    state["market"] = now
    if before and before != now:
        return [f"Market changed from <b>{esc(before)}</b> to <b>{esc(now)}</b>.\n{MARKET_LINE[now]}"]
    return []


# ---------------------------------------------------------------------------
# weekly
# ---------------------------------------------------------------------------
def weekly_summary(mk: dict, wl: pd.DataFrame, as_of: str | None) -> str:
    br = mk.get("breadth", {})
    lines = [f"<b>Weekly SEPA scan</b> (prices as of {esc(as_of)})",
             MARKET_LINE[market_state(mk)],
             f"Trend Template passes: <b>{br.get('tt_pass', '—')}</b> · "
             f"above 200-day: {_f(br.get('pct_above_200')):.0%} · VCP setups: {br.get('vcp_setups', '—')}"]
    secs = mk.get("sectors") or []
    if secs:
        lines.append("Top sectors: " + ", ".join(esc(s["sector"]) for s in secs[:3]))
    if not wl.empty:
        cand = wl[wl["vcp_status"].isin(["Breakout", "Ready", "Breakout (weak volume)"])].copy()
        if not cand.empty:
            def flag(col):
                return (cand[col].astype(str) == "True").astype(int) if col in cand else 0
            cand["_lead"] = flag("rs_line_leads") * 2 + flag("rs_line_high")
            cand = cand.sort_values(["_lead", "rs_rating"], ascending=False).head(8)
            lines.append("\n<b>Setups to watch</b>")
            for r in cand.to_dict("records"):
                star = " ★" if r["_lead"] >= 2 else (" ✓" if r["_lead"] == 1 else "")
                lines.append(f"• <b>{esc(r['symbol'])}</b> {esc(r['vcp_status'])}: pivot ${_f(r['pivot']):,.2f}, "
                             f"RS {_f(r['rs_rating']):.0f}{star}")
    return "\n".join(lines)
