"""HolidayCalendar: YAML loading, SQLite round-trip, trading-day queries."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from data.holidays import DEFAULT_YAML_PATH, HolidayCalendar

FIXTURE = Path(__file__).parent / "fixtures" / "sample_holidays.yaml"


def _calendar(tmp_path: Path, yaml_path: Path = FIXTURE) -> HolidayCalendar:
    cal = HolidayCalendar(tmp_path / "holidays.db")
    cal.load_from_yaml(yaml_path)
    return cal


def test_load_from_fixture_inserts_expected_count(tmp_path: Path) -> None:
    cal = _calendar(tmp_path)
    # 3 in 2026 + 1 in 2027.
    assert cal.count() == 4


def test_is_trading_holiday(tmp_path: Path) -> None:
    cal = _calendar(tmp_path)
    assert cal.is_trading_holiday(date(2026, 1, 26))
    assert cal.is_trading_holiday(date(2026, 4, 20))
    assert not cal.is_trading_holiday(date(2026, 4, 21))  # Tuesday after, no holiday


def test_is_trading_day_weekend_vs_holiday(tmp_path: Path) -> None:
    cal = _calendar(tmp_path)
    assert not cal.is_trading_day(date(2026, 4, 18))  # Saturday
    assert not cal.is_trading_day(date(2026, 4, 19))  # Sunday
    assert not cal.is_trading_day(date(2026, 4, 20))  # Holiday (fixture)
    assert cal.is_trading_day(date(2026, 4, 21))      # Regular Tuesday


def test_next_trading_day_skips_weekend_and_holiday(tmp_path: Path) -> None:
    cal = _calendar(tmp_path)
    # Friday 2026-04-17 → next trading day should skip Sat/Sun + Mon (holiday)
    # and land on Tuesday 2026-04-21.
    assert cal.next_trading_day(date(2026, 4, 17)) == date(2026, 4, 21)
    # Monday holiday → next trading day = Tuesday.
    assert cal.next_trading_day(date(2026, 4, 20)) == date(2026, 4, 21)


def test_holidays_for_year_ordered(tmp_path: Path) -> None:
    cal = _calendar(tmp_path)
    got = cal.holidays_for_year(2026)
    assert [d.isoformat() for d, _ in got] == [
        "2026-01-26",
        "2026-04-20",
        "2026-08-15",
    ]
    # 2027 has 1 entry.
    assert len(cal.holidays_for_year(2027)) == 1


def test_load_is_idempotent(tmp_path: Path) -> None:
    cal = _calendar(tmp_path)
    n1 = cal.count()
    cal.load_from_yaml(FIXTURE)  # reload
    assert cal.count() == n1  # no duplicates due to ON CONFLICT upsert


def test_default_yaml_parses(tmp_path: Path) -> None:
    """The shipped default YAML must always be loadable."""
    cal = HolidayCalendar(tmp_path / "holidays.db")
    count = cal.load_from_yaml(DEFAULT_YAML_PATH)
    assert count >= 5  # at least Republic Day + Maharashtra + Indep + Gandhi + Christmas for one year


def test_rejects_non_int_year(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text('"2026":\n  - { date: "2026-01-26", name: "x" }\n')
    cal = HolidayCalendar(tmp_path / "holidays.db")
    with pytest.raises(ValueError, match="year key must be int"):
        cal.load_from_yaml(bad)
