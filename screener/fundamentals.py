"""EPS (from /earnings) and sector (from /profile), cached between runs.

Grow plan has /earnings and /profile but not /income_statement or /statistics,
so sales growth and margins are checked by hand on the final shortlist.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta

import config as C

log = logging.getLogger(__name__)
EARN_PATH = C.CACHE_DIR / "earnings.json"
PROF_PATH = C.CACHE_DIR / "profiles.json"


def _load(path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("cache %s unreadable, starting fresh", path)
    return {}


def _save(path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=0, sort_keys=True, default=str))


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# EPS metrics
# ---------------------------------------------------------------------------
def eps_metrics(records: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    rows = []
    next_date = None
    for r in records:
        try:
            d = datetime.strptime(str(r.get("date")), "%Y-%m-%d").date()
        except ValueError:
            continue
        a = _num(r.get("eps_actual"))
        if a is None:
            if d >= today and (next_date is None or d < next_date):
                next_date = d
            continue
        rows.append((d, a, _num(r.get("surprise_prc"))))
    rows.sort(key=lambda x: x[0], reverse=True)

    out = {
        "eps_quarters": len(rows), "eps_last": None, "eps_g0": None, "eps_g1": None,
        "eps_g2": None, "eps_accel": False, "eps_turnaround": False, "eps_note": "",
        "surprise_last": None, "surprise_streak": 0, "last_report": None,
        "next_report": next_date.isoformat() if next_date else None,
    }
    if not rows:
        return out
    out["eps_last"] = rows[0][1]
    out["last_report"] = rows[0][0].isoformat()
    out["surprise_last"] = rows[0][2]
    streak = 0
    for _, _, s in rows:
        if s is not None and s > 0:
            streak += 1
        else:
            break
    out["surprise_streak"] = streak

    def yoy(i):
        """Year-over-year growth, or None when the base quarter is too small
        or negative for a percentage to mean anything (e.g. 0.02 -> 1.40
        would show +6,900%)."""
        if i + 4 >= len(rows):
            return None
        cur, base = rows[i][1], rows[i + 4][1]
        if base < C.EPS_MIN_BASE:
            return None
        return (cur - base) / base

    g = [yoy(i) for i in range(3)]
    out["eps_g0"], out["eps_g1"], out["eps_g2"] = g
    if len(rows) > 4:
        base, cur = rows[4][1], rows[0][1]
        if base <= 0 < cur:
            out["eps_turnaround"] = True
            out["eps_note"] = "turnaround (loss → profit)"
        elif 0 < base < C.EPS_MIN_BASE:
            out["eps_note"] = "base quarter too small for %"
        elif base <= 0 and cur <= 0:
            out["eps_note"] = "loss-making"
    if g[0] is not None and g[1] is not None:
        out["eps_accel"] = bool(g[0] > g[1] and (g[2] is None or g[1] > g[2]))
    return out


def eps_pass(m: dict) -> bool:
    """Latest quarter up >= MIN_EPS_GROWTH year over year on a meaningful base.
    Turnarounds are flagged in eps_note but don't count as a pass."""
    g0 = m.get("eps_g0")
    return bool(g0 is not None and g0 >= C.MIN_EPS_GROWTH)


# ---------------------------------------------------------------------------
# Cache refresh
# ---------------------------------------------------------------------------
def _stale_earnings(entry: dict | None, today: date) -> bool:
    if not entry or "records" not in entry:  # old cache format had no raw records
        return True
    fetched = date.fromisoformat(entry["fetched"])
    if (today - fetched).days >= C.EARNINGS_REFRESH_DAYS:
        return True
    nxt = entry.get("metrics", {}).get("next_report")
    # refetch a few days after the scheduled report so actuals are in
    if nxt and date.fromisoformat(nxt) + timedelta(days=2) <= today and fetched <= date.fromisoformat(nxt):
        return True
    return False


def _stale_profile(entry: dict | None, today: date) -> bool:
    if not entry:
        return True
    return (today - date.fromisoformat(entry["fetched"])).days >= C.PROFILE_REFRESH_DAYS


def refresh(client, symbols: list[str], budget: int, today: date | None = None) -> tuple[dict, dict, int]:
    """Fetch stale earnings/profiles for `symbols` (in priority order) within a
    credit budget. Returns (earnings_cache, profile_cache, credits_spent)."""
    today = today or date.today()
    earn, prof = _load(EARN_PATH), _load(PROF_PATH)
    spent = 0
    for n, sym in enumerate(symbols, 1):
        if _stale_profile(prof.get(sym), today) and spent + 10 <= budget:
            p = client.profile(sym)
            spent += 10
            prof[sym] = {
                "fetched": today.isoformat(),
                "sector": p.get("sector") or "", "industry": p.get("industry") or "",
                "name": p.get("name") or "",
            }
        if _stale_earnings(earn.get(sym), today) and spent + 20 <= budget:
            recs = client.earnings(sym)
            spent += 20
            earn[sym] = {"fetched": today.isoformat(), "records": _slim(recs)}
        if n % 25 == 0:
            _save(EARN_PATH, earn)
            _save(PROF_PATH, prof)
            log.info("fundamentals: %d/%d symbols, %d credits", n, len(symbols), spent)
    if spent + 10 > budget:
        log.info("fundamental credit budget reached (%d); stale names refresh next run", spent)
    _save(EARN_PATH, earn)
    _save(PROF_PATH, prof)
    return earn, prof, spent


def _slim(records: list[dict]) -> list[dict]:
    keep = ("date", "eps_actual", "eps_estimate", "surprise_prc")
    return [{k: r.get(k) for k in keep} for r in records]


def metrics_for(entry: dict | None, today: date | None = None) -> dict:
    """EPS metrics, recomputed from cached raw records so rule changes apply
    without refetching."""
    if not entry or "records" not in entry:
        return {}
    return eps_metrics(entry["records"], today)


# Company profiles use Morningstar-style names; the sector ETFs use GICS.
SECTOR_MAP = {
    "Healthcare": "Health Care",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Financial Services": "Financials",
    "Basic Materials": "Materials",
    "Communication": "Communication Services",
    "Information Technology": "Technology",
}


def normalize_sector(name: str | None) -> str:
    name = (name or "").strip()
    return SECTOR_MAP.get(name, name)


def profiles_for(client, symbols: list[str], today: date | None = None) -> dict:
    """Profiles for a few symbols (used to settle duplicate tickers)."""
    today = today or date.today()
    prof = _load(PROF_PATH)
    for sym in symbols:
        if _stale_profile(prof.get(sym), today):
            p = client.profile(sym)
            prof[sym] = {
                "fetched": today.isoformat(),
                "sector": p.get("sector") or "", "industry": p.get("industry") or "",
                "name": p.get("name") or "",
            }
    _save(PROF_PATH, prof)
    return {s: prof[s] for s in symbols}


def load_caches() -> tuple[dict, dict]:
    return _load(EARN_PATH), _load(PROF_PATH)
