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
from typing import Literal
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
from brokers.trade_mode import (
    DEFAULT_TRADE_MODE,
    VALID_TRADE_MODES,
    check_and_maybe_reject,
)
from config.settings import Settings
from data.instruments import InstrumentMaster
from data.market_data import CandleFetcher, UpstoxFetcher, YFinanceFetcher
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
        # Fill policy:
        #   live_market       — fetch LTP from the data source at place-time
        #                       and fill IMMEDIATELY (best live-broker parity;
        #                       requires a fetcher that exposes get_ltp()).
        #   next_candle_open  — legacy: queue order, fill on next candle's open.
        self.fill_mode: str = paper_cfg.get("fill_on", "live_market")

        # Collaborators.
        self.store = StateStore(self._db_path)
        _seed_control_flags(self.store, settings)
        self.om = OrderManager(
            self.store,
            starting_cash=settings.capital.starting_inr,
            slippage_pct=self.slippage_pct,
        )
        self.instruments = instruments or InstrumentMaster(
            db_path=self._db_path,
            cache_dir=self._db_path.parent / "instruments",
        )
        self.fetcher: CandleFetcher = candle_fetcher or _default_fetcher(
            settings, instruments=self.instruments,
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
        *,
        intent: Literal["entry", "exit"] = "entry",
        ts: datetime | None = None,
    ) -> Order:
        """Paper-specific extension over ``BrokerBase.place_order``.

        Accepts an optional ``ts`` so backtest and dry-run drivers can
        pin the order's timestamp to the simulated tick time, and an
        ``intent`` kwarg used by trade-mode enforcement. In
        ``trade_mode = watch_only`` an ``intent="entry"`` call returns a
        REJECTED_BY_TRADE_MODE order without touching the order book;
        exits (``intent="exit"``) always flow through.
        """
        rejection = check_and_maybe_reject(
            self.store, symbol, qty, side, order_type, intent, "PaperBroker",
        )
        if rejection is not None:
            return rejection
        order = self.om.submit(
            symbol=symbol, qty=qty, side=side, order_type=order_type,
            price=price, trigger_price=trigger_price, ts=ts,
        )
        # Live-market mode: fill MARKET orders now at real-time LTP + slippage
        # instead of waiting for next_candle_open. Closer to how a real
        # exchange fills MARKET orders.
        if (
            self.fill_mode == "live_market"
            and order.status == "PENDING"
            and order_type == OrderType.MARKET
        ):
            self._try_fill_live(order, ts)
            # On successful fill, _fill() removes from om.orders and persists
            # FILLED state — refresh from SQLite so caller sees final status.
            if order.id not in self.om.orders:
                refreshed = self.store.get_order(order.id)
                if refreshed is not None:
                    order = refreshed
        return order

    def _try_fill_live(self, order: Order, ts: datetime | None = None) -> bool:
        """Fill the given PENDING MARKET order using live LTP + slippage.

        Best-effort: returns False + leaves the order PENDING if no LTP is
        available (backtest fetcher, network hiccup). The next scheduler
        tick's settle() will then fall back to next-candle-open logic.
        """
        symbol = order.symbol
        ltp = self._lookup_live_ltp(symbol)
        if ltp is None or ltp <= 0:
            logger.debug("live fill skipped for {} (no LTP available)", symbol)
            return False
        slip_sign = 1 if order.side == Side.BUY else -1
        fill_price = ltp * (1 + slip_sign * self.slippage_pct / 100.0)
        fill_ts = ts or datetime.now(IST)
        try:
            self.om._fill(order, fill_price, fill_ts)
            self._ltp[symbol] = ltp
        except Exception as exc:
            logger.warning("live fill failed for {}: {}", symbol, exc)
            return False
        return True

    def _lookup_live_ltp(self, symbol: str) -> float | None:
        """Return real-time LTP for ``symbol`` or None if unavailable.

        Requires a fetcher that exposes ``get_ltp``. Backtest / test
        fetchers (FakeCandleFetcher) deliberately fall through to None so
        legacy next-candle-open settlement kicks in on the next tick.
        """
        fetcher = getattr(self, "fetcher", None)
        if fetcher is None or not hasattr(fetcher, "get_ltp"):
            return None
        try:
            prices = fetcher.get_ltp([symbol])
        except Exception as exc:
            logger.debug("get_ltp failed for {}: {}", symbol, exc)
            return None
        ltp = prices.get(symbol)
        if ltp and ltp > 0:
            return float(ltp)
        return None

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

    def refresh_live_ltp(self, symbols: list[str] | None = None) -> dict[str, float]:
        """Pull real-time LTP for the given symbols (default: open positions)
        and mark-to-market. Used by the dashboard to render live P&L
        between scheduler ticks.

        No-op + silent fallback if the fetcher doesn't expose ``get_ltp``
        (e.g. yfinance backend, tests).
        """
        if not hasattr(self.fetcher, "get_ltp"):
            return {}
        targets = symbols if symbols is not None else [p.symbol for p in self.om.positions.values() if p.qty != 0]
        if not targets:
            return {}
        try:
            fresh = self.fetcher.get_ltp(targets)   # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("refresh_live_ltp failed: {}", exc)
            return {}
        if fresh:
            self._ltp.update(fresh)
            self.om.mark_to_market(self._ltp)
        return fresh

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
    # Kill switch — a control_flags entry the scan loop polls every tick. #
    # Values: "armed" (default, trading allowed) | "tripped" (halt).      #
    # ------------------------------------------------------------------ #

    def set_kill_switch(self, on: bool = True, actor: str = "system") -> None:
        self.store.set_flag(
            "kill_switch", "tripped" if on else "armed", actor=actor,
        )

    def is_kill_switch_on(self) -> bool:
        return self.store.get_flag("kill_switch", "armed") == "tripped"

    # ------------------------------------------------------------------ #
    # Options-bucket trading (Phase 3.x)                                  #
    # ------------------------------------------------------------------ #

    @property
    def options_cash(self) -> float:
        """Return cash remaining in the options bucket. Computed from the
        configured starting amount minus net premium-spend on open
        positions (which is just sum of entry premium notionals)."""
        starting = float(self.settings.capital.options_inr or 0.0)
        spent = sum(
            p["entry_premium"] * p["lot_size"] * p["qty_lots"]
            for p in self.store.load_options_positions()
        )
        # Add realised P&L from closed options trades (if any audit row exists).
        realised = self.store.get_flag("options_realised_pnl", "0")
        try:
            realised_f = float(realised or 0)
        except Exception:
            realised_f = 0.0
        return starting + realised_f - spent

    def place_options_order(
        self,
        underlying: str,
        option_type: str,           # CE | PE
        spot_at_entry: float,
        qty_lots: int = 1,
        intent: str = "entry",
        ts: datetime | None = None,
    ) -> dict | None:
        """Buy an ATM monthly option on the given underlying. Resolves the
        contract from cache, fetches premium LTP from Upstox, fills
        immediately at premium + slippage, persists to options_positions.

        Returns the persisted position dict on success, None if the
        signal had to be skipped (no contract / premium too high / no
        cash).  Always-buy semantics — long calls or long puts only.
        """
        from data.options_chain import resolve_atm_option
        from datetime import date as _date

        ts = ts or datetime.now(IST)
        cfg = self.settings.strategy
        contract = resolve_atm_option(
            db_path=self._db_path,
            underlying=underlying,
            side=option_type,
            spot=spot_at_entry,
            today=ts.date() if hasattr(ts, "date") else _date.today(),
            min_days_to_expiry=cfg.options_min_days_to_expiry,
        )
        if contract is None:
            logger.warning(
                "options entry skipped — no contract: {} {} spot={}",
                underlying, option_type, spot_at_entry,
            )
            return None

        # Fetch premium LTP. Use a synthetic key built from the trading
        # symbol — UpstoxFetcher.get_ltp_by_keys speaks raw instrument keys.
        instrument_key = f"NSE_FO|{contract.trading_symbol}"
        premium = 0.0
        if hasattr(self.fetcher, "get_ltp_by_keys"):
            try:
                premiums = self.fetcher.get_ltp_by_keys([instrument_key])
                # The response key is the trading_symbol per Upstox convention
                premium = float(premiums.get(instrument_key) or 0.0)
            except Exception as exc:
                logger.warning("options premium fetch failed: {}", exc)
        if premium <= 0:
            # Fall back to a synthetic ATM premium estimate (~1.5% of spot)
            # for paper-mode testing when live LTP isn't available.
            premium = spot_at_entry * 0.015
            logger.info(
                "options entry using synthetic premium ₹{:.2f} (~1.5% of spot)",
                premium,
            )

        # Premium-cap guard (per-lot ceiling).
        per_lot_cost = premium * contract.lot_size
        if per_lot_cost > cfg.options_premium_cap_per_lot:
            logger.warning(
                "options entry skipped — premium ₹{:.0f}/lot > cap ₹{:.0f}",
                per_lot_cost, cfg.options_premium_cap_per_lot,
            )
            return None

        # Bucket cash check.
        slip = 1 + self.slippage_pct / 100.0   # buys pay slightly more
        fill_premium = premium * slip
        cost = fill_premium * contract.lot_size * qty_lots
        if cost > self.options_cash + 1e-6:
            logger.warning(
                "options entry skipped — cost ₹{:.0f} > options bucket ₹{:.0f}",
                cost, self.options_cash,
            )
            return None

        # Persist position.
        pos = {
            "contract_key": contract.trading_symbol,
            "underlying": underlying,
            "option_type": option_type,
            "strike": contract.strike,
            "expiry": contract.expiry.isoformat(),
            "lot_size": contract.lot_size,
            "qty_lots": qty_lots,
            "entry_premium": fill_premium,
            "entry_spot": spot_at_entry,
            "high_water_premium": fill_premium,
            "breakeven_locked": 0,
            "opened_at": ts.isoformat(),
            "last_premium": fill_premium,
        }
        self.store.upsert_options_position(**pos)
        order_id = f"opt_{contract.trading_symbol}_{int(ts.timestamp())}"
        self.store.append_options_order(
            id=order_id, contract_key=contract.trading_symbol,
            underlying=underlying, side="BUY", qty_lots=qty_lots,
            status="FILLED", avg_premium=fill_premium, intent=intent,
            ts=ts.isoformat(), filled_at=ts.isoformat(),
        )
        logger.info(
            "OPTIONS ENTRY {} {} {:.0f}{} qty={}lots premium=₹{:.2f} cost=₹{:,.0f}",
            underlying, contract.expiry, contract.strike, option_type,
            qty_lots, fill_premium, cost,
        )
        return pos

    def close_options_position(
        self,
        contract_key: str,
        exit_premium: float,
        reason: str,
        ts: datetime | None = None,
    ) -> dict | None:
        """Square off an options position. Records realised P&L to the
        options bucket via the realised-pnl flag. Returns the closed
        position (with realised_pnl filled in) or None if not open."""
        ts = ts or datetime.now(IST)
        positions = {p["contract_key"]: p for p in self.store.load_options_positions()}
        pos = positions.get(contract_key)
        if pos is None:
            return None
        slip = 1 - self.slippage_pct / 100.0   # sells receive slightly less
        sell_premium = max(0.0, exit_premium * slip)
        gross = (sell_premium - pos["entry_premium"]) * pos["lot_size"] * pos["qty_lots"]
        # Update realised pnl flag.
        prev_pnl = float(self.store.get_flag("options_realised_pnl", "0") or 0)
        self.store.set_flag(
            "options_realised_pnl", f"{prev_pnl + gross:.4f}", actor="options_close",
        )
        order_id = f"opt_x_{contract_key}_{int(ts.timestamp())}"
        self.store.append_options_order(
            id=order_id, contract_key=contract_key,
            underlying=pos["underlying"], side="SELL", qty_lots=pos["qty_lots"],
            status="FILLED", avg_premium=sell_premium, intent=f"exit:{reason}",
            ts=ts.isoformat(), filled_at=ts.isoformat(),
        )
        self.store.delete_options_position(contract_key)
        logger.info(
            "OPTIONS EXIT {} reason={} premium=₹{:.2f} → ₹{:.2f} pnl=₹{:+,.0f}",
            contract_key, reason, pos["entry_premium"], sell_premium, gross,
        )
        return {**pos, "exit_premium": sell_premium, "realised_pnl": gross,
                "exit_reason": reason, "closed_at": ts.isoformat()}

    def get_options_positions(self) -> list[dict]:
        return self.store.load_options_positions()


