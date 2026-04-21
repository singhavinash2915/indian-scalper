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
from tests.fixtures import paper_mode, running_scheduler
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
    settings = paper_mode(Settings.from_template())
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
    # D11 Slice 1 — scheduler_state defaults to "stopped" on first init,
    # which would short-circuit every run_tick in this suite. Flip it
    # to "running" here so existing tests exercise the full pipeline;
    # tests that specifically verify stopped/paused branches override.
    running_scheduler(broker)
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
    # D11 Slice 1: kill-switch short-circuit reason is now "killed".
    # A first kill tick with a running scheduler also square-offs + stops.
    assert report.skipped_reason == "killed"
    assert report.signals == []
    assert ctx.broker.store.get_flag("scheduler_state") == "stopped"


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

    # Next tick is fully locked out — kill check sees scheduler already
    # stopped (by the drawdown inline square-off) and short-circuits.
    next_report = run_tick(ctx, T_ENTRY + timedelta(minutes=5))
    assert next_report.skipped_reason == "killed"


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


# ---------------------------------------------------------------- #
# D11 Slice 1 — scheduler state machine                             #
# ---------------------------------------------------------------- #

def test_scheduler_stopped_short_circuits(tmp_path: Path) -> None:
    """When scheduler_state=stopped (the first-run default), run_tick
    exits before fetching anything."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ctx.broker.store.set_flag("scheduler_state", "stopped", actor="test")
    report = run_tick(ctx, T_ENTRY)
    assert report.skipped_reason == "scheduler_stopped"
    assert report.signals == []
    assert report.exits == []


def test_paused_skips_scoring_but_updates_ltps(tmp_path: Path) -> None:
    """Paused state: no scoring, no orders, but mark-to-market + equity
    snapshot still run so the dashboard stays alive."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    # Open a position first to have something to mark.
    from brokers.base import OrderType, Side
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T_ENTRY)
    bullish = _bullish_candles()
    ctx.broker.settle("RELIANCE", bullish[-1])
    assert len(ctx.broker.get_positions()) == 1
    equity_rows_before = len(ctx.broker.store.load_equity_curve())

    ctx.broker.store.set_flag("scheduler_state", "paused", actor="test")
    # Advance the fetcher's latest candle so LTP changes.
    new_bar = Candle(
        ts=bullish[-1].ts + timedelta(minutes=15),
        open=bullish[-1].close, high=bullish[-1].close + 5,
        low=bullish[-1].close - 1, close=bullish[-1].close + 3, volume=1000,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(new_bar)  # type: ignore[attr-defined]

    report = run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    assert report.skipped_reason == "paused"
    assert report.signals == []
    assert report.exits == []
    # LTP refreshed.
    assert ctx.broker._ltp["RELIANCE"] == new_bar.close
    # Equity snapshot written during paused refresh.
    assert len(ctx.broker.store.load_equity_curve()) > equity_rows_before


def test_paused_does_not_fill_pending_orders(tmp_path: Path) -> None:
    """Pending orders stay pending across a paused tick — settle is
    skipped entirely while paused."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    from brokers.base import OrderType, Side

    ctx.broker.store.set_flag("scheduler_state", "paused", actor="test")
    pending = ctx.broker.place_order(
        "RELIANCE", 5, Side.BUY, OrderType.LIMIT, price=900.0, ts=T_ENTRY,
    )
    # Append a candle that would trigger the limit fill if settle ran.
    last = _bullish_candles()[-1]
    touch_candle = Candle(
        ts=last.ts + timedelta(minutes=15),
        open=last.close, high=last.close + 1,
        low=899.0, close=last.close - 1, volume=1000,
    )
    ctx.broker.fetcher._series["RELIANCE"].append(touch_candle)  # type: ignore[attr-defined]

    run_tick(ctx, T_ENTRY + timedelta(minutes=15))

    # Still pending — settle never ran under paused.
    assert pending.id in ctx.broker.orders
    assert ctx.broker.orders[pending.id].status == "PENDING"


def test_kill_tick_squares_off_and_stops(tmp_path: Path) -> None:
    """A first kill tick with a running scheduler and open positions
    generates exit orders AND pins scheduler_state=stopped."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    from brokers.base import OrderType, Side

    # Open a position first.
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T_ENTRY)
    ctx.broker.settle("RELIANCE", _bullish_candles()[-1])
    assert len(ctx.broker.get_positions()) == 1

    # Flip kill switch — simulating the UI click.
    ctx.broker.set_kill_switch(True, actor="web")

    report = run_tick(ctx, T_ENTRY + timedelta(minutes=15))
    assert report.skipped_reason == "killed"
    assert any(e.reason == "kill_switch" for e in report.exits)
    assert "killed_squared_off" in report.notes
    # Scheduler pinned to stopped.
    assert ctx.broker.store.get_flag("scheduler_state") == "stopped"


