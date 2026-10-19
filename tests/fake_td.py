"""Offline stand-in for TwelveDataClient so the pipeline and app can be tested
without an API key. Generates synthetic stocks with known shapes."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd


def _bdays(n: int, end: date | None = None) -> pd.DatetimeIndex:
    end = pd.Timestamp(end or date.today())
    return pd.bdate_range(end=end, periods=n)


def _ohlc(close: np.ndarray, rng, vol_base: float, vol: np.ndarray | None = None) -> pd.DataFrame:
    n = len(close)
    spread = np.abs(rng.normal(0, 0.006, n)) + 0.004
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum.reduce([high, open_, close])
    low = np.minimum.reduce([low, open_, close])
    if vol is None:
        vol = vol_base * np.exp(rng.normal(0, 0.25, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                        index=_bdays(n))


def vcp_stock(rng, n=300, start=20.0, breakout=False):
    """Strong uptrend, then a base with pullbacks of ~22%, ~11%, ~5%."""
    up = start * np.exp(np.cumsum(rng.normal(0.007, 0.008, n - 90)))
    top = up[-1]
    legs = [(top, top * 0.78, 14), (top * 0.78, top * 0.97, 14),
            (top * 0.97, top * 0.86, 10), (top * 0.86, top * 0.955, 12),
            (top * 0.955, top * 0.905, 8), (top * 0.905, top * 0.945, 12)]
    seg = [np.linspace(a, b, k) for a, b, k in legs]
    base = np.concatenate(seg)
    used = len(up) + len(base)
    tail_n = n - used
    tail = np.full(tail_n, base[-1]) * (1 + rng.normal(0, 0.002, tail_n))
    close = np.concatenate([up, base, tail])
    vol = 2e6 * np.exp(rng.normal(0, 0.15, n))
    vol[-25:] *= 0.55  # dry-up
    if breakout:
        close[-1] = top * 0.955 * 1.008
        vol[-1] = 2e6 * 2.2
    return _ohlc(close, rng, 2e6, vol)


def trend_stock(rng, n=300, drift=0.003):
    close = 30 * np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
    return _ohlc(close, rng, 1.5e6)


def down_stock(rng, n=300):
    close = 80 * np.exp(np.cumsum(rng.normal(-0.002, 0.015, n)))
    return _ohlc(close, rng, 1.5e6)


def illiquid_stock(rng, n=300):
    close = 4 * np.exp(np.cumsum(rng.normal(0.002, 0.02, n)))
    return _ohlc(close, rng, 50_000)


class FakeClient:
    def __init__(self, seed=7, breakout=False):
        rng = np.random.default_rng(seed)
        self.data: dict[str, pd.DataFrame] = {}
        self.names: dict[str, str] = {}
        for i in range(8):
            self.data[f"VCP{chr(65 + i)}"] = vcp_stock(rng, breakout=(breakout and i == 0))
        for i in range(60):
            self.data[f"UP{chr(65 + i // 26)}{chr(65 + i % 26)}"] = trend_stock(rng, drift=rng.uniform(0.0, 0.004))
        for i in range(40):
            self.data[f"DN{chr(65 + i // 26)}{chr(65 + i % 26)}"] = down_stock(rng)
        for i in range(10):
            self.data[f"IL{chr(65 + i)}"] = illiquid_stock(rng)
        # an old ticker that returns the same bars as VCPA (like CDEV vs PR)
        self.data["OLDA"] = self.data["VCPA"]
        self.no_profile = {"OLDA"}
        for sym in ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB",
                    "XLU", "XLRE", "XLC"]:
            self.data[sym] = trend_stock(rng, drift=rng.uniform(-0.001, 0.0015))
        self.credits_per_min = 55
        self.credits_used = 0
        self.calls: list[str] = []

    def stocks(self, country="United States"):
        rows = [{"symbol": s, "name": f"{s} Corp", "exchange": "NASDAQ" if k % 2 else "NYSE",
                 "type": "Common Stock"}
                for k, s in enumerate(self.data) if not s.startswith("X") and s not in ("SPY", "QQQ", "IWM")]
        rows.append({"symbol": "WEIRD-WS", "name": "Warrant", "exchange": "NYSE", "type": "Common Stock"})
        rows.append({"symbol": "OTCX", "name": "OTC", "exchange": "OTC", "type": "Common Stock"})
        self.calls.append("stocks")
        return pd.DataFrame(rows)

    def time_series(self, symbols, outputsize=300):
        self.credits_used += len(symbols)
        self.calls.append(f"time_series x{len(symbols)}")
        return {s: self.data[s].tail(outputsize).copy() for s in symbols if s in self.data}

    def earnings(self, symbol, outputsize=12):
        self.credits_used += 20
        self.calls.append(f"earnings {symbol}")
        today = date.today()
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        good = symbol.startswith("VCP") and symbol != "VCPB"
        recs = [{"date": (today + timedelta(days=30)).isoformat(), "eps_actual": None,
                 "eps_estimate": 1.0, "surprise_prc": None}]
        eps = 1.0
        vals = []
        for q in range(10):
            vals.append(eps)
            eps *= (0.80 + 0.015 * q) if good else rng.uniform(0.95, 1.05)
        if symbol == "VCPC":  # year-ago quarter of $0.02 -> % growth is meaningless
            vals = [1.40, 1.10, 0.90, 0.50, 0.02, 0.01, 0.30, 0.20, 0.10, 0.05]
        if symbol == "VCPE":  # loss a year ago, profit now
            vals = [0.60, 0.40, 0.20, -0.10, -0.30, -0.20, -0.10, 0.05, 0.10, 0.10]
        for q, v in enumerate(vals):  # q=0 latest
            recs.append({"date": (today - timedelta(days=20 + 91 * q)).isoformat(),
                         "eps_actual": round(v, 3), "eps_estimate": round(v * 0.95, 3),
                         "surprise_prc": 5.0 if good else rng.uniform(-5, 5)})
        return recs

    def profile(self, symbol):
        self.credits_used += 10
        self.calls.append(f"profile {symbol}")
        if symbol in self.no_profile:
            return {}
        sectors = ["Technology", "Healthcare", "Industrials", "Financial Services", "Energy"]
        return {"sector": sectors[hash(symbol) % len(sectors)], "industry": "Test Industry",
                "name": f"{symbol} Corp"}
