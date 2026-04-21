"""End-to-end scan loop — scenario tests.

Each scenario constructs a ``ScanContext`` against a PaperBroker with a
FakeCandleFetcher and drives one or more ``run_tick`` calls with a
synthetic ``ts``. We assert on ``TickReport`` + broker state, not on
log output.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brokers.base import Candle
from brokers.paper import PaperBroker
from config.settings import Settings
from data.holidays import HolidayCalendar
from data.instruments import InstrumentMaster
from data.market_data import FakeCandleFetcher, df_to_candles
from scheduler.scan_loop import ScanContext, run_tick
from tests.fixtures.synthetic import bullish_breakout_df, flat_chop_df

IST = ZoneInfo("Asia/Kolkata")
# A Monday inside NSE entry window at 10:30 IST, after the 09:30 block.
T_ENTRY = datetime(2026, 4, 20, 10, 30, tzinfo=IST)
T_EOD = datetime(2026, 4, 20, 15, 25, tzinfo=IST)


# ---------------------------------------------------------------- #
# Shared builders                                                   #
# ---------------------------------------------------------------- #

def _build_ctx(
    tmp_path: Path,
    universe: list[str],
    candles_per_symbol: dict[str, list[Candle]],
    min_score: int = 4,
) -> ScanContext:
    settings = Settings.from_template()
    # The synthetic bullish fixture is engineered to fire the four
    # regime-level factors reliably but not the timing-sensitive
    # VWAP / MACD crosses (see D3 notes). min_score=4 lets the scan
    # loop exercise the full entry pipeline without pulling in a live
    # market-data fixture that reliably lands 6/8.
    settings.strategy.min_score = min_score

    fetcher = FakeCandleFetcher(candles_per_symbol)

    instruments = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instrument_cache",
    )
    fixture = Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    instruments.load_equity_from_csv(fixture)

    broker = PaperBroker(
        settings,
        db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    return ScanContext(
        settings=settings,
        broker=broker,
        universe=universe,
        instruments=instruments,
    )


def _bullish_candles(symbol: str = "RELIANCE") -> list[Candle]:
    """Grab the D3 bullish fixture and convert to the broker's Candle type."""
    df = bullish_breakout_df()
    return df_to_candles(df)


def _flat_candles() -> list[Candle]:
    df = flat_chop_df()
    return df_to_candles(df)


# ---------------------------------------------------------------- #
# Tick-level gates                                                  #
# ---------------------------------------------------------------- #

