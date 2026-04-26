"""Paper-mode order lifecycle + fill simulator.

The OrderManager owns:

* The in-memory view of pending orders (synced to SQLite).
* The in-memory view of open positions (synced to SQLite).
* Running cash balance.

``PaperBroker`` composes an OrderManager and delegates to it. Live
brokers (UpstoxBroker, later) do NOT use this class — the exchange
handles fills for real money. Instead they reuse ``StateStore`` to
record the fills the exchange reports back.

Fill model (paper mode only):
  * MARKET orders fill on the NEXT ``settle(symbol, candle)`` call at
    ``candle.open * (1 ± slippage_pct/100)``. Sign depends on side.
  * LIMIT / SL / SL-M: a first-cut implementation. LIMIT fills when
    the candle's range crosses the limit price. SL / SL-M fill when
    the candle's range touches the trigger. Partial fills are not
    simulated — every fill is all-or-nothing at one price.

Cash accounting is flat — BUY debits ``fill_price * qty``, SELL credits
the same. F&O margin is a deliberate simplification and will be wired
up alongside the F&O deliverable.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger

from brokers.base import Candle, Order, OrderType, Position, Side
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")

OPEN_STATUSES = {"PENDING"}


class InsufficientFundsError(Exception):
    """Raised when a BUY order would drive cash below zero."""


class OrderManager:
    def __init__(
        self,
        store: StateStore,
        starting_cash: float,
        slippage_pct: float = 0.05,
    ) -> None:
        self.store = store
        self.slippage_pct = slippage_pct

        # In-memory caches, re-populated from SQLite on construction.
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.cash: float = starting_cash
        self._recover(starting_cash)

    # ------------------------------------------------------------------ #
    # Recovery                                                            #
    # ------------------------------------------------------------------ #

    def _recover(self, starting_cash: float) -> None:
        """Load pending orders + open positions from SQLite.

        Cash is derived from starting_cash and the sum of filled BUY/SELL
        flows — reconstructing from the orders log is the only way to
        keep cash consistent across restarts without a separate
        ``cash_ledger`` table.
        """
        all_orders = self.store.load_orders()
        for o in all_orders:
            if o.status in OPEN_STATUSES:
                self.orders[o.id] = o

        for p in self.store.load_positions():
            self.positions[p.symbol] = p

        # Replay filled orders to derive current cash.
        cash = starting_cash
        for o in all_orders:
            if o.status != "FILLED":
                continue
            flow = o.avg_price * o.filled_qty
            cash += -flow if o.side == Side.BUY else flow
        self.cash = cash

        if all_orders or self.positions:
            logger.info(
                "OrderManager recovered {} pending orders, {} open positions, cash=₹{:,.2f}",
                len(self.orders),
                len(self.positions),
                self.cash,
            )

    # ------------------------------------------------------------------ #
    # Order lifecycle                                                     #
    # ------------------------------------------------------------------ #

    def submit(
        self,
        symbol: str,
        qty: int,
        side: Side,
        order_type: OrderType,
        *,
        price: float | None = None,
        trigger_price: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")

        order = Order(
            id=uuid.uuid4().hex,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            status="PENDING",
            filled_qty=0,
            avg_price=0.0,
            ts=ts or datetime.now(IST),
        )
        self.orders[order.id] = order
        self.store.save_order(order)
        self.store.append_audit(
            "order_submitted",
            order_id=order.id,
            symbol=symbol,
            details={
                "side": side.value, "qty": qty, "order_type": order_type.value,
                "price": price, "trigger_price": trigger_price,
            },
        )
        return order

    def cancel(self, order_id: str) -> bool:
        order = self.orders.get(order_id)
        if order is None or order.status != "PENDING":
            return False
        order = replace(order, status="CANCELLED")
        self.orders.pop(order_id, None)
        self.store.update_order_status(order_id, "CANCELLED")
        self.store.append_audit("order_cancelled", order_id=order_id, symbol=order.symbol)
        return True

    def modify(self, order_id: str, **changes: object) -> Order:
        order = self.orders.get(order_id)
        if order is None or order.status != "PENDING":
            raise KeyError(f"no pending order {order_id!r}")
        allowed = {"price", "trigger_price", "qty"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"cannot modify fields: {sorted(unknown)}")
        new = replace(order, **changes)  # type: ignore[arg-type]
        self.orders[order_id] = new
        self.store.save_order(new)
        self.store.append_audit(
            "order_modified", order_id=order_id, symbol=new.symbol,
            details={k: v for k, v in changes.items()},
        )
        return new

    # ------------------------------------------------------------------ #
    # Settlement                                                          #
    # ------------------------------------------------------------------ #

    def settle_on_candle(self, symbol: str, candle: Candle) -> list[Order]:
        """Fill any pending orders for this symbol using this candle.

        MARKET   → candle.open ± slippage.
        LIMIT    → candle touched the limit price (low ≤ limit ≤ high).
                   Fill at limit.
        SL / SL-M→ candle touched the trigger (low ≤ trigger ≤ high).
                   Fill at trigger + slippage.
        """
        filled: list[Order] = []
        for order in list(self.orders.values()):
            if order.symbol != symbol or order.status != "PENDING":
                continue
            fill_price = self._resolve_fill_price(order, candle)
            if fill_price is None:
                continue
            try:
                filled.append(self._fill(order, fill_price, candle.ts))
            except InsufficientFundsError as exc:
                # _fill already wrote REJECTED + audit row; log and skip
                # so other orders for this symbol still get a chance.
                from loguru import logger
                logger.warning("settle skip {} ({})", order.symbol, exc)
                continue
        return filled

    def _resolve_fill_price(self, order: Order, candle: Candle) -> float | None:
        slip_sign = 1 if order.side == Side.BUY else -1
        slip_factor = 1 + slip_sign * self.slippage_pct / 100.0

        if order.order_type == OrderType.MARKET:
            return candle.open * slip_factor
        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise ValueError(f"LIMIT order {order.id} missing price")
            if candle.low <= order.price <= candle.high:
                return order.price
            return None
        if order.order_type in (OrderType.SL, OrderType.SL_M):
            if order.trigger_price is None:
                raise ValueError(f"{order.order_type} order {order.id} missing trigger_price")
            if candle.low <= order.trigger_price <= candle.high:
                return order.trigger_price * slip_factor
            return None
        raise ValueError(f"unsupported order_type {order.order_type}")

    def _fill(self, order: Order, fill_price: float, filled_at: datetime) -> Order:
        # Cash check for BUY orders.
        if order.side == Side.BUY and fill_price * order.qty > self.cash + 1e-6:
            self.store.update_order_status(order.id, "REJECTED")
            self.store.append_audit(
                "order_rejected",
                order_id=order.id, symbol=order.symbol,
                details={"reason": "insufficient_funds", "cash": self.cash},
            )
            self.orders.pop(order.id, None)
            raise InsufficientFundsError(
                f"order {order.id}: need ₹{fill_price * order.qty:,.2f}, have ₹{self.cash:,.2f}"
            )

        # Apply cash flow.
        if order.side == Side.BUY:
            self.cash -= fill_price * order.qty
        else:
            self.cash += fill_price * order.qty

        # Update position.
        self._apply_to_position(order, fill_price, filled_at)

        filled = replace(
            order, status="FILLED", filled_qty=order.qty, avg_price=fill_price
        )
        self.orders.pop(order.id, None)
        self.store.update_order_status(
            order.id, "FILLED",
            filled_qty=order.qty, avg_price=fill_price, filled_at=filled_at,
        )
        self.store.append_audit(
            "order_filled",
            order_id=order.id, symbol=order.symbol,
            details={"fill_price": fill_price, "qty": order.qty, "side": order.side.value},
        )
        logger.info(
            "FILL {} {} {}@{:.2f}",
            order.side.value, order.symbol, order.qty, fill_price,
        )
        return filled

    def _apply_to_position(
        self, order: Order, fill_price: float, filled_at: datetime
    ) -> None:
        delta = order.qty if order.side == Side.BUY else -order.qty
        existing = self.positions.get(order.symbol)

        if existing is None:
            new_pos = Position(
                symbol=order.symbol,
                qty=delta,
                avg_price=fill_price,
                opened_at=filled_at,
            )
            self.positions[order.symbol] = new_pos
            self.store.save_position(new_pos)
            return

        new_qty = existing.qty + delta
        if new_qty == 0:
            # Fully flat. Drop the row.
            self.positions.pop(order.symbol, None)
            self.store.delete_position(order.symbol)
            return

        # Adding to an existing position → recalc weighted-average price.
        # Reducing (or flipping) → keep avg_price until flat / flipped.
        if (existing.qty > 0 and delta > 0) or (existing.qty < 0 and delta < 0):
            # Same direction, averaging in.
            total_cost = existing.avg_price * abs(existing.qty) + fill_price * abs(delta)
            new_avg = total_cost / abs(new_qty)
            updated = replace(existing, qty=new_qty, avg_price=new_avg)
        elif (existing.qty > 0 and new_qty < 0) or (existing.qty < 0 and new_qty > 0):
            # Flipped side in one fill — reopen at fill_price.
            updated = replace(
                existing, qty=new_qty, avg_price=fill_price, opened_at=filled_at,
            )
        else:
            # Reducing, still on the same side. Keep avg_price, update qty.
            updated = replace(existing, qty=new_qty)

        self.positions[order.symbol] = updated
        self.store.save_position(updated)

    # ------------------------------------------------------------------ #
    # Mark-to-market                                                      #
    # ------------------------------------------------------------------ #

    def mark_to_market(self, prices: dict[str, float]) -> None:
        """Update in-memory LTP on every position that has a fresh price."""
        for sym, ltp in prices.items():
            pos = self.positions.get(sym)
            if pos is not None:
                self.positions[sym] = replace(pos, ltp=ltp)

    def total_pnl(self) -> float:
        return sum(p.pnl for p in self.positions.values())

    def equity(self) -> float:
        used = sum(abs(p.qty) * p.avg_price for p in self.positions.values())
        return self.cash + used + self.total_pnl()

    def snapshot_equity(self, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(IST)
        pnl = self.total_pnl()
        used = sum(abs(p.qty) * p.avg_price for p in self.positions.values())
        self.store.snapshot_equity(ts, self.cash + used + pnl, self.cash, pnl)
