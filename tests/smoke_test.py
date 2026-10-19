"""Offline end-to-end test: runs weekly + daily on synthetic data in a temp
folder (your real data/ is not touched). No API key needed.

    python tests/smoke_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

import config as C  # noqa: E402
import pipeline  # noqa: E402
from screener import fundamentals as F  # noqa: E402
from tests.fake_td import FakeClient  # noqa: E402


def point_to(tmp: Path) -> None:
    C.DATA_DIR, C.CACHE_DIR = tmp, tmp / "cache"
    for name, fname in [("UNIVERSE_PATH", "universe.csv"), ("SCREEN_PATH", "screen.csv"),
                        ("WATCH_PATH", "watchlist.csv"), ("MARKET_PATH", "market.json"),
                        ("META_PATH", "run_meta.json"), ("SIGNALS_PATH", "signals_log.csv")]:
        setattr(pipeline, name, tmp / fname)
    F.EARN_PATH, F.PROF_PATH = tmp / "cache" / "earnings.json", tmp / "cache" / "profiles.json"


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        point_to(tmp)
        pipeline.run_weekly(FakeClient(), rebuild=True)
        wl = pd.read_csv(tmp / "watchlist.csv")
        assert len(wl) >= 5, f"expected synthetic VCP setups, got {len(wl)}"
        assert set(wl["vcp_status"]) <= pipeline.WATCH_STATUSES

        again = FakeClient()
        pipeline.run_weekly(again)
        assert not any(c.startswith(("earnings", "profile")) for c in again.calls), "cache not reused"

        pipeline.run_daily(FakeClient(breakout=True))
        wl = pd.read_csv(tmp / "watchlist.csv").set_index("symbol")
        assert wl.loc["VCPA", "vcp_status"] == "Breakout", wl.loc["VCPA", "vcp_status"]
        log = pd.read_csv(tmp / "signals_log.csv")
        assert "VCPA" in set(log["symbol"])
    print("smoke test passed")


if __name__ == "__main__":
    main()
