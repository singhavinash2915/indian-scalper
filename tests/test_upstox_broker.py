"""UpstoxBroker — mock-based unit tests.

No live credentials, no network. Every SDK API is a ``MagicMock``
returning canned responses; we verify:
  * Correct parameters are passed to the SDK.
  * Responses are parsed into our domain dataclasses.
  * The tenacity retry wrapper triggers on 5xx / 429 and gives up on 4xx.
  * Symbol → instrument_key resolution uses the ISIN column.
  * Kill-switch state lives in StateStore (dashboard-compatible).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from brokers.base import OrderType, Side
from brokers.upstox import UPSTOX_API_VERSION, UpstoxBroker, _is_retryable
from config.settings import Settings
from data.instruments import InstrumentMaster


# --------------------------------------------------------------------- #
# Test helpers                                                           #
# --------------------------------------------------------------------- #

def _seed_instruments(tmp_path: Path) -> InstrumentMaster:
    master = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    master.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    return master


def _build_broker(tmp_path: Path, **api_mocks):
    settings = Settings.from_template()
    instruments = _seed_instruments(tmp_path)
    apis = {
        "order_api": MagicMock(name="OrderApi"),
        "portfolio_api": MagicMock(name="PortfolioApi"),
        "history_api": MagicMock(name="HistoryApi"),
        "market_api": MagicMock(name="MarketQuoteApi"),
        "user_api": MagicMock(name="UserApi"),
    }
    apis.update(api_mocks)
    broker = UpstoxBroker(
        settings,
        instruments=instruments,
        db_path=str(tmp_path / "scalper.db"),
        **apis,
    )
    return broker, apis


# --------------------------------------------------------------------- #
# Retry policy                                                           #
# --------------------------------------------------------------------- #

def test_is_retryable_server_error() -> None:
    exc = SimpleNamespace(status=503)
    # Monkey-patch ApiException isinstance check: we emulate by using a
    # real ApiException since the SDK is installed via the upstox extra.
    from upstox_client.rest import ApiException
    real_exc = ApiException(status=503, reason="Service Unavailable")
    assert _is_retryable(real_exc) is True
    assert _is_retryable(type(exc)(status=429)) is False  # SimpleNamespace isn't ApiException
    rate_limited = ApiException(status=429, reason="Too Many Requests")
    assert _is_retryable(rate_limited) is True


def test_is_retryable_client_error_is_not_retried() -> None:
    from upstox_client.rest import ApiException
    bad_req = ApiException(status=400, reason="Bad Request")
    not_found = ApiException(status=404, reason="Not Found")
    assert _is_retryable(bad_req) is False
    assert _is_retryable(not_found) is False


def test_is_retryable_network_errors() -> None:
    assert _is_retryable(ConnectionError("reset")) is True
    assert _is_retryable(TimeoutError("timed out")) is True
    assert _is_retryable(ValueError("unrelated")) is False


# --------------------------------------------------------------------- #
# Symbol ↔ instrument_key                                                #
# --------------------------------------------------------------------- #

def test_key_resolver_builds_nse_eq_key(tmp_path: Path) -> None:
    broker, _ = _build_broker(tmp_path)
    # RELIANCE's ISIN in the fixture is INE002A01018.
    assert broker._key_resolver("RELIANCE") == "NSE_EQ|INE002A01018"


def test_key_resolver_raises_on_unknown_symbol(tmp_path: Path) -> None:
    broker, _ = _build_broker(tmp_path)
    with pytest.raises(KeyError):
        broker._key_resolver("NOT_A_SYMBOL")


def test_custom_key_resolver_honoured(tmp_path: Path) -> None:
    settings = Settings.from_template()
    instruments = _seed_instruments(tmp_path)
    custom = lambda sym: f"CUSTOM|{sym}"
    broker = UpstoxBroker(
        settings, instruments=instruments, db_path=str(tmp_path / "scalper.db"),
        order_api=MagicMock(), portfolio_api=MagicMock(), history_api=MagicMock(),
        market_api=MagicMock(), user_api=MagicMock(), key_resolver=custom,
    )
    assert broker._key_resolver("ANYTHING") == "CUSTOM|ANYTHING"


# --------------------------------------------------------------------- #
# Orders                                                                 #
# --------------------------------------------------------------------- #

def test_place_order_builds_request_and_persists(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["order_api"].place_order.return_value = SimpleNamespace(
        data=SimpleNamespace(order_id="UPSTOX-123")
    )

    order = broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)

    assert order.id == "UPSTOX-123"
    assert order.status == "PENDING"
    assert order.symbol == "RELIANCE"

    # One SDK call with api_version 2.0.
    apis["order_api"].place_order.assert_called_once()
    body, api_version = apis["order_api"].place_order.call_args[0]
    assert api_version == UPSTOX_API_VERSION
    # Body has our mapped fields.
    assert body.quantity == 10
    assert body.transaction_type == "BUY"
    assert body.order_type == "MARKET"
    assert body.instrument_token == "NSE_EQ|INE002A01018"
    assert body.product == "I"
    assert body.tag == "indian-scalper"

    # Persisted + audited.
    assert broker.store.get_order("UPSTOX-123") is not None
    audit = broker.store.load_audit()
    assert any(r["action"] == "upstox_order_placed" for r in audit)


def test_place_order_rejects_non_positive_qty(tmp_path: Path) -> None:
    broker, _ = _build_broker(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        broker.place_order("RELIANCE", 0, Side.BUY, OrderType.MARKET)


def test_place_order_passes_trigger_price_for_sl(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["order_api"].place_order.return_value = {"data": {"order_id": "O1"}}

    broker.place_order(
        "RELIANCE", 10, Side.SELL, OrderType.SL_M, trigger_price=980.0,
    )
    body = apis["order_api"].place_order.call_args[0][0]
    assert body.order_type == "SL-M"
    assert body.trigger_price == 980.0
    assert body.transaction_type == "SELL"


def test_cancel_order_hits_sdk_and_updates_status(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["order_api"].place_order.return_value = {"data": {"order_id": "O-42"}}
    broker.place_order("RELIANCE", 5, Side.BUY, OrderType.MARKET)

    apis["order_api"].cancel_order.return_value = {"data": {"order_id": "O-42"}}
    ok = broker.cancel_order("O-42")
    assert ok is True
    apis["order_api"].cancel_order.assert_called_once_with("O-42", UPSTOX_API_VERSION)

    cached = broker.store.get_order("O-42")
    assert cached is not None and cached.status == "CANCELLED"


def test_modify_order_passes_new_fields(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["order_api"].place_order.return_value = {"data": {"order_id": "O-9"}}
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.LIMIT, price=1000.0)

    apis["order_api"].modify_order.return_value = {"data": {"order_id": "O-9"}}
    out = broker.modify_order("O-9", price=995.0, qty=12)
    body = apis["order_api"].modify_order.call_args[0][0]
    assert body.price == 995.0
    assert body.quantity == 12
    assert out.id == "O-9"


# --------------------------------------------------------------------- #
# Reads                                                                  #
# --------------------------------------------------------------------- #

def test_get_positions_parses_response(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["portfolio_api"].get_positions.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(
                trading_symbol="RELIANCE", quantity=10,
                average_price=1000.0, last_price=1050.0,
            ),
            # Zero-quantity rows are filtered out.
            SimpleNamespace(
                trading_symbol="TCS", quantity=0,
                average_price=3000.0, last_price=3000.0,
            ),
        ]
    )
    positions = broker.get_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "RELIANCE"
    assert p.qty == 10
    assert p.avg_price == 1000.0
    assert p.ltp == 1050.0


def test_get_funds_parses_equity_block(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["user_api"].get_user_fund_margin.return_value = {
        "data": {
            "equity": {"available_margin": 400_000.0, "used_margin": 50_000.0},
            "commodity": {"available_margin": 0, "used_margin": 0},
        }
    }
    funds = broker.get_funds()
    assert funds["available"] == 400_000.0
    assert funds["used"] == 50_000.0
    assert funds["equity"] == 450_000.0


def test_get_ltp_bundles_keys_and_parses(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["market_api"].ltp.return_value = {
        "data": {
            "NSE_EQ:INE002A01018": {"last_price": 1050.25},
            "NSE_EQ:INE467B01029": {"last_price": 3100.75},
        }
    }
    ltps = broker.get_ltp(["RELIANCE", "TCS"])

    apis["market_api"].ltp.assert_called_once()
    csv_arg = apis["market_api"].ltp.call_args[0][0]
    # Keys are comma-joined in the request.
    assert "NSE_EQ|INE002A01018" in csv_arg
    assert "NSE_EQ|INE467B01029" in csv_arg

    assert ltps == {"RELIANCE": 1050.25, "TCS": 3100.75}


def test_get_ltp_empty_input_short_circuits(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    assert broker.get_ltp([]) == {}
    apis["market_api"].ltp.assert_not_called()


def test_get_candles_uses_intraday_endpoint_for_1m(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    apis["history_api"].get_intra_day_candle_data.return_value = {
        "data": {
            "candles": [
                # ts, open, high, low, close, volume, ...
                ["2026-04-21T10:00:00+05:30", 1000, 1005, 999, 1003, 12345, 0],
                ["2026-04-21T10:01:00+05:30", 1003, 1007, 1002, 1006, 23456, 0],
            ]
        }
    }
    candles = broker.get_candles("RELIANCE", "1m", lookback=10)

    apis["history_api"].get_intra_day_candle_data.assert_called_once()
    key, interval, api_version = apis["history_api"].get_intra_day_candle_data.call_args[0]
    assert key == "NSE_EQ|INE002A01018"
    assert interval == "1minute"
    assert api_version == UPSTOX_API_VERSION

    assert len(candles) == 2
    assert candles[0].open == 1000
    assert candles[-1].close == 1006
    assert candles[0].ts < candles[-1].ts


def test_get_candles_rejects_unsupported_interval(tmp_path: Path) -> None:
    broker, _ = _build_broker(tmp_path)
    with pytest.raises(ValueError, match="unsupported interval"):
        broker.get_candles("RELIANCE", "3h", lookback=10)


# --------------------------------------------------------------------- #
# Retry integration                                                      #
# --------------------------------------------------------------------- #

def test_retry_on_server_error_then_success(tmp_path: Path) -> None:
    """The tenacity decorator should retry a 503 then succeed on next try."""
    from upstox_client.rest import ApiException

    broker, apis = _build_broker(tmp_path)
    apis["order_api"].place_order.side_effect = [
        ApiException(status=503, reason="Service Unavailable"),
        SimpleNamespace(data=SimpleNamespace(order_id="OK-1")),
    ]
    # Speed up: replace the decorator's wait with zero so tests don't sleep.
    # We can't easily patch tenacity per-test; rely on the min=1s wait
    # being fast enough for one retry.
    order = broker.place_order("RELIANCE", 1, Side.BUY, OrderType.MARKET)
    assert order.id == "OK-1"
    assert apis["order_api"].place_order.call_count == 2


def test_retry_gives_up_on_4xx(tmp_path: Path) -> None:
    """A 400 Bad Request should fail fast — not retry."""
    from upstox_client.rest import ApiException

    broker, apis = _build_broker(tmp_path)
    apis["order_api"].place_order.side_effect = ApiException(
        status=400, reason="Bad Request",
    )
    with pytest.raises(ApiException):
        broker.place_order("RELIANCE", 1, Side.BUY, OrderType.MARKET)
    # Only one attempt — tenacity didn't retry.
    assert apis["order_api"].place_order.call_count == 1


# --------------------------------------------------------------------- #
# Kill-switch parity with PaperBroker                                    #
# --------------------------------------------------------------------- #

def test_local_kill_switch_roundtrip(tmp_path: Path) -> None:
    broker, _ = _build_broker(tmp_path)
    assert broker.is_kill_switch_on() is False
    broker.set_kill_switch(True)
    assert broker.is_kill_switch_on() is True
    broker.set_kill_switch(False)
    assert broker.is_kill_switch_on() is False


def test_server_kill_switch_calls_sdk(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    broker.update_server_kill_switch(segment="EQ", on=True)
    apis["user_api"].update_kill_switch.assert_called_once()
    body, api_version = apis["user_api"].update_kill_switch.call_args[0]
    assert api_version == UPSTOX_API_VERSION
    assert body.segment == "EQ"
    assert body.action == "ENABLE"


def test_server_kill_switch_off_sends_disable(tmp_path: Path) -> None:
    broker, apis = _build_broker(tmp_path)
    broker.update_server_kill_switch(segment="EQ", on=False)
    body, _ = apis["user_api"].update_kill_switch.call_args[0]
    assert body.action == "DISABLE"


# --------------------------------------------------------------------- #
# Constructor safety — live SDK init requires env token                  #
# --------------------------------------------------------------------- #

def test_live_init_fails_without_env_token(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.from_template()
    instruments = _seed_instruments(tmp_path)
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="UPSTOX_ACCESS_TOKEN"):
        UpstoxBroker(settings, instruments=instruments, db_path=str(tmp_path / "scalper.db"))
