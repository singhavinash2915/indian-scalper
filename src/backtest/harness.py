"""Backtest harness — drives the scan loop over historical candles.

The harness iterates the union of all candle timestamps across the
backtest universe, sets a ``BacktestCandleFetcher`` to "only reveal
candles ≤ now", and calls ``run_tick(ctx, ts)`` for each bar. Because
we reuse the real scan-loop pipeline, strategy + risk code paths in a
backtest are byte-identical to production — no parallel "backtest
engine" that drifts out of sync.

At the end, ``BacktestResult.metrics`` reports Sharpe, Max DD, win
rate, avg R:R, and the total return.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from backtest.metrics import (
    compute_avg_holding_minutes,
    compute_avg_rr,
    compute_max_drawdown,
    compute_sharpe,
    compute_total_pnl,
    compute_win_rate,
)
from backtest.trades import Trade, extract_trades
from brokers.base import Candle
from brokers.paper import PaperBroker
from data.market_data import FakeCandleFetcher
from scheduler.scan_loop import ScanContext, TickReport, run_tick


# --------------------------------------------------------------------- #
# Config + result                                                       #
# --------------------------------------------------------------------- #

@dataclass
class BacktestConfig:
    """How the backtest should run. Separate from ``Settings`` because
    these are runtime harness knobs, not strategy config."""

    bars_per_year: int = 252  # for Sharpe annualisation
    stop_at_ts: datetime | None = None  # truncate the run early


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[dict[str, Any]]
    tick_reports: list[TickReport]
    metrics: dict[str, float]
    final_equity: float
    starting_equity: float
    timestamps_processed: int = 0
    ticks_skipped: int = 0

    @property
    def total_return_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.final_equity - self.starting_equity) / self.starting_equity * 100.0

    def summary(self) -> str:
        m = self.metrics
        return (
            "Backtest summary\n"
            f"  bars processed : {self.timestamps_processed}\n"
            f"  ticks skipped  : {self.ticks_skipped}\n"
            f"  trades         : {len(self.trades)}\n"
            f"  starting equity: ₹{self.starting_equity:,.2f}\n"
            f"  final equity   : ₹{self.final_equity:,.2f}\n"
            f"  total return   : {self.total_return_pct:+.2f}%\n"
            f"  sharpe (annual): {m.get('sharpe', float('nan')):.2f}\n"
            f"  max drawdown   : {m.get('max_dd_pct', 0.0):.2f}%\n"
            f"  win rate       : {m.get('win_rate', 0.0):.1f}%\n"
            f"  avg R:R        : {m.get('avg_rr', float('nan')):.2f}\n"
            f"  avg hold (min) : {m.get('avg_holding_minutes', 0.0):.1f}\n"
            f"  total trade P&L: ₹{m.get('total_trade_pnl', 0.0):,.2f}\n"
        )


# --------------------------------------------------------------------- #
# BacktestCandleFetcher                                                 #
# --------------------------------------------------------------------- #

class BacktestCandleFetcher(FakeCandleFetcher):
    """FakeCandleFetcher variant that enforces a "simulated now".

    ``set_now(ts)`` sets a cutoff; ``get_candles`` returns only bars at
    or before that cutoff. This keeps the scan loop from peeking into
    the future during a historical replay.
    """

    def __init__(self, series: dict[str, list[Candle]] | None = None) -> None:
        super().__init__(series)
        self._now: datetime | None = None

    def set_now(self, ts: datetime) -> None:
        self._now = ts

    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]:
        if symbol not in self._series:
            raise KeyError(f"BacktestCandleFetcher: no candles seeded for {symbol!r}")
        series = self._series[symbol]
        if self._now is not None:
            series = [c for c in series if c.ts <= self._now]
        return series[-lookback:] if lookback > 0 else list(series)


# --------------------------------------------------------------------- #
# The harness                                                           #
# --------------------------------------------------------------------- #

class BacktestHarness:
    """Drives a full scan-loop replay over a candle dataset.

    The broker passed in MUST already have its ``candle_fetcher`` wired
    to the ``BacktestCandleFetcher`` returned from ``prepare_fetcher``,
    or equivalent. The harness advances that fetcher's simulated "now"
    at each bar.
    """

    def __init__(
        self,
        ctx: ScanContext,
        fetcher: BacktestCandleFetcher,
    ) -> None:
        self.ctx = ctx
        self.fetcher = fetcher

    @staticmethod
    def prepare_fetcher(series: dict[str, list[Candle]]) -> BacktestCandleFetcher:
        """Convenience factory — callers pass the output to PaperBroker."""
        return BacktestCandleFetcher(series)

    def run(self, cfg: BacktestConfig | None = None) -> BacktestResult:
        cfg = cfg or BacktestConfig()
        starting_equity = self.ctx.broker.get_funds()["equity"]
        tick_reports: list[TickReport] = []

        timestamps = _collect_timestamps(self.fetcher, cfg.stop_at_ts)
        if not timestamps:
            logger.warning("Backtest has no bars to process")
            return _empty_result(self.ctx.broker, starting_equity)

        logger.info(
            "Backtest starting | bars={} symbols={} range={}..{}",
            len(timestamps),
            len(self.fetcher._series),  # pyright: ignore[reportPrivateUsage]
            timestamps[0], timestamps[-1],
        )

        skipped = 0
        for ts in timestamps:
            self.fetcher.set_now(ts)
            report = run_tick(self.ctx, ts)
            tick_reports.append(report)
            if report.skipped_reason is not None:
                skipped += 1

        # Collect results.
        orders = self.ctx.broker.store.load_orders()
        trades = extract_trades(orders)
        equity_curve = self.ctx.broker.store.load_equity_curve()
        final_equity = self.ctx.broker.get_funds()["equity"]

        metrics = {
            "sharpe": compute_sharpe(equity_curve, bars_per_year=cfg.bars_per_year),
            "max_dd_pct": compute_max_drawdown(equity_curve)["max_dd_pct"],
            "win_rate": compute_win_rate(trades),
            "avg_rr": compute_avg_rr(trades),
            "avg_holding_minutes": compute_avg_holding_minutes(trades),
            "total_trade_pnl": compute_total_pnl(trades),
        }

        logger.info(
            "Backtest done | trades={} total_return={:+.2f}% sharpe={:.2f} max_dd={:.2f}%",
            len(trades),
            (final_equity - starting_equity) / starting_equity * 100.0
            if starting_equity else 0.0,
            metrics["sharpe"] if metrics["sharpe"] == metrics["sharpe"] else 0.0,
            metrics["max_dd_pct"],
        )

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            tick_reports=tick_reports,
            metrics=metrics,
            final_equity=final_equity,
            starting_equity=starting_equity,
            timestamps_processed=len(timestamps),
            ticks_skipped=skipped,
        )


# --------------------------------------------------------------------- #
# Internals                                                             #
# --------------------------------------------------------------------- #

def _collect_timestamps(
    fetcher: BacktestCandleFetcher, stop_at: datetime | None,
) -> list[datetime]:
    """Union + sort all bar timestamps across the seeded series."""
    all_ts: set[datetime] = set()
    for series in fetcher._series.values():  # pyright: ignore[reportPrivateUsage]
        for c in series:
            all_ts.add(c.ts)
    ordered = sorted(all_ts)
    if stop_at is not None:
        ordered = [t for t in ordered if t <= stop_at]
    return ordered


def _empty_result(broker: PaperBroker, starting_equity: float) -> BacktestResult:
    return BacktestResult(
        trades=[],
        equity_curve=[],
        tick_reports=[],
        metrics={
            "sharpe": float("nan"),
            "max_dd_pct": 0.0,
            "win_rate": 0.0,
            "avg_rr": float("nan"),
            "avg_holding_minutes": 0.0,
            "total_trade_pnl": 0.0,
        },
        final_equity=broker.get_funds()["equity"],
        starting_equity=starting_equity,
    )
