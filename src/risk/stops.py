"""Stop-loss, take-profit, trailing stop, and time-stop logic.

ATR-driven throughout — all stop distances are multiples of ATR so they
scale with realised volatility, per the Reddit momentum playbook plus
the Indian-market adjustments documented in PROMPT.md.

Volatility regime for the trailing multiplier is decided by where the
current ATR sits in the distribution of the last ~50 ATR readings:
high-volatility regime → *tighter* trail (the high-vol multiplier in
cfg is the smaller of the two); low-volatility regime → wider trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from brokers.base import Position, Side
from config.settings import RiskCfg


# ---------------------------------------------------------------- #
# Initial stop + take-profit                                        #
# ---------------------------------------------------------------- #

def atr_stop_price(
    entry: float, atr: float, multiplier: float, side: Side
) -> float:
    """Initial protective stop.

    BUY  → entry − multiplier × atr (stop sits below entry).
    SELL → entry + multiplier × atr (stop sits above entry).
    """
    if atr <= 0:
        raise ValueError(f"atr must be positive, got {atr}")
    if multiplier <= 0:
        raise ValueError(f"multiplier must be positive, got {multiplier}")
    return entry - multiplier * atr if side == Side.BUY else entry + multiplier * atr


def take_profit_price(
    entry: float, atr: float, multiplier: float, side: Side
) -> float:
    """Take-profit at entry ± multiplier × atr on the trade's direction."""
    if atr <= 0:
        raise ValueError(f"atr must be positive, got {atr}")
    if multiplier <= 0:
        raise ValueError(f"multiplier must be positive, got {multiplier}")
    return entry + multiplier * atr if side == Side.BUY else entry - multiplier * atr


# ---------------------------------------------------------------- #
# Trailing stop                                                     #
# ---------------------------------------------------------------- #

def trailing_multiplier(atr_series: pd.Series, cfg: RiskCfg) -> float:
    """Pick the trailing multiplier based on the current volatility regime.

    High-vol regime (ATR above the last-50 median) → tighter trail.
    Low-vol regime (ATR below the median)          → wider trail.

    Falls back to the low-vol multiplier when we don't yet have 50 bars
    of ATR history — the conservative choice.
    """
    clean = atr_series.dropna()
    if len(clean) < 50:
        return cfg.trailing_atr_multiplier_low_vol
    window = clean.iloc[-50:]
    median = float(np.median(window.values))
    current = float(clean.iloc[-1])
    if current >= median:
        return cfg.trailing_atr_multiplier_high_vol
    return cfg.trailing_atr_multiplier_low_vol


def update_trail_stop(
    position: Position,
    current_price: float,
    atr: float,
    multiplier: float,
) -> float:
    """Return the new trailing stop. Only ratchets in the favourable
    direction — never loosens an existing stop.

    Long:  candidate = current_price − multiplier × atr; new = max(existing, candidate).
    Short: candidate = current_price + multiplier × atr; new = min(existing, candidate).
    """
    if atr <= 0:
        raise ValueError(f"atr must be positive, got {atr}")

    side = Side.BUY if position.qty > 0 else Side.SELL
    if side == Side.BUY:
        candidate = current_price - multiplier * atr
        existing = position.trail_stop if position.trail_stop is not None else position.stop_loss
        return max(existing, candidate) if existing is not None else candidate
    # SELL / short
    candidate = current_price + multiplier * atr
    existing = position.trail_stop if position.trail_stop is not None else position.stop_loss
    return min(existing, candidate) if existing is not None else candidate


# ---------------------------------------------------------------- #
# Time stop                                                         #
# ---------------------------------------------------------------- #

@dataclass(frozen=True)
class TimeStopDecision:
    close_now: bool
    age_minutes: float
    price_move: float
    atr: float
    reason: str | None = None


def check_time_stop(
    position: Position,
    current_price: float,
    atr: float,
    now: datetime,
    cfg: RiskCfg,
) -> TimeStopDecision:
    """Close a dead position: age > time_stop_minutes AND |move| ≤ 0.5 × ATR.

    "Move" is measured against the position's entry price. Half an ATR
    is the PROMPT-specified deadband — tight enough to exit stalled
    trades, loose enough to let normal mean-reverting noise pass.
    """
    if position.opened_at is None:
        return TimeStopDecision(False, 0.0, 0.0, atr, "no opened_at timestamp")

    age = now - position.opened_at
    age_minutes = age.total_seconds() / 60.0
    move = abs(current_price - position.avg_price)

    if age_minutes < cfg.time_stop_minutes:
        return TimeStopDecision(
            False, age_minutes, move, atr,
            f"age {age_minutes:.0f}m < {cfg.time_stop_minutes}m threshold",
        )
    deadband = 0.5 * atr
    if move > deadband:
        return TimeStopDecision(
            False, age_minutes, move, atr,
            f"move {move:.2f} > deadband {deadband:.2f}",
        )
    return TimeStopDecision(
        True, age_minutes, move, atr,
        f"aged out after {age_minutes:.0f}m with move {move:.2f} ≤ {deadband:.2f}",
    )


def minutes_since(ts: datetime, now: datetime) -> float:
    """Utility — minutes between two tz-aware timestamps."""
    if ts.tzinfo is None or now.tzinfo is None:
        raise ValueError("minutes_since requires tz-aware timestamps")
    return (now - ts).total_seconds() / 60.0