def test_already_killed_and_stopped_just_skips(tmp_path: Path) -> None:
    """Second kill tick (kill + stopped already in place) short-circuits
    silently — no second square-off attempt."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ctx.broker.store.set_flag("scheduler_state", "stopped", actor="test")
    ctx.broker.set_kill_switch(True, actor="test")

    report = run_tick(ctx, T_ENTRY)
    assert report.skipped_reason == "killed"
    assert report.exits == []
    assert "killed_squared_off" not in report.notes


def test_drawdown_breach_squares_off_inline(tmp_path: Path) -> None:
    """When drawdown trips during the same tick, positions are squared
    off IN THAT TICK — no waiting for the next cycle."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    from brokers.base import OrderType, Side

    # Open a position so there's something to square off.
    ctx.broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T_ENTRY)
    ctx.broker.settle("RELIANCE", _bullish_candles()[-1])

    # Seed a big drawdown: peak 600k, now 500k → 16.7% breach.
    ctx.broker.store.snapshot_equity(
        datetime(2026, 4, 19, 14, 0, tzinfo=IST),
        equity=600_000, cash=500_000, pnl=100_000,
    )
    ctx.broker.store.snapshot_equity(
        datetime(2026, 4, 20, 9, 15, tzinfo=IST),
        equity=500_000, cash=500_000, pnl=0,
    )
    ctx.broker.om.cash = 500_000

    report = run_tick(ctx, T_ENTRY)
    # Portfolio-halt path fired.
    assert "halted" in (report.skipped_reason or "")
    assert "kill_switch_set_by_drawdown" in report.notes
    # Square-off ran inline (not waiting for next tick).
    assert any(e.reason == "drawdown_circuit" for e in report.exits)
    # Scheduler pinned to stopped + kill latched.
    assert ctx.broker.store.get_flag("scheduler_state") == "stopped"
    assert ctx.broker.is_kill_switch_on() is True


# ---------------------------------------------------------------- #
# D11 Slice 2 — universe registry integration                       #
# ---------------------------------------------------------------- #

def test_scan_loop_uses_enabled_symbols_from_registry(tmp_path: Path) -> None:
    """Registry-backed universe: a disabled symbol is dropped from the
    next tick; no signals for it."""
    from data.universe import UniverseRegistry

    ctx = _build_ctx(tmp_path, ["RELIANCE", "TCS"], {
        "RELIANCE": _bullish_candles(),
        "TCS": _bullish_candles(),
    })
    registry = UniverseRegistry(ctx.broker.store, ctx.instruments)
    registry.seed_if_empty(["RELIANCE", "TCS"])
    # Disable RELIANCE; only TCS should be scanned.
    registry.set_enabled("RELIANCE", "EQ", False)
    ctx.universe_registry = registry

    assert ctx.effective_universe() == ["TCS"]
    report = run_tick(ctx, T_ENTRY)
    # Any signal should be for TCS only.
    assert all(s.symbol == "TCS" for s in report.signals)


