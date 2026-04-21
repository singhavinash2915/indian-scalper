"""Trade extraction from the orders table.

Walks filled orders in timestamp order and pairs BUYs with SELLs
(FIFO) to reconstruct closed round-trip trades. Partial closes are
supported — one BUY can produce multiple Trade rows as a SELL chips
away at its open lots.

Long-only for now. Short trades (SELL-to-open) are ignored; when the
scan loop adds short signals in a future deliverable, the logic here
will need to grow the mirror case.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from brokers.base import Order, Side


@dataclass(frozen=True)
class Trade:
    """A closed round-trip — opened by one order, closed by (part of)
    another. ``qty`` is the matched quantity, not the original order size."""

    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    qty: int
    side: Side
    pnl: float
    pnl_pct: float

    @property
    def holding_minutes(self) -> float:
        return (self.exit_ts - self.entry_ts).total_seconds() / 60.0

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


def extract_trades(orders: Iterable[Order]) -> list[Trade]:
    """FIFO-match BUY/SELL orders into closed Trades.

    A BUY opens or adds to a long lot queue per symbol. A SELL
    consumes lots in the order they were opened, emitting one Trade
    per consumed lot. Any remaining open BUY lots at the end of the
    series represent still-open positions — not reported as trades.
    """
    open_lots: dict[str, deque[tuple[Order, int]]] = {}
    trades: list[Trade] = []

    for o in sorted((o for o in orders if o.status == "FILLED"), key=lambda x: x.ts):
        queue = open_lots.setdefault(o.symbol, deque())
        if o.side == Side.BUY:
            queue.append((o, o.filled_qty))
            continue

        # SELL — consume FIFO.
        remaining = o.filled_qty
        while remaining > 0 and queue:
            entry_order, lot_qty = queue[0]
            match = min(remaining, lot_qty)
            pnl = (o.avg_price - entry_order.avg_price) * match
            pnl_pct = (
                (o.avg_price - entry_order.avg_price) / entry_order.avg_price * 100.0
                if entry_order.avg_price > 0 else 0.0
            )
            trades.append(
                Trade(
                    symbol=o.symbol,
                    entry_ts=entry_order.ts,
                    exit_ts=o.ts,
                    entry_price=entry_order.avg_price,
                    exit_price=o.avg_price,
                    qty=match,
                    side=Side.BUY,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                )
            )
            remaining -= match
            lot_qty -= match
            if lot_qty == 0:
                queue.popleft()
            else:
                queue[0] = (entry_order, lot_qty)
        # If `remaining > 0` and queue emptied, we're seeing a
        # SELL-to-open (short). Ignored for now — see module docstring.

    return trades
