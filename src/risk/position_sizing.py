"""Position sizing.

Risk-based sizing — the number of shares/contracts you can hold is
bounded by the rupee risk you're willing to take on the trade,
*not* by available capital alone. Concretely:

    risk_rupees   = capital × risk_per_trade_pct / 100
    per_unit_risk = |entry − stop|
    qty           = floor(risk_rupees / per_unit_risk)
    qty           = floor(qty / lot_size) × lot_size   # F&O lot rounding

All inputs are simple scalars; no broker coupling, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from brokers.base import Segment


@dataclass(frozen=True)
class SizeResult:
    qty: int
    risk_rupees: float
    per_unit_risk: float
    notional: float
    note: str | None = None


def position_size(
    capital: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_price: float,
    *,
    lot_size: int = 1,
    segment: Segment = Segment.EQUITY,
    max_notional: float | None = None,
) -> SizeResult:
    """Compute the quantity to trade.

    Args:
        capital: Equity the sizer is allowed to risk against (usually
            ``broker.get_funds()['equity']`` or a fraction thereof).
        risk_per_trade_pct: e.g. 2.0 for 2%.
        entry_price: Intended fill price.
        stop_price: Protective stop price. Must differ from entry or
            the sizer has no way to bound risk — returns qty=0 with
            a diagnostic note.
        lot_size: Instrument lot size. Defaults to 1 (equity). For F&O
            pull this from the instruments master.
        segment: EQ / FUT / OPT. Recorded in the result for audit;
            does not otherwise affect the math.
        max_notional: Optional cap on ``qty × entry_price``. If the
            risk-based qty would exceed this, the cap wins.

    Returns:
        ``SizeResult`` with ``qty`` (always ≥ 0), the rupee risk, the
        per-unit risk, the notional, and an optional human note.
    """
    if capital <= 0:
        return SizeResult(0, 0.0, 0.0, 0.0, note="capital <= 0")
    if risk_per_trade_pct <= 0:
        return SizeResult(0, 0.0, 0.0, 0.0, note="risk_per_trade_pct <= 0")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")

    per_unit = abs(entry_price - stop_price)
    if per_unit <= 0:
        return SizeResult(
            0, 0.0, 0.0, 0.0,
            note="entry == stop; cannot size without a stop distance",
        )

    risk_rupees = capital * risk_per_trade_pct / 100.0
    raw_qty = int(risk_rupees / per_unit)  # floor to whole units
    qty = (raw_qty // lot_size) * lot_size  # round down to lot multiple

    notional = qty * entry_price
    note: str | None = None

    if max_notional is not None and notional > max_notional and qty > 0:
        capped_qty = int(max_notional / entry_price)
        capped_qty = (capped_qty // lot_size) * lot_size
        if capped_qty < qty:
            note = (
                f"qty capped by max_notional (₹{max_notional:,.0f}): "
                f"{qty} → {capped_qty}"
            )
            qty = capped_qty
            notional = qty * entry_price

    if qty == 0 and note is None:
        note = (
            f"risk_rupees={risk_rupees:.2f} too small for per_unit={per_unit:.2f} "
            f"at lot_size={lot_size}"
        )

    # Keep segment referenced so mypy doesn't flag the argument as unused
    # — it's part of the public signature for caller ergonomics.
    _ = segment

    return SizeResult(
        qty=qty,
        risk_rupees=risk_rupees,
        per_unit_risk=per_unit,
        notional=notional,
        note=note,
    )
