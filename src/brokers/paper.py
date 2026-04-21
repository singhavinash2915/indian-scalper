"""In-memory paper broker that simulates fills at next-candle-open + slippage.

Composes three collaborators:

* ``StateStore`` (``src/execution/state.py``) — SQLite persistence.
* ``OrderManager`` (``src/execution/order_manager.py``) — pending-order
  queue, fill simulation, cash accounting, position tracking.
* ``CandleFetcher`` (``src/data/market_data.py``) — candle source.
  Defaults to ``YFinanceFetcher``; tests/backtests inject a
  ``FakeCandleFetcher``.

The settle / mark-to-market / kill-switch surface is what the scan loop
(Deliverable 6) will drive.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

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
from data.instruments import InstrumentMaster
from data.market_data import CandleFetcher, YFinanceFetcher
from execution.order_manager import OrderManager
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")


class PaperBroker(BrokerBase):
    def __init__(
        self,
        settings: Settings,
        db_path: str | Path | None = None,
        candle_fetcher: CandleFetcher | None = None,
        instruments: InstrumentMaster | None = None,
    ) -> None:
        self.settings = settings

        # Resolve paths / defaults from config.yaml ``storage`` block.
        storage_cfg = settings.raw.get("storage", {})
        self._db_path = Path(db_path or storage_cfg.get("db_path", "data/scalper.db"))
        self._candles_cache_dir = Path(
            storage_cfg.get("candles_cache_dir", "data/candles")
        )

        paper_cfg = settings.raw.get("paper", {})
        self.slippage_pct: float = paper_cfg.get("slippage_pct", 0.05)

        # Collaborators.
        self.store = StateStore(self._db_path)
        self.om = OrderManager(
            self.store,
            starting_cash=settings.capital.starting_inr,
            slippage_pct=self.slippage_pct,
        )
        self.fetcher: CandleFetcher = candle_fetcher or _default_fetcher()
        self.instruments = instruments or InstrumentMaster(
            db_path=self._db_path,
            cache_dir=self._db_path.parent / "instruments",
        )

        # Running LTP cache — updated by settle() + mark_to_market().
        self._ltp: dict[str, float] = {sym: p.ltp for sym, p in self.om.positions.items()}

        logger.info(
            "PaperBroker ready | starting_cash=₹{:,.0f} db={} fetcher={}",
            self.om.cash, self._db_path, type(self.fetcher).__name__,
        )

    # ------------------------------------------------------------------ #
    # Convenience properties                                              #
    # ------------------------------------------------------------------ #

    @property
    def cash(self) -> float:
        return self.om.cash

    @property
    def orders(self) -> dict[str, Order]:
        return self.om.orders

    @property
    def positions(self) -> dict[str, Position]:
        return self.om.positions

    # ------------------------------------------------------------------ #
    # BrokerBase: reference / reads                                       #
    # ------------------------------------------------------------------ #

    def get_instruments(self) -> list[Instrument]:
        return self.instruments.filter()

    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]:
        return self.fetcher.get_candles(symbol, interval, lookback)

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in symbols:
            cached = self._ltp.get(sym)
            if cached is not None:
                out[sym] = cached
                continue
            # Cold read — pull the last closed candle and seed the cache.
            candles = self.fetcher.get_candles(
                sym, self.settings.strategy.candle_interval, lookback=1
            )
            if candles:
                out[sym] = candles[-1].close
                self._ltp[sym] = candles[-1].close
        return out

    # ------------------------------------------------------------------ #
    # BrokerBase: order lifecycle                                         #
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        symbol: str,
        qty: int,
        side: Side,
        order_type: OrderType,
        price: float | None = None,
        trigger_price: float | None = None,
        ts: datetime | None = None,
    ) -> Order:
        """Paper-specific extension over ``BrokerBase.place_order``: accepts
        an optional ``ts`` so backtest and dry-run drivers can pin the
        order's timestamp to the simulated tick time. Falls back to
        ``datetime.now(IST)`` when omitted (live-ish paper mode)."""
        return self.om.submit(
            symbol=symbol, qty=qty, side=side, order_type=order_type,
            price=price, trigger_price=trigger_price, ts=ts,
        )

    def modify_order(self, order_id: str, **kwargs: object) -> Order:
        return self.om.modify(order_id, **kwargs)

    def cancel_order(self, order_id: str) -> bool:
        return self.om.cancel(order_id)

    # ------------------------------------------------------------------ #
    # Paper-specific simulation hooks                                     #
    # ------------------------------------------------------------------ #

    def settle(self, symbol: str, candle: Candle) -> list[Order]:
        """Advance the simulation for this symbol by one candle.

        Fills any pending orders against ``candle``, updates the LTP
        cache with ``candle.close``, and records an equity snapshot.
        The scan loop (Deliverable 6) calls this for every symbol it
        fetches a fresh bar for.
        """
        filled = self.om.settle_on_candle(symbol, candle)
        self._ltp[symbol] = candle.close
        self.om.mark_to_market(self._ltp)
        self.om.snapshot_equity(candle.ts)
        return filled

    def mark_to_market(self, prices: dict[str, float]) -> None:
        self._ltp.update(prices)
        self.om.mark_to_market(self._ltp)
        self.om.snapshot_equity()

    def set_position_stops(
        self,
        symbol: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        trail_stop: float | None = None,
    ) -> None:
        """Attach or update protective levels on an existing position.

        The scan loop calls this right after an entry fills — at that
        point it has the ATR-derived stop / take-profit from sizing but
        the position was just created without them. No-op if the symbol
        isn't in the position book.
        """
        from dataclasses import replace

        pos = self.om.positions.get(symbol)
        if pos is None:
            return
        updated = replace(
            pos,
            stop_loss=stop_loss if stop_loss is not None else pos.stop_loss,
            take_profit=take_profit if take_profit is not None else pos.take_profit,
            trail_stop=trail_stop if trail_stop is not None else pos.trail_stop,
        )
        self.om.positions[symbol] = updated
        self.store.save_position(updated)

    # ------------------------------------------------------------------ #
    # BrokerBase: portfolio reads                                         #
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Position]:
        return list(self.om.positions.values())

    def get_funds(self) -> dict[str, float]:
        used = sum(abs(p.qty) * p.avg_price for p in self.om.positions.values())
        pnl = self.om.total_pnl()
        return {
            "available": self.om.cash,
            "used": used,
            "equity": self.om.cash + used + pnl,
        }

    # ------------------------------------------------------------------ #
    # Kill switch — writes a KV flag that the scan loop polls every tick  #
    # ------------------------------------------------------------------ #

    def set_kill_switch(self, on: bool = True) -> None:
        self.store.set_flag("kill_switch", "1" if on else "0")

    def is_kill_switch_on(self) -> bool:
        return self.store.get_flag("kill_switch", "0") == "1"


def _default_fetcher() -> CandleFetcher:
    """Deferred construction so importing PaperBroker doesn't force a
    yfinance install on systems that only ever run tests."""
    return YFinanceFetcher()
