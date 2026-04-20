"""Indicator wrappers — verify stable output schemas + known-property sanity.

We don't try to re-derive pandas-ta's numerics; instead we check invariants
that would hold for any correct implementation:
  * EMA of constant series = same constant (once warm).
  * RSI of strictly rising series → 100.
  * RSI of strictly falling series → 0.
  * MACD histogram of strongly trending series has the expected sign.
  * ADX of strongly trending series > ADX of chop.
  * Bollinger bands: lower < middle < upper, bandwidth > 0.
  * Supertrend direction flips across up→down regimes.
  * VWAP resets across trading days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from strategy import indicators as ind
from tests.fixtures.synthetic import bullish_breakout_df, flat_chop_df

IST = timezone(timedelta(hours=5, minutes=30))


# ---------- EMA ---------- #

def test_ema_of_constant_series_equals_constant() -> None:
    s = pd.Series([100.0] * 50)
    e = ind.ema(s, length=10)
    # After warm-up the EMA must equal the constant to high precision.
    assert e.iloc[-1] == pytest.approx(100.0)


def test_ema_returns_named_series() -> None:
    s = pd.Series(np.linspace(1, 100, 100))
    e = ind.ema(s, length=20)
    assert e.name == "ema_20"


# ---------- RSI ---------- #

def test_rsi_rising_series_approaches_100() -> None:
    s = pd.Series(np.arange(1, 101, dtype=float))
    r = ind.rsi(s, length=14)
    assert r.iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_rsi_falling_series_approaches_0() -> None:
    s = pd.Series(np.arange(100, 0, -1, dtype=float))
    r = ind.rsi(s, length=14)
    assert r.iloc[-1] == pytest.approx(0.0, abs=1e-6)


# ---------- MACD ---------- #

def test_macd_columns_renamed() -> None:
    s = pd.Series(np.linspace(100, 200, 100))
    m = ind.macd(s)
    assert list(m.columns) == ["macd", "hist", "signal"]


def test_macd_histogram_positive_on_uptrend() -> None:
    # Accelerating uptrend — MACD line keeps growing, so the signal line
    # (EMA of MACD) lags and histogram stays clearly positive. A linear
    # series would let the signal catch up and drive hist → 0.
    s = pd.Series(np.arange(1, 101, dtype=float) ** 1.5)
    m = ind.macd(s)
    assert m["hist"].iloc[-1] > 0


# ---------- ADX ---------- #

def test_adx_columns_renamed() -> None:
    df = bullish_breakout_df()
    a = ind.adx(df["high"], df["low"], df["close"])
    assert list(a.columns) == ["adx", "dmp", "dmn"]


def test_adx_higher_in_trend_than_chop() -> None:
    trend = bullish_breakout_df()
    chop = flat_chop_df()
    adx_trend = ind.adx(trend["high"], trend["low"], trend["close"])["adx"].iloc[-1]
    adx_chop = ind.adx(chop["high"], chop["low"], chop["close"])["adx"].iloc[-1]
    assert adx_trend > adx_chop


# ---------- Bollinger Bands ---------- #

def test_bbands_ordering_and_columns() -> None:
    df = bullish_breakout_df()
    b = ind.bbands(df["close"])
    assert list(b.columns) == ["lower", "middle", "upper", "bandwidth", "percent"]
    last = b.iloc[-1]
    assert last["lower"] < last["middle"] < last["upper"]
    assert last["bandwidth"] > 0


# ---------- Supertrend ---------- #

def test_supertrend_columns_and_direction_sign() -> None:
    df = bullish_breakout_df()
    s = ind.supertrend(df["high"], df["low"], df["close"])
    assert list(s.columns) == ["line", "direction", "long", "short"]
    # After a strong bullish breakout the direction should be +1.
    assert int(s["direction"].iloc[-1]) == 1


# ---------- ATR ---------- #

def test_atr_positive_and_scales_with_range() -> None:
    df = bullish_breakout_df()
    a = ind.atr(df["high"], df["low"], df["close"])
    assert a.iloc[-1] > 0
    # ATR of the bullish-breakout series (wide-range bars) should exceed
    # ATR of the flat-chop series (tight bars).
    chop = flat_chop_df()
    a_chop = ind.atr(chop["high"], chop["low"], chop["close"])
    assert a.iloc[-1] > a_chop.iloc[-1]


# ---------- Volume SMA ---------- #

def test_volume_sma_length_and_nans() -> None:
    v = pd.Series(np.arange(1, 101, dtype=float))
    s = ind.volume_sma(v, length=20)
    # First 19 rows must be NaN (min_periods=length).
    assert s.iloc[18] != s.iloc[18]  # NaN
    assert s.iloc[19] == pytest.approx(v.iloc[:20].mean())


# ---------- VWAP ---------- #

def test_vwap_resets_across_trading_days() -> None:
    """A second trading day's VWAP must not carry over day 1's cumulative sums."""
    rng = np.random.default_rng(1)
    n = 20
    # Day 1 at one price regime, day 2 at a very different regime.
    idx_day1 = pd.DatetimeIndex(
        [datetime(2026, 4, 21, 9, 15, tzinfo=IST) + timedelta(minutes=15 * i) for i in range(n)]
    )
    idx_day2 = pd.DatetimeIndex(
        [datetime(2026, 4, 22, 9, 15, tzinfo=IST) + timedelta(minutes=15 * i) for i in range(n)]
    )
    close1 = 100 + rng.normal(0, 0.5, n)      # ndarray — no index alignment
    close2 = 500 + rng.normal(0, 0.5, n)
    df1 = pd.DataFrame(
        {
            "open": close1, "high": close1 + 0.2, "low": close1 - 0.2,
            "close": close1, "volume": np.full(n, 1000),
        },
        index=idx_day1,
    )
    df2 = pd.DataFrame(
        {
            "open": close2, "high": close2 + 0.2, "low": close2 - 0.2,
            "close": close2, "volume": np.full(n, 1000),
        },
        index=idx_day2,
    )
    df = pd.concat([df1, df2])
    v = ind.vwap(df)
    # Day 2's first VWAP (= v.iloc[n]) should sit near day 2's price
    # regime, not day 1's — proving the cumulative sums reset.
    first_day2 = float(v.iloc[n])
    assert 450 < first_day2 < 550, first_day2
    # Day 1's last VWAP (= v.iloc[n-1]) should sit near day 1's regime.
    last_day1 = float(v.iloc[n - 1])
    assert 80 < last_day1 < 120, last_day1


def test_vwap_requires_datetime_index() -> None:
    df = bullish_breakout_df().reset_index(drop=True)  # range index
    with pytest.raises(TypeError, match="DatetimeIndex"):
        ind.vwap(df)
