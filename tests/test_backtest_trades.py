"""FIFO trade extraction from the orders table."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backtest.trades import extract_trades
from brokers.base import Order, OrderType, Side

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


def _order(
    id: str, symbol: str, side: Side, qty: int, avg_price: float, offset_min: int,
    status: str = "FILLED",
) -> Order:
    return Order(
        id=id, symbol=symbol, side=side, qty=qty,
        order_type=OrderType.MARKET, price=None, trigger_price=None,
        status=status, filled_qty=qty if status == "FILLED" else 0,
        avg_price=avg_price, ts=T0 + timedelta(minutes=offset_min),
    )


def test_simple_buy_sell_produces_one_trade() -> None:
    orders = [
        _order("b1", "RELIANCE", Side.BUY, 10, 1000.0, 0),
        _order("s1", "RELIANCE", Side.SELL, 10, 1050.0, 30),
    ]
    trades = extract_trades(orders)
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "RELIANCE"
    assert t.qty == 10
    assert t.entry_price == 1000.0
    assert t.exit_price == 1050.0
    assert t.pnl == 500.0
    assert t.pnl_pct == 5.0
    assert t.holding_minutes == 30.0
    assert t.is_winner is True


def test_losing_trade_has_negative_pnl() -> None:
    orders = [
        _order("b1", "RELIANCE", Side.BUY, 10, 1000.0, 0),
        _order("s1", "RELIANCE", Side.SELL, 10, 980.0, 15),
    ]
    trades = extract_trades(orders)
    assert trades[0].pnl == -200.0
    assert trades[0].is_winner is False


def test_pending_orders_ignored() -> None:
    orders = [
        _order("b1", "RELIANCE", Side.BUY, 10, 1000.0, 0),
        _order("s1", "RELIANCE", Side.SELL, 10, 1050.0, 30, status="PENDING"),
    ]
    trades = extract_trades(orders)
    assert trades == []


def test_fifo_pairs_across_partial_closes() -> None:
    """Buy 10, buy 10, sell 15 → two trades: (entry1, 10) + (entry2, 5)."""
    orders = [
        _order("b1", "RELIANCE", Side.BUY, 10, 1000.0, 0),
        _order("b2", "RELIANCE", Side.BUY, 10, 1010.0, 15),
        _order("s1", "RELIANCE", Side.SELL, 15, 1020.0, 30),
    ]
    trades = extract_trades(orders)
    assert len(trades) == 2
    # FIFO: first trade matches entry from b1 (10 shares).
    assert trades[0].entry_price == 1000.0
    assert trades[0].qty == 10
    assert trades[0].pnl == 200.0
    # Second trade matches remaining 5 from b2.
    assert trades[1].entry_price == 1010.0
    assert trades[1].qty == 5
    assert trades[1].pnl == 50.0


def test_open_position_at_end_not_reported_as_trade() -> None:
    orders = [
        _order("b1", "RELIANCE", Side.BUY, 10, 1000.0, 0),
    ]
    trades = extract_trades(orders)
    assert trades == []


def test_multiple_symbols_tracked_independently() -> None:
    orders = [
        _order("b1", "RELIANCE", Side.BUY, 10, 1000.0, 0),
        _order("b2", "TCS", Side.BUY, 5, 3000.0, 0),
        _order("s1", "RELIANCE", Side.SELL, 10, 1050.0, 30),
        _order("s2", "TCS", Side.SELL, 5, 2950.0, 45),
    ]
    trades = extract_trades(orders)
    assert len(trades) == 2
    symbols = {t.symbol for t in trades}
    assert symbols == {"RELIANCE", "TCS"}
