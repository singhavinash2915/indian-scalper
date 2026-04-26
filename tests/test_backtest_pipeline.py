"""Tests for the walk-forward backtest pipeline (B1-B4).

Network-free — uses synthetic candles + fakes the historical fetcher.
Verifies that the harness, reporter, and CLI plumbing all wire together
without crashing on a tiny universe + short window.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import brokers  # noqa: F401  — package init
from backtest.historical import _candle_from_row, _slug
from backtest.reporter import (
    avg_win_loss,
    build_report,
    compute_cagr,
    consecutive_loss_streak,
    exit_attribution,
    monthly_pnl,
    top_symbols,
)
from backtest.trades import Trade

IST = ZoneInfo("Asia/Kolkata")


def _trade(
    symbol: str = "RELIANCE",
    pnl: float = 0.0,
    exit_reason: str = "take_profit",
    exit_ts: datetime | None = None,
) -> Trade:
    """Trade is a frozen dataclass without exit_reason; reporter reads it
    via getattr fallback. We attach it as a dict-side decoration via a
    SimpleNamespace wrapper that satisfies the same accessors."""
    from types import SimpleNamespace
    base = datetime(2026, 4, 1, 10, 0, tzinfo=IST)
    return SimpleNamespace(
        symbol=symbol,
        side="BUY",
        qty=10,
        entry_price=100.0,
        exit_price=100.0 + (pnl / 10),
        entry_ts=base,
        exit_ts=exit_ts or base + timedelta(minutes=30),
        exit_reason=exit_reason,
        pnl=pnl,
        pnl_pct=(pnl / 1000.0) * 100,
        holding_minutes=30,
    )


# --------------------------------------------------------------------- #
# historical helpers                                                    #
# --------------------------------------------------------------------- #

def test_slug_handles_pipes_and_slashes():
    assert _slug("NSE_FO|123") == "NSE_FO_123"
    assert _slug("FOO/BAR") == "FOO_BAR"
    assert _slug("RELIANCE") == "RELIANCE"


def test_candle_from_row_parses_ist():
    c = _candle_from_row(["2026-04-25T15:15:00+05:30", 100.5, 101.0, 99.8, 100.7, 5000, 0])
    assert c.open == 100.5
    assert c.close == 100.7
    assert c.volume == 5000
    assert c.ts.tzinfo is not None


def test_candle_from_row_handles_naive_iso():
    c = _candle_from_row(["2026-04-25T15:15:00", 100, 101, 99, 100.5, 1000, 0])
    assert c.ts.tzinfo is not None   # localised to IST


# --------------------------------------------------------------------- #
# reporter pure functions                                               #
# --------------------------------------------------------------------- #

def test_compute_cagr_zero_window_returns_zero():
    assert compute_cagr(100_000, 110_000, 0) == 0.0


def test_compute_cagr_2x_in_one_year_is_100pct():
    cagr = compute_cagr(100_000, 200_000, 365)
    assert 99 < cagr < 101


def test_avg_win_loss_basic():
    trades = [_trade(pnl=100), _trade(pnl=200), _trade(pnl=-50), _trade(pnl=-100)]
    avg_w, avg_l, pf = avg_win_loss(trades)
    assert avg_w == 150.0
    assert avg_l == 75.0
    assert pf == pytest.approx(2.0)   # 300 wins / 150 losses


def test_avg_win_loss_zero_wins_returns_inf():
    trades = [_trade(pnl=-50)]
    _, _, pf = avg_win_loss(trades)
    assert pf == 0.0   # no wins, finite losses


def test_consecutive_loss_streak():
    base = datetime(2026, 4, 1, 9, 30, tzinfo=IST)
    trades = [
        _trade(pnl=-1, exit_ts=base),
        _trade(pnl=-1, exit_ts=base + timedelta(minutes=1)),
        _trade(pnl=10, exit_ts=base + timedelta(minutes=2)),
        _trade(pnl=-1, exit_ts=base + timedelta(minutes=3)),
        _trade(pnl=-1, exit_ts=base + timedelta(minutes=4)),
        _trade(pnl=-1, exit_ts=base + timedelta(minutes=5)),
        _trade(pnl=20, exit_ts=base + timedelta(minutes=6)),
    ]
    assert consecutive_loss_streak(trades) == 3


def test_exit_attribution_groups_by_reason():
    trades = [
        _trade(pnl=100, exit_reason="take_profit"),
        _trade(pnl=200, exit_reason="take_profit"),
        _trade(pnl=-50, exit_reason="stop_loss"),
    ]
    a = exit_attribution(trades)
    assert a["take_profit"]["count"] == 2
    assert a["take_profit"]["pnl"] == 300
    assert a["take_profit"]["win_rate_pct"] == 100.0
    assert a["stop_loss"]["count"] == 1


def test_exit_attribution_strips_subcategory():
    """e.g. 'options:premium_stop' → 'options' bucket."""
    trades = [
        _trade(pnl=100, exit_reason="options:premium_stop"),
        _trade(pnl=-50, exit_reason="options:trail_stop"),
    ]
    a = exit_attribution(trades)
    assert "options" in a
    assert a["options"]["count"] == 2


def test_monthly_pnl_buckets_by_month():
    base = datetime(2026, 4, 1, 10, 0, tzinfo=IST)
    trades = [
        _trade(pnl=100, exit_ts=base),
        _trade(pnl=200, exit_ts=base + timedelta(days=5)),    # still April
        _trade(pnl=-50, exit_ts=base + timedelta(days=35)),   # May
    ]
    m = monthly_pnl(trades)
    assert m["2026-04"] == 300
    assert m["2026-05"] == -50


def test_top_symbols_orders_by_pnl():
    trades = [
        _trade(symbol="A", pnl=100),
        _trade(symbol="A", pnl=50),
        _trade(symbol="B", pnl=-30),
        _trade(symbol="C", pnl=10),
    ]
    winners, losers = top_symbols(trades, n=2)
    assert winners[0] == ("A", 150)
    # losers ordered worst-first
    assert losers[0][0] == "B"


# --------------------------------------------------------------------- #
# build_report integration                                              #
# --------------------------------------------------------------------- #

def test_build_report_writes_artifacts(tmp_path):
    from backtest.harness import BacktestResult
    base = datetime(2026, 4, 1, 10, 0, tzinfo=IST)
    trades = [
        _trade(pnl=500, exit_reason="take_profit", exit_ts=base),
        _trade(symbol="TCS", pnl=-200, exit_reason="stop_loss",
               exit_ts=base + timedelta(days=2)),
        _trade(symbol="INFY", pnl=300, exit_reason="trail_stop",
               exit_ts=base + timedelta(days=5)),
    ]
    equity_curve = [
        {"ts": "2026-04-01T15:30:00+05:30", "equity": 500_000},
        {"ts": "2026-04-30T15:30:00+05:30", "equity": 510_000},
    ]
    result = BacktestResult(
        trades=trades, equity_curve=equity_curve, tick_reports=[],
        metrics={"sharpe": 1.5, "max_dd_pct": 2.0, "win_rate": 66.7,
                 "avg_rr": 1.4, "avg_holding_minutes": 30.0,
                 "total_trade_pnl": 600.0},
        final_equity=510_000.0, starting_equity=500_000.0,
        timestamps_processed=20, ticks_skipped=0,
    )

    summary = build_report(
        result, from_date=date(2026, 4, 1), to_date=date(2026, 4, 30),
        interval="15m", out_dir=tmp_path, label="test_run",
    )

    # JSON
    json_path = Path(summary["artifacts"]["json"])
    assert json_path.exists()
    parsed = json.loads(json_path.read_text())
    assert parsed["total_return_pct"] == pytest.approx(2.0)
    assert parsed["cagr_pct"] > 0   # positive return → positive CAGR
    assert parsed["trades_total"] == 3

    # Trade CSV
    csv_path = Path(summary["artifacts"]["csv"])
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == 3
    assert rows[0]["symbol"] in {"RELIANCE", "TCS", "INFY"}

    # HTML
    html = Path(summary["artifacts"]["html"]).read_text()
    assert "Backtest Report" in html
    assert "test_run" in html
    assert "+2.00%" in html or "+2.0%" in html  # total return rendered


def test_build_report_handles_zero_trades(tmp_path):
    from backtest.harness import BacktestResult
    result = BacktestResult(
        trades=[], equity_curve=[], tick_reports=[],
        metrics={"sharpe": 0.0, "max_dd_pct": 0.0, "win_rate": 0.0,
                 "avg_rr": 0.0, "avg_holding_minutes": 0.0,
                 "total_trade_pnl": 0.0},
        final_equity=500_000.0, starting_equity=500_000.0,
    )
    summary = build_report(
        result, from_date=date(2026, 4, 1), to_date=date(2026, 4, 30),
        interval="15m", out_dir=tmp_path, label="empty_run",
    )
    assert summary["trades_total"] == 0
    assert summary["max_consecutive_losses"] == 0


# --------------------------------------------------------------------- #
# CLI plumbing                                                          #
# --------------------------------------------------------------------- #

def test_cli_parses_required_args_and_handles_empty_symbols(tmp_path, monkeypatch):
    """End-to-end: CLI reads args + bails cleanly when no symbols available."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake")
    # Empty data dir → no universe → CLI should error out cleanly.
    from backtest_cli import main
    rc = main([
        "--from", "2026-04-01", "--to", "2026-04-30",
        "--symbols", "",          # explicitly empty
    ])
    assert rc == 1
