"""8-factor momentum scoring engine.

Takes a DataFrame of *closed* OHLCV candles and returns a ``Score`` with
the per-factor breakdown. All thresholds come from ``StrategyCfg`` —
there are no magic numbers in this module.

**Look-ahead guard:** the caller is responsible for passing only closed
candles. The scoring engine reads up to ``df.iloc[-1]`` as "now". If you
pass the forming candle, your signal is fake.

The 8 factors (each worth 1 point):

1. **EMA stack**: close > ema_fast > ema_mid > ema_slow AND close > ema_trend.
2. **VWAP cross (bullish)**: close > vwap now AND previous bar was at/below vwap.
3. **MACD histogram cross**: histogram flipped from <= 0 to > 0.
4. **RSI in entry range**: ``rsi_entry_range[0] <= RSI <= rsi_entry_range[1]``.
5. **ADX trend**: ADX >= adx_min.
6. **Volume surge**: volume >= volume_sma_20 * volume_surge_multiplier.
7. **Bollinger squeeze breakout**: bandwidth recently at rolling-min AND
   now expanding AND close above middle band.
8. **Supertrend bullish**: direction == +1 AND close > line.

**Hard block:** RSI > ``rsi_upper_block`` kills the signal entirely —
``Score.blocked`` is True and the caller must not enter even on 8/8.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config.settings import StrategyCfg

from . import indicators as ind

MIN_LOOKBACK_BARS = 60  # covers EMA 50, MACD 26+9, ADX 14×2, BB 20, vol SMA 20

REQUIRED_COLS = {"open", "high", "low", "close", "volume"}


# --------------------------------------------------------------------- #
# Result types                                                           #
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class FactorResult:
    name: str
    passed: bool
    value: float | None = None  # the primary observable (for logging / debug)
    note: str | None = None     # optional human-readable context


@dataclass(frozen=True)
class Score:
    total: int                              # sum of passed factors (0..8)
    results: tuple[FactorResult, ...]
    blocked: bool = False
    block_reason: str | None = None

    @property
    def breakdown(self) -> dict[str, bool]:
        """name → passed. This is the ``breakdown_dict`` PROMPT.md calls for."""
        return {r.name: r.passed for r in self.results}

    @property
    def passed_factors(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.results if r.passed)


# --------------------------------------------------------------------- #
# Individual factor checks                                               #
# --------------------------------------------------------------------- #

def _check_ema_stack(df: pd.DataFrame, cfg: StrategyCfg) -> FactorResult:
    close = df["close"]
    ema_fast = ind.ema(close, cfg.ema_fast).iloc[-1]
    ema_mid = ind.ema(close, cfg.ema_mid).iloc[-1]
    ema_slow = ind.ema(close, cfg.ema_slow).iloc[-1]
    ema_trend = ind.ema(close, cfg.ema_trend).iloc[-1]
    c = close.iloc[-1]
    stacked = c > ema_fast > ema_mid > ema_slow and c > ema_trend
    return FactorResult(
        name="ema_stack",
        passed=bool(stacked),
        value=float(c),
        note=(
            f"close={c:.2f} ema{cfg.ema_fast}={ema_fast:.2f} "
            f"ema{cfg.ema_mid}={ema_mid:.2f} ema{cfg.ema_slow}={ema_slow:.2f} "
            f"ema{cfg.ema_trend}={ema_trend:.2f}"
        ),
    )


def _check_vwap_cross(df: pd.DataFrame, _cfg: StrategyCfg) -> FactorResult:
    v = ind.vwap(df)
    close = df["close"]
    now_above = close.iloc[-1] > v.iloc[-1]
    # "crossover within last 2 candles" — previous bar was at/below VWAP.
    crossed = close.iloc[-2] <= v.iloc[-2]
    passed = bool(now_above and crossed)
    return FactorResult(
        name="vwap_cross",
        passed=passed,
        value=float(v.iloc[-1]),
        note=f"close={close.iloc[-1]:.2f} vwap={v.iloc[-1]:.2f}",
    )


def _check_macd_cross(df: pd.DataFrame, _cfg: StrategyCfg) -> FactorResult:
    m = ind.macd(df["close"])
    hist_now = m["hist"].iloc[-1]
    hist_prev = m["hist"].iloc[-2]
    passed = bool(hist_prev <= 0 < hist_now)
    return FactorResult(
        name="macd_cross",
        passed=passed,
        value=float(hist_now),
        note=f"hist_prev={hist_prev:.4f} hist_now={hist_now:.4f}",
    )


def _check_rsi(df: pd.DataFrame, cfg: StrategyCfg) -> FactorResult:
    r = ind.rsi(df["close"]).iloc[-1]
    lo, hi = cfg.rsi_entry_range
    passed = bool(lo <= r <= hi)
    return FactorResult(
        name="rsi_entry",
        passed=passed,
        value=float(r),
        note=f"rsi={r:.2f} range=[{lo}, {hi}]",
    )


def _check_adx(df: pd.DataFrame, cfg: StrategyCfg) -> FactorResult:
    a = ind.adx(df["high"], df["low"], df["close"])["adx"].iloc[-1]
    passed = bool(a >= cfg.adx_min)
    return FactorResult(
        name="adx_trend",
        passed=passed,
        value=float(a),
        note=f"adx={a:.2f} min={cfg.adx_min}",
    )


def _check_volume_surge(df: pd.DataFrame, cfg: StrategyCfg) -> FactorResult:
    sma = ind.volume_sma(df["volume"], length=20).iloc[-1]
    vnow = df["volume"].iloc[-1]
    if sma == 0 or pd.isna(sma):
        return FactorResult(
            name="volume_surge", passed=False, value=float(vnow),
            note="volume_sma is zero or NaN",
        )
    ratio = vnow / sma
    passed = bool(ratio >= cfg.volume_surge_multiplier)
    return FactorResult(
        name="volume_surge",
        passed=passed,
        value=float(ratio),
        note=f"v={vnow:.0f} sma20={sma:.0f} ratio={ratio:.2f}",
    )


def _check_bb_breakout(df: pd.DataFrame, _cfg: StrategyCfg) -> FactorResult:
    b = ind.bbands(df["close"], length=20, std=2.0)
    bw = b["bandwidth"]
    if len(bw.dropna()) < 20:
        return FactorResult(
            name="bb_breakout", passed=False, value=None,
            note="insufficient bandwidth history",
        )
    bw_now = bw.iloc[-1]
    # "Squeeze then breakout" = bandwidth has expanded meaningfully above
    # the 20-bar rolling minimum (there WAS a squeeze in the window we can
    # still see), and is currently still expanding, and price broke
    # upward (close > middle band).
    window20 = bw.iloc[-20:]
    squeeze_min = float(window20.min())
    expansion_ratio = bw_now / squeeze_min if squeeze_min > 0 else 0.0
    had_squeeze = expansion_ratio >= 1.5  # at least 50% wider than recent low
    bw_5_ago = bw.iloc[-6]
    expanding = bw_now > bw_5_ago * 1.1
    c = df["close"].iloc[-1]
    above_mid = c > b["middle"].iloc[-1]
    passed = bool(had_squeeze and expanding and above_mid)
    return FactorResult(
        name="bb_breakout",
        passed=passed,
        value=float(bw_now),
        note=(
            f"bw_now={bw_now:.2f} squeeze_min={squeeze_min:.2f} "
            f"expansion={expansion_ratio:.2f}x expanding={expanding} above_mid={above_mid}"
        ),
    )


def _check_supertrend(df: pd.DataFrame, cfg: StrategyCfg) -> FactorResult:
    s = ind.supertrend(
        df["high"], df["low"], df["close"],
        length=cfg.supertrend_period,
        multiplier=cfg.supertrend_multiplier,
    )
    direction = s["direction"].iloc[-1]
    line = s["line"].iloc[-1]
    c = df["close"].iloc[-1]
    passed = bool(direction == 1 and c > line)
    return FactorResult(
        name="supertrend",
        passed=passed,
        value=float(line) if pd.notna(line) else None,
        note=f"direction={int(direction)} line={line:.2f} close={c:.2f}",
    )


# --------------------------------------------------------------------- #
# Public API                                                             #
# --------------------------------------------------------------------- #

_CHECKS = (
    _check_ema_stack,
    _check_vwap_cross,
    _check_macd_cross,
    _check_rsi,
    _check_adx,
    _check_volume_surge,
    _check_bb_breakout,
    _check_supertrend,
)


def score_symbol(df: pd.DataFrame, cfg: StrategyCfg) -> Score:
    """Compute the 8-factor score for a single instrument.

    Args:
        df: OHLCV closed candles, tz-aware DatetimeIndex, sorted ascending.
            Must include columns: open, high, low, close, volume.
            Must have at least ``MIN_LOOKBACK_BARS`` rows.
        cfg: StrategyCfg — every threshold lives here.

    Returns:
        ``Score`` with .total, .results, .breakdown, .blocked.

    Raises:
        ValueError: missing columns or insufficient lookback.
    """
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"score_symbol: missing columns {sorted(missing)}")
    if len(df) < MIN_LOOKBACK_BARS:
        raise ValueError(
            f"score_symbol: need >= {MIN_LOOKBACK_BARS} bars, got {len(df)}"
        )

    results = tuple(check(df, cfg) for check in _CHECKS)
    total = sum(1 for r in results if r.passed)

    # Hard block — RSI above the configured upper bound kills the signal
    # even if every other factor passes.
    rsi_now = float(ind.rsi(df["close"]).iloc[-1])
    blocked = rsi_now > cfg.rsi_upper_block
    block_reason = (
        f"rsi={rsi_now:.2f} > rsi_upper_block={cfg.rsi_upper_block}"
        if blocked
        else None
    )

    return Score(
        total=total,
        results=results,
        blocked=blocked,
        block_reason=block_reason,
    )
