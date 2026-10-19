"""Minimal Twelve Data client with a per-minute credit limiter."""
from __future__ import annotations

import logging
import time
from collections import deque

import pandas as pd
import requests

log = logging.getLogger(__name__)
BASE_URL = "https://api.twelvedata.com"


class RateLimitError(RuntimeError):
    pass


class TwelveDataClient:
    def __init__(self, api_key: str, credits_per_min: int = 55):
        if not api_key:
            raise ValueError("TWELVE_DATA_API_KEY is not set")
        self.api_key = api_key
        self.credits_per_min = max(1, credits_per_min)
        self._window: deque[tuple[float, int]] = deque()
        self.session = requests.Session()
        self.credits_used = 0

    # -- rate limiting ------------------------------------------------------
    def _reserve(self, credits: int) -> None:
        credits = min(credits, self.credits_per_min)
        while True:
            now = time.monotonic()
            while self._window and now - self._window[0][0] >= 60:
                self._window.popleft()
            used = sum(c for _, c in self._window)
            if used + credits <= self.credits_per_min:
                self._window.append((now, credits))
                return
            time.sleep(max(0.5, 60 - (now - self._window[0][0]) + 0.5))

    def get(self, endpoint: str, params: dict, credits: int):
        params = {**params, "apikey": self.api_key}
        for attempt in range(6):
            self._reserve(credits)
            try:
                r = self.session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=90)
            except requests.RequestException as e:
                log.warning("network error on %s (%s), retrying", endpoint, e)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(61)
                continue
            try:
                data = r.json()
            except ValueError:
                time.sleep(10)
                continue
            if isinstance(data, dict) and data.get("status") == "error" and data.get("code") == 429:
                log.info("rate limited by server, waiting 61s")
                time.sleep(61)
                continue
            self.credits_used += credits
            return data
        raise RateLimitError(f"gave up on {endpoint} after retries")

    # -- endpoints ----------------------------------------------------------
    def stocks(self, country: str = "United States") -> pd.DataFrame:
        data = self.get("stocks", {"country": country, "type": "Common Stock"}, credits=1)
        return pd.DataFrame(data.get("data", []))

    def time_series(self, symbols: list[str], outputsize: int = 300) -> dict[str, pd.DataFrame]:
        """Batch daily bars. Costs 1 credit per symbol."""
        out: dict[str, pd.DataFrame] = {}
        if not symbols:
            return out
        data = self.get(
            "time_series",
            {"symbol": ",".join(symbols), "interval": "1day", "outputsize": outputsize},
            credits=len(symbols),
        )
        if len(symbols) == 1:
            data = {symbols[0]: data}
        if isinstance(data, dict) and data.get("status") == "error":
            log.warning("batch failed: %s", data.get("message"))
            return out
        for sym, payload in data.items():
            if not isinstance(payload, dict) or payload.get("status") == "error":
                continue
            df = values_to_frame(payload.get("values", []))
            if df is not None:
                out[sym] = df
        return out

    def earnings(self, symbol: str, outputsize: int = 12) -> list[dict]:
        data = self.get("earnings", {"symbol": symbol, "outputsize": outputsize}, credits=20)
        return data.get("earnings", []) if isinstance(data, dict) else []

    def profile(self, symbol: str) -> dict:
        data = self.get("profile", {"symbol": symbol}, credits=10)
        if not isinstance(data, dict) or data.get("status") == "error":
            return {}
        return data


def values_to_frame(values: list[dict]) -> pd.DataFrame | None:
    if not values:
        return None
    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ("open", "high", "low", "close", "volume"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df:
        df["volume"] = float("nan")
    return df.sort_values("datetime").set_index("datetime")[["open", "high", "low", "close", "volume"]]
