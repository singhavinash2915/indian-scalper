"""IST timezone helpers + NSE market-hours gating.

Holiday awareness is opt-in: callers pass a ``HolidayCalendar`` (from
``src.data.holidays``) and weekends + calendar holidays are both filtered
out. When no calendar is supplied, only the weekend check runs — keeps
tests and boot-time paths that don't yet have a populated calendar
working unchanged.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from config.settings import Settings

if TYPE_CHECKING:
    from data.holidays import HolidayCalendar

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Always tz-aware; never use naive datetimes elsewhere in the codebase."""
    return datetime.now(IST)


def parse_hhmm(s: str) -> dtime:
    """Parse a ``"HH:MM"`` string from config.yaml into a ``datetime.time``."""
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def is_market_open(
    settings: Settings,
    ts: datetime | None = None,
    calendar: HolidayCalendar | None = None,
) -> bool:
    """True if ``ts`` falls inside the NSE equity session.

    Checks weekday, holiday calendar (if provided), and session window.
    """
    ts = ts or now_ist()
    if ts.weekday() >= 5:  # Sat/Sun
        return False
    if calendar is not None and calendar.is_trading_holiday(ts.date()):
        return False
    start = parse_hhmm(settings.market.session_start)
    end = parse_hhmm(settings.market.session_end)
    return start <= ts.time() <= end


def can_enter_new_trade(
    settings: Settings,
    ts: datetime | None = None,
    calendar: HolidayCalendar | None = None,
) -> bool:
    """Entry window = session_start + skip_first_minutes .. entry_cutoff.

    Inherits the holiday-awareness of ``is_market_open``.
    """
    ts = ts or now_ist()
    if not is_market_open(settings, ts, calendar=calendar):
        return False
    session_start = parse_hhmm(settings.market.session_start)
    cutoff = parse_hhmm(settings.market.entry_cutoff)
    earliest = (
        datetime.combine(ts.date(), session_start)
        + timedelta(minutes=settings.market.skip_first_minutes)
    ).time()
    return earliest <= ts.time() <= cutoff
