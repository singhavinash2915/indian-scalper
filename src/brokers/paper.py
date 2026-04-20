"""In-memory paper broker that simulates fills at next-candle-open + slippage.

This is a *scaffold only* — all abstract methods remain ``NotImplementedError``
stubs. Deliverable 4 fleshes out order placement, SQLite persistence, and
idempotent recovery. The constructor + SQLite schema live here so tests can
instantiate the broker and later deliverables just fill in behaviour.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from brokers.base import (
    BrokerBase,
    Candle,
    Instrument,
    Order,
    OrderType,
    Position,
    Side,
)
from config.settings import Settings


class PaperBroker(BrokerBase):
    """In-memory paper broker. State persisted to SQLite for restart safety."""

    def __init__(self, settings: Settings, db_path: str | None = None) -> None:
        self.settings = settings
        self.cash: float = settings.capital.starting_inr
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}

        paper_cfg = settings.raw.get("paper", {})
        self.slippage_pct: float = paper_cfg.get("slippage_pct", 0.05)

        storage_cfg = settings.raw.get("storage", {})
        self._db_path = db_path or storage_cfg.get("db_path", "data/scalper.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, symbol TEXT, side TEXT, qty INTEGER,
                    order_type TEXT, price REAL, trigger_price REAL,
                    status TEXT, filled_qty INTEGER, avg_price REAL, ts TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY, qty INTEGER, avg_price REAL,
                    stop_loss REAL, take_profit REAL, trail_stop REAL,
                    opened_at TEXT
                );
                CREATE TABLE IF NOT EXISTS equity_curve (
                    ts TEXT PRIMARY KEY, equity REAL, cash REAL, pnl REAL
                );
                """
            )

    # ------------------------------------------------------------------ #
    # BrokerBase contract — stubs. Implemented in Deliverable 4.          #
    # ------------------------------------------------------------------ #

    def get_instruments(self) -> list[Instrument]:
        raise NotImplementedError("Load from cached NSE master CSV (Deliverable 2).")

    def get_candles(self, symbol: str, interval: str, lookback: int) -> list[Candle]:
        raise NotImplementedError(
            "Fetch historical candles from Upstox public API or yfinance "
            "(.NS suffix) and cache to data/candles/ (Deliverable 2)."
        )

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        raise NotImplementedError("Return last close of most recent candle (Deliverable 2).")

    def place_order(
        self,
        symbol: str,
        qty: int,
        side: Side,
        order_type: OrderType,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> Order:
        raise NotImplementedError(
            "Create Order, persist to sqlite, fill at next candle open + "
            "slippage. Update self.cash and self.positions (Deliverable 4)."
        )

    def modify_order(self, order_id: str, **kwargs: object) -> Order:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Already functional                                                  #
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    def get_funds(self) -> dict[str, float]:
        used = sum(abs(p.qty) * p.avg_price for p in self.positions.values())
        pnl = sum(p.pnl for p in self.positions.values())
        return {
            "available": self.cash,
            "used": used,
            "equity": self.cash + used + pnl,
        }
