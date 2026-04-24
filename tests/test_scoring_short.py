"""Tests for the bearish (short-side) scorer."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from config.settings import StrategyCfg
from strategy.scoring import MIN_LOOKBACK_BARS, score_symbol, score_symbol_short

IST = ZoneInfo("Asia/Kolkata")


def _bearish_df(n: int = 120) -> pd.DataFrame:
    """Downward-trending OHLCV with a recent breakdown."""
    base = datetime(2026, 4, 24, 9, 15, tzinfo=IST)
    rng = np.random.default_rng(7)
    price = 200.0
    rows = []
    for i in range(n):
        # Steady downward drift + small noise.
        drift = -0.45
        price = price + drift + rng.normal(0, 0.1)
        o = price + rng.uniform(-0.1, 0.1)
        c = price + rng.uniform(-0.1, 0.1)
        h = max(o, c) + 0.15
        low = min(o, c) - 0.15
        rows.append({
            "ts": base + timedelta(minutes=15 * i),
            "open": o, "high": h, "low": low, "close": c,
            "volume": 1000 + (3000 if i == n - 1 else 0),   # volume spike on last bar
        })
    df = pd.DataFrame(rows).set_index("ts")
    return df


def _bullish_df(n: int = 120) -> pd.DataFrame:
    """Upward-trending OHLCV — bearish scorer should NOT light up."""
    base = datetime(2026, 4, 24, 9, 15, tzinfo=IST)
    rng = np.random.default_rng(3)
    price = 100.0
    rows = []
    for i in range(n):
        price = price + 0.4 + rng.normal(0, 0.1)
        o = price + rng.uniform(-0.1, 0.1)
        c = price + rng.uniform(-0.1, 0.1)
        rows.append({
            "ts": base + timedelta(minutes=15 * i),
            "open": o, "high": max(o, c) + 0.15, "low": min(o, c) - 0.15, "close": c,
            "volume": 1000,
        })
    return pd.DataFrame(rows).set_index("ts")


def _make_cfg() -> StrategyCfg:
    return StrategyCfg(
        candle_interval="15m", scan_interval_seconds=300, min_score=6,
        rsi_upper_block=78, rsi_entry_range=(55, 75), adx_min=15,
        volume_surge_multiplier=2.0, ema_fast=5, ema_mid=13, ema_slow=34,
        ema_trend=50, supertrend_period=10, supertrend_multiplier=3.0,
        enable_shorts=True,
        short_rsi_entry_low=25, short_rsi_entry_high=45, short_rsi_hard_block=22,
    )


def test_short_scorer_fires_on_bearish_data():
    df = _bearish_df()
    cfg = _make_cfg()
    s = score_symbol_short(df, cfg)
    # Downward trend + volume spike should pass several bearish factors.
    assert s.total >= 3
    # EMA stack + supertrend should be the most reliable — both down.
    names_passed = set(s.passed_factors)
    assert "ema_stack" in names_passed
    assert "supertrend" in names_passed


def test_long_scorer_stays_cold_on_bearish_data():
    df = _bearish_df()
    cfg = _make_cfg()
    long_s = score_symbol(df, cfg)
    assert long_s.total < cfg.min_score


def test_short_scorer_stays_cold_on_bullish_data():
    df = _bullish_df()
    cfg = _make_cfg()
    short_s = score_symbol_short(df, cfg)
    assert short_s.total < cfg.min_score


def test_short_hard_block_when_rsi_deeply_oversold():
    """Craft a series where RSI drops below the hard-block threshold."""
    base = datetime(2026, 4, 24, 9, 15, tzinfo=IST)
    price = 100.0
    rows = []
    for i in range(120):
        price = price - 1.5   # aggressive down; RSI will crater
        rows.append({
            "ts": base + timedelta(minutes=15 * i),
            "open": price + 0.1, "high": price + 0.2, "low": price - 0.1,
            "close": price, "volume": 1000,
        })
    df = pd.DataFrame(rows).set_index("ts")
    cfg = _make_cfg()
    s = score_symbol_short(df, cfg)
    assert s.blocked is True
    assert "short_rsi_hard_block" in (s.block_reason or "")


def test_short_scorer_requires_min_lookback():
    tiny = _bearish_df(n=5)
    with pytest.raises(ValueError, match="need >="):
        score_symbol_short(tiny, _make_cfg())
