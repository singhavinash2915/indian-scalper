"""Trade-mode enforcement — shared D11 Slice 0 tests for both brokers.

Covers the PROMPT's five acceptance criteria:

1. ``place_order`` returns a rejection when ``trade_mode = watch_only``,
   regardless of broker.
2. Mode change writes an ``operator_audit`` row.
3. ``live`` mode refused without the env var.
4. Watch_only ↔ paper ↔ watch_only is reversible mid-session.
5. Existing positions still manage (exits go through) under watch_only.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from brokers.base import OrderType, Side
from brokers.paper import PaperBroker
from brokers.trade_mode import (
    DEFAULT_TRADE_MODE,
    LIVE_ACK_ENV,
    REJECTED_BY_TRADE_MODE,
    check_and_maybe_reject,
    current_trade_mode,
    live_trading_acknowledged,
)
from brokers.upstox import UpstoxBroker
from config.settings import Settings
from data.instruments import InstrumentMaster
from data.market_data import FakeCandleFetcher
from execution.state import StateStore
from tests.fixtures import paper_mode


# ---------------------------------------------------------------- #
# Helpers                                                           #
# ---------------------------------------------------------------- #

def _instruments(tmp_path: Path) -> InstrumentMaster:
    m = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    m.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    return m


def _paper(tmp_path: Path, *, trade_mode: str | None = None) -> PaperBroker:
    settings = Settings.from_template()
    if trade_mode is not None:
        settings.raw.setdefault("runtime", {})["initial_trade_mode"] = trade_mode
    return PaperBroker(
        settings,
        db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=FakeCandleFetcher({}),
        instruments=_instruments(tmp_path),
    )


def _upstox(tmp_path: Path, *, trade_mode: str | None = None) -> UpstoxBroker:
    settings = Settings.from_template()
    if trade_mode is not None:
        settings.raw.setdefault("runtime", {})["initial_trade_mode"] = trade_mode
    return UpstoxBroker(
        settings,
        instruments=_instruments(tmp_path),
        db_path=str(tmp_path / "scalper.db"),
        order_api=MagicMock(), portfolio_api=MagicMock(),
        history_api=MagicMock(), market_api=MagicMock(), user_api=MagicMock(),
    )


# ---------------------------------------------------------------- #
# First-run defaults                                                #
# ---------------------------------------------------------------- #

def test_first_run_defaults_trade_mode_to_watch_only(tmp_path: Path) -> None:
    broker = _paper(tmp_path)  # no runtime override → template's watch_only
    assert current_trade_mode(broker.store) == "watch_only"
    assert broker.store.get_flag("scheduler_state") == "stopped"
    assert broker.store.get_flag("kill_switch") == "armed"


def test_initial_trade_mode_from_config_respected(tmp_path: Path) -> None:
    broker = _paper(tmp_path, trade_mode="paper")
    assert current_trade_mode(broker.store) == "paper"


def test_invalid_initial_trade_mode_falls_back_to_watch_only(tmp_path: Path) -> None:
    broker = _paper(tmp_path, trade_mode="bogus_mode")
    assert current_trade_mode(broker.store) == DEFAULT_TRADE_MODE


def test_initial_flags_preserved_across_broker_restart(tmp_path: Path) -> None:
    broker = _paper(tmp_path, trade_mode="paper")
    broker.store.set_flag("trade_mode", "watch_only", actor="test")
    # Second broker against same DB must NOT re-seed trade_mode back to paper.
    broker2 = _paper(tmp_path, trade_mode="paper")
    assert current_trade_mode(broker2.store) == "watch_only"


# ---------------------------------------------------------------- #
# PaperBroker — defense in depth                                    #
# ---------------------------------------------------------------- #

def test_paper_watch_only_blocks_entry_orders(tmp_path: Path) -> None:
    broker = _paper(tmp_path)  # watch_only
    order = broker.place_order(
        "RELIANCE", 10, Side.BUY, OrderType.MARKET, intent="entry",
    )
    assert order.status == REJECTED_BY_TRADE_MODE
    assert order.id.startswith("blocked-")
    # Not persisted as a real order.
    assert broker.store.get_order(order.id) is None
    # No position opened.
    assert broker.get_positions() == []
    # Audited.
    audit = broker.store.load_operator_audit()
    assert any(r["action"] == "order_blocked_by_trade_mode" for r in audit)


def test_paper_watch_only_allows_exit_orders(tmp_path: Path) -> None:
    broker = _paper(tmp_path, trade_mode="paper")
    # Open a position in paper mode first.
    from brokers.base import Candle
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, intent="entry", ts=T0)
    broker.settle("RELIANCE", Candle(T0, open=1000, high=1001, low=999, close=1000, volume=100))
    assert len(broker.get_positions()) == 1

    # Flip to watch_only mid-session — exit orders must still go through.
    broker.store.set_flag("trade_mode", "watch_only", actor="test")
    exit_order = broker.place_order(
        "RELIANCE", 10, Side.SELL, OrderType.MARKET, intent="exit", ts=T0,
    )
    assert exit_order.status == "PENDING"
    assert exit_order.id != ""  # real uuid from OrderManager, not blocked-*


def test_paper_default_intent_is_entry_and_blocked(tmp_path: Path) -> None:
    """An unannotated place_order call defaults to intent='entry' and
    is therefore blocked in watch_only — the safe default."""
    broker = _paper(tmp_path)
    order = broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    assert order.status == REJECTED_BY_TRADE_MODE


def test_paper_mode_lets_entries_through(tmp_path: Path) -> None:
    broker = _paper(tmp_path, trade_mode="paper")
    order = broker.place_order(
        "RELIANCE", 10, Side.BUY, OrderType.MARKET, intent="entry",
    )
    assert order.status == "PENDING"
    assert not order.id.startswith("blocked-")


# ---------------------------------------------------------------- #
# UpstoxBroker — same defense in depth                              #
# ---------------------------------------------------------------- #

def test_upstox_watch_only_blocks_entry_orders(tmp_path: Path) -> None:
    broker = _upstox(tmp_path)  # watch_only
    order = broker.place_order(
        "RELIANCE", 10, Side.BUY, OrderType.MARKET, intent="entry",
    )
    assert order.status == REJECTED_BY_TRADE_MODE
    # The SDK was NOT called.
    broker._order_api.place_order.assert_not_called()


def test_upstox_watch_only_allows_exit_orders(tmp_path: Path) -> None:
    broker = _upstox(tmp_path)  # watch_only
    broker._order_api.place_order.return_value = MagicMock(
        data=MagicMock(order_id="UP-123"),
    )
    order = broker.place_order(
        "RELIANCE", 5, Side.SELL, OrderType.MARKET, intent="exit",
    )
    # Exit flowed through the real (mocked) SDK path.
    assert order.id == "UP-123"
    broker._order_api.place_order.assert_called_once()


def test_upstox_paper_mode_lets_entries_through(tmp_path: Path) -> None:
    broker = _upstox(tmp_path, trade_mode="paper")
    broker._order_api.place_order.return_value = MagicMock(
        data=MagicMock(order_id="UP-42"),
    )
    order = broker.place_order(
        "RELIANCE", 1, Side.BUY, OrderType.MARKET, intent="entry",
    )
    assert order.id == "UP-42"


# ---------------------------------------------------------------- #
# Reversibility mid-session                                         #
# ---------------------------------------------------------------- #

def test_watch_only_to_paper_to_watch_only_is_reversible(tmp_path: Path) -> None:
    broker = _paper(tmp_path)  # watch_only

    blocked = broker.place_order("RELIANCE", 5, Side.BUY, OrderType.MARKET, intent="entry")
    assert blocked.status == REJECTED_BY_TRADE_MODE

    broker.store.set_flag("trade_mode", "paper", actor="test")
    ok = broker.place_order("RELIANCE", 5, Side.BUY, OrderType.MARKET, intent="entry")
    assert ok.status == "PENDING"

    broker.store.set_flag("trade_mode", "watch_only", actor="test")
    blocked_again = broker.place_order("TCS", 3, Side.BUY, OrderType.MARKET, intent="entry")
    assert blocked_again.status == REJECTED_BY_TRADE_MODE


# ---------------------------------------------------------------- #
# set_flag audit trail                                              #
# ---------------------------------------------------------------- #

def test_set_flag_writes_operator_audit_row(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_flag("trade_mode", "paper", actor="web")
    audit = store.load_operator_audit()
    assert any(r["action"] == "flag_set:trade_mode" for r in audit)
    row = next(r for r in audit if r["action"] == "flag_set:trade_mode")
    assert row["actor"] == "web"
    assert row["payload"]["value"] == "paper"
    assert row["payload"]["previous"] is None  # first set


def test_set_flag_records_previous_value(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_flag("trade_mode", "paper", actor="system")
    store.set_flag("trade_mode", "watch_only", actor="web")
    audit = store.load_operator_audit(limit=10)
    latest = next(r for r in audit if r["action"] == "flag_set:trade_mode")
    assert latest["payload"]["previous"] == "paper"
    assert latest["payload"]["value"] == "watch_only"


# ---------------------------------------------------------------- #
# Env-var gate                                                      #
# ---------------------------------------------------------------- #

def test_live_trading_acknowledged_env_var(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_ACK_ENV, raising=False)
    assert live_trading_acknowledged() is False
    monkeypatch.setenv(LIVE_ACK_ENV, "no")
    assert live_trading_acknowledged() is False
    monkeypatch.setenv(LIVE_ACK_ENV, "yes")
    assert live_trading_acknowledged() is True
    monkeypatch.setenv(LIVE_ACK_ENV, "YES")  # case-insensitive
    assert live_trading_acknowledged() is True


# ---------------------------------------------------------------- #
# check_and_maybe_reject — unit tests                               #
# ---------------------------------------------------------------- #

def test_check_and_maybe_reject_returns_none_in_paper(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_flag("trade_mode", "paper", actor="test")
    result = check_and_maybe_reject(
        store, "RELIANCE", 10, Side.BUY, OrderType.MARKET,
        intent="entry", broker_name="TestBroker",
    )
    assert result is None


def test_check_and_maybe_reject_returns_none_for_exits_in_watch_only(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_flag("trade_mode", "watch_only", actor="test")
    result = check_and_maybe_reject(
        store, "RELIANCE", 10, Side.SELL, OrderType.MARKET,
        intent="exit", broker_name="TestBroker",
    )
    assert result is None


def test_check_and_maybe_reject_blocks_entries_in_watch_only(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    store.set_flag("trade_mode", "watch_only", actor="test")
    result = check_and_maybe_reject(
        store, "RELIANCE", 10, Side.BUY, OrderType.MARKET,
        intent="entry", broker_name="TestBroker",
    )
    assert result is not None
    assert result.status == REJECTED_BY_TRADE_MODE
