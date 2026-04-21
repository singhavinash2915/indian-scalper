"""Broker abstraction + implementations.

Strategy and risk code MUST only depend on ``BrokerBase`` — never import
broker SDKs directly. This keeps the paper → Upstox swap a one-line config
change.
"""

from brokers.base import (
    BrokerBase,
    Candle,
    Instrument,
    Order,
    OrderType,
    Position,
    Segment,
    Side,
)
from brokers.paper import PaperBroker
from brokers.upstox import UpstoxBroker

__all__ = [
    "BrokerBase",
    "Candle",
    "Instrument",
    "Order",
    "OrderType",
    "PaperBroker",
    "Position",
    "Segment",
    "Side",
    "UpstoxBroker",
]
