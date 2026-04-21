"""Backtest metrics — Sharpe, MaxDD, win rate, avg R:R."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backtest.metrics import (
    compute_avg_holding_minutes,
    compute_avg_rr,
    compute_max_drawdown,
    compute_sharpe,
    compute_total_pnl,
    compute_win_rate,
)
from backtest.trades import Trade
from brokers.base import Side

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


def _trade(pnl: float, minutes: int = 30) -> Trade:
    entry = 1000.0
    exit = entry + pnl / 10  # 10 units
    return Trade(
        symbol="X",
        entry_ts=T0,
        exit_ts=T0 + timedelta(minutes=minutes),
        entry_price=entry,
        exit_price=exit,
        qty=10,
        side=Side.BUY,
        pnl=pnl,
        pnl_pct=(exit - entry) / entry * 100,
    )


def _curve(*equities: float) -> list[dict]:
    return [
        {"ts": (T0 + timedelta(minutes=i)).isoformat(), "equity": eq, "cash": 0, "pnl": 0}
        for i, eq in enumerate(equities)
    ]


# ---------------------------------------------------------------- #
# Sharpe                                                            #
# ---------------------------------------------------------------- #

def test_sharpe_nan_on_empty_curve() -> None:
    assert math.isnan(compute_sharpe([]))


def test_sharpe_nan_on_single_point() -> None:
    assert math.isnan(compute_sharpe(_curve(500_000)))


def test_sharpe_positive_on_steady_growth() -> None:
    # 1% per bar with some noise → positive Sharpe.
    vals = [500_000]
    for i in range(1, 21):
        noise = 0.002 * (1 if i % 3 else -1)  # tiny wobble
        vals.append(vals[-1] * (1 + 0.01 + noise))
    s = compute_sharpe(_curve(*vals), bars_per_year=252)
    assert s > 0
    assert math.isfinite(s)


def test_sharpe_negative_on_steady_decline() -> None:
    vals = [500_000]
    for i in range(1, 21):
        noise = 0.002 * (1 if i % 3 else -1)
        vals.append(vals[-1] * (1 - 0.01 + noise))
    s = compute_sharpe(_curve(*vals), bars_per_year=252)
    assert s < 0


def test_sharpe_nan_on_flat_curve() -> None:
    # Zero-variance returns → undefined Sharpe.
    s = compute_sharpe(_curve(*([500_000] * 10)))
    assert math.isnan(s)


# ---------------------------------------------------------------- #
# Max drawdown                                                      #
# ---------------------------------------------------------------- #

def test_max_drawdown_zero_on_monotone_rise() -> None:
    res = compute_max_drawdown(_curve(500_000, 510_000, 520_000, 530_000))
    assert res["max_dd_pct"] == 0.0


def test_max_drawdown_computes_peak_to_trough() -> None:
    # Peak 600k, trough 450k → 25% drawdown.
    res = compute_max_drawdown(_curve(500_000, 600_000, 550_000, 450_000, 480_000))
    assert res["max_dd_pct"] == 25.0
    assert res["peak_equity"] == 600_000
    assert res["trough_equity"] == 450_000


def test_max_drawdown_empty_returns_zero() -> None:
    res = compute_max_drawdown([])
    assert res["max_dd_pct"] == 0.0
    assert res["peak_ts"] is None


# ---------------------------------------------------------------- #
# Win rate                                                          #
# ---------------------------------------------------------------- #

def test_win_rate_all_winners() -> None:
    assert compute_win_rate([_trade(100), _trade(50)]) == 100.0


def test_win_rate_mixed() -> None:
    assert compute_win_rate([_trade(100), _trade(-50), _trade(25), _trade(-25)]) == 50.0


def test_win_rate_empty_is_zero() -> None:
    assert compute_win_rate([]) == 0.0


# ---------------------------------------------------------------- #
# Avg R:R                                                           #
# ---------------------------------------------------------------- #

def test_avg_rr_classic_case() -> None:
    # Wins avg 100, losses avg -50 → R:R = 2.0.
    trades = [_trade(100), _trade(100), _trade(-50), _trade(-50)]
    assert compute_avg_rr(trades) == 2.0


def test_avg_rr_nan_when_no_losses() -> None:
    assert math.isnan(compute_avg_rr([_trade(100), _trade(50)]))


def test_avg_rr_nan_when_no_wins() -> None:
    assert math.isnan(compute_avg_rr([_trade(-100), _trade(-50)]))


# ---------------------------------------------------------------- #
# Aggregates                                                        #
# ---------------------------------------------------------------- #

def test_total_pnl() -> None:
    assert compute_total_pnl([_trade(100), _trade(-40), _trade(25)]) == 85


def test_avg_holding_minutes() -> None:
    trades = [_trade(100, minutes=10), _trade(50, minutes=30), _trade(-20, minutes=20)]
    assert compute_avg_holding_minutes(trades) == 20.0
