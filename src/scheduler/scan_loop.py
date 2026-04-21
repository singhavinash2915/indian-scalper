"""End-to-end scan loop.

Wires every earlier deliverable together: market-hours + holiday gating
(D2) → candle fetch (D4) → indicator scoring (D3) → risk gates + sizing
+ stops (D5) → order placement + settle (D4) → position management +
EOD square-off.

The loop is split into two layers:

* ``run_tick(ctx, ts)`` — one deterministic pass. Pure enough to
  unit-test scenario-by-scenario: kill switch, market closed, entry,
  position management, time stop, EOD, daily-loss halt, drawdown halt.
* ``run_scan_loop(ctx)`` — APScheduler wrapper that calls ``run_tick``
  every ``scan_interval_seconds``. Production entry point.

Design notes:
- Every tick gets a UUID ``trace_id`` stamped into the returned
  ``TickReport`` and logged on every decision, per PROMPT's
  "observable" constraint.
- The loop never imports the Upstox SDK directly — all broker access
  goes through ``BrokerBase`` + the PaperBroker-specific hooks
  (``settle`` / ``mark_to_market`` / ``set_position_stops``).
- Entry stops are computed at placement time and stashed in
  ``ctx.pending_stops`` keyed by order_id. When the order fills on a
  subsequent tick, the stops are applied to the freshly-created
  position. If the loop crashes between fill and stop-apply, the
  management branch notices a position without stops and rebuilds them
  from the current ATR.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
from loguru import logger

from brokers.base import OrderType, Position, Segment, Side
from brokers.paper import PaperBroker
from config.settings import Settings
from data.holidays import HolidayCalendar
from data.instruments import InstrumentMaster
from data.universe import UniverseRegistry
from risk.circuit_breaker import (
    check_daily_loss_limit,
    check_drawdown_circuit,
    check_position_limits,
    combine_gates,
    is_eod_squareoff_time,
    peak_equity_from_curve,
    start_of_day_equity,
)
from risk.position_sizing import position_size
from risk.stops import (
    atr_stop_price,
    check_time_stop,
    take_profit_price,
    trailing_multiplier,
    update_trail_stop,
)
from scheduler.market_hours import (
    can_enter_new_trade,
    is_market_open,
    now_ist,
)
from strategy import indicators as ind
from strategy.scoring import MIN_LOOKBACK_BARS, score_symbol


# ---------------------------------------------------------------- #
# Reports                                                           #
# ---------------------------------------------------------------- #

@dataclass
class SignalReport:
    symbol: str
    score: int
    qty: int
    entry: float
    stop: float
    take_profit: float
    order_id: str


@dataclass
class ExitReport:
    symbol: str
    reason: str  # "stop_loss" | "take_profit" | "trail_stop" | "time_stop" | "eod_squareoff"
    order_id: str


@dataclass
class TickReport:
    trace_id: str
    ts: datetime
    skipped_reason: str | None = None  # whole-tick skip
    signals: list[SignalReport] = field(default_factory=list)
    exits: list[ExitReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ScanContext:
    """Everything ``run_tick`` needs. Passed in so the same function
    drives both production and tests without touching module-level
    globals.
    """

    settings: Settings
    broker: PaperBroker
    universe: list[str]
    instruments: InstrumentMaster
    calendar: HolidayCalendar | None = None
    # D11 Slice 2: when set, the scan loop uses the registry as the
    # live source of truth for "which symbols do we scan this tick"
    # and "does this symbol have a per-symbol watch-only override".
    # When None, falls back to ``universe`` — keeps older ScanContext
    # callers (tests, pre-Slice-2 wiring) working.
    universe_registry: UniverseRegistry | None = None
    # ``pending_stops`` maps order_id → (stop_loss, take_profit). Scan
    # loop populates it at entry; the next settle applies and clears.
    pending_stops: dict[str, tuple[float, float]] = field(default_factory=dict)

    def effective_universe(self) -> list[str]:
        """Which symbols should the scan loop touch this tick?

        Registry, when present AND populated, wins. When the table is
        empty or no registry is attached, fall back to the static
        ``universe`` list — lets tests and bootstrap paths work without
        touching the DB.
        """
        if self.universe_registry is not None and self.universe_registry.count() > 0:
            return self.universe_registry.enabled_symbols()
        return list(self.universe)


# ---------------------------------------------------------------- #
# Tick entry point                                                  #
# ---------------------------------------------------------------- #

def run_tick(ctx: ScanContext, ts: datetime | None = None) -> TickReport:
    ts = ts or now_ist()
    trace_id = uuid.uuid4().hex[:8]
    report = TickReport(trace_id=trace_id, ts=ts)

    store = ctx.broker.store

    # 1. Kill switch (emergency override).
    #    Kill is MORE than a skip: square off every open position with
    #    ``intent="exit"`` (bypassing trade_mode watch_only since exits
    #    always flow) and pin ``scheduler_state = "stopped"`` so the
    #    scheduler won't resume until the operator re-arms. Once already
    #    stopped, subsequent ticks just skip silently.
    if ctx.broker.is_kill_switch_on():
        current_state = store.get_flag("scheduler_state", "stopped")
        if current_state != "stopped":
            report.exits.extend(
                _squareoff_all(ctx, ts, trace_id, reason="kill_switch")
            )
            store.set_flag("scheduler_state", "stopped", actor="kill_switch")
            report.notes.append("killed_squared_off")
            logger.warning(
                "[{}] KILL SWITCH tripped — squared off {} positions, "
                "scheduler_state → stopped",
                trace_id, len(report.exits),
            )
        report.skipped_reason = "killed"
        return report

    # 2. Scheduler state — explicit operator gate (Slice 1).
    scheduler_state = store.get_flag("scheduler_state", "stopped")
    if scheduler_state == "stopped":
        report.skipped_reason = "scheduler_stopped"
        return report

    # 3. Market hours (incl. holidays).
    if not is_market_open(ctx.settings, ts, calendar=ctx.calendar):
        report.skipped_reason = "market_closed"
        return report

    # 4. PAUSED mode: keep market data fresh (so the dashboard stays
    #    alive) but skip every order-producing path. No settle — that
    #    would fill any stale pending orders; operator wants a full
    #    hold. LTPs and equity curve snapshots only.
    if scheduler_state == "paused":
        _refresh_ltps(ctx, trace_id)
        report.skipped_reason = "paused"
        return report

    # 5. Fetch latest candles + settle pending orders for every symbol
    #    that matters (enabled-universe ∪ currently-open). Universe is
    #    looked up fresh each tick so toggles from the dashboard take
    #    effect immediately without a scheduler restart.
    symbols = set(ctx.effective_universe()) | {p.symbol for p in ctx.broker.get_positions()}
    candles_by_symbol: dict[str, list] = {}
    for sym in symbols:
        try:
            candles = ctx.broker.get_candles(
                sym, ctx.settings.strategy.candle_interval, lookback=120,
            )
        except Exception as exc:
            logger.error("[{}] fetch {} failed: {}", trace_id, sym, exc)
            continue
        if not candles:
            continue
        candles_by_symbol[sym] = candles

        # Settle pending orders against the most recent closed candle.
        filled = ctx.broker.settle(sym, candles[-1])
        for f in filled:
            if f.id in ctx.pending_stops:
                stop, tp = ctx.pending_stops.pop(f.id)
                ctx.broker.set_position_stops(f.symbol, stop_loss=stop, take_profit=tp)
                logger.info(
                    "[{}] stops attached | {} SL={:.2f} TP={:.2f}",
                    trace_id, f.symbol, stop, tp,
                )

    # 6. EOD square-off — close every intraday position and stop.
    if (
        ctx.settings.risk.eod_squareoff_intraday
        and is_eod_squareoff_time(ts, ctx.settings.market)
    ):
        report.exits.extend(_squareoff_all(ctx, ts, trace_id))
        report.skipped_reason = "eod_squareoff"
        return report

    # 7. Manage existing positions: stops, trailing, time stop.
    report.exits.extend(_manage_positions(ctx, candles_by_symbol, ts, trace_id))

    # 8. Entry window check.
    if not can_enter_new_trade(ctx.settings, ts, calendar=ctx.calendar):
        report.notes.append("outside_entry_window")
        return report

    # 9. Portfolio-level gates (daily loss, drawdown). Drawdown-circuit
    #    trip is immediate: kill switch flipped, positions squared off
    #    in the same tick, scheduler_state pinned to stopped. No
    #    waiting for the next scheduler cycle to act on a breach.
    funds = ctx.broker.get_funds()
    equity_curve = store.load_equity_curve()
    peak = peak_equity_from_curve(equity_curve)
    sod = start_of_day_equity(equity_curve, ts) or ctx.settings.capital.starting_inr

    portfolio_gate = combine_gates(
        check_daily_loss_limit(funds["equity"], sod, ctx.settings.risk),
        check_drawdown_circuit(funds["equity"], peak, ctx.settings.risk),
    )
    if not portfolio_gate.allow_new_entries:
        report.skipped_reason = f"halted:{portfolio_gate.reason}"
        logger.warning("[{}] portfolio halt: {}", trace_id, portfolio_gate.reason)
        dd_gate = check_drawdown_circuit(funds["equity"], peak, ctx.settings.risk)
        if not dd_gate.allow_new_entries:
            ctx.broker.set_kill_switch(True, actor="drawdown_circuit")
            report.exits.extend(
                _squareoff_all(ctx, ts, trace_id, reason="drawdown_circuit")
            )
            store.set_flag("scheduler_state", "stopped", actor="drawdown_circuit")
            report.notes.append("kill_switch_set_by_drawdown")
        return report

    # 8. Per-symbol evaluation.
    for sym in ctx.effective_universe():
        signal = _evaluate_symbol(ctx, sym, candles_by_symbol.get(sym), ts, trace_id)
        if signal:
            report.signals.append(signal)

    return report


# ---------------------------------------------------------------- #
# Internals                                                         #
# ---------------------------------------------------------------- #

def _candles_to_df(candles) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        },
        index=pd.DatetimeIndex([c.ts for c in candles], name="ts"),
    )


def _segment_map(ctx: ScanContext, symbols: list[str]) -> dict[str, Segment]:
    out: dict[str, Segment] = {}
    for sym in symbols:
        inst = ctx.instruments.get(sym)
        out[sym] = inst.segment if inst is not None else Segment.EQUITY
    return out


def _evaluate_symbol(
    ctx: ScanContext,
    symbol: str,
    candles,
    ts: datetime,
    trace_id: str,
) -> SignalReport | None:
    if not candles or len(candles) < MIN_LOOKBACK_BARS:
        return None

    # Don't double up on an existing long.
    if any(p.symbol == symbol and p.qty > 0 for p in ctx.broker.get_positions()):
        return None

    df = _candles_to_df(candles)
    score = score_symbol(df, ctx.settings.strategy)
    if score.blocked:
        logger.debug("[{}] {} blocked: {}", trace_id, symbol, score.block_reason)
        return None
    if score.total < ctx.settings.strategy.min_score:
        logger.debug("[{}] {} score {} < {}", trace_id, symbol, score.total, ctx.settings.strategy.min_score)
        return None

    # Position-count gate — symbol-specific because it depends on the
    # target instrument's segment.
    segments = _segment_map(ctx, [p.symbol for p in ctx.broker.get_positions()] + [symbol])
    gate = check_position_limits(
        ctx.broker.get_positions(),
        segments[symbol],
        ctx.settings.risk,
        instrument_segments=segments,
    )
    if not gate.allow_new_entries:
        logger.debug("[{}] {} position cap: {}", trace_id, symbol, gate.reason)
        return None

    # ATR → stop + take-profit.
    atr_series = ind.atr(df["high"], df["low"], df["close"])
    atr = float(atr_series.iloc[-1])
    if atr <= 0:
        return None

    entry = float(df["close"].iloc[-1])
    stop = atr_stop_price(
        entry, atr, ctx.settings.risk.stop_atr_multiplier, Side.BUY,
    )
    tp = take_profit_price(
        entry, atr, ctx.settings.risk.take_profit_atr_multiplier, Side.BUY,
    )

    inst = ctx.instruments.get(symbol)
    lot_size = inst.lot_size if inst else 1

    funds = ctx.broker.get_funds()
    # Cap notional at 95% of available cash — the 5% buffer absorbs
    # per-order slippage + bar-to-bar drift between sizing and fill, so
    # a single position never triggers the InsufficientFundsError guard
    # at settle time.
    size = position_size(
        capital=funds["equity"],
        risk_per_trade_pct=ctx.settings.risk.risk_per_trade_pct,
        entry_price=entry,
        stop_price=stop,
        lot_size=lot_size,
        segment=segments[symbol],
        max_notional=funds["available"] * 0.95,
    )
    if size.qty == 0:
        logger.debug("[{}] {} sized to zero: {}", trace_id, symbol, size.note)
        return None

    # Per-symbol watch-only override (D11 Slice 2): score is still
    # computed (so Slice 3 signal-snapshot tables can record it) but
    # no order flows. Useful for "I want to shadow INFY for a week
    # before letting the bot trade it".
    if (
        ctx.universe_registry is not None
        and ctx.universe_registry.has_watch_only_override(symbol)
    ):
        logger.info(
            "[{}] {} score={}/8 watch_only_override — signal logged, no order",
            trace_id, symbol, score.total,
        )
        return None

    order = ctx.broker.place_order(
        symbol, size.qty, Side.BUY, OrderType.MARKET,
        intent="entry", ts=ts,
    )
    # Only stash stops if the order was actually accepted — a trade-mode
    # rejection returns a non-PENDING order the broker never queues.
    if order.status == "PENDING":
        ctx.pending_stops[order.id] = (stop, tp)

    logger.info(
        "[{}] SIGNAL {} qty={} entry={:.2f} stop={:.2f} tp={:.2f} score={}/8",
        trace_id, symbol, size.qty, entry, stop, tp, score.total,
    )
    return SignalReport(
        symbol=symbol, score=score.total, qty=size.qty,
        entry=entry, stop=stop, take_profit=tp, order_id=order.id,
    )


def _manage_positions(
    ctx: ScanContext,
    candles_by_symbol: dict[str, list],
    ts: datetime,
    trace_id: str,
) -> list[ExitReport]:
    exits: list[ExitReport] = []
    for pos in list(ctx.broker.get_positions()):
        candles = candles_by_symbol.get(pos.symbol)
        if not candles:
            continue
        df = _candles_to_df(candles)
        if len(df) < MIN_LOOKBACK_BARS:
            continue
        atr_series = ind.atr(df["high"], df["low"], df["close"])
        atr = float(atr_series.iloc[-1])
        if atr <= 0:
            continue
        last = candles[-1]
        current_price = float(last.close)

        # 1. Hard stop / take-profit / trail-stop check against candle range.
        exit_reason = _exit_triggered(pos, last)
        if exit_reason:
            report = _close_position(ctx, pos, exit_reason, trace_id, ts=ts)
            if report:
                exits.append(report)
            continue

        # 2. Update trailing stop (ratchet only).
        mult = trailing_multiplier(atr_series, ctx.settings.risk)
        new_trail = update_trail_stop(pos, current_price, atr, mult)
        if new_trail is not None and new_trail != pos.trail_stop:
            ctx.broker.set_position_stops(pos.symbol, trail_stop=new_trail)

        # 3. Defensive: if the position has no stop_loss (crash between
        #    fill and attach), set one from current ATR.
        if pos.stop_loss is None:
            side = Side.BUY if pos.qty > 0 else Side.SELL
            recomputed = atr_stop_price(
                pos.avg_price, atr, ctx.settings.risk.stop_atr_multiplier, side,
            )
            ctx.broker.set_position_stops(pos.symbol, stop_loss=recomputed)
            logger.warning(
                "[{}] {} missing stop_loss — rebuilt from ATR → {:.2f}",
                trace_id, pos.symbol, recomputed,
            )

        # 4. Time stop.
        decision = check_time_stop(pos, current_price, atr, ts, ctx.settings.risk)
        if decision.close_now:
            report = _close_position(ctx, pos, "time_stop", trace_id, ts=ts)
            if report:
                exits.append(report)
    return exits


def _exit_triggered(pos: Position, candle) -> str | None:
    """Did the candle range touch any protective level? Returns the
    reason label or None."""
    if pos.qty > 0:  # long
        if pos.stop_loss is not None and candle.low <= pos.stop_loss:
            return "stop_loss"
        if pos.trail_stop is not None and candle.low <= pos.trail_stop:
            return "trail_stop"
        if pos.take_profit is not None and candle.high >= pos.take_profit:
            return "take_profit"
    else:  # short
        if pos.stop_loss is not None and candle.high >= pos.stop_loss:
            return "stop_loss"
        if pos.trail_stop is not None and candle.high >= pos.trail_stop:
            return "trail_stop"
        if pos.take_profit is not None and candle.low <= pos.take_profit:
            return "take_profit"
    return None


def _close_position(
    ctx: ScanContext, pos: Position, reason: str, trace_id: str,
    ts: datetime | None = None,
) -> ExitReport | None:
    side = Side.SELL if pos.qty > 0 else Side.BUY
    qty = abs(pos.qty)
    try:
        order = ctx.broker.place_order(
            pos.symbol, qty, side, OrderType.MARKET,
            intent="exit", ts=ts,
        )
    except Exception as exc:  # pragma: no cover — broker failure
        logger.error("[{}] close {} failed: {}", trace_id, pos.symbol, exc)
        return None
    logger.info(
        "[{}] EXIT {} reason={} qty={} order={}",
        trace_id, pos.symbol, reason, qty, order.id,
    )
    return ExitReport(symbol=pos.symbol, reason=reason, order_id=order.id)


def _squareoff_all(
    ctx: ScanContext,
    ts: datetime,
    trace_id: str,
    reason: str = "eod_squareoff",
) -> list[ExitReport]:
    """Close every open position with an exit-intent MARKET order.

    ``reason`` is passed through to the ``ExitReport`` so the dashboard
    / audit trail distinguishes EOD square-offs from kill-switch
    square-offs from drawdown-circuit square-offs.
    """
    exits: list[ExitReport] = []
    for pos in list(ctx.broker.get_positions()):
        report = _close_position(ctx, pos, reason, trace_id, ts=ts)
        if report:
            exits.append(report)
    return exits


def _refresh_ltps(ctx: ScanContext, trace_id: str) -> None:
    """Paused-mode heartbeat — fetch the last candle for every open
    position + every universe symbol, feed the closes into the broker's
    mark-to-market so LTP / equity curve stay fresh on the dashboard.
    No ``settle``, so no pending order fills; no management, so no
    exit orders. True observe-only tick."""
    symbols = set(ctx.universe) | {p.symbol for p in ctx.broker.get_positions()}
    if not symbols:
        return
    ltps: dict[str, float] = {}
    for sym in symbols:
        try:
            candles = ctx.broker.get_candles(
                sym, ctx.settings.strategy.candle_interval, lookback=1,
            )
        except Exception as exc:
            logger.warning("[{}] paused-mode fetch {} failed: {}", trace_id, sym, exc)
            continue
        if candles:
            ltps[sym] = candles[-1].close
    if ltps:
        ctx.broker.mark_to_market(ltps)


# ---------------------------------------------------------------- #
# APScheduler production wrapper                                    #
# ---------------------------------------------------------------- #

def run_scan_loop(ctx: ScanContext) -> None:
    """Production entry point. Blocks forever; Ctrl-C to stop.

    Uses APScheduler's ``BlockingScheduler`` with an ``IntervalTrigger``
    keyed off ``settings.strategy.scan_interval_seconds`` (default 300s =
    5 minutes). Tests use ``run_tick`` directly — no scheduler needed.
    """
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    logger.info(
        "Scan loop starting | mode={} broker={} universe={} interval={}s",
        ctx.settings.mode,
        ctx.settings.broker,
        len(ctx.universe),
        ctx.settings.strategy.scan_interval_seconds,
    )

    scheduler = BlockingScheduler(timezone=str(now_ist().tzinfo))
    scheduler.add_job(
        lambda: _safe_run_tick(ctx),
        IntervalTrigger(seconds=ctx.settings.strategy.scan_interval_seconds),
        next_run_time=now_ist(),
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        logger.info("Scan loop interrupted — shutting down")
        scheduler.shutdown()


def _safe_run_tick(ctx: ScanContext) -> None:
    """Wrap ``run_tick`` so a bug in one tick doesn't kill the scheduler."""
    try:
        run_tick(ctx)
    except Exception as exc:  # pragma: no cover
        logger.exception("scan tick crashed: {}", exc)
