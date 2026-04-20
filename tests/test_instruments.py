"""InstrumentMaster: CSV loader, SQLite round-trip, filters."""

from __future__ import annotations

from pathlib import Path

import pytest

from brokers.base import Segment
from data.instruments import InstrumentMaster

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "sample_equity_master.csv"


def _master(tmp_path: Path) -> InstrumentMaster:
    m = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "cache",
    )
    m.load_equity_from_csv(FIXTURE_CSV)
    return m


def test_loader_keeps_only_eq_series(tmp_path: Path) -> None:
    m = _master(tmp_path)
    # Fixture has 4 EQ + 1 BL + 1 BE — only EQ should be kept.
    assert m.count() == 4


def test_get_by_symbol(tmp_path: Path) -> None:
    m = _master(tmp_path)
    reliance = m.get("RELIANCE")
    assert reliance is not None
    assert reliance.symbol == "RELIANCE"
    assert reliance.exchange == "NSE"
    assert reliance.segment == Segment.EQUITY
    assert reliance.tick_size == 0.05
    assert reliance.lot_size == 1
    assert reliance.expiry is None


def test_get_unknown_returns_none(tmp_path: Path) -> None:
    m = _master(tmp_path)
    assert m.get("NOT_A_REAL_SYMBOL") is None


def test_filter_by_segment(tmp_path: Path) -> None:
    m = _master(tmp_path)
    equity = m.filter(segment=Segment.EQUITY)
    assert {i.symbol for i in equity} == {"RELIANCE", "TCS", "HDFCBANK", "INFY"}
    # All returned rows should actually be equity.
    assert all(i.segment == Segment.EQUITY for i in equity)


def test_filter_by_exchange(tmp_path: Path) -> None:
    m = _master(tmp_path)
    nse = m.filter(exchange="NSE")
    assert len(nse) == 4
    bse = m.filter(exchange="BSE")
    assert bse == []


def test_load_is_idempotent(tmp_path: Path) -> None:
    m = _master(tmp_path)
    n = m.count()
    m.load_equity_from_csv(FIXTURE_CSV)  # reload
    assert m.count() == n  # upsert, no duplicates


def test_rejects_empty_csv(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("SYMBOL, NAME OF COMPANY, SERIES\n")
    m = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "cache",
    )
    with pytest.raises(ValueError, match="no EQ rows"):
        m.load_equity_from_csv(empty)
