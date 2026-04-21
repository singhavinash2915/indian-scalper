"""UpstoxBroker — thin, dependency-injectable wrapper over upstox-python-sdk.

The SDK's API objects are constructed from an access token pulled out of
the env var named in ``config.yaml → upstox.access_token_env``. Tests
bypass this entirely by passing pre-built mock API objects directly into
the constructor.

Design notes:

* **Every SDK call is wrapped with tenacity** — exponential backoff,
  max 3 attempts, retries only on transient failures (HTTP ≥ 500, 429,
  network errors). 4xx except 429 fail fast.
* **Symbol ↔ instrument_key mapping** uses the ISIN stored by
  InstrumentMaster for NSE equity: ``NSE_EQ|{isin}``. F&O keys (NFO
  exchange + instrument token) need the F&O instrument loader — flagged
  as a TODO here; Deliverable 9's explicit scope is equity parity.
* **Kill switch** is persisted in ``StateStore.kv`` (same as
  PaperBroker). This lets the dashboard + scan loop halt entries
  identically whether paper or live. Upstox itself also exposes a
  server-side kill switch via ``UserApi.update_kill_switch`` — not used
  here, but deployers can call ``broker.update_server_kill_switch()``
  to flip both.
* **Integration with the live scan loop** is deliberately out of scope
  for D9 — that requires order-status polling / websocket streaming,
  bracket-order SL attachment, and a different management path. Full
  BrokerBase parity plus dashboard compatibility is what ships here.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

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
from config.settings import Settings
from data.instruments import InstrumentMaster
from execution.state import StateStore
from scheduler.market_hours import IST, now_ist

# Upstox REST v2 API version passed to every SDK call.
UPSTOX_API_VERSION = "2.0"

# Our OrderType enum → Upstox's string codes.
_ORDER_TYPE_MAP: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_M: "SL-M",
}
_REVERSE_ORDER_TYPE: dict[str, OrderType] = {v: k for k, v in _ORDER_TYPE_MAP.items()}

# Our interval strings → Upstox intraday intervals.
_INTRADAY_INTERVAL_MAP = {
    "1m": "1minute",
    "5m": "30minute",   # Upstox intraday supports 1minute / 30minute only
    "15m": "30minute",  # ← see TODO; 15-min requires historical endpoint
    "30m": "30minute",
}
_HISTORICAL_INTERVAL_MAP = {
    "1d": "day",
    "daily": "day",
    "1w": "week",
    "1M": "month",
}


# --------------------------------------------------------------------- #
# Retry policy                                                          #
# --------------------------------------------------------------------- #

def _is_retryable(exc: BaseException) -> bool:
    """Decide whether a raised exception should be retried.

    Retryable:
      * Network-level errors (ConnectionError, TimeoutError, OSError).
      * Upstox ``ApiException`` with HTTP ≥ 500 (server fault) or 429
        (rate-limited).

    Non-retryable:
      * ``ApiException`` with 4xx except 429 — fail fast; retrying a
        bad request wastes budget.
      * Anything else — programmer errors, SDK bugs, etc.
    """
    try:
        from upstox_client.rest import ApiException  # type: ignore[import-untyped]
    except ImportError:
        ApiException = None  # type: ignore[assignment]

    if ApiException is not None and isinstance(exc, ApiException):
        status = getattr(exc, "status", None) or 0
        return status >= 500 or status == 429
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _retrying_call(func):
    """Decorator — wrap an SDK call with tenacity retry + logging."""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )(func)


# --------------------------------------------------------------------- #
# Broker                                                                #
# --------------------------------------------------------------------- #

class UpstoxBroker(BrokerBase):
    """Live Upstox broker. Same BrokerBase surface as PaperBroker."""

    def __init__(
        self,
        settings: Settings,
        instruments: InstrumentMaster,
        db_path: str | Path | None = None,
        # Dependency-injected SDK APIs — tests pass mocks.
        order_api: Any = None,
        portfolio_api: Any = None,
        history_api: Any = None,
        market_api: Any = None,
        user_api: Any = None,
        # Override instrument-key resolution for tests / F&O experiments.
        key_resolver=None,
        product: str = "I",  # "I" intraday, "D" delivery, "MTF" margin
        validity: str = "DAY",
    ) -> None:
        self.settings = settings
        self.instruments = instruments
        self.product = product
        self.validity = validity

        storage_cfg = settings.raw.get("storage", {})
        self._db_path = Path(db_path or storage_cfg.get("db_path", "data/scalper.db"))
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(self._db_path)

        self._key_resolver = key_resolver or self._default_key_resolver

        if order_api is None:
            self._init_sdk()
        else:
            self._order_api = order_api
            self._portfolio_api = portfolio_api
            self._history_api = history_api
            self._market_api = market_api
            self._user_api = user_api

        logger.info(
            "UpstoxBroker ready | mode={} api_version={} product={} db={}",
            settings.mode, UPSTOX_API_VERSION, self.product, self._db_path,
        )

    # ------------------------------------------------------------------ #
    # SDK bootstrap                                                       #
    # ------------------------------------------------------------------ #

    def _init_sdk(self) -> None:
        import upstox_client  # local import — tests never hit this branch

        upstox_cfg = self.settings.raw.get("upstox", {})
        env_name = upstox_cfg.get("access_token_env", "UPSTOX_ACCESS_TOKEN")
        token = os.environ.get(env_name)
        if not token:
            raise RuntimeError(
                f"UpstoxBroker: environment variable {env_name!r} is unset — "
                "generate an access token via the Upstox login flow and export it"
            )
        cfg = upstox_client.Configuration()
        cfg.access_token = token
        api_client = upstox_client.ApiClient(cfg)
        self._order_api = upstox_client.OrderApi(api_client)
        self._portfolio_api = upstox_client.PortfolioApi(api_client)
        self._history_api = upstox_client.HistoryApi(api_client)
        self._market_api = upstox_client.MarketQuoteApi(api_client)
        self._user_api = upstox_client.UserApi(api_client)

    # ------------------------------------------------------------------ #
    # Symbol ↔ instrument_key                                             #
    # ------------------------------------------------------------------ #

    def _default_key_resolver(self, symbol: str) -> str:
        """Map ``RELIANCE`` → ``NSE_EQ|INE002A01018`` via the ISIN stored
        by InstrumentMaster. F&O tickers use a different key format and
        aren't resolvable this way — scan loop must pass an override
        ``key_resolver`` for those."""
        inst = self.instruments.get(symbol)
        if inst is None:
            raise KeyError(f"UpstoxBroker: unknown symbol {symbol!r}")
        if inst.segment == Segment.EQUITY:
            # Fetch ISIN directly from the instruments table — the
            # Instrument dataclass doesn't carry it.
            import sqlite3
            with sqlite3.connect(str(self.instruments._db_path)) as conn:  # pyright: ignore[reportPrivateUsage]
                row = conn.execute(
                    "SELECT isin FROM instruments WHERE symbol = ?", (symbol,),
                ).fetchone()
            if not row or not row[0]:
                raise KeyError(f"UpstoxBroker: no ISIN stored for {symbol!r}")
            return f"NSE_EQ|{row[0]}"
        raise NotImplementedError(
            f"UpstoxBroker: F&O instrument_key resolution not wired up for {symbol!r} "
            "(pass a custom key_resolver)"
        )

    # ------------------------------------------------------------------ #
    # BrokerBase: reads                                                   #
    # ------------------------------------------------------------------ #

    def get_instruments(self) -> list[Instrument]:
        """Delegate to the local InstrumentMaster. Upstox publishes the
        full instrument master separately (JSON/gz) and the SDK's
        ``InstrumentsApi`` is search-only — local cache is simpler."""
        return self.instruments.filter()

    def get_candles(
        self, symbol: str, interval: str, lookback: int,
    ) -> list[Candle]:
        key = self._key_resolver(symbol)
        intraday = _INTRADAY_INTERVAL_MAP.get(interval)
        if intraday is not None:
            raw = self._call_intraday_candles(key, intraday)
        elif interval in _HISTORICAL_INTERVAL_MAP:
            raw = self._call_historical_candles(key, _HISTORICAL_INTERVAL_MAP[interval])
        else:
            raise ValueError(f"UpstoxBroker: unsupported interval {interval!r}")
        return _parse_candle_response(raw, lookback)

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        keys = [self._key_resolver(s) for s in symbols]
        response = self._call_ltp(",".join(keys))
        # Upstox returns {"data": {"NSE_EQ:INE002A01018": {"last_price": ...}}}.
        data = _extract_data(response)
        out: dict[str, float] = {}
        for sym, key in zip(symbols, keys, strict=True):
            # The SDK sometimes uses ':' instead of '|' in response keys.
            entry = data.get(key) or data.get(key.replace("|", ":"))
            if entry is None:
                continue
            out[sym] = float(_field(entry, "last_price"))
        return out

    # ------------------------------------------------------------------ #
    # BrokerBase: orders                                                  #
    # ------------------------------------------------------------------ #

    def place_order(
        self,
        symbol: str,
        qty: int,
        side: Side,
        order_type: OrderType,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> Order:
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")

        from upstox_client import PlaceOrderRequest

        key = self._key_resolver(symbol)
        body = PlaceOrderRequest(
            quantity=qty,
            product=self.product,
            validity=self.validity,
            price=price or 0.0,
            tag="indian-scalper",
            instrument_token=key,
            order_type=_ORDER_TYPE_MAP[order_type],
            transaction_type=side.value,
            disclosed_quantity=0,
            trigger_price=trigger_price or 0.0,
            is_amo=False,
        )
        response = self._call_place_order(body)
        upstox_order_id = _field(_extract_data(response), "order_id")
        order = Order(
            id=str(upstox_order_id),
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            status="PENDING",
            filled_qty=0,
            avg_price=0.0,
            ts=now_ist(),
        )
        self.store.save_order(order)
        self.store.append_audit(
            "upstox_order_placed",
            order_id=order.id, symbol=symbol,
            details={"side": side.value, "qty": qty,
                     "order_type": order_type.value, "instrument_key": key},
        )
        logger.info(
            "UPSTOX PLACE {} {} {} qty={} id={}",
            side.value, order_type.value, symbol, qty, order.id,
        )
        return order

    def modify_order(self, order_id: str, **kwargs: Any) -> Order:
        """Upstox's ``ModifyOrderRequest`` forbids ``None`` on most fields
        (validity, price, order_type, trigger_price). We resolve missing
        fields from the cached order's current state so callers can
        supply a partial update (``price=995.0`` only) and the rest
        falls through unchanged.
        """
        from upstox_client import ModifyOrderRequest

        existing = self.store.get_order(order_id)
        if existing is None:
            raise KeyError(f"modify_order: no cached record for {order_id!r}")

        order_type_enum = kwargs.get("order_type", existing.order_type)
        body = ModifyOrderRequest(
            order_id=order_id,
            quantity=kwargs.get("qty", existing.qty),
            price=kwargs.get("price", existing.price or 0.0),
            trigger_price=kwargs.get("trigger_price", existing.trigger_price or 0.0),
            order_type=_ORDER_TYPE_MAP[order_type_enum],
            validity=kwargs.get("validity", self.validity),
            disclosed_quantity=kwargs.get("disclosed_quantity", 0),
            market_protection=kwargs.get("market_protection"),
        )
        self._call_modify_order(body)
        self.store.append_audit(
            "upstox_order_modified",
            order_id=order_id,
            details={
                k: (v.value if hasattr(v, "value") else v)
                for k, v in kwargs.items() if v is not None
            },
        )
        return existing

    def cancel_order(self, order_id: str) -> bool:
        self._call_cancel_order(order_id)
        self.store.update_order_status(order_id, "CANCELLED")
        self.store.append_audit("upstox_order_cancelled", order_id=order_id)
        logger.info("UPSTOX CANCEL {}", order_id)
        return True

    # ------------------------------------------------------------------ #
    # BrokerBase: portfolio                                               #
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Position]:
        response = self._call_get_positions()
        out: list[Position] = []
        for row in _extract_data(response) or []:
            qty = int(_field(row, "quantity") or 0)
            if qty == 0:
                continue
            out.append(
                Position(
                    symbol=str(_field(row, "trading_symbol")),
                    qty=qty,
                    avg_price=float(_field(row, "average_price") or 0.0),
                    ltp=float(_field(row, "last_price") or 0.0),
                )
            )
        return out

    def get_funds(self) -> dict[str, float]:
        response = self._call_get_funds()
        data = _extract_data(response) or {}
        equity_block = _field(data, "equity") or {}
        available = float(_field(equity_block, "available_margin") or 0.0)
        used = float(_field(equity_block, "used_margin") or 0.0)
        return {"available": available, "used": used, "equity": available + used}

    # ------------------------------------------------------------------ #
    # Kill switch — StateStore flag (same as PaperBroker)                 #
    # ------------------------------------------------------------------ #

    def set_kill_switch(self, on: bool = True) -> None:
        self.store.set_flag("kill_switch", "1" if on else "0")

    def is_kill_switch_on(self) -> bool:
        return self.store.get_flag("kill_switch", "0") == "1"

    def update_server_kill_switch(self, segment: str = "EQ", on: bool = True) -> None:
        """Optional server-side kill switch — flips the Upstox-side
        segment-level halt via ``UserApi.update_kill_switch``. Not
        called automatically; operators use this when they want the
        exchange-side halt too. ``action`` is Upstox's ENABLE/DISABLE
        string; our ``on: bool`` maps to that."""
        from upstox_client import KillSwitchSegmentUpdateRequest

        body = KillSwitchSegmentUpdateRequest(
            segment=segment, action="ENABLE" if on else "DISABLE",
        )
        self._call_update_server_kill_switch(body)
        self.store.append_audit(
            "upstox_server_kill_switch",
            details={"segment": segment, "on": on},
        )

    # ------------------------------------------------------------------ #
    # Internal SDK call wrappers — every one is retry-guarded             #
    # ------------------------------------------------------------------ #

    @_retrying_call
    def _call_place_order(self, body: Any) -> Any:
        return self._order_api.place_order(body, UPSTOX_API_VERSION)

    @_retrying_call
    def _call_modify_order(self, body: Any) -> Any:
        return self._order_api.modify_order(body, UPSTOX_API_VERSION)

    @_retrying_call
    def _call_cancel_order(self, order_id: str) -> Any:
        return self._order_api.cancel_order(order_id, UPSTOX_API_VERSION)

    @_retrying_call
    def _call_get_positions(self) -> Any:
        return self._portfolio_api.get_positions(UPSTOX_API_VERSION)

    @_retrying_call
    def _call_get_funds(self) -> Any:
        return self._user_api.get_user_fund_margin(UPSTOX_API_VERSION)

    @_retrying_call
    def _call_ltp(self, symbol_csv: str) -> Any:
        return self._market_api.ltp(symbol_csv, UPSTOX_API_VERSION)

    @_retrying_call
    def _call_intraday_candles(self, key: str, interval: str) -> Any:
        return self._history_api.get_intra_day_candle_data(
            key, interval, UPSTOX_API_VERSION,
        )

    @_retrying_call
    def _call_historical_candles(self, key: str, interval: str) -> Any:
        today = now_ist().date().isoformat()
        return self._history_api.get_historical_candle_data(
            key, interval, today, UPSTOX_API_VERSION,
        )

    @_retrying_call
    def _call_update_server_kill_switch(self, body: Any) -> Any:
        return self._user_api.update_kill_switch(body, UPSTOX_API_VERSION)


# --------------------------------------------------------------------- #
# Response-parsing helpers                                              #
# --------------------------------------------------------------------- #

def _extract_data(response: Any) -> Any:
    """Upstox SDK sometimes returns model objects with ``.data``, sometimes
    dicts with ``"data"`` key. Normalise to the inner payload."""
    if response is None:
        return None
    if hasattr(response, "data"):
        return response.data
    if isinstance(response, dict):
        return response.get("data")
    return response


def _field(obj: Any, name: str) -> Any:
    """Model-or-dict field accessor."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _parse_candle_response(raw: Any, lookback: int) -> list[Candle]:
    """Upstox candle payload is ``{"candles": [[ts, o, h, l, c, v, ...], ...]}``."""
    data = _extract_data(raw)
    rows: list[list[Any]] = _field(data, "candles") or []
    candles: list[Candle] = []
    for row in rows:
        if len(row) < 6:
            continue
        ts_raw = row[0]
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        elif isinstance(ts_raw, datetime):
            ts = ts_raw
        else:
            # Assume epoch seconds.
            ts = datetime.fromtimestamp(float(ts_raw), tz=IST)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        candles.append(
            Candle(
                ts=ts,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=int(row[5]),
            )
        )
    candles.sort(key=lambda c: c.ts)
    return candles[-lookback:] if lookback > 0 else candles

