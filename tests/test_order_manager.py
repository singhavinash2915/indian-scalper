"""OrderManager — submit, fill, cancel, partial flip, recovery, guardrails."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brokers.base import Candle, OrderType, Side
from execution.order_manager import InsufficientFundsError, OrderManager
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


def _candle(t: datetime, o: float, h: float, low: float, c: float, v: int = 1000) -> Candle:
    return Candle(ts=t, open=o, high=h, low=low, close=c, volume=v)


def _om(tmp_path: Path, cash: float = 500_000.0, slippage: float = 0.05) -> OrderManager:
    return OrderManager(
        StateStore(tmp_path / "state.db"),
        starting_cash=cash,
        slippage_pct=slippage,
    )


# ---------------------------------------------------------------- #
# Submit                                                            #
# ---------------------------------------------------------------- #

def test_submit_places_pending_order(tmp_path: Path) -> None:
    om = _om(tmp_path)
    order = om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    assert order.status == "PENDING"
    assert order.id in om.orders
    # Persisted to SQLite.
    assert om.store.get_order(order.id) is not None


def test_submit_rejects_non_positive_qty(tmp_path: Path) -> None:
    om = _om(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        om.submit("RELIANCE", 0, Side.BUY, OrderType.MARKET)


# ---------------------------------------------------------------- #
# Market fills                                                      #
# ---------------------------------------------------------------- #

def test_market_buy_fills_at_next_open_plus_slippage(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=100_000.0, slippage=0.05)
    om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)

    next_candle = _candle(T0 + timedelta(minutes=15), o=1000, h=1005, low=995, c=1002)
    filled = om.settle_on_candle("RELIANCE", next_candle)

    assert len(filled) == 1
    f = filled[0]
    assert f.status == "FILLED"
    assert f.avg_price == pytest.approx(1000 * 1.0005)  # 0.05% slippage
    assert f.filled_qty == 10
    # Cash debited.
    assert om.cash == pytest.approx(100_000 - (1000 * 1.0005) * 10)
    # Position opened.
    assert om.positions["RELIANCE"].qty == 10
    assert om.positions["RELIANCE"].avg_price == pytest.approx(1000 * 1.0005)
    # Order no longer pending.
    assert f.id not in om.orders


def test_market_sell_fills_below_open_by_slippage(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=100_000.0)
    # Open a long first so there's something to sell.
    om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    om.settle_on_candle("RELIANCE", _candle(T0, 1000, 1001, 999, 1000))
    # Now sell.
    om.submit("RELIANCE", 10, Side.SELL, OrderType.MARKET, ts=T0)
    filled = om.settle_on_candle(
        "RELIANCE", _candle(T0 + timedelta(minutes=15), 1100, 1105, 1095, 1102)
    )
    assert filled[0].avg_price == pytest.approx(1100 * 0.9995)
    # Position fully closed.
    assert "RELIANCE" not in om.positions


# ---------------------------------------------------------------- #
# Limit / SL                                                        #
# ---------------------------------------------------------------- #

def test_limit_order_fills_only_when_candle_crosses(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=100_000.0)
    om.submit(
        "RELIANCE", 10, Side.BUY, OrderType.LIMIT, price=990.0, ts=T0,
    )
    # Candle range [995, 1005] — limit 990 not touched.
    miss = om.settle_on_candle("RELIANCE", _candle(T0, 1000, 1005, 995, 1000))
    assert miss == []

    # Candle range [985, 1000] — limit 990 touched, fill at limit price.
    hit = om.settle_on_candle(
        "RELIANCE", _candle(T0 + timedelta(minutes=15), 998, 1000, 985, 995)
    )
    assert len(hit) == 1
    assert hit[0].avg_price == 990.0  # limit fills AT the limit, no slippage


def test_stop_loss_order_fills_with_slippage_on_trigger(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=100_000.0)
    # Open long first.
    om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    om.settle_on_candle("RELIANCE", _candle(T0, 1000, 1005, 995, 1000))
    # Place SL-M sell at 980.
    om.submit(
        "RELIANCE", 10, Side.SELL, OrderType.SL_M, trigger_price=980.0, ts=T0,
    )
    # Candle dips to 975 — trigger hit.
    filled = om.settle_on_candle(
        "RELIANCE", _candle(T0 + timedelta(minutes=15), 985, 990, 975, 982)
    )
    assert len(filled) == 1
    # SL-M fills at trigger minus slippage for SELL.
    assert filled[0].avg_price == pytest.approx(980.0 * 0.9995)


# ---------------------------------------------------------------- #
# Cancel + modify                                                   #
# ---------------------------------------------------------------- #

def test_cancel_pending_order(tmp_path: Path) -> None:
    om = _om(tmp_path)
    order = om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    assert om.cancel(order.id) is True
    assert order.id not in om.orders
    # Filling after cancel is a no-op.
    filled = om.settle_on_candle("RELIANCE", _candle(T0, 1000, 1001, 999, 1000))
    assert filled == []


def test_cancel_unknown_returns_false(tmp_path: Path) -> None:
    om = _om(tmp_path)
    assert om.cancel("does-not-exist") is False


def test_modify_pending_limit_price(tmp_path: Path) -> None:
    om = _om(tmp_path)
    order = om.submit(
        "RELIANCE", 10, Side.BUY, OrderType.LIMIT, price=990.0, ts=T0,
    )
    new = om.modify(order.id, price=985.0)
    assert new.price == 985.0
    assert om.orders[order.id].price == 985.0


def test_modify_rejects_unknown_fields(tmp_path: Path) -> None:
    om = _om(tmp_path)
    order = om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    with pytest.raises(ValueError, match="cannot modify"):
        om.modify(order.id, status="FILLED")


# ---------------------------------------------------------------- #
# Cash guard                                                        #
# ---------------------------------------------------------------- #

def test_buy_with_insufficient_cash_is_rejected(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=1_000.0)
    om.submit("RELIANCE", 100, Side.BUY, OrderType.MARKET, ts=T0)
    with pytest.raises(InsufficientFundsError):
        om.settle_on_candle("RELIANCE", _candle(T0, 1000, 1005, 995, 1000))
    # Cash untouched, no position opened.
    assert om.cash == 1_000.0
    assert om.positions == {}


# ---------------------------------------------------------------- #
# Partial fills / averaging                                         #
# ---------------------------------------------------------------- #

def test_averaging_in_combines_prices(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=500_000.0)
    om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    om.settle_on_candle(
        "RELIANCE", _candle(T0, 1000, 1001, 999, 1000)
    )
    om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    om.settle_on_candle(
        "RELIANCE", _candle(T0 + timedelta(minutes=15), 2000, 2005, 1995, 2000)
    )
    # With slippage: avg should weight the two buys equally.
    expected = ((1000 * 1.0005) * 10 + (2000 * 1.0005) * 10) / 20
    assert om.positions["RELIANCE"].qty == 20
    assert om.positions["RELIANCE"].avg_price == pytest.approx(expected)


# ---------------------------------------------------------------- #
# Mark to market / equity                                           #
# ---------------------------------------------------------------- #

def test_mark_to_market_updates_position_ltp_and_pnl(tmp_path: Path) -> None:
    om = _om(tmp_path, cash=500_000.0)
    om.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    om.settle_on_candle("RELIANCE", _candle(T0, 1000, 1001, 999, 1000))
    om.mark_to_market({"RELIANCE": 1100.0})
    pos = om.positions["RELIANCE"]
    assert pos.ltp == 1100.0
    # ~100 profit per share × 10 shares, minus slippage effect.
    assert pos.pnl > 900


# ---------------------------------------------------------------- #
# Recovery                                                          #
# ---------------------------------------------------------------- #

def test_restart_recovers_pending_orders_and_positions(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore(db)

    # First session: one pending + one filled.
    om1 = OrderManager(store, starting_cash=500_000.0, slippage_pct=0.05)
    om1.submit("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    om1.settle_on_candle("RELIANCE", _candle(T0, 1000, 1001, 999, 1000))
    # Another pending order that has not yet settled.
    pending = om1.submit(
        "TCS", 5, Side.BUY, OrderType.LIMIT, price=3000.0, ts=T0,
    )
    cash_after_session1 = om1.cash

    # Second session — reconstruct from the same store.
    store2 = StateStore(db)  # reopens same file
    om2 = OrderManager(store2, starting_cash=500_000.0, slippage_pct=0.05)
    # Cash reconstructed from filled-order flow.
    assert om2.cash == pytest.approx(cash_after_session1)
    # Pending TCS recovered.
    assert pending.id in om2.orders
    assert om2.orders[pending.id].status == "PENDING"
    # RELIANCE position recovered.
    assert "RELIANCE" in om2.positions
    assert om2.positions["RELIANCE"].qty == 10
