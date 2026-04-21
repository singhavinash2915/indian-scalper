"""StateStore — SQLite DAO round-trips + idempotency + audit append-only."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brokers.base import Order, OrderType, Position, Side
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")


def _order(**overrides) -> Order:
    base = dict(
        id="abc123",
        symbol="RELIANCE",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.MARKET,
        price=None,
        trigger_price=None,
        status="PENDING",
        filled_qty=0,
        avg_price=0.0,
        ts=datetime(2026, 4, 21, 10, 0, tzinfo=IST),
    )
    base.update(overrides)
    return Order(**base)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


# ---------------------------------------------------------------- #
# Orders                                                            #
# ---------------------------------------------------------------- #

def test_save_and_load_order(tmp_path: Path) -> None:
    s = _store(tmp_path)
    o = _order()
    s.save_order(o)

    got = s.get_order(o.id)
    assert got is not None
    assert got.symbol == "RELIANCE"
    assert got.side == Side.BUY
    assert got.order_type == OrderType.MARKET
    assert got.status == "PENDING"
    assert got.ts == o.ts  # round-tripped ISO string


def test_save_order_is_idempotent(tmp_path: Path) -> None:
    s = _store(tmp_path)
    o = _order()
    s.save_order(o)
    s.save_order(o)  # upsert, no duplicate
    assert len(s.load_orders()) == 1


def test_update_order_status(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.save_order(_order())
    filled_at = datetime(2026, 4, 21, 10, 15, tzinfo=IST)
    s.update_order_status(
        "abc123", "FILLED", filled_qty=10, avg_price=100.5, filled_at=filled_at
    )
    got = s.get_order("abc123")
    assert got is not None
    assert got.status == "FILLED"
    assert got.filled_qty == 10
    assert got.avg_price == 100.5


def test_load_orders_filtered_by_status(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.save_order(_order(id="p1"))
    s.save_order(_order(id="p2"))
    s.save_order(_order(id="f1", status="FILLED"))
    assert {o.id for o in s.load_orders(status="PENDING")} == {"p1", "p2"}
    assert {o.id for o in s.load_orders(status="FILLED")} == {"f1"}


# ---------------------------------------------------------------- #
# Positions                                                         #
# ---------------------------------------------------------------- #

def test_position_round_trip_and_delete(tmp_path: Path) -> None:
    s = _store(tmp_path)
    pos = Position(
        symbol="RELIANCE", qty=10, avg_price=100.0,
        stop_loss=95.0, take_profit=110.0,
        opened_at=datetime(2026, 4, 21, tzinfo=IST),
    )
    s.save_position(pos)

    got = s.load_positions()
    assert len(got) == 1
    assert got[0].symbol == "RELIANCE"
    assert got[0].qty == 10
    assert got[0].stop_loss == 95.0

    s.delete_position("RELIANCE")
    assert s.load_positions() == []


# ---------------------------------------------------------------- #
# Equity curve                                                      #
# ---------------------------------------------------------------- #

def test_equity_snapshot_and_load(tmp_path: Path) -> None:
    s = _store(tmp_path)
    t1 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)
    t2 = datetime(2026, 4, 21, 10, 5, tzinfo=IST)
    s.snapshot_equity(t1, equity=500_000, cash=500_000, pnl=0)
    s.snapshot_equity(t2, equity=501_000, cash=500_000, pnl=1_000)
    curve = s.load_equity_curve()
    assert [c["equity"] for c in curve] == [500_000, 501_000]


# ---------------------------------------------------------------- #
# Audit log                                                         #
# ---------------------------------------------------------------- #

def test_audit_is_append_only_and_ordered(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.append_audit("order_submitted", order_id="x", symbol="RELIANCE", details={"qty": 10})
    s.append_audit("order_filled", order_id="x", symbol="RELIANCE", details={"price": 100.0})
    rows = s.load_audit()
    assert [r["action"] for r in rows] == ["order_submitted", "order_filled"]
    assert rows[0]["details"] == {"qty": 10}
    assert rows[1]["details"] == {"price": 100.0}


# ---------------------------------------------------------------- #
# KV flags                                                          #
# ---------------------------------------------------------------- #

def test_kill_switch_flag_roundtrip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.get_flag("kill_switch") is None
    assert s.get_flag("kill_switch", default="0") == "0"
    s.set_flag("kill_switch", "1")
    assert s.get_flag("kill_switch") == "1"
    s.set_flag("kill_switch", "0")
    assert s.get_flag("kill_switch") == "0"
