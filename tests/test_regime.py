"""Tests for src/strategy/regime.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from brokers.base import Candle
from strategy.regime import probe_regime

IST = ZoneInfo("Asia/Kolkata")


def _trending_candles(n: int = 120, start_price: float = 100.0, drift: float = 0.4) -> list[Candle]:
    """Build n candles with strong one-directional drift — ADX should be HIGH."""
    base = datetime(2026, 4, 24, 9, 15, tzinfo=IST)
    out = []
    price = start_price
    for i in range(n):
        o = price
        c = price + drift
        h = max(o, c) + 0.15
        low_p = min(o, c) - 0.15
        out.append(Candle(
            ts=base + timedelta(minutes=15 * i),
            open=o, high=h, low=low_p, close=c, volume=1000,
        ))
        price = c
    return out


def _ranging_candles(n: int = 120, base_price: float = 100.0) -> list[Candle]:
    """Build n candles oscillating tightly — ADX should be LOW."""
    base = datetime(2026, 4, 24, 9, 15, tzinfo=IST)
    rng = np.random.default_rng(42)
    out = []
    for i in range(n):
        o = base_price + rng.uniform(-0.4, 0.4)
        c = base_price + rng.uniform(-0.4, 0.4)
        out.append(Candle(
            ts=base + timedelta(minutes=15 * i),
            open=o, high=max(o, c) + 0.1, low=min(o, c) - 0.1, close=c, volume=1000,
        ))
    return out


class _Fetcher:
    def __init__(self, series: dict[str, list[Candle]]):
        self._series = series

    def get_candles(self, symbol: str, interval: str, lookback: int) -> list[Candle]:
        return self._series.get(symbol, [])


def test_trending_market_passes():
    candles = _trending_candles()
    fetcher = _Fetcher({"RELIANCE": candles})
    snap = probe_regime(fetcher, "RELIANCE", "15m", min_adx=22.0)
    assert snap.trending is True
    assert snap.adx > 22.0


def test_ranging_market_fails():
    candles = _ranging_candles()
    fetcher = _Fetcher({"RELIANCE": candles})
    snap = probe_regime(fetcher, "RELIANCE", "15m", min_adx=22.0)
    assert snap.trending is False
    assert snap.adx < 22.0
    assert "adx" in snap.reason


def test_fetch_failure_fails_open():
    """If the fetcher raises, caller must keep trading (don't silently halt)."""
    class _BadFetcher:
        def get_candles(self, *a, **kw):
            raise RuntimeError("network down")
    snap = probe_regime(_BadFetcher(), "RELIANCE", "15m", min_adx=22.0)
    assert snap.trending is True
    assert "fetch_error" in snap.reason


def test_insufficient_history_fails_open():
    fetcher = _Fetcher({"RELIANCE": _trending_candles(n=5)})
    snap = probe_regime(fetcher, "RELIANCE", "15m", min_adx=22.0)
    assert snap.trending is True
    assert "insufficient_candles" in snap.reason
