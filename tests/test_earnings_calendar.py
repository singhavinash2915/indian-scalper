"""Tests for src/strategy/earnings_calendar.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from strategy.earnings_calendar import (
    load_earnings_today,
    symbol_passes_earnings_filter,
)


def test_load_parses_symbols_strips_comments_and_blanks(tmp_path: Path):
    p = tmp_path / "today.csv"
    p.write_text(
        "# header comment\n"
        "RELIANCE\n"
        "\n"
        "  TCS  \n"
        "# another comment\n"
        "infy\n"
    )
    out = load_earnings_today(p)
    assert out == {"RELIANCE", "TCS", "INFY"}


def test_load_handles_csv_extra_columns(tmp_path: Path):
    p = tmp_path / "today.csv"
    p.write_text("RELIANCE,2026-04-27,Q4\nTCS,2026-04-27,Q4\n")
    assert load_earnings_today(p) == {"RELIANCE", "TCS"}


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_earnings_today(tmp_path / "nope.csv") == set()


def test_filter_off_passes_everything():
    ok, reason = symbol_passes_earnings_filter("RELIANCE", {"TCS"}, "off")
    assert ok is True
    assert reason is None


def test_filter_exclude_blocks_listed_symbols():
    ok, reason = symbol_passes_earnings_filter("RELIANCE", {"RELIANCE"}, "exclude")
    assert ok is False
    assert reason == "earnings_today"


def test_filter_exclude_passes_unlisted():
    ok, _ = symbol_passes_earnings_filter("VBL", {"RELIANCE", "TCS"}, "exclude")
    assert ok is True


def test_filter_restrict_to_passes_listed():
    ok, _ = symbol_passes_earnings_filter("RELIANCE", {"RELIANCE", "TCS"}, "restrict_to")
    assert ok is True


def test_filter_restrict_to_blocks_unlisted():
    ok, reason = symbol_passes_earnings_filter("VBL", {"RELIANCE"}, "restrict_to")
    assert ok is False
    assert reason == "not_in_earnings_calendar"


def test_filter_restrict_to_blocks_all_when_calendar_empty():
    ok, reason = symbol_passes_earnings_filter("RELIANCE", set(), "restrict_to")
    assert ok is False
    assert reason == "earnings_calendar_empty"


def test_filter_unknown_mode_fails_open():
    ok, _ = symbol_passes_earnings_filter("RELIANCE", set(), "garbage")
    assert ok is True


@pytest.mark.parametrize("mode", ["OFF", "Exclude", "RESTRICT_TO"])
def test_filter_mode_case_insensitive(mode):
    # symbol_passes_earnings_filter normalises mode to lowercase.
    ok, _ = symbol_passes_earnings_filter("X", set(), mode)
    # OFF + Exclude (X not in set) + RESTRICT_TO (calendar empty) — first two pass, third blocks.
    if mode.lower() == "restrict_to":
        assert ok is False
    else:
        assert ok is True
