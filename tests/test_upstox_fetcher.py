"""Tests for UpstoxFetcher + _default_fetcher selection."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from brokers.paper import _default_fetcher
from config.settings import Settings
from data.instruments import InstrumentMaster
from data.market_data import (
    UpstoxFetcher,
    YFinanceFetcher,
    _interval_to_minutes,
    _resample_candles,
    _resample_candles_30m_to,
    _rows_to_candles,
)


# --------------------------------------------------------------------- #
# Pure helpers                                                          #
# --------------------------------------------------------------------- #

def test_interval_to_minutes_variants():
    assert _interval_to_minutes("1m") == 1
    assert _interval_to_minutes("15m") == 15
    assert _interval_to_minutes("1h") == 60
    assert _interval_to_minutes("2h") == 120
    with pytest.raises(ValueError):
        _interval_to_minutes("daily")


def test_rows_to_candles_parses_ist_and_naive():
    rows = [
        ["2026-04-21T09:15:00+05:30", 100.0, 102.0, 99.0, 101.0, 1000, 0],
        ["2026-04-21T09:30:00+05:30", 101.0, 103.0, 100.5, 102.5, 2000, 0],
    ]
    candles = _rows_to_candles(rows)
    assert len(candles) == 2
    assert candles[0].open == 100.0 and candles[0].close == 101.0
    assert candles[0].volume == 1000
    assert candles[0].ts.tzinfo is not None


def test_resample_1m_to_15m_aggregates_ohlcv():
    """Fifteen 1-minute bars should collapse into one 15m bar."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from brokers.base import Candle
    IST = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    candles_1m = []
    for i in range(30):   # 30 minutes → 2 × 15m bars
        candles_1m.append(Candle(
            ts=start + timedelta(minutes=i),
            open=100.0 + i, high=101.0 + i, low=99.0 + i,
            close=100.5 + i, volume=1000,
        ))
    out = _resample_candles(candles_1m, 15)
    assert len(out) == 2
    # First 15m bar: open of minute-0, close of minute-14, hi/lo spread, sum vol
    assert out[0].open == 100.0
    assert out[0].close == 114.5
    assert out[0].high == 115.0
    assert out[0].low == 99.0
    assert out[0].volume == 15_000


def test_resample_30m_to_15m_duplicates_for_warmup():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from brokers.base import Candle
    IST = ZoneInfo("Asia/Kolkata")
    candles = [Candle(
        ts=datetime(2026, 4, 21, 9, 15, tzinfo=IST),
        open=100.0, high=105.0, low=99.0, close=103.0, volume=6000,
    )]
    out = _resample_candles_30m_to(candles, 15)
    # 30m → 15m: each bar splits into 2, volume halved
    assert len(out) == 2
    assert out[0].volume == 3000
    assert out[0].open == out[1].open == 100.0  # same OHLC across splits


# --------------------------------------------------------------------- #
# UpstoxFetcher — full path with mocked HTTP                            #
# --------------------------------------------------------------------- #

@pytest.fixture
def instruments_db(tmp_path: Path) -> InstrumentMaster:
    """Minimal InstrumentMaster with one row for RELIANCE."""
    db = tmp_path / "test.db"
    master = InstrumentMaster(db_path=db, cache_dir=tmp_path / "cache")
    # Seed one row with an ISIN.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO instruments
               (symbol, exchange, segment, tick_size, lot_size, name, isin, series, updated_at)
               VALUES (?, 'NSE', 'EQ', 0.05, 1, 'Reliance Industries',
                       'INE002A01018', 'EQ', '2026-04-21T00:00:00')""",
            ("RELIANCE",),
        )
    return master


def test_upstox_fetcher_requires_token(instruments_db: InstrumentMaster):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
        with pytest.raises(RuntimeError, match="UPSTOX_ACCESS_TOKEN"):
            UpstoxFetcher(instruments=instruments_db)


def test_upstox_fetcher_resolves_instrument_key(instruments_db: InstrumentMaster):
    f = UpstoxFetcher(access_token="fake", instruments=instruments_db)
    assert f._instrument_key("RELIANCE") == "NSE_EQ|INE002A01018"


def test_upstox_fetcher_raises_on_unknown_symbol(instruments_db: InstrumentMaster):
    f = UpstoxFetcher(access_token="fake", instruments=instruments_db)
    with pytest.raises(ValueError, match="no ISIN"):
        f._instrument_key("UNKNOWN")


def test_upstox_fetcher_get_candles_happy_path(instruments_db: InstrumentMaster):
    """End-to-end: intraday 1m → resample to 15m. HTTP layer mocked."""
    f = UpstoxFetcher(access_token="fake", instruments=instruments_db)
    # Build 60 1-minute rows (Upstox returns newest-first).
    rows_newest_first = []
    base_hi = 1500.0
    for i in reversed(range(60)):
        rows_newest_first.append([
            f"2026-04-21T{9 + i // 60:02d}:{15 + (i % 60) if i < 45 else (i - 45):02d}:00+05:30",
            base_hi + i, base_hi + i + 1, base_hi + i - 0.5, base_hi + i + 0.5,
            1000 * (i + 1), 0,
        ])

    def fake_get(url, params=None):
        return {"data": {"candles": rows_newest_first}}

    f._http_get = fake_get   # type: ignore[method-assign]

    candles = f.get_candles("RELIANCE", "15m", lookback=4)
    # 60 minutes → 4 × 15m bars
    assert len(candles) == 4
    # Volume should be summed over each 15-min window.
    assert all(c.volume > 0 for c in candles)


# --------------------------------------------------------------------- #
# _default_fetcher selection logic                                      #
# --------------------------------------------------------------------- #

def _settings_with_data_source(tmp_path: Path, source: str) -> Settings:
    cfg = tmp_path / "config.yaml"
    # Minimal valid config — reuse the template's required sections.
    from config.settings import CONFIG_YAML_TEMPLATE
    base = CONFIG_YAML_TEMPLATE.replace("source: auto", f"source: {source}")
    if "source:" not in base:
        base += f"\ndata:\n  source: {source}\n"
    cfg.write_text(base)
    return Settings.load(cfg)


def test_default_fetcher_auto_picks_yfinance_without_token(tmp_path):
    """No UPSTOX_ACCESS_TOKEN → fall back to yfinance even on auto."""
    settings = _settings_with_data_source(tmp_path, "auto")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
        fetcher = _default_fetcher(settings)
    assert isinstance(fetcher, YFinanceFetcher)


def test_default_fetcher_auto_picks_upstox_with_token(tmp_path):
    """UPSTOX_ACCESS_TOKEN set → UpstoxFetcher wins on auto."""
    db = tmp_path / "t.db"
    master = InstrumentMaster(db_path=db, cache_dir=tmp_path / "cache")
    settings = _settings_with_data_source(tmp_path, "auto")
    with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "fake"}, clear=False):
        fetcher = _default_fetcher(settings, instruments=master)
    assert isinstance(fetcher, UpstoxFetcher)


def test_default_fetcher_explicit_yfinance_overrides_token(tmp_path):
    """data.source=yfinance must win even if Upstox token is present."""
    settings = _settings_with_data_source(tmp_path, "yfinance")
    with patch.dict(os.environ, {"UPSTOX_ACCESS_TOKEN": "fake"}, clear=False):
        fetcher = _default_fetcher(settings)
    assert isinstance(fetcher, YFinanceFetcher)
