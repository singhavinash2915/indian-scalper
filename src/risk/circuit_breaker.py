"""Pre-entry risk gates and portfolio-level circuit breakers.

These are the *four* gates the scan loop consults before allowing a new
trade to open, plus the EOD square-off predicate:

1. Position limits          — max_equity_positions + max_fno_positions.
2. Daily loss limit         — halt trading for the rest of the session.
3. Drawdown circuit breaker — halt until a human resets it.
4. Kill switch              — owned by ``StateStore.get_flag``, handled
                              in the scan loop, not here.

Every check returns a ``RiskGate`` — an `allow_new_entries` bool + a
human-readable `reason`. Scan loop logs the reason whenever a gate
blocks entry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time as dtime

from brokers.base import Position, Segment
from config.settings import MarketCfg, RiskCfg


@dataclass(frozen=True)
class RiskGate:
    allow_new_entries: bool
    reason: str | None = None


# ---------------------------------------------------------------- #
# Position count limits                                             #
# ---------------------------------------------------------------- #

def check_position_limits(
    open_positions: Iterable[Position],
    new_segment: Segment,
    cfg: RiskCfg,
    instrument_segments: dict[str, Segment] | None = None,
) -> RiskGate:
    """Block if opening a new ``new_segment`` position would push us
    over the per-segment limit.

    ``instrument_segments`` is an optional lookup from symbol → Segment
    so the gate can bucket existing positions correctly. If not
    provided, all positions are counted as equity (conservative).
    """
    equity_count = 0
    fno_count = 0
    for p in open_positions:
        seg = (
            instrument_segments.get(p.symbol, Segment.EQUITY)
            if instrument_segments else Segment.EQUITY
        )
        if seg == Segment.EQUITY:
            equity_count += 1
        else:
            fno_count += 1

    if new_segment == Segment.EQUITY:
        if equity_count >= cfg.max_equity_positions:
            return RiskGate(
                False,
                f"equity positions at cap ({equity_count}/{cfg.max_equity_positions})",
            )
    else:
        if fno_count >= cfg.max_fno_positions:
            return RiskGate(
                False,
                f"F&O positions at cap ({fno_count}/{cfg.max_fno_positions})",
            )
    return RiskGate(True)


# ---------------------------------------------------------------- #
# Daily loss limit                                                  #
# ---------------------------------------------------------------- #

def check_daily_loss_limit(
    current_equity: float,
    start_of_day_equity: float,
    cfg: RiskCfg,
) -> RiskGate:
    """Halt the rest of the day if current equity has dropped
    ``daily_loss_limit_pct`` from the start-of-day value.

    This halt is automatically released at the next trading day's
    start of session — caller tracks ``start_of_day_equity`` per day.
    """
    if start_of_day_equity <= 0:
        return RiskGate(True)  # can't compute; don't block

    day_pnl_pct = (current_equity - start_of_day_equity) / start_of_day_equity * 100.0
    if day_pnl_pct <= -cfg.daily_loss_limit_pct:
        return RiskGate(
            False,
            f"daily loss {day_pnl_pct:.2f}% breached limit "
            f"-{cfg.daily_loss_limit_pct}%",
        )
    return RiskGate(True)


# ---------------------------------------------------------------- #
# Drawdown from peak                                                #
# ---------------------------------------------------------------- #

def check_drawdown_circuit(
    current_equity: float,
    peak_equity: float,
    cfg: RiskCfg,
) -> RiskGate:
    """Halt until manual reset if equity has dropped ``drawdown_circuit_breaker_pct``
    from the all-time peak equity.

    Unlike the daily-loss halt, this one does NOT auto-release overnight —
    a human has to inspect, adjust, and explicitly clear the circuit
    breaker flag (mechanism handled by the scan loop, not here).
    """
    if peak_equity <= 0:
        return RiskGate(True)

    dd_pct = (peak_equity - current_equity) / peak_equity * 100.0
    if dd_pct >= cfg.drawdown_circuit_breaker_pct:
        return RiskGate(
            False,
            f"drawdown {dd_pct:.2f}% from peak ₹{peak_equity:,.0f} "
            f"breached circuit breaker {cfg.drawdown_circuit_breaker_pct}%",
        )
    return RiskGate(True)


# ---------------------------------------------------------------- #
# EOD square-off predicate                                          #
# ---------------------------------------------------------------- #

def is_eod_squareoff_time(now: datetime, market_cfg: MarketCfg) -> bool:
    """True if ``now.time() >= market_cfg.eod_squareoff`` — scan loop
    closes all intraday positions at this point.
    """
    hh, mm = map(int, market_cfg.eod_squareoff.split(":"))
    cutoff = dtime(hh, mm)
    return now.time() >= cutoff


# ---------------------------------------------------------------- #
# Helpers for the scan loop                                         #
# ---------------------------------------------------------------- #

def combine_gates(*gates: RiskGate) -> RiskGate:
    """Short-circuit AND across multiple gates. Returns the first
    blocking gate so its reason is surfaced; otherwise a clean pass."""
    for g in gates:
        if not g.allow_new_entries:
            return g
    return RiskGate(True)


def peak_equity_from_curve(equity_curve_rows: Iterable[dict]) -> float:
    """Scan the equity_curve table output and return the running peak.
    Returns 0.0 for an empty curve."""
    peak = 0.0
    for row in equity_curve_rows:
        eq = float(row.get("equity", 0.0))
        if eq > peak:
            peak = eq
    return peak


def start_of_day_equity(
    equity_curve_rows: Iterable[dict], session_date: datetime
) -> float | None:
    """First ``equity`` on ``session_date`` (the IST calendar day of
    ``session_date``). Returns ``None`` if there's no row for that day
    yet — caller falls back to opening balance."""
    target = session_date.date()
    for row in equity_curve_rows:
        ts = datetime.fromisoformat(row["ts"])
        if ts.date() == target:
            return float(row["equity"])
    return None
