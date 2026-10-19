"""Trend Template, RS score and VCP detection. Pure functions on daily OHLCV."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

import config as C


def _f(x) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return math.nan
    return x if math.isfinite(x) else math.nan


# ---------------------------------------------------------------------------
# Trend Template (criteria 1-7; criterion 8 needs the whole universe)
# ---------------------------------------------------------------------------
def trend_template(df: pd.DataFrame) -> dict:
    """Returns metrics and pass/fail for criteria 1-7 plus liquidity."""
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    n = len(df)
    out: dict = {"bars": n}
    last = _f(close.iloc[-1]) if n else math.nan
    out["close"] = last
    out["avg_vol50"] = _f(vol.tail(50).mean())
    out["dollar_vol50"] = _f((close.tail(50) * vol.tail(50)).mean())
    out["liquid"] = bool(last >= C.MIN_PRICE and out["avg_vol50"] >= C.MIN_AVG_VOLUME)

    need = 200 + C.SMA200_RISING_LOOKBACK
    if n < need:
        out["enough_history"] = False
        for i in range(1, 8):
            out[f"c{i}"] = False
        return out
    out["enough_history"] = True

    sma50 = close.rolling(50).mean()
    sma150 = close.rolling(150).mean()
    sma200 = close.rolling(200).mean()
    s50, s150, s200 = _f(sma50.iloc[-1]), _f(sma150.iloc[-1]), _f(sma200.iloc[-1])
    s200_prev = _f(sma200.iloc[-1 - C.SMA200_RISING_LOOKBACK])
    hi52 = _f(high.tail(252).max())
    lo52 = _f(low.tail(252).min())

    # how many consecutive months (21-bar steps) the 200-day has been rising
    months_rising = 0
    for k in range(1, 7):
        a, b = -1 - 21 * (k - 1), -1 - 21 * k
        if -b > n - 199:
            break
        if sma200.iloc[a] > sma200.iloc[b]:
            months_rising += 1
        else:
            break

    out.update(
        sma50=s50, sma150=s150, sma200=s200, high_52w=hi52, low_52w=lo52,
        pct_above_low=last / lo52 - 1 if lo52 else math.nan,
        pct_below_high=1 - last / hi52 if hi52 else math.nan,
        sma200_months_rising=months_rising,
        c1=bool(last > s150 and last > s200),
        c2=bool(s150 > s200),
        c3=bool(s200 > s200_prev),
        c4=bool(s50 > s150 and s50 > s200),
        c5=bool(last > s50),
        c6=bool(last >= (1 + C.MIN_ABOVE_52W_LOW) * lo52),
        c7=bool(last >= (1 - C.MAX_BELOW_52W_HIGH) * hi52),
    )
    return out


def rs_raw(close: pd.Series) -> float:
    """IBD-style weighted performance; ranked into 1-99 across the universe later."""
    n = len(close)

    def roc(k: int) -> float:
        if n <= k:
            return math.nan
        return _f(close.iloc[-1] / close.iloc[-1 - k] - 1)

    # shorter histories (recent IPOs) use the periods available, reweighted
    parts = [(0.4, roc(63)), (0.2, roc(126)), (0.2, roc(189)), (0.2, roc(252))]
    parts = [(w, r) for w, r in parts if not math.isnan(r)]
    if not parts:
        return math.nan
    wsum = sum(w for w, _ in parts)
    return sum(w * r for w, r in parts) / wsum


def rs_rating(raw: pd.Series) -> pd.Series:
    """Percentile rank 1-99."""
    pct = raw.rank(pct=True)
    return (pct * 98 + 1).round().clip(1, 99)


def perf(close: pd.Series, bars: int) -> float:
    if len(close) <= bars:
        return math.nan
    return _f(close.iloc[-1] / close.iloc[-1 - bars] - 1)


# ---------------------------------------------------------------------------
# VCP
# ---------------------------------------------------------------------------
def zigzag(high: np.ndarray, low: np.ndarray, pct: float) -> list[tuple[int, float, str]]:
    """Confirmed swing points: (index, price, 'H' or 'L')."""
    n = len(high)
    if n < 3:
        return []
    piv: list[tuple[int, float, str]] = []
    trend = 0
    hi, hi_i, lo, lo_i = high[0], 0, low[0], 0
    for i in range(1, n):
        if trend == 0:
            if high[i] >= hi:
                hi, hi_i = high[i], i
            if low[i] <= lo:
                lo, lo_i = low[i], i
            if hi >= lo * (1 + pct) and hi_i > lo_i:
                piv.append((lo_i, lo, "L"))
                trend = 1
            elif lo <= hi * (1 - pct) and lo_i > hi_i:
                piv.append((hi_i, hi, "H"))
                trend = -1
                # restart the high tracker from the low
                hi, hi_i = high[i], i
        elif trend == 1:
            if high[i] >= hi:
                hi, hi_i = high[i], i
            elif low[i] <= hi * (1 - pct):
                piv.append((hi_i, hi, "H"))
                trend = -1
                lo, lo_i = low[i], i
        else:
            if low[i] <= lo:
                lo, lo_i = low[i], i
            elif high[i] >= lo * (1 + pct):
                piv.append((lo_i, lo, "L"))
                trend = 1
                hi, hi_i = high[i], i
    return piv


EMPTY_VCP = {
    "vcp": False, "vcp_reason": "", "vcp_status": "", "contractions": 0,
    "depths": "", "base_depth": math.nan, "last_depth": math.nan, "base_weeks": math.nan,
    "pivot": math.nan, "pivot_date": None, "stop": math.nan, "risk_pct": math.nan,
    "dist_to_pivot": math.nan, "vol_dryup": False, "tight": False,
    "breakout_date": None, "breakout_vol_ratio": math.nan,
}


def detect_vcp(df: pd.DataFrame) -> dict:
    """Heuristic VCP: a base of 2-6 progressively smaller pullbacks.

    Shortlists candidates for a human to confirm on the chart; it is not a
    substitute for looking at the chart.
    """
    out = dict(EMPTY_VCP)
    w = df.tail(C.VCP_LOOKBACK)
    n = len(w)
    if n < C.VCP_MIN_BASE_BARS + 5:
        out["vcp_reason"] = "not enough bars"
        return out
    high, low = w["high"].to_numpy(float), w["low"].to_numpy(float)
    close, vol = w["close"].to_numpy(float), w["volume"].to_numpy(float)

    swings = zigzag(high, low, C.VCP_SWING_PCT)
    highs = [(i, p) for i, p, t in swings if t == "H"]
    if not highs:
        out["vcp_reason"] = "no swing highs"
        return out

    # base starts at the highest confirmed swing high in the window
    base_i, base_p = max(highs, key=lambda x: (x[1], x[0]))
    if n - 1 - base_i < C.VCP_MIN_BASE_BARS:
        out["vcp_reason"] = "base too young"
        return out
    cons = [(i, p) for i, p in highs if i >= base_i]

    # depth of each contraction = drop from its swing high to the lowest low
    # before the next swing high (or today, for the last one)
    depths, lows_i = [], []
    for k, (i, p) in enumerate(cons):
        j = cons[k + 1][0] if k + 1 < len(cons) else n
        seg = low[i:j]
        m = int(np.argmin(seg))
        depths.append(1 - seg[m] / p)
        lows_i.append(i + m)

    out["contractions"] = len(depths)
    out["depths"] = " → ".join(f"{d * 100:.0f}%" for d in depths)
    out["base_depth"] = depths[0]
    out["last_depth"] = depths[-1]
    out["base_weeks"] = round((n - 1 - base_i) / 5, 1)

    pivot_i, pivot = cons[-1]
    stop_floor = pivot * (1 - C.MAX_STOP_PCT)
    last_low = float(low[lows_i[-1]])
    stop = max(last_low, stop_floor)
    out["pivot"] = float(pivot)
    out["pivot_date"] = w.index[pivot_i].strftime("%Y-%m-%d")
    out["stop"] = float(stop)
    out["risk_pct"] = 1 - stop / pivot
    out["dist_to_pivot"] = close[-1] / pivot - 1

    avg50 = float(np.nanmean(df["volume"].tail(50)))
    avg10 = float(np.nanmean(vol[-10:]))
    out["vol_dryup"] = bool(avg10 < avg50)
    rng10 = (high[-10:].max() - low[-10:].min()) / close[-1]
    out["tight"] = bool(rng10 <= 0.10)

    # rules
    reasons = []
    if not (C.VCP_MIN_CONTRACTIONS <= len(depths) <= C.VCP_MAX_CONTRACTIONS):
        reasons.append(f"{len(depths)} contraction(s)")
    if depths[0] > C.VCP_MAX_FIRST_DEPTH:
        reasons.append("base too deep")
    if depths[0] < C.VCP_MIN_FIRST_DEPTH:
        reasons.append("base too shallow")
    if depths[-1] > C.VCP_MAX_LAST_DEPTH:
        reasons.append("last pullback too deep")
    if len(depths) >= 2:
        shrinking = all(depths[k] <= depths[k - 1] * 1.10 for k in range(1, len(depths)))
        if not shrinking or depths[-1] >= depths[0]:
            reasons.append("pullbacks not contracting")
    if reasons:
        out["vcp_reason"] = ", ".join(reasons)
        return out

    out["vcp"] = True
    st = entry_status(df, pivot, stop, out["pivot_date"])
    out.update(st)
    return out


def entry_status(df: pd.DataFrame, pivot: float, stop: float, pivot_date: str | None) -> dict:
    """Where price stands versus a fixed pivot. Used weekly and daily."""
    close, vol = df["close"], df["volume"]
    last = float(close.iloc[-1])
    avg50 = vol.rolling(50).mean().shift(1)  # average before each day
    res = {
        "dist_to_pivot": last / pivot - 1,
        "breakout_date": None,
        "breakout_vol_ratio": math.nan,
    }
    after = df.index > pd.Timestamp(pivot_date) if pivot_date else np.ones(len(df), bool)
    crossed = (close > pivot) & after
    if crossed.any():
        d = crossed[crossed].index[0]
        res["breakout_date"] = d.strftime("%Y-%m-%d")
        a = avg50.loc[d]
        res["breakout_vol_ratio"] = _f(vol.loc[d] / a) if a else math.nan

    if last < stop:
        status = "Failed"
    elif last > pivot * (1 + C.VCP_MAX_CHASE):
        status = "Extended"
    elif last > pivot:
        r = res["breakout_vol_ratio"]
        status = "Breakout" if r >= C.BREAKOUT_VOL_MULT else "Breakout (weak volume)"
    elif last >= pivot * (1 - C.VCP_READY_WITHIN):
        status = "Ready"
    else:
        status = "Forming"
    res["vcp_status"] = status
    return res
