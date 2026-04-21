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
