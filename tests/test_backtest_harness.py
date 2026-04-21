"""End-to-end backtest + dry-run. Drives the full scan loop over a
synthetic bullish series and asserts on trades, metrics, and the
future-masking contract of BacktestCandleFetcher."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backtest.dry_run import run_dry_run
from backtest.harness import (
    BacktestCandleFetcher,
    BacktestConfig,
    BacktestHarness,
)
from brokers.paper import PaperBroker
from config.settings import Settings
from data.instruments import InstrumentMaster
from data.market_data import df_to_candles
from tests.fixtures import paper_mode, running_scheduler
from scheduler.scan_loop import ScanContext
from tests.fixtures.synthetic import bullish_breakout_df

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------- #
# Shared fixtures                                                        #
# --------------------------------------------------------------------- #

def _build(tmp_path: Path, min_score: int = 4) -> tuple[ScanContext, BacktestCandleFetcher]:
    settings = paper_mode(Settings.from_template())
    settings.strategy.min_score = min_score
    # Shorter time-stop so the harness test can exit a stalled position
    # before the short candle series runs out.
    settings.risk.time_stop_minutes = 20

    candles = df_to_candles(bullish_breakout_df())
    fetcher = BacktestCandleFetcher({"RELIANCE": candles})

    instruments = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    broker = PaperBroker(
        settings,
        db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    running_scheduler(broker)  # D11 Slice 1 — see paper-mode note in _build_ctx.
    ctx = ScanContext(
        settings=settings, broker=broker, universe=["RELIANCE"],
        instruments=instruments,
    )
    return ctx, fetcher


# --------------------------------------------------------------------- #
# BacktestCandleFetcher contract                                         #
# --------------------------------------------------------------------- #

def test_fetcher_masks_future_candles() -> None:
    candles = df_to_candles(bullish_breakout_df())
    fetcher = BacktestCandleFetcher({"RELIANCE": candles})

    # Before set_now, returns the full series (tail-lookback).
    assert fetcher.get_candles("RELIANCE", "1m", 5)[-1].ts == candles[-1].ts

    # After set_now, never returns beyond the cutoff.
    cutoff = candles[50].ts
    fetcher.set_now(cutoff)
    out = fetcher.get_candles("RELIANCE", "1m", 200)
    assert all(c.ts <= cutoff for c in out)
    assert out[-1].ts == cutoff


def test_fetcher_raises_on_unseeded_symbol() -> None:
    fetcher = BacktestCandleFetcher({"RELIANCE": []})
    with pytest.raises(KeyError):
        fetcher.get_candles("UNKNOWN", "1m", 10)


# --------------------------------------------------------------------- #
# Harness run end-to-end                                                 #
# --------------------------------------------------------------------- #

def test_harness_produces_result_with_expected_fields(tmp_path: Path) -> None:
    ctx, fetcher = _build(tmp_path)
    harness = BacktestHarness(ctx, fetcher)

    result = harness.run(BacktestConfig(bars_per_year=252 * 375))  # 1-min bars

    assert result.timestamps_processed == 120   # bullish_breakout_df default
    assert result.starting_equity == ctx.settings.capital.starting_inr
    assert set(result.metrics.keys()) == {
        "sharpe", "max_dd_pct", "win_rate", "avg_rr",
        "avg_holding_minutes", "total_trade_pnl",
    }
    # Equity curve got at least one snapshot.
    assert len(result.equity_curve) >= 1


def test_harness_closes_trade_on_bullish_fixture(tmp_path: Path) -> None:
    """With min_score=4 + a short time-stop, the harness should open at
    least one entry AND close it before the series ends — producing a
    fully-formed trade row."""
    ctx, fetcher = _build(tmp_path)
    result = BacktestHarness(ctx, fetcher).run()
    assert len(result.trades) >= 1, f"expected ≥1 closed trade, got 0. {result.summary()}"
    t = result.trades[0]
    assert t.symbol == "RELIANCE"
    assert t.qty > 0
    assert t.holding_minutes >= 1


def test_harness_skips_bars_outside_session(tmp_path: Path) -> None:
    """The default 2026-04-21 09:15 start is a Tuesday inside session —
    nothing should be skipped for market-hours reasons. If we shift the
    series into a Saturday, every tick should skip with market_closed."""
    settings = Settings.from_template()
    candles_df = bullish_breakout_df()
    # Shift timestamps to a Saturday.
    candles_df.index = candles_df.index + (
        datetime(2026, 4, 18, tzinfo=IST) - datetime(2026, 4, 21, tzinfo=IST)
    )
    candles = df_to_candles(candles_df)
    fetcher = BacktestCandleFetcher({"RELIANCE": candles})

    instruments = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    broker = PaperBroker(
        settings,
        db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    ctx = ScanContext(
        settings=settings, broker=broker, universe=["RELIANCE"],
        instruments=instruments,
    )

    result = BacktestHarness(ctx, fetcher).run()
    assert result.ticks_skipped == result.timestamps_processed
    # No trades possible when every tick is skipped.
    assert result.trades == []


def test_harness_empty_series_is_safe(tmp_path: Path) -> None:
    settings = Settings.from_template()
    fetcher = BacktestCandleFetcher({"RELIANCE": []})
    instruments = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    broker = PaperBroker(
        settings, db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=fetcher, instruments=instruments,
    )
    ctx = ScanContext(
        settings=settings, broker=broker, universe=["RELIANCE"],
        instruments=instruments,
    )
    result = BacktestHarness(ctx, fetcher).run()
    assert result.timestamps_processed == 0
    assert result.trades == []


def test_result_summary_renders_without_errors(tmp_path: Path) -> None:
    ctx, fetcher = _build(tmp_path)
    result = BacktestHarness(ctx, fetcher).run()
    text = result.summary()
    assert "Backtest summary" in text
    assert "trades" in text
    assert "sharpe" in text.lower()


# --------------------------------------------------------------------- #
# Dry-run wrapper                                                        #
# --------------------------------------------------------------------- #

def test_dry_run_calls_sleep_once_per_bar_except_last(tmp_path: Path) -> None:
    """Dry-run should sleep (n_bars - 1) times, not after the final bar."""
    ctx, fetcher = _build(tmp_path)
    # Truncate to 5 bars so this is quick.
    fetcher._series["RELIANCE"] = fetcher._series["RELIANCE"][:5]  # type: ignore[attr-defined]

    calls: list[float] = []
    run_dry_run(
        ctx, fetcher, speed_multiplier=1000.0,
        cfg=BacktestConfig(),
        sleep_fn=calls.append,
    )
    # 5 bars → 4 sleeps.
    assert len(calls) == 4
    # All sleeps are the same duration.
    assert len(set(calls)) == 1


def test_dry_run_rejects_bad_speed(tmp_path: Path) -> None:
    ctx, fetcher = _build(tmp_path)
    with pytest.raises(ValueError, match="speed_multiplier"):
        run_dry_run(ctx, fetcher, speed_multiplier=0.0, sleep_fn=lambda _: None)


def test_dry_run_rejects_unknown_interval(tmp_path: Path) -> None:
    ctx, fetcher = _build(tmp_path)
    ctx.settings.strategy.candle_interval = "3h"  # not in the interval map
    with pytest.raises(ValueError, match="unsupported candle_interval"):
        run_dry_run(ctx, fetcher, speed_multiplier=10.0, sleep_fn=lambda _: None)


def test_dry_run_returns_same_shape_as_harness(tmp_path: Path) -> None:
    ctx, fetcher = _build(tmp_path)
    result = run_dry_run(
        ctx, fetcher, speed_multiplier=1_000_000.0,
        sleep_fn=lambda _: None,  # no actual sleep
    )
    assert result.timestamps_processed == 120
    assert "sharpe" in result.metrics


def test_dry_run_stop_at_truncates(tmp_path: Path) -> None:
    ctx, fetcher = _build(tmp_path)
    full = fetcher._series["RELIANCE"]  # type: ignore[attr-defined]
    stop_at = full[49].ts   # include only first 50 bars
    result = run_dry_run(
        ctx, fetcher,
        speed_multiplier=1_000_000.0,
        cfg=BacktestConfig(stop_at_ts=stop_at),
        sleep_fn=lambda _: None,
    )
    assert result.timestamps_processed == 50
