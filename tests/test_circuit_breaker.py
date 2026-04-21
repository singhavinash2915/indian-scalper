"""Risk gates: position limits, daily loss, drawdown, EOD square-off."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from brokers.base import Position, Segment
from config.settings import Settings
from risk.circuit_breaker import (
    RiskGate,
    check_daily_loss_limit,
    check_drawdown_circuit,
    check_position_limits,
    combine_gates,
    is_eod_squareoff_time,
    peak_equity_from_curve,
    start_of_day_equity,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def settings():
    return Settings.from_template()


def _pos(symbol: str, qty: int = 10) -> Position:
    return Position(symbol=symbol, qty=qty, avg_price=100.0)


# ---------------------------------------------------------------- #
# Position limits                                                   #
# ---------------------------------------------------------------- #

def test_equity_position_cap_allows_up_to_limit(settings) -> None:
    positions = [_pos("RELIANCE"), _pos("TCS")]
    g = check_position_limits(positions, Segment.EQUITY, settings.risk)
    assert g.allow_new_entries is True


def test_equity_position_cap_blocks_at_limit(settings) -> None:
    # cfg.max_equity_positions = 3 by default.
    positions = [_pos("A"), _pos("B"), _pos("C")]
    g = check_position_limits(positions, Segment.EQUITY, settings.risk)
    assert g.allow_new_entries is False
    assert g.reason is not None and "equity" in g.reason


def test_fno_limit_uses_segment_map(settings) -> None:
    positions = [_pos("NIFTYFUT"), _pos("BANKNIFTYFUT")]
    segments = {"NIFTYFUT": Segment.FUTURES, "BANKNIFTYFUT": Segment.FUTURES}
    # cfg.max_fno_positions = 2 by default → one more F&O blocked.
    g = check_position_limits(
        positions, Segment.FUTURES, settings.risk, instrument_segments=segments,
    )
    assert g.allow_new_entries is False
    assert g.reason is not None and "F&O" in g.reason


def test_fno_cap_allows_equity_trade_when_fno_is_full(settings) -> None:
    positions = [_pos("N"), _pos("B")]
    segments = {"N": Segment.FUTURES, "B": Segment.FUTURES}
    g = check_position_limits(
        positions, Segment.EQUITY, settings.risk, instrument_segments=segments,
    )
    assert g.allow_new_entries is True


# ---------------------------------------------------------------- #
# Daily loss limit                                                  #
# ---------------------------------------------------------------- #

def test_daily_loss_limit_passes_when_flat(settings) -> None:
    g = check_daily_loss_limit(500_000, 500_000, settings.risk)
    assert g.allow_new_entries is True


def test_daily_loss_limit_passes_when_up(settings) -> None:
    g = check_daily_loss_limit(505_000, 500_000, settings.risk)
    assert g.allow_new_entries is True


def test_daily_loss_limit_blocks_at_threshold(settings) -> None:
    # cfg.daily_loss_limit_pct = 3.0 → 485,000 = -3.0%, blocked.
    g = check_daily_loss_limit(485_000, 500_000, settings.risk)
    assert g.allow_new_entries is False
    assert g.reason is not None and "daily loss" in g.reason


def test_daily_loss_limit_ignores_zero_start(settings) -> None:
    g = check_daily_loss_limit(100, 0, settings.risk)
    assert g.allow_new_entries is True


# ---------------------------------------------------------------- #
# Drawdown circuit breaker                                          #
# ---------------------------------------------------------------- #

def test_drawdown_circuit_passes_when_near_peak(settings) -> None:
    g = check_drawdown_circuit(550_000, 560_000, settings.risk)
    assert g.allow_new_entries is True  # ~1.8% < 10%


def test_drawdown_circuit_blocks_at_threshold(settings) -> None:
    # 10% from peak → blocked.
    g = check_drawdown_circuit(450_000, 500_000, settings.risk)
    assert g.allow_new_entries is False
    assert g.reason is not None and "drawdown" in g.reason


def test_drawdown_circuit_ignores_zero_peak(settings) -> None:
    g = check_drawdown_circuit(0, 0, settings.risk)
    assert g.allow_new_entries is True


# ---------------------------------------------------------------- #
# EOD square-off                                                    #
# ---------------------------------------------------------------- #

def test_is_eod_squareoff_time(settings) -> None:
    # default eod_squareoff = "15:20"
    before = datetime(2026, 4, 21, 15, 19, tzinfo=IST)
    at = datetime(2026, 4, 21, 15, 20, tzinfo=IST)
    after = datetime(2026, 4, 21, 15, 25, tzinfo=IST)
    assert is_eod_squareoff_time(before, settings.market) is False
    assert is_eod_squareoff_time(at, settings.market) is True
    assert is_eod_squareoff_time(after, settings.market) is True


# ---------------------------------------------------------------- #
# Gate combinator                                                   #
# ---------------------------------------------------------------- #

def test_combine_gates_short_circuits_on_first_block() -> None:
    a = RiskGate(True)
    b = RiskGate(False, "limits")
    c = RiskGate(False, "drawdown")
    result = combine_gates(a, b, c)
    assert result.allow_new_entries is False
    assert result.reason == "limits"


def test_combine_gates_all_pass_returns_clean_gate() -> None:
    result = combine_gates(RiskGate(True), RiskGate(True))
    assert result.allow_new_entries is True
    assert result.reason is None


# ---------------------------------------------------------------- #
# Helpers over equity_curve rows                                    #
# ---------------------------------------------------------------- #

def test_peak_equity_from_curve() -> None:
    rows = [
        {"ts": "2026-04-21T10:00:00+05:30", "equity": 500_000, "cash": 500_000, "pnl": 0},
        {"ts": "2026-04-21T11:00:00+05:30", "equity": 510_000, "cash": 500_000, "pnl": 10_000},
        {"ts": "2026-04-21T12:00:00+05:30", "equity": 495_000, "cash": 500_000, "pnl": -5_000},
        {"ts": "2026-04-21T13:00:00+05:30", "equity": 515_000, "cash": 500_000, "pnl": 15_000},
    ]
    assert peak_equity_from_curve(rows) == 515_000


def test_peak_equity_from_empty_curve_is_zero() -> None:
    assert peak_equity_from_curve([]) == 0.0


def test_start_of_day_equity_returns_first_row_for_date() -> None:
    rows = [
        {"ts": "2026-04-20T15:30:00+05:30", "equity": 500_000, "cash": 500_000, "pnl": 0},
        {"ts": "2026-04-21T09:15:00+05:30", "equity": 502_000, "cash": 502_000, "pnl": 0},
        {"ts": "2026-04-21T09:30:00+05:30", "equity": 505_000, "cash": 502_000, "pnl": 3_000},
    ]
    session = datetime(2026, 4, 21, 10, 0, tzinfo=IST)
    assert start_of_day_equity(rows, session) == 502_000


def test_start_of_day_equity_returns_none_when_no_row() -> None:
    rows = [
        {"ts": "2026-04-20T15:30:00+05:30", "equity": 500_000, "cash": 500_000, "pnl": 0},
    ]
    session = datetime(2026, 4, 21, 10, 0, tzinfo=IST)
    assert start_of_day_equity(rows, session) is None