def test_watch_only_override_blocks_entry_but_scores(tmp_path: Path) -> None:
    """Per-symbol watch-only override: no entry order placed even though
    global trade_mode=paper (via paper_mode() in _build_ctx)."""
    from data.universe import UniverseRegistry

    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    registry = UniverseRegistry(ctx.broker.store, ctx.instruments)
    registry.seed_if_empty(["RELIANCE"])
    registry.set_watch_only_override("RELIANCE", "EQ", True)
    ctx.universe_registry = registry

    report = run_tick(ctx, T_ENTRY)
    # No signal (the SignalReport is generated only on a placed entry).
    assert report.signals == []
    # No orders placed, no positions opened.
    assert ctx.broker.orders == {}
    assert ctx.broker.get_positions() == []


def test_registry_is_consulted_every_tick(tmp_path: Path) -> None:
    """Mid-session toggle takes effect on the next tick with no
    scheduler restart."""
    from data.universe import UniverseRegistry

    bullish = _bullish_candles()
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": bullish})
    registry = UniverseRegistry(ctx.broker.store, ctx.instruments)
    registry.seed_if_empty(["RELIANCE"])
    ctx.universe_registry = registry

    r1 = run_tick(ctx, T_ENTRY)
    assert len(r1.signals) == 1

    ctx.broker.fetcher._series["RELIANCE"].append(  # type: ignore[attr-defined]
        Candle(
            ts=bullish[-1].ts + timedelta(minutes=1),
            open=bullish[-1].close, high=bullish[-1].close + 1,
            low=bullish[-1].close - 0.5, close=bullish[-1].close + 0.5, volume=2000,
        )
    )

    # Disable the symbol mid-session — next tick produces no signal.
    registry.set_enabled("RELIANCE", "EQ", False)
    r2 = run_tick(ctx, T_ENTRY + timedelta(minutes=1))
    assert r2.signals == []


def test_empty_registry_falls_back_to_static_universe(tmp_path: Path) -> None:
    """When a registry is attached but the table is empty, scan loop
    uses the static ``ctx.universe`` list — bootstrap scenario."""
    from data.universe import UniverseRegistry

    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    registry = UniverseRegistry(ctx.broker.store, ctx.instruments)
    # No seed call — table is empty.
    ctx.universe_registry = registry

    assert registry.count() == 0
    assert ctx.effective_universe() == ["RELIANCE"]


# ---------------------------------------------------------------- #
# D11 Slice 3 — signal_snapshots at every decision branch           #
# ---------------------------------------------------------------- #

