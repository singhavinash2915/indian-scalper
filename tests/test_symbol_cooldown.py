"""Tests for symbol_cooldown table + StateStore helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")


def test_set_and_get_cooldown(tmp_path: Path):
    s = StateStore(tmp_path / "t.db")
    until = datetime(2100, 1, 1, tzinfo=IST)   # far future
    s.set_symbol_cooldown("RELIANCE", until=until, reason="stop_loss")
    out = s.get_symbol_cooldown_until("RELIANCE")
    assert out is not None
    assert out == until


def test_cooldown_idempotent_overwrite(tmp_path: Path):
    s = StateStore(tmp_path / "t.db")
    s.set_symbol_cooldown("RELIANCE", until=datetime(2100, 1, 1, tzinfo=IST), reason="stop_loss")
    later = datetime(2100, 6, 1, tzinfo=IST)
    s.set_symbol_cooldown("RELIANCE", until=later, reason="trail_stop")
    assert s.get_symbol_cooldown_until("RELIANCE") == later


def test_cooldown_expired_returns_none_and_prunes(tmp_path: Path):
    s = StateStore(tmp_path / "t.db")
    past = datetime.now(IST) - timedelta(minutes=5)
    s.set_symbol_cooldown("RELIANCE", until=past, reason="stop_loss")
    # First read: detects expiry, returns None, prunes the row.
    assert s.get_symbol_cooldown_until("RELIANCE") is None
    # Second read: row gone.
    assert s.get_symbol_cooldown_until("RELIANCE") is None


def test_is_symbol_in_cooldown(tmp_path: Path):
    s = StateStore(tmp_path / "t.db")
    future = datetime.now(IST) + timedelta(minutes=30)
    s.set_symbol_cooldown("VBL", until=future, reason="stop_loss")
    assert s.is_symbol_in_cooldown("VBL") is True
    assert s.is_symbol_in_cooldown("UNKNOWN") is False


def test_symbols_are_isolated(tmp_path: Path):
    s = StateStore(tmp_path / "t.db")
    future = datetime.now(IST) + timedelta(minutes=30)
    s.set_symbol_cooldown("A", until=future, reason="stop_loss")
    assert s.is_symbol_in_cooldown("A") is True
    assert s.is_symbol_in_cooldown("B") is False
