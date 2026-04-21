"""Synthetic OHLCV generators — shipped as package code so the
preflight backtest-regression check can import them without pulling
the ``tests/`` directory into the installed wheel.

Deterministic (seeded). Each scenario is engineered so the scoring
engine produces a known outcome:

* ``bullish_breakout_df`` — long flat squeeze then a moderate uptrend
  with a final-bar volume spike. Tuned so RSI lands in the entry
  range (55–75) rather than above the hard-block threshold (78).
  Reliably scores ≥ 4/8 without triggering the hard block.
* ``flat_chop_df`` — pure sideways noise; scores low.
* ``parabolic_df`` — relentless vertical rally that pushes RSI above
  ``rsi_upper_block`` → triggers the hard block.

Previously lived at ``tests/fixtures/synthetic.py``; that path
re-exports from here for back-compat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))


def _index(n_bars: int, start: datetime | None = None) -> pd.DatetimeIndex:
    """1-minute bars on a single synthetic session.

    Using 1-minute (not 15-minute) spacing keeps all bars in one
    calendar day even at n_bars=120, so the intraday VWAP reset does
    not fire mid-series and fixture behaviour stays predictable.
    """
    start = start or datetime(2026, 4, 21, 9, 15, tzinfo=IST)
    return pd.DatetimeIndex(
        [start + timedelta(minutes=i) for i in range(n_bars)],
        name="ts",
    )


def _ohlc_from_close(close: np.ndarray, spread: np.ndarray) -> dict[str, np.ndarray]:
    opens = np.concatenate(([close[0]], close[:-1]))
    highs = np.maximum(opens, close) + spread
    lows = np.minimum(opens, close) - spread
    return {"open": opens, "high": highs, "low": lows, "close": close}


def bullish_breakout_df(n_bars: int = 120, seed: int = 7) -> pd.DataFrame:
    """Flat squeeze → bullish uptrend (with pullbacks) → final-bar volume spike.

    The trend phase uses an explicit linear ramp plus *larger* gaussian
    noise. Why not a cumsum random-walk with positive drift? Because a
    cumsum with drift > noise is monotonic and pegs RSI at 100, which
    trips the hard block. The linear-ramp + independent-noise design
    guarantees bar-to-bar pullbacks so RSI lands in the 55–75 entry range.

    Phase ratios scale with ``n_bars`` so short-history error tests
    (n_bars ≈ 59) still generate cleanly.
    """
    rng = np.random.default_rng(seed)
    trend = max(14, n_bars // 4)  # at least one full RSI window in the trend
    flat = n_bars - trend

    flat_close = 1000 + rng.normal(0, 0.3, flat)
    ramp = np.linspace(0.0, 0.4 * trend, trend)
    trend_noise = rng.normal(0, 1.0, trend)
    trend_close = flat_close[-1] + ramp + trend_noise
    close = np.concatenate([flat_close, trend_close])

    spread = np.concatenate(
        [rng.uniform(0.15, 0.4, flat), rng.uniform(0.5, 1.1, trend)]
    )
    ohlc = _ohlc_from_close(close, spread)

    vol_flat = rng.integers(1_500, 2_500, flat)
    vol_trend = rng.integers(3_000, 5_000, trend)
    volume = np.concatenate([vol_flat, vol_trend]).astype(int)
    volume[-1] = int(volume[-trend:-1].mean() * 4)

    return pd.DataFrame({**ohlc, "volume": volume}, index=_index(n_bars))


def flat_chop_df(n_bars: int = 120, seed: int = 7) -> pd.DataFrame:
    """Pure sideways noise. Should score well under ``min_score``."""
    rng = np.random.default_rng(seed)
    close = 1000 + np.cumsum(rng.normal(0.0, 0.25, n_bars))
    spread = rng.uniform(0.2, 0.5, n_bars)
    ohlc = _ohlc_from_close(close, spread)
    volume = rng.integers(1_500, 2_500, n_bars)
    return pd.DataFrame({**ohlc, "volume": volume}, index=_index(n_bars))


def parabolic_df(n_bars: int = 120, seed: int = 7) -> pd.DataFrame:
    """Relentless uptrend → RSI > 78 → hard block triggers."""
    rng = np.random.default_rng(seed)
    close = 1000 + np.cumsum(np.abs(rng.normal(3.0, 0.2, n_bars)))
    spread = rng.uniform(0.5, 1.2, n_bars)
    ohlc = _ohlc_from_close(close, spread)
    volume = rng.integers(4_000, 8_000, n_bars)
    return pd.DataFrame({**ohlc, "volume": volume}, index=_index(n_bars))