def _default_fetcher(
    settings: Settings, instruments: InstrumentMaster | None = None,
) -> CandleFetcher:
    """Pick the candle backend based on ``data.source`` in config.

    - ``upstox`` (default when UPSTOX_ACCESS_TOKEN is set) → real-time NSE
      via Upstox REST. Requires instrument master for symbol→ISIN lookup.
    - ``yfinance`` → delayed Yahoo feed. Default fallback.

    Deferred construction so importing PaperBroker doesn't force a
    yfinance or httpx import on test-only systems.
    """
    import os
    data_cfg = settings.raw.get("data", {}) or {}
    source = (data_cfg.get("source") or "").strip().lower()

    # ``auto`` (default): Upstox if token present, else yfinance.
    if not source or source == "auto":
        source = "upstox" if os.environ.get("UPSTOX_ACCESS_TOKEN") else "yfinance"

    if source == "upstox":
        try:
            fetcher = UpstoxFetcher(instruments=instruments)
            logger.info("data.source=upstox — real-time NSE feed")
            return fetcher
        except RuntimeError as exc:
            logger.warning("UpstoxFetcher unavailable ({}); falling back to yfinance", exc)
    return YFinanceFetcher()


def _seed_control_flags(store: "StateStore", settings: Settings) -> None:
    """Seed the operator control-plane flags on first broker init.

    Only rows that don't already exist are written — subsequent
    restarts preserve whatever the operator (or a prior scheduler run)
    set. Uses ``settings.runtime.initial_trade_mode`` if present,
    falling back to the PROMPT-mandated ``watch_only`` default.
    """
    runtime_cfg = settings.raw.get("runtime", {}) or {}
    initial_mode = runtime_cfg.get("initial_trade_mode", DEFAULT_TRADE_MODE)
    if initial_mode not in VALID_TRADE_MODES:
        logger.warning(
            "runtime.initial_trade_mode={!r} invalid; falling back to {}",
            initial_mode, DEFAULT_TRADE_MODE,
        )
        initial_mode = DEFAULT_TRADE_MODE
    store.ensure_initial_flags(
        {
            "trade_mode": initial_mode,
            "scheduler_state": "stopped",
            "kill_switch": "armed",
        },
        actor="system_init",
    )
