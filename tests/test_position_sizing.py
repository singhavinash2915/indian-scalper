"""Position sizing math — risk-based qty with lot-size rounding."""

from __future__ import annotations

import pytest

from brokers.base import Segment
from risk.position_sizing import position_size


def test_basic_equity_sizing() -> None:
    # ₹5L capital × 2% = ₹10,000 risk. Stop distance ₹5 → 2000 shares.
    r = position_size(
        capital=500_000,
        risk_per_trade_pct=2.0,
        entry_price=100.0,
        stop_price=95.0,
    )
    assert r.qty == 2000
    assert r.risk_rupees == pytest.approx(10_000.0)
    assert r.per_unit_risk == pytest.approx(5.0)
    assert r.notional == pytest.approx(200_000.0)


def test_rounds_down_to_lot_size_for_fno() -> None:
    # Risk budget allows ~133 units; lot size 50 → round down to 100.
    r = position_size(
        capital=500_000,
        risk_per_trade_pct=2.0,
        entry_price=20000.0,
        stop_price=19925.0,   # ₹75 stop distance → raw qty = 10000 / 75 = 133
        lot_size=50,
        segment=Segment.FUTURES,
    )
    assert r.qty == 100
    assert r.qty % 50 == 0


def test_returns_zero_when_stop_equals_entry() -> None:
    r = position_size(
        capital=500_000, risk_per_trade_pct=2.0,
        entry_price=100.0, stop_price=100.0,
    )
    assert r.qty == 0
    assert r.note is not None
    assert "stop" in r.note.lower()


def test_returns_zero_when_capital_is_zero() -> None:
    r = position_size(
        capital=0.0, risk_per_trade_pct=2.0,
        entry_price=100.0, stop_price=95.0,
    )
    assert r.qty == 0


def test_returns_zero_when_risk_pct_is_zero() -> None:
    r = position_size(
        capital=500_000, risk_per_trade_pct=0.0,
        entry_price=100.0, stop_price=95.0,
    )
    assert r.qty == 0


def test_rejects_nonpositive_lot_size() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        position_size(
            capital=500_000, risk_per_trade_pct=2.0,
            entry_price=100.0, stop_price=95.0, lot_size=0,
        )


def test_max_notional_cap_overrides_risk_sizing() -> None:
    # Risk says 2000 shares (₹200k notional). Cap at ₹50k → 500 shares.
    r = position_size(
        capital=500_000, risk_per_trade_pct=2.0,
        entry_price=100.0, stop_price=95.0,
        max_notional=50_000,
    )
    assert r.qty == 500
    assert r.note is not None and "capped" in r.note


def test_notional_cap_respects_lot_size() -> None:
    # Cap would give 500 shares but lot_size is 200 → 400 shares.
    r = position_size(
        capital=500_000, risk_per_trade_pct=2.0,
        entry_price=100.0, stop_price=95.0,
        lot_size=200, max_notional=50_000,
    )
    assert r.qty == 400


def test_sell_side_stop_works_the_same() -> None:
    """Short trade — stop above entry. |entry − stop| is what matters."""
    r = position_size(
        capital=500_000, risk_per_trade_pct=2.0,
        entry_price=100.0, stop_price=105.0,  # short, 5pt above entry
    )
    assert r.qty == 2000  # same math as long
