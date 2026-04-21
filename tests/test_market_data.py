"""CandleFetcher protocol + FakeCandleFetcher + CSV round-trip.

``YFinanceFetcher`` is not exercised here — it hits the network and
depends on external market data. Its integration is smoke-tested
manually.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brokers.base import Candle
from data.market_data import (
    FakeCandleFetcher,
    build_synthetic_candles,
    candles_from_csv,
    candles_to_csv,
)

IST = ZoneInfo("Asia/Kolkata")


def test_fake_fetcher_returns_seeded_candles() -> None:
    candles = build_synthetic_candles(
        start=datetime(2026, 4, 21, 9, 15, tzinfo=IST),
        interval_minutes=15,
        closes=[100.0, 101.0, 102.0, 103.0, 104.0],
    )
    f = FakeCandleFetcher({"RELIANCE": candles})
    got = f.get_candles("RELIANCE", "15m", lookback=3)
    assert [c.close for c in got] == [102.0, 103.0, 104.0]


def test_fake_fetcher_raises_on_unseeded_symbol() -> None:
    f = FakeCandleFetcher()
    with pytest.raises(KeyError):
        f.get_candles("UNKNOWN", "15m", 10)


def test_candles_csv_roundtrip(tmp_path: Path) -> None:
    candles = build_synthetic_candles(
        start=datetime(2026, 4, 21, 9, 15, tzinfo=IST),
        interval_minutes=15,
        closes=[100.0, 100.5, 101.0],
        volumes=[1000, 1500, 2000],
    )
    path = tmp_path / "cache" / "RELIANCE.csv"
    candles_to_csv(candles, path)
    assert path.exists()
    loaded = candles_from_csv(path)
    assert len(loaded) == len(candles)
    # Close values preserved.
    assert [c.close for c in loaded] == [c.close for c in candles]
    # Timestamps tz-aware and preserved.
    assert loaded[0].ts == candles[0].ts


def test_build_synthetic_candles_shapes() -> None:
    candles = build_synthetic_candles(
        start=datetime(2026, 4, 21, 9, 15, tzinfo=IST),
        interval_minutes=1,
        closes=[100.0, 101.0, 100.5],
    )
    assert len(candles) == 3
    # Second candle's open should equal first candle's close (continuity).
    assert candles[1].open == 100.0


# --------------------------------------------------------------------- #
# Bulk save/load (Tuesday-dry-run tooling)                               #
# --------------------------------------------------------------------- #

def test_save_and_load_bulk_round_trip(tmp_path: Path) -> None:
    from data.market_data import load_candles_bulk, save_candles_bulk

    start = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    series = {
        "RELIANCE": build_synthetic_candles(start, 15, [100.0, 101.0, 102.0]),
        "TCS": build_synthetic_candles(start, 15, [3000.0, 3005.0]),
    }
    written = save_candles_bulk(series, tmp_path / "cache")
    assert set(written.keys()) == {"RELIANCE", "TCS"}

    loaded = load_candles_bulk(tmp_path / "cache")
    assert set(loaded.keys()) == {"RELIANCE", "TCS"}
    assert [c.close for c in loaded["RELIANCE"]] == [100.0, 101.0, 102.0]
    assert [c.close for c in loaded["TCS"]] == [3000.0, 3005.0]


def test_load_bulk_filters_by_symbol_list(tmp_path: Path) -> None:
    from data.market_data import load_candles_bulk, save_candles_bulk

    start = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    series = {
        "A": build_synthetic_candles(start, 1, [1.0]),
        "B": build_synthetic_candles(start, 1, [2.0]),
        "C": build_synthetic_candles(start, 1, [3.0]),
    }
    save_candles_bulk(series, tmp_path / "cache")
    loaded = load_candles_bulk(tmp_path / "cache", symbols=["A", "C"])
    assert set(loaded.keys()) == {"A", "C"}


def test_load_bulk_nonexistent_dir_returns_empty(tmp_path: Path) -> None:
    from data.market_data import load_candles_bulk

    assert load_candles_bulk(tmp_path / "does_not_exist") == {}


def test_save_bulk_skips_empty_series(tmp_path: Path) -> None:
    from data.market_data import load_candles_bulk, save_candles_bulk

    # One symbol with data, one without.
    start = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    series = {
        "HAS_DATA": build_synthetic_candles(start, 1, [100.0]),
        "EMPTY": [],
    }
    save_candles_bulk(series, tmp_path / "cache")
    loaded = load_candles_bulk(tmp_path / "cache")
    assert "HAS_DATA" in loaded
    assert "EMPTY" not in loaded
