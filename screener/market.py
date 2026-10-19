"""Market context: index trend and sector ETF strength."""
from __future__ import annotations

import math

import pandas as pd

import config as C
from screener.indicators import perf, rs_raw


def index_status(df: pd.DataFrame) -> dict:
    c = df["close"]
    s50, s200 = c.rolling(50).mean(), c.rolling(200).mean()
    last = float(c.iloc[-1])
    out = {
        "close": last,
        "chg_1d": perf(c, 1), "chg_1w": perf(c, 5), "chg_1m": perf(c, 21),
        "sma50": float(s50.iloc[-1]), "sma200": float(s200.iloc[-1]),
        "above_50": bool(last > s50.iloc[-1]), "above_200": bool(last > s200.iloc[-1]),
        "sma200_rising": bool(len(c) > 221 and s200.iloc[-1] > s200.iloc[-22]),
    }
    if out["above_50"] and out["above_200"] and s50.iloc[-1] > s200.iloc[-1] and out["sma200_rising"]:
        out["trend"] = "Uptrend"
    elif out["above_200"]:
        out["trend"] = "Under pressure"
    else:
        out["trend"] = "Downtrend"
    return out


def sector_table(prices: dict[str, pd.DataFrame]) -> list[dict]:
    rows = []
    for sym, name in C.SECTOR_ETFS.items():
        df = prices.get(sym)
        if df is None or len(df) < 60:
            continue
        c = df["close"]
        s50 = c.rolling(50).mean().iloc[-1]
        s200 = c.rolling(200).mean().iloc[-1] if len(c) >= 200 else math.nan
        rows.append({
            "etf": sym, "sector": name, "rs_score": rs_raw(c),
            "perf_1m": perf(c, 21), "perf_3m": perf(c, 63), "perf_6m": perf(c, 126),
            "above_50": bool(c.iloc[-1] > s50),
            "above_200": bool(c.iloc[-1] > s200) if not math.isnan(s200) else None,
        })
    rows.sort(key=lambda r: (-(r["rs_score"] if not math.isnan(r["rs_score"]) else -9)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def build(prices: dict[str, pd.DataFrame]) -> dict:
    idx = {}
    for sym, name in C.BENCHMARKS.items():
        df = prices.get(sym)
        if df is not None and len(df) >= 200:
            idx[sym] = {"name": name, **index_status(df)}
    return {"indexes": idx, "sectors": sector_table(prices)}
