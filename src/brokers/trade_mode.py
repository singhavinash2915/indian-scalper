"""Trade-mode enforcement shared between PaperBroker and UpstoxBroker.

The trade mode is a single control_flags entry that the broker reads at
*every* ``place_order`` call — deliberately not cached, so a UI flip
takes effect on the next call without any scheduler/broker restart.

Semantics:
  * ``watch_only`` — scoring runs and snapshots are written, but no
    orders that *open* risk are placed. Exits (stop, trail, EOD
    square-off, manual close) are still allowed so flipping to
    watch_only mid-day doesn't strand open positions.
  * ``paper``      — full paper trading.
  * ``live``       — full live trading (UpstoxBroker only). Requires
    ``LIVE_TRADING_ACKNOWLEDGED=yes`` env var before the mode even
    flips, plus a one-time UI confirmation (dashboard).

This module is broker-agnostic: both brokers call
``check_and_maybe_reject`` at the top of ``place_order``. If it returns
a non-None ``Order``, that's the rejection object the caller returns
verbatim — no raising, so the scheduler never sees an exception just
because the operator flipped to watch_only mid-tick.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from loguru import logger

from brokers.base import Order, OrderType, Side

if TYPE_CHECKING:
    from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")

TradeMode = Literal["watch_only", "paper", "live"]

REJECTED_BY_TRADE_MODE = "REJECTED_BY_TRADE_MODE"
VALID_TRADE_MODES: tuple[TradeMode, ...] = ("watch_only", "paper", "live")
DEFAULT_TRADE_MODE: TradeMode = "watch_only"

LIVE_ACK_ENV = "LIVE_TRADING_ACKNOWLEDGED"


def current_trade_mode(store: StateStore) -> TradeMode:
    """Read the effective trade mode from control_flags. Falls back to
    ``watch_only`` if the flag hasn't been seeded yet — safer default
    than blindly trading."""
    raw = store.get_flag("trade_mode", DEFAULT_TRADE_MODE)
    if raw in VALID_TRADE_MODES:
        return raw  # type: ignore[return-value]
    logger.warning(
        "Unknown trade_mode {!r} in control_flags; treating as watch_only", raw,
    )
    return "watch_only"


def live_trading_acknowledged() -> bool:
    """Env-var gate for the ``live`` mode — checked at mode-change time
    (UI refuses to even issue a confirm token without it). The broker
    itself does not re-read this at every ``place_order`` call; once
    live is committed to control_flags, the scheduler trusts it."""
    return os.environ.get(LIVE_ACK_ENV, "").strip().lower() == "yes"


def check_and_maybe_reject(
    store: StateStore,
    symbol: str,
    qty: int,
    side: Side,
    order_type: OrderType,
    intent: Literal["entry", "exit"],
    broker_name: str,
) -> Order | None:
    """Return a REJECTED_BY_TRADE_MODE ``Order`` if trade mode blocks
    this call; ``None`` if the call should proceed.

    Watch-only blocks ``intent="entry"`` unconditionally; exits always
    flow through (stops, trails, EOD).
    """
    mode = current_trade_mode(store)
    if mode != "watch_only":
        return None
    if intent != "entry":
        return None

    rejected_id = f"blocked-{uuid.uuid4().hex[:10]}"
    ts = datetime.now(IST)
    rejected = Order(
        id=rejected_id,
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=order_type,
        price=None,
        trigger_price=None,
        status=REJECTED_BY_TRADE_MODE,
        filled_qty=0,
        avg_price=0.0,
        ts=ts,
    )
    payload = {
        "broker": broker_name,
        "symbol": symbol,
        "qty": qty,
        "side": side.value,
        "order_type": order_type.value,
        "intent": intent,
        "mode": mode,
        "rejected_id": rejected_id,
    }
    # audit — explicit, independent of set_flag's automatic flag_set rows
    store.append_operator_audit(
        "order_blocked_by_trade_mode",
        actor=broker_name,
        payload=payload,
        ts=ts,
    )
    logger.warning(
        "trade_mode={} blocked {} {} {} {}x{} (intent={}) — would-be order id={}",
        mode, broker_name, side.value, order_type.value, symbol, qty,
        intent, rejected_id,
    )
    return rejected