def test_entered_action_writes_snapshot(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    run_tick(ctx, T_ENTRY)
    rows = ctx.broker.store.load_recent_signals()
    # At least one row for RELIANCE with the `entered` action.
    assert any(r["symbol"] == "RELIANCE" and r["action"] == "entered"
               for r in rows)
    entered = next(r for r in rows if r["action"] == "entered")
    assert entered["trade_mode"] == "paper"
    # Breakdown dict matches the scoring engine's shape.
    assert {"ema_stack", "vwap_cross", "rsi_entry"}.issubset(entered["breakdown"])


def test_skipped_score_writes_snapshot(tmp_path: Path) -> None:
    """Flat chop → score below min_score → `skipped_score` row."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _flat_candles()})
    run_tick(ctx, T_ENTRY)
    rows = ctx.broker.store.load_recent_signals()
    assert any(r["symbol"] == "RELIANCE" and r["action"] == "skipped_score"
               for r in rows)


def test_watch_only_override_writes_snapshot(tmp_path: Path) -> None:
    from data.universe import UniverseRegistry

    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    reg = UniverseRegistry(ctx.broker.store, ctx.instruments)
    reg.seed_if_empty(["RELIANCE"])
    reg.set_watch_only_override("RELIANCE", "EQ", True)
    ctx.universe_registry = reg

    run_tick(ctx, T_ENTRY)
    rows = ctx.broker.store.load_recent_signals()
    watch = next(
        (r for r in rows if r["action"] == "watch_only_logged"), None,
    )
    assert watch is not None
    assert watch["symbol"] == "RELIANCE"
    assert watch["reason"] == "per_symbol_override"
    # Score was still computed (not zero for the bullish fixture).
    assert watch["score"] > 0


def test_global_watch_only_writes_snapshot(tmp_path: Path) -> None:
    """Global trade_mode=watch_only → broker rejects entry → scan loop
    records a watch_only_logged snapshot with the broker-rejection
    reason."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ctx.broker.store.set_flag("trade_mode", "watch_only", actor="test")

    run_tick(ctx, T_ENTRY)
    rows = ctx.broker.store.load_recent_signals()
    watch = next(
        (r for r in rows if r["action"] == "watch_only_logged"), None,
    )
    assert watch is not None
    assert "broker_rejection" in watch["reason"]
    assert watch["trade_mode"] == "watch_only"


def test_insufficient_candles_writes_skipped_filter(tmp_path: Path) -> None:
    short = _bullish_candles()[:10]  # well below MIN_LOOKBACK_BARS
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": short})
    run_tick(ctx, T_ENTRY)
    rows = ctx.broker.store.load_recent_signals()
    assert any(r["action"] == "skipped_filter"
               and "insufficient_candles" in (r["reason"] or "")
               for r in rows)


def test_prune_old_snapshots_runs_once_per_day(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    # Seed an old snapshot (8 days back).
    from datetime import timedelta as td
    old_ts = T_ENTRY - td(days=8)
    ctx.broker.store.append_signal_snapshot(
        ts=old_ts, symbol="OLD", score=0, breakdown={},
        action="entered", reason=None, trace_id=None, trade_mode="paper",
    )
    pre = ctx.broker.store.load_recent_signals(limit=500)
    assert any(r["symbol"] == "OLD" for r in pre)

    run_tick(ctx, T_ENTRY)

    post = ctx.broker.store.load_recent_signals(limit=500)
    assert not any(r["symbol"] == "OLD" for r in post)
    # Marker set so next tick same day doesn't rerun the DELETE.
    assert ctx.broker.store.get_flag("last_signal_prune_date") is not None


# ---------------------------------------------------------------- #
# D11 Slice 2 toolkit — ignore_market_hours (for scalper-tick CLI)  #
# ---------------------------------------------------------------- #

def test_ignore_market_hours_bypasses_weekend(tmp_path: Path) -> None:
    """``scalper-tick --ignore-market-hours`` drives the full pipeline
    even when ``is_market_open`` returns False — used by the Tuesday
    dry-run playbook's Phase 3 rehearsal."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    # Saturday — is_market_open would normally return False.
    saturday_11am = datetime(2026, 4, 18, 11, 0, tzinfo=IST)

    # Without the flag: skipped as market closed.
    report_guarded = run_tick(ctx, saturday_11am)
    assert report_guarded.skipped_reason == "market_closed"

    # With the flag: falls through, snapshots written.
    report_forced = run_tick(ctx, saturday_11am, ignore_market_hours=True)
    assert report_forced.skipped_reason is None
    # Either entered a position or logged skipped_score/skipped_filter,
    # but NOT market_closed and NOT outside_entry_window.
    assert "outside_entry_window" not in report_forced.notes


def test_ignore_market_hours_still_respects_kill_switch(tmp_path: Path) -> None:
    """Kill switch is an emergency override — ignore_market_hours must
    NOT bypass it."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ctx.broker.set_kill_switch(True, actor="test")
    report = run_tick(
        ctx, T_ENTRY, ignore_market_hours=True,
    )
    # Killed short-circuits before the market-hours gate is even considered.
    assert report.skipped_reason == "killed"


def test_ignore_market_hours_still_respects_scheduler_stopped(tmp_path: Path) -> None:
    """scheduler_state=stopped must still halt even with the override."""
    ctx = _build_ctx(tmp_path, ["RELIANCE"], {"RELIANCE": _bullish_candles()})
    ctx.broker.store.set_flag("scheduler_state", "stopped", actor="test")
    report = run_tick(
        ctx, T_ENTRY, ignore_market_hours=True,
    )
    assert report.skipped_reason == "scheduler_stopped"