def test_kill_switch_skips_whole_tick(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ctx.broker.set_kill_switch(True)
    report = run_tick(ctx, T_ENTRY)
    assert report.skipped_reason == "kill_switch"
    assert report.signals == []


def test_market_closed_skips(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    sunday = datetime(2026, 4, 19, 11, 0, tzinfo=IST)
    report = run_tick(ctx, sunday)
    assert report.skipped_reason == "market_closed"


def test_holiday_skips(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    cal = HolidayCalendar(tmp_path / "holidays.db")
    cal.load_from_yaml(Path(__file__).parent / "fixtures" / "sample_holidays.yaml")
    ctx.calendar = cal
    # 2026-04-20 is flagged as a holiday in the fixture.
    report = run_tick(ctx, T_ENTRY)
    assert report.skipped_reason == "market_closed"


# ---------------------------------------------------------------- #
# Entry path                                                        #
# ---------------------------------------------------------------- #

def test_bullish_signal_places_entry_and_stashes_stops(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    report = run_tick(ctx, T_ENTRY)

    assert report.skipped_reason is None
    assert len(report.signals) == 1
    signal = report.signals[0]
    assert signal.symbol == "RELIANCE"
    assert signal.qty > 0
    assert signal.stop < signal.entry
    assert signal.take_profit > signal.entry

    # Order was placed, sitting pending, stops queued for next fill.
    assert any(o.status == "PENDING" for o in ctx.broker.orders.values())
    assert signal.order_id in ctx.pending_stops


def test_flat_chop_produces_no_signal(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _flat_candles()})
    report = run_tick(ctx, T_ENTRY)
    assert report.signals == []


def test_entry_then_next_tick_fills_and_attaches_stops(tmp_path: Path) -> None:
    """Two-tick flow: tick 1 places a pending MARKET order; tick 2
    settles it against the next bar and attaches the stashed stops."""
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    report1 = run_tick(ctx, T_ENTRY)
    assert len(report1.signals) == 1
    signal = report1.signals[0]

    # Simulate a next bar arriving — append one more candle and re-seed.
    next_bar = Candle(
        ts=bullish[-1].ts + timedelta(minutes=15),
        open=bullish[-1].close,
        high=bullish[-1].close + 1.0,
        low=bullish[-1].close - 0.5,
        close=bullish[-1].close + 0.7,
        volume=3000,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(next_bar)  # type: ignore[attr-defined]

    T2 = T_ENTRY + timedelta(minutes=15)
    run_tick(ctx, T2)

    positions = ctx.broker.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "RELIANCE"
    # Stashed stops were applied on fill.
    assert p.stop_loss == pytest.approx(signal.stop)
    assert p.take_profit == pytest.approx(signal.take_profit)
    # pending_stops drained.
    assert signal.order_id not in ctx.pending_stops


def test_does_not_double_up_on_existing_position(tmp_path: Path) -> None:
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    # Tick 1: signal placed.
    run_tick(ctx, T_ENTRY)
    # Settle + fill: advance one bar and tick again.
    ctx.broker.fetcher._series["RELIANCE"].append(  # type: ignore[attr-defined]
        Candle(
            ts=bullish[-1].ts + timedelta(minutes=15),
            open=bullish[-1].close, high=bullish[-1].close + 1,
            low=bullish[-1].close - 0.5, close=bullish[-1].close + 0.5, volume=2000,
        )
    )
    run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    # Position is open.
    assert len(ctx.broker.get_positions()) == 1

    # Tick 3: same fixture, still bullish, but we already hold → no new signal.
    report3 = run_tick(ctx, T_ENTRY + timedelta(minutes=30))
    assert report3.signals == []


# ---------------------------------------------------------------- #
# EOD square-off                                                    #
# ---------------------------------------------------------------- #

def test_eod_squareoff_closes_all_positions(tmp_path: Path) -> None:
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    # Open a position first: manually place + settle.
    from brokers.base import OrderType, Side
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    ctx.broker.settle("RELIANCE", bullish[-1])
    assert len(ctx.broker.get_positions()) == 1

    # Now run tick at EOD time.
    report = run_tick(ctx, T_EOD)
    assert report.skipped_reason == "eod_squareoff"
    assert len(report.exits) == 1
    assert report.exits[0].reason == "eod_squareoff"
    # Close order is now PENDING; settle on next bar would flatten it.
    pending = [o for o in ctx.broker.orders.values() if o.status == "PENDING"]
    assert len(pending) == 1
    assert pending[0].side == Side.SELL


# ---------------------------------------------------------------- #
# Position management — stops + TP + time stop                      #
# ---------------------------------------------------------------- #

def test_stop_loss_triggers_exit_when_candle_breaches(tmp_path: Path) -> None:
    from brokers.base import OrderType, Side
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    ctx.broker.settle("RELIANCE", bullish[-1])
    # Set a stop just above the last candle's low so the next tick's
    # candle range touches it.
    last = bullish[-1]
    ctx.broker.set_position_stops("RELIANCE", stop_loss=last.low + 0.01)

    # Append a breach candle where low dips below the stop.
    breach = Candle(
        ts=last.ts + timedelta(minutes=15),
        open=last.close, high=last.close + 0.1,
        low=last.low - 2.0,           # well below stop
        close=last.close - 1.0, volume=3000,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(breach)  # type: ignore[attr-defined]

    report = run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    assert any(e.reason == "stop_loss" for e in report.exits)


def test_take_profit_triggers_exit(tmp_path: Path) -> None:
    from brokers.base import OrderType, Side
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    ctx.broker.settle("RELIANCE", bullish[-1])
    last = bullish[-1]
    ctx.broker.set_position_stops("RELIANCE", take_profit=last.high + 0.01)

    # Append a breakout candle where high clears the TP.
    breakout = Candle(
        ts=last.ts + timedelta(minutes=15),
        open=last.close, high=last.high + 5.0,
        low=last.low, close=last.high + 2.0, volume=4000,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(breakout)  # type: ignore[attr-defined]

    report = run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    assert any(e.reason == "take_profit" for e in report.exits)


def test_time_stop_fires_on_aged_flat_position(tmp_path: Path) -> None:
    from brokers.base import OrderType, Side
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    # Settle at the LAST candle of the series, anchoring opened_at.
    ctx.broker.settle("RELIANCE", bullish[-1])
    pos = ctx.broker.get_positions()[0]
    # Backdate opened_at so the position looks old.
    from dataclasses import replace
    aged = replace(pos, opened_at=T_ENTRY - timedelta(minutes=120))
    ctx.broker.om.positions["RELIANCE"] = aged
    ctx.broker.store.save_position(aged)
    # Seed a candle whose close is essentially identical to avg_price.
    flat_bar = Candle(
        ts=bullish[-1].ts + timedelta(minutes=15),
        open=pos.avg_price, high=pos.avg_price + 0.1,
        low=pos.avg_price - 0.05, close=pos.avg_price, volume=1500,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(flat_bar)  # type: ignore[attr-defined]

    report = run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    assert any(e.reason == "time_stop" for e in report.exits)


def test_trail_stop_ratchets_up_on_favourable_move(tmp_path: Path) -> None:
    from brokers.base import OrderType, Side
    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    ctx.broker.settle("RELIANCE", bullish[-1])
    # Seed an initial trail_stop far below so a rising price clearly tightens it.
    ctx.broker.set_position_stops("RELIANCE", trail_stop=bullish[-1].close - 100)

    last = bullish[-1]
    rally = Candle(
        ts=last.ts + timedelta(minutes=15),
        open=last.close, high=last.close + 3.0,
        low=last.close - 0.2, close=last.close + 2.5, volume=3000,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(rally)  # type: ignore[attr-defined]

    before = ctx.broker.get_positions()[0].trail_stop
    run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    after = ctx.broker.get_positions()[0].trail_stop
    assert after is not None
    assert after > before  # trail tightened


# ---------------------------------------------------------------- #
# Portfolio gates                                                   #
# ---------------------------------------------------------------- #

def test_daily_loss_halt_blocks_entries(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    # Seed the equity curve so start-of-day is 500,000 but now we're
    # 4% underwater — tripping the 3% daily-loss limit.
    ctx.broker.store.snapshot_equity(
        datetime(2026, 4, 20, 9, 15, tzinfo=IST),
        equity=500_000, cash=500_000, pnl=0,
    )
    ctx.broker.store.snapshot_equity(
        datetime(2026, 4, 20, 10, 20, tzinfo=IST),
        equity=480_000, cash=480_000, pnl=-20_000,
    )
    # Drain the broker's cash so get_funds reports the drawdown equity.
    ctx.broker.om.cash = 480_000

    report = run_tick(ctx, T_ENTRY)
    assert report.skipped_reason is not None
    assert "halted" in report.skipped_reason
    assert "daily loss" in report.skipped_reason
    assert report.signals == []


def test_drawdown_circuit_latches_kill_switch(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    # Peak at 600k; now at 500k → 16.7% drawdown → trips circuit breaker.
    ctx.broker.store.snapshot_equity(
        datetime(2026, 4, 20, 9, 15, tzinfo=IST),
        equity=500_000, cash=500_000, pnl=0,
    )
    ctx.broker.store.snapshot_equity(
        datetime(2026, 4, 19, 14, 0, tzinfo=IST),
        equity=600_000, cash=500_000, pnl=100_000,
    )
    ctx.broker.om.cash = 500_000

    report = run_tick(ctx, T_ENTRY)
    assert report.skipped_reason is not None and "halted" in report.skipped_reason
    # Kill switch is latched for downstream ticks.
    assert ctx.broker.is_kill_switch_on() is True
    assert "kill_switch_set_by_drawdown" in report.notes

    # Next tick is fully locked out.
    next_report = run_tick(ctx, T_ENTRY + timedelta(minutes=5))
    assert next_report.skipped_reason == "kill_switch"


# ---------------------------------------------------------------- #
# Outside entry window                                              #
# ---------------------------------------------------------------- #

def test_outside_entry_window_manages_only(tmp_path: Path) -> None:
    """After 15:00 but before EOD square-off — position management runs,
    but no new entries."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ts = datetime(2026, 4, 20, 15, 5, tzinfo=IST)  # past entry cutoff, before EOD
    report = run_tick(ctx, ts)
    assert report.signals == []
    assert "outside_entry_window" in report.notes
