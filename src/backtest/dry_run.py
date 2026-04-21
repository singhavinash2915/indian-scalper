"""Dry-run mode — play a saved day through the scan loop at Nx wall speed.

PROMPT.md: "tick through a saved day of candles at 10× speed to validate
end-to-end before paper-trading live". The point is to watch logs and
dashboard behaviour in near-real-time without waiting for a live session.

Under the hood: same ``BacktestHarness`` loop as a historical backtest,
plus a calibrated ``time.sleep`` between ticks derived from the bar
interval and the user's speed multiplier.
"""

from __future__ import annotations

import time

from loguru import logger

from backtest.harness import (
    BacktestCandleFetcher,
    BacktestConfig,
    BacktestHarness,
    BacktestResult,
    _collect_timestamps,
)
from backtest.metrics import (
    compute_avg_holding_minutes,
    compute_avg_rr,
    compute_max_drawdown,
    compute_sharpe,
    compute_total_pnl,
    compute_win_rate,
)
from backtest.trades import extract_trades
from scheduler.scan_loop import ScanContext, TickReport, run_tick

# Parse common interval strings to real-world seconds between bars.
_INTERVAL_SECONDS = {
    "1m": 60,
    "2m": 120,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "60m": 3600,
    "1h": 3600,
    "1d": 86400,
    "daily": 86400,
}


def run_dry_run(
    ctx: ScanContext,
    fetcher: BacktestCandleFetcher,
    speed_multiplier: float = 10.0,
    cfg: BacktestConfig | None = None,
    sleep_fn=time.sleep,
) -> BacktestResult:
    """Replay the seeded candle series through ``run_tick`` with
    wall-clock sleeps proportional to the bar interval.

    Args:
        ctx: Scan context (shares everything run_tick needs).
        fetcher: Already seeded + wired into ``ctx.broker``.
        speed_multiplier: 10× means a 15-min bar advances every 90s.
        cfg: BacktestConfig (only ``stop_at_ts`` is honoured here).
        sleep_fn: Injectable for tests — default ``time.sleep``. A
            test passes a no-op or a counter to keep runtime bounded.

    Returns a ``BacktestResult`` shaped identically to a historical
    backtest, so the same summary() can be logged.
    """
    cfg = cfg or BacktestConfig()
    if speed_multiplier <= 0:
        raise ValueError(f"speed_multiplier must be > 0, got {speed_multiplier}")

    interval_key = ctx.settings.strategy.candle_interval
    bar_seconds = _INTERVAL_SECONDS.get(interval_key)
    if bar_seconds is None:
        raise ValueError(
            f"dry_run: unsupported candle_interval {interval_key!r}. "
            f"Known: {sorted(_INTERVAL_SECONDS)}"
        )
    sleep_per_bar = bar_seconds / speed_multiplier

    timestamps = _collect_timestamps(fetcher, cfg.stop_at_ts)
    if not timestamps:
        logger.warning("Dry run has no bars to process")

    starting_equity = ctx.broker.get_funds()["equity"]
    tick_reports: list[TickReport] = []
    skipped = 0

    logger.info(
        "Dry run starting | bars={} interval={} speed={}x sleep_per_bar={:.2f}s",
        len(timestamps), interval_key, speed_multiplier, sleep_per_bar,
    )

    for i, ts in enumerate(timestamps):
        fetcher.set_now(ts)
        report = run_tick(ctx, ts)
        tick_reports.append(report)
        if report.skipped_reason is not None:
            skipped += 1
        # Sleep after every bar except the last so the run terminates
        # predictably.
        if i < len(timestamps) - 1 and sleep_per_bar > 0:
            sleep_fn(sleep_per_bar)

    # Same result-building as a backtest — trade extraction + metrics.
    orders = ctx.broker.store.load_orders()
    trades = extract_trades(orders)
    equity_curve = ctx.broker.store.load_equity_curve()
    final_equity = ctx.broker.get_funds()["equity"]
    metrics = {
        "sharpe": compute_sharpe(equity_curve, bars_per_year=cfg.bars_per_year),
        "max_dd_pct": compute_max_drawdown(equity_curve)["max_dd_pct"],
        "win_rate": compute_win_rate(trades),
        "avg_rr": compute_avg_rr(trades),
        "avg_holding_minutes": compute_avg_holding_minutes(trades),
        "total_trade_pnl": compute_total_pnl(trades),
    }

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


# Silence unused-import (BacktestHarness exported for ergonomic re-use).
_ = BacktestHarness
