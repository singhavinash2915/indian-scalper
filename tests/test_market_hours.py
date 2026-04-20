"""Market-hours gating: weekend, pre-open, session window, entry cutoff,
and holiday awareness via an optional HolidayCalendar."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from config.settings import Settings
from data.holidays import HolidayCalendar
from scheduler.market_hours import (
    IST,
    can_enter_new_trade,
    is_market_open,
    parse_hhmm,
)

HOLIDAY_FIXTURE = Path(__file__).parent / "fixtures" / "sample_holidays.yaml"


@pytest.fixture
def calendar(tmp_path: Path) -> HolidayCalendar:
    cal = HolidayCalendar(tmp_path / "holidays.db")
    cal.load_from_yaml(HOLIDAY_FIXTURE)
    return cal


def _ts(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_parse_hhmm() -> None:
    t = parse_hhmm("09:15")
    assert (t.hour, t.minute) == (9, 15)


def test_weekend_is_closed() -> None:
    settings = Settings.from_template()
    # 2026-04-18 is a Saturday, 2026-04-19 Sunday.
    assert not is_market_open(settings, _ts(2026, 4, 18, 11, 0))
    assert not is_market_open(settings, _ts(2026, 4, 19, 11, 0))


def test_session_window_weekday() -> None:
    settings = Settings.from_template()
    # 2026-04-20 is a Monday.
    assert not is_market_open(settings, _ts(2026, 4, 20, 9, 0))   # before open
    assert is_market_open(settings, _ts(2026, 4, 20, 9, 15))      # open edge
    assert is_market_open(settings, _ts(2026, 4, 20, 12, 30))     # mid
    assert is_market_open(settings, _ts(2026, 4, 20, 15, 30))     # close edge
    assert not is_market_open(settings, _ts(2026, 4, 20, 15, 31)) # after close


def test_entry_window_skips_first_15_minutes() -> None:
    settings = Settings.from_template()
    # First 15 min of session should block new entries (09:15–09:30).
    assert not can_enter_new_trade(settings, _ts(2026, 4, 20, 9, 15))
    assert not can_enter_new_trade(settings, _ts(2026, 4, 20, 9, 29))
    assert can_enter_new_trade(settings, _ts(2026, 4, 20, 9, 30))
    assert can_enter_new_trade(settings, _ts(2026, 4, 20, 14, 59))


def test_entry_cutoff_enforced() -> None:
    settings = Settings.from_template()
    # No new entries after 15:00.
    assert can_enter_new_trade(settings, _ts(2026, 4, 20, 15, 0))
    assert not can_enter_new_trade(settings, _ts(2026, 4, 20, 15, 1))
    assert not can_enter_new_trade(settings, _ts(2026, 4, 20, 15, 20))


def test_no_entry_on_weekend() -> None:
    settings = Settings.from_template()
    assert not can_enter_new_trade(settings, _ts(2026, 4, 18, 11, 0))


# ---------------------------------------------------------------- #
# Holiday-aware variants                                            #
# ---------------------------------------------------------------- #

def test_market_closed_on_holiday(calendar: HolidayCalendar) -> None:
    """2026-04-20 is flagged as a holiday in the fixture."""
    settings = Settings.from_template()
    ts = _ts(2026, 4, 20, 11, 0)
    # Without calendar, weekday + in-session → open.
    assert is_market_open(settings, ts)
    # With calendar, the holiday suppresses the session.
    assert not is_market_open(settings, ts, calendar=calendar)


def test_cannot_enter_trade_on_holiday(calendar: HolidayCalendar) -> None:
    settings = Settings.from_template()
    ts = _ts(2026, 4, 20, 11, 0)
    assert can_enter_new_trade(settings, ts)
    assert not can_enter_new_trade(settings, ts, calendar=calendar)


def test_non_holiday_unaffected_by_calendar(calendar: HolidayCalendar) -> None:
    """Regular weekdays still gate normally when a calendar is attached."""
    settings = Settings.from_template()
    ts = _ts(2026, 4, 21, 11, 0)  # Tuesday, not a holiday
    assert is_market_open(settings, ts, calendar=calendar)
    assert can_enter_new_trade(settings, ts, calendar=calendar)
