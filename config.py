"""Central settings. Change thresholds here, not in the code."""
import os
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

# --- Twelve Data -----------------------------------------------------------
API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
# Credits per minute on your plan (Grow 377). Check your Twelve Data dashboard and set the
# GitHub variable TD_CREDITS_PER_MIN if yours differs.
CREDITS_PER_MIN = int(os.environ.get("TD_CREDITS_PER_MIN", "377"))
HISTORY_BARS = 300  # enough for 52-week stats, SMA200 + 1 month, and 12-month RS

# --- Universe (option 2: liquid US common stocks) --------------------------
EXCHANGES = {"NYSE", "NASDAQ"}
MIN_PRICE = 10.0
MIN_AVG_VOLUME = 500_000  # 50-day average shares
# Every N days the weekly scan re-checks ALL US common stocks for liquidity
# (slower run, picks up IPOs and newly liquid names). Other weeks it only
# fetches the cached liquid list.
UNIVERSE_REBUILD_DAYS = 28
BATCH_SIZE = 50  # symbols per /time_series request (kept <= credits per minute)

# --- Trend Template --------------------------------------------------------
SMA200_RISING_LOOKBACK = 21  # trading days (~1 month)
MIN_ABOVE_52W_LOW = 0.30     # price at least 30% above 52-week low
MAX_BELOW_52W_HIGH = 0.25    # price within 25% of 52-week high
MIN_RS_RATING = 70

# --- VCP heuristic ---------------------------------------------------------
VCP_LOOKBACK = 130           # bars (~6 months) to look for the base
VCP_SWING_PCT = 0.03         # zigzag threshold for swing highs/lows
VCP_MIN_BASE_BARS = 15       # ~3 weeks minimum base
VCP_MIN_CONTRACTIONS = 2
VCP_MAX_CONTRACTIONS = 6
VCP_MIN_FIRST_DEPTH = 0.08   # first pullback at least 8% (filters noise)
VCP_MAX_FIRST_DEPTH = 0.35   # first pullback no deeper than 35%
VCP_MAX_LAST_DEPTH = 0.10    # final contraction no deeper than 10%
VCP_READY_WITHIN = 0.05      # "Ready" = close within 5% below pivot
VCP_MAX_CHASE = 0.05         # beyond 5% above pivot = "Extended"
BREAKOUT_VOL_MULT = 1.4      # breakout day volume vs 50-day average
MAX_STOP_PCT = 0.08          # suggested stop never wider than 8% below pivot

# --- Fundamentals (Grow plan: /earnings + /profile) ------------------------
MIN_EPS_GROWTH = 0.25
EARNINGS_REFRESH_DAYS = 100  # refetch at least this often, or after a report
PROFILE_REFRESH_DAYS = 180
FUNDAMENTAL_CREDIT_BUDGET = int(os.environ.get("TD_FUNDAMENTAL_BUDGET", "20000"))

# --- Market context --------------------------------------------------------
BENCHMARKS = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000"}
SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLV": "Health Care", "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples", "XLI": "Industrials", "XLB": "Materials",
    "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Services",
}
