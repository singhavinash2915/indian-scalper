"""Backtest performance metrics.

All functions are pure and tolerant of empty inputs — zero trades, a
single equity-curve row, etc. — returning NaN or 0.0 instead of
raising. Callers can check ``result.metrics`` without defensive
wrapping everywhere.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from backtest.trades import Trade


# --------------------------------------------------------------------- #
# Sharpe                                                                 #
# --------------------------------------------------------------------- #

def compute_sharpe(
    equity_curve: list[dict],
    bars_per_year: int = 252,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualised Sharpe from bar-to-bar equity returns.

    Args:
        equity_curve: List of dicts with an ``equity`` field (ordered
            by time; harness writes them that way).
        bars_per_year: How many equity snapshots make a year.
            * Daily  = 252
            * 15-min = 252 × 25 = 6300
            * 1-min  = 252 × 375 = 94_500
        risk_free_rate: Annualised. Default 0 — we're measuring alpha
            over simply-not-trading.

    Returns NaN if the curve is too short to compute a std-dev.
    """
    if len(equity_curve) < 2:
        return float("nan")

    equities = [float(row["equity"]) for row in equity_curve]
    # Simple returns per bar.
    returns = [
        (equities[i] - equities[i - 1]) / equities[i - 1]
        for i in range(1, len(equities))
        if equities[i - 1] != 0
    ]
    if len(returns) < 2:
        return float("nan")

    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return float("nan")

    excess = mean - risk_free_rate / bars_per_year
    return (excess / std) * math.sqrt(bars_per_year)


# --------------------------------------------------------------------- #
# Max drawdown                                                           #
# --------------------------------------------------------------------- #

def compute_max_drawdown(equity_curve: list[dict]) -> dict:
    """Peak-to-trough drawdown over the curve.

    Returns {'max_dd_pct', 'peak_ts', 'trough_ts', 'peak_equity',
    'trough_equity'}. All zero-ish for empty / single-row curves.
    """
    empty = {
        "max_dd_pct": 0.0,
        "peak_ts": None,
        "trough_ts": None,
        "peak_equity": 0.0,
        "trough_equity": 0.0,
    }
    if not equity_curve:
        return empty

    peak = float("-inf")
    peak_ts = None
    max_dd = 0.0
    best = dict(empty)

    for row in equity_curve:
        eq = float(row["equity"])
        ts = row.get("ts")
        if eq > peak:
            peak = eq
            peak_ts = ts
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
                best = {
                    "max_dd_pct": dd,
                    "peak_ts": peak_ts,
                    "trough_ts": ts,
                    "peak_equity": peak,
                    "trough_equity": eq,
                }
    return best


# --------------------------------------------------------------------- #
# Win rate + avg R:R                                                     #
# --------------------------------------------------------------------- #

def compute_win_rate(trades: Iterable[Trade]) -> float:
    trades = list(trades)
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.is_winner)
    return wins / len(trades) * 100.0


def compute_avg_rr(trades: Iterable[Trade]) -> float:
    """Average reward-to-risk ratio = |avg_win| / |avg_loss|.

    A realised-outcome R:R, not planned-stop R:R. Returns NaN when
    we have no wins or no losses (ratio is undefined)."""
    trades = list(trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    if not wins or not losses:
        return float("nan")
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    if avg_loss == 0:
        return float("nan")
    return abs(avg_win / avg_loss)


def compute_total_pnl(trades: Iterable[Trade]) -> float:
    return sum(t.pnl for t in trades)


def compute_avg_holding_minutes(trades: Iterable[Trade]) -> float:
    trades = list(trades)
    if not trades:
        return 0.0
    return sum(t.holding_minutes for t in trades) / len(trades)
