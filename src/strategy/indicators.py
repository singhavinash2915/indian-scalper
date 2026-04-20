"""Pure-function indicator wrappers over pandas-ta.

These wrappers exist for three reasons:

1. **Stable column names.** pandas-ta embeds parameters in its output
   column names (``MACD_12_26_9``, ``SUPERT_10_3``, ``BBL_20_2.0_2.0``).
   Callers downstream should not care about parameters — we rename to
   predictable short names (``macd``, ``hist``, ``line``, ``lower`` …).

2. **Type narrowing.** Every wrapper returns either a ``pd.Series`` (for
   single-value indicators) or a ``pd.DataFrame`` (for multi-output).
   Callers never get a ``DataFrame | None``.

3. **Intraday VWAP.** pandas-ta's VWAP is session-based and requires a
   tz-aware DatetimeIndex; we implement it explicitly so session
   reset behaviour is obvious.

Every function is side-effect free and does not mutate its inputs.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta


# --------------------------------------------------------------------- #
# Single-series indicators                                              #
# --------------------------------------------------------------------- #

def ema(close: pd.Series, length: int) -> pd.Series:
    """Exponential moving average."""
    out = ta.ema(close, length=length)
    if out is None:
        raise ValueError(f"ema(length={length}): pandas-ta returned None")
    return out.rename(f"ema_{length}")


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder)."""
    out = ta.rsi(close, length=length)
    if out is None:
        raise ValueError(f"rsi(length={length}): pandas-ta returned None")
    return out.rename(f"rsi_{length}")


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> pd.Series:
    """Average True Range — needed here (used by risk) so risk/stops.py
    doesn't have to import pandas_ta directly."""
    out = ta.atr(high, low, close, length=length)
    if out is None:
        raise ValueError(f"atr(length={length}): pandas-ta returned None")
    return out.rename(f"atr_{length}")


def volume_sma(volume: pd.Series, length: int = 20) -> pd.Series:
    """Simple moving average of volume — used for the volume-surge factor."""
    return volume.rolling(window=length, min_periods=length).mean().rename(
        f"volume_sma_{length}"
    )


# --------------------------------------------------------------------- #
# Multi-output indicators                                                #
# --------------------------------------------------------------------- #

def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD → DataFrame with columns ``macd``, ``hist``, ``signal``."""
    out = ta.macd(close, fast=fast, slow=slow, signal=signal)
    if out is None:
        raise ValueError("macd: pandas-ta returned None")
    suffix = f"{fast}_{slow}_{signal}"
    return out.rename(
        columns={
            f"MACD_{suffix}": "macd",
            f"MACDh_{suffix}": "hist",
            f"MACDs_{suffix}": "signal",
        }
    )[["macd", "hist", "signal"]]


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
) -> pd.DataFrame:
    """ADX → DataFrame with columns ``adx``, ``dmp``, ``dmn``."""
    out = ta.adx(high, low, close, length=length)
    if out is None:
        raise ValueError(f"adx(length={length}): pandas-ta returned None")
    rename: dict[str, str] = {}
    for col in out.columns:
        if col.startswith("ADX_"):
            rename[col] = "adx"
        elif col.startswith("DMP_"):
            rename[col] = "dmp"
        elif col.startswith("DMN_"):
            rename[col] = "dmn"
    return out.rename(columns=rename)[["adx", "dmp", "dmn"]]


def bbands(close: pd.Series, length: int = 20, std: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands → columns ``lower``, ``middle``, ``upper``,
    ``bandwidth``, ``percent``."""
    out = ta.bbands(close, length=length, std=std)
    if out is None:
        raise ValueError(f"bbands(length={length}): pandas-ta returned None")
    rename: dict[str, str] = {}
    for col in out.columns:
        if col.startswith("BBL_"):
            rename[col] = "lower"
        elif col.startswith("BBM_"):
            rename[col] = "middle"
        elif col.startswith("BBU_"):
            rename[col] = "upper"
        elif col.startswith("BBB_"):
            rename[col] = "bandwidth"
        elif col.startswith("BBP_"):
            rename[col] = "percent"
    return out.rename(columns=rename)[["lower", "middle", "upper", "bandwidth", "percent"]]


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 10,
    multiplier: float = 3.0,
) -> pd.DataFrame:
    """Supertrend → columns ``line`` (trend line), ``direction`` (+1/-1),
    ``long`` (line when trend up else NaN), ``short`` (line when trend
    down else NaN)."""
    out = ta.supertrend(high, low, close, length=length, multiplier=multiplier)
    if out is None:
        raise ValueError(f"supertrend(length={length}): pandas-ta returned None")
    rename: dict[str, str] = {}
    for col in out.columns:
        # pandas-ta emits SUPERT_10_3, SUPERTd_10_3, SUPERTl_10_3, SUPERTs_10_3.
        # Prefix match — pandas-ta's multiplier formatting ("3" vs "3.0")
        # varies across versions so never key on the exact suffix.
        if col.startswith("SUPERTd"):
            rename[col] = "direction"
        elif col.startswith("SUPERTl"):
            rename[col] = "long"
        elif col.startswith("SUPERTs"):
            rename[col] = "short"
        elif col.startswith("SUPERT"):  # must come after SUPERTd/l/s
            rename[col] = "line"
    return out.rename(columns=rename)[["line", "direction", "long", "short"]]


# --------------------------------------------------------------------- #
# VWAP — intraday, cumulative, resets each trading day                   #
# --------------------------------------------------------------------- #

def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP with daily reset.

    Requires a tz-aware DatetimeIndex. Typical price = (H + L + C) / 3.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("vwap: df.index must be a DatetimeIndex")
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = typical * df["volume"]
    # Group by calendar date of the index — handles overnight gaps and
    # multi-day DataFrames cleanly.
    day_key = df.index.normalize()
    cum_tpv = tpv.groupby(day_key).cumsum()
    cum_vol = df["volume"].groupby(day_key).cumsum()
    # Guard against zero-volume bars at session open; float-dtype NaN
    # (not pd.NA) keeps the Series numeric for downstream arithmetic.
    cum_vol_safe = cum_vol.where(cum_vol != 0)
    return (cum_tpv / cum_vol_safe).rename("vwap")
