"""Backtest harness + dry-run runner.

Both replay historical candles through the same ``run_tick`` used by
the live scan loop. Strategy + risk code paths in a backtest are
byte-identical to production — no parallel engine.
"""

from backtest.dry_run import run_dry_run
from backtest.harness import (
    BacktestCandleFetcher,
    BacktestConfig,
    BacktestHarness,
    BacktestResult,
)
from backtest.metrics import (
    compute_avg_holding_minutes,
    compute_avg_rr,
    compute_max_drawdown,
    compute_sharpe,
    compute_total_pnl,
    compute_win_rate,
)
from backtest.trades import Trade, extract_trades

__all__ = [
    "BacktestCandleFetcher",
    "BacktestConfig",
    "BacktestHarness",
    "BacktestResult",
    "Trade",
    "compute_avg_holding_minutes",
    "compute_avg_rr",
    "compute_max_drawdown",
    "compute_sharpe",
    "compute_total_pnl",
    "compute_win_rate",
    "extract_trades",
    "run_dry_run",
]
