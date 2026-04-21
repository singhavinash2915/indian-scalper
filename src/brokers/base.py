"""Broker abstract + shared domain types.

Moved verbatim from ``bootstrap.py`` so every module that talks to a broker
(paper, Upstox, future Zerodha) lines up on the same contract and types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo


# ----------------------------------------------------------------------------
# Enums
# ----------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class Segment(str, Enum):
    EQUITY = "EQ"
    FUTURES = "FUT"
    OPTIONS = "OPT"


# ----------------------------------------------------------------------------
# Value objects
# ----------------------------------------------------------------------------

@dataclass
class Instrument:
    """An exchange-tradeable symbol. Lot size + tick size come from the
    instrument master, never hardcoded."""

    symbol: str
    exchange: str
    segment: Segment
    tick_size: float = 0.05
    lot_size: int = 1
    expiry: datetime | None = None
    strike: float | None = None
    option_type: Literal["CE", "PE"] | None = None


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Order:
    id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    price: float | None = None
    trigger_price: float | None = None
    status: str = "PENDING"
    filled_qty: int = 0
    avg_price: float = 0.0
    ts: datetime = field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Kolkata"))
    )


@dataclass
class Position:
    symbol: str
    qty: int  # +long, -short
    avg_price: float
    ltp: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    trail_stop: float | None = None
    opened_at: datetime | None = None

    @property
    def pnl(self) -> float:
        return (self.ltp - self.avg_price) * self.qty

    @property
    def pnl_pct(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return (self.ltp - self.avg_price) / self.avg_price * 100


# ----------------------------------------------------------------------------
# Broker contract
# ----------------------------------------------------------------------------

class BrokerBase(ABC):
    """Every broker implementation — paper, Upstox, later Zerodha —
    implements exactly this surface. Nothing else."""

    @abstractmethod
    def get_instruments(self) -> list[Instrument]: ...

    @abstractmethod
    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]: ...

    @abstractmethod
    def get_ltp(self, symbols: list[str]) -> dict[str, float]: ...

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        qty: int,
        side: Side,
        order_type: OrderType,
        price: float | None = None,
        trigger_price: float | None = None,
        *,
        intent: Literal["entry", "exit"] = "entry",
    ) -> Order:
        """Submit an order.

        ``intent`` tells the broker whether the order opens new risk
        (``"entry"``) or reduces existing risk (``"exit"``). In
        ``watch_only`` trade mode the broker blocks ``"entry"`` orders
        unconditionally but still honours ``"exit"`` so stops, trailing
        stops, and EOD square-off can drain existing positions safely.
        Default is ``"entry"`` — errs on the side of blocking when the
        caller forgets to annotate.
        """
        ...

    @abstractmethod
    def modify_order(self, order_id: str, **kwargs: object) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_funds(self) -> dict[str, float]:
        """Returns {'available': ..., 'used': ..., 'equity': ...} in INR."""
