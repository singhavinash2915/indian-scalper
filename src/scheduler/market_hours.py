"""IST timezone helpers + NSE market-hours gating.

Deliverable 2 will plug in the NSE holiday calendar (``src/data/holidays.py``)
at the TODO below.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from config.settings import Settings

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Always tz-aware; never use naive datetimes elsewhere in the codebase."""
    return datetime.now(IST)


def parse_hhmm(s: str) -> dtime:
    """Parse a ``"HH:MM"`` string from config.yaml into a ``datetime.time``."""
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def is_market_open(settings: Settings, ts: datetime | None = None) -> bool:
    """True if ``ts`` falls inside the NSE equity session (weekday + within
    session_start/session_end). Holiday-aware check is added in Deliverable 2.
    """
    ts = ts or now_ist()
    if ts.weekday() >= 5:  # Sat/Sun
        return False
    # TODO(Deliverable 2): plug in NSE holiday calendar check here.
    start = parse_hhmm(settings.market.session_start)
    end = parse_hhmm(settings.market.session_end)
    return start <= ts.time() <= end


def can_enter_new_trade(settings: Settings, ts: datetime | None = None) -> bool:
    """Entry window = session_start + skip_first_minutes .. entry_cutoff."""
    ts = ts or now_ist()
    if not is_market_open(settings, ts):
        return False
    session_start = parse_hhmm(settings.market.session_start)
    cutoff = parse_hhmm(settings.market.entry_cutoff)
    earliest = (
        datetime.combine(ts.date(), session_start)
        + timedelta(minutes=settings.market.skip_first_minutes)
    ).time()
    return earliest <= ts.time() <= cutoff
