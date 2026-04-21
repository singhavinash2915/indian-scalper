"""StateStore.signal_snapshots DAO — unit tests for D11 Slice 3.

Covers append/load/filter/prune + the counterfactual query the spec
names explicitly: "show me trades that would have fired if mode had
been paper, on days mode was watch_only".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from brokers.base import Side  # noqa: F401 — loads brokers package first to dodge a circular import
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


# ---------------- append + load ---------------- #

def test_append_and_load_recent(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    s.append_signal_snapshot(
        ts=T0, symbol="RELIANCE", score=6,
        breakdown={"ema_stack": True, "rsi_entry": True},
        action="entered", reason="placed MARKET BUY", trace_id="abc123",
        trade_mode="paper",
    )
    rows = s.load_recent_signals()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "RELIANCE"
    assert r["score"] == 6
    assert r["action"] == "entered"
    assert r["trade_mode"] == "paper"
    assert r["breakdown"] == {"ema_stack": True, "rsi_entry": True}
    assert r["trace_id"] == "abc123"


def test_load_recent_orders_newest_first(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    for i in range(5):
        s.append_signal_snapshot(
            ts=T0 + timedelta(minutes=i), symbol="X", score=i,
            breakdown={}, action="skipped_score", reason=None,
            trace_id=None, trade_mode="paper",
        )
    rows = s.load_recent_signals(limit=3)
    assert [r["score"] for r in rows] == [4, 3, 2]


def test_load_recent_respects_limit_cap(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    for i in range(10):
        s.append_signal_snapshot(
            ts=T0, symbol="X", score=0, breakdown={},
            action="skipped_filter", reason=None, trace_id=None,
            trade_mode="paper",
        )
    rows = s.load_recent_signals(limit=5)
    assert len(rows) == 5


def test_load_recent_filters_by_min_score(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    for i in range(0, 9):
        s.append_signal_snapshot(
            ts=T0, symbol="X", score=i, breakdown={},
            action="entered", reason=None, trace_id=None, trade_mode="paper",
        )
    rows = s.load_recent_signals(min_score=6)
    assert all(r["score"] >= 6 for r in rows)
    assert len(rows) == 3  # 6, 7, 8


def test_load_recent_filters_by_actions(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    s.append_signal_snapshot(
        ts=T0, symbol="A", score=6, breakdown={}, action="entered",
        reason=None, trace_id=None, trade_mode="paper",
    )
    s.append_signal_snapshot(
        ts=T0, symbol="B", score=2, breakdown={}, action="skipped_score",
        reason=None, trace_id=None, trade_mode="paper",
    )
    rows = s.load_recent_signals(actions=["entered"])
    assert len(rows) == 1
    assert rows[0]["symbol"] == "A"


def test_load_signals_for_symbol(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    s.append_signal_snapshot(
        ts=T0, symbol="RELIANCE", score=5, breakdown={},
        action="skipped_score", reason=None, trace_id=None, trade_mode="paper",
    )
    s.append_signal_snapshot(
        ts=T0, symbol="TCS", score=5, breakdown={},
        action="skipped_score", reason=None, trace_id=None, trade_mode="paper",
    )
    rows = s.load_signals_for_symbol("RELIANCE")
    assert all(r["symbol"] == "RELIANCE" for r in rows)
    assert len(rows) == 1


def test_load_signals_for_symbol_lookback_excludes_older(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    # Row 1: 3 hours ago. Row 2: 40 hours ago (outside 24h lookback).
    now = datetime.now(IST)
    s.append_signal_snapshot(
        ts=now - timedelta(hours=3), symbol="X", score=6, breakdown={},
        action="entered", reason=None, trace_id=None, trade_mode="paper",
    )
    s.append_signal_snapshot(
        ts=now - timedelta(hours=40), symbol="X", score=6, breakdown={},
        action="entered", reason=None, trace_id=None, trade_mode="paper",
    )
    assert len(s.load_signals_for_symbol("X", lookback_hours=24)) == 1
    assert len(s.load_signals_for_symbol("X", lookback_hours=72)) == 2


# ---------------- counterfactual query ---------------- #

def test_counterfactual_watch_only_high_scores(tmp_path: Path) -> None:
    """Spec's counterfactual query: "show me trades that would have
    fired if mode had been paper, on days mode was watch_only"."""
    s = StateStore(tmp_path / "state.db")
    # Two scored-but-watch-only-logged rows with score ≥ min_score.
    s.append_signal_snapshot(
        ts=T0, symbol="A", score=7, breakdown={}, action="watch_only_logged",
        reason="global mode", trace_id=None, trade_mode="watch_only",
    )
    s.append_signal_snapshot(
        ts=T0, symbol="B", score=6, breakdown={}, action="watch_only_logged",
        reason="per_symbol_override", trace_id=None, trade_mode="watch_only",
    )
    # Distractor: entered in paper mode (should NOT appear).
    s.append_signal_snapshot(
        ts=T0, symbol="C", score=6, breakdown={}, action="entered",
        reason=None, trace_id=None, trade_mode="paper",
    )
    # Distractor: low score, watch_only (should NOT appear).
    s.append_signal_snapshot(
        ts=T0, symbol="D", score=2, breakdown={}, action="watch_only_logged",
        reason=None, trace_id=None, trade_mode="watch_only",
    )

    would_have_traded = s.load_recent_signals(
        min_score=6,
        actions=["watch_only_logged"],
        trade_modes=["watch_only"],
    )
    assert {r["symbol"] for r in would_have_traded} == {"A", "B"}


# ---------------- pruning ---------------- #

def test_prune_older_than_n_days(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    now = datetime.now(IST)
    # 3 rows: 8 days old, 3 days old, 1 day old.
    for days in (8, 3, 1):
        s.append_signal_snapshot(
            ts=now - timedelta(days=days), symbol="X", score=6, breakdown={},
            action="entered", reason=None, trace_id=None, trade_mode="paper",
        )
    deleted = s.prune_signal_snapshots_older_than(days=7)
    assert deleted == 1
    rows = s.load_recent_signals(limit=100)
    assert len(rows) == 2


def test_prune_noop_when_nothing_old(tmp_path: Path) -> None:
    s = StateStore(tmp_path / "state.db")
    now = datetime.now(IST)
    s.append_signal_snapshot(
        ts=now, symbol="X", score=6, breakdown={}, action="entered",
        reason=None, trace_id=None, trade_mode="paper",
    )
    assert s.prune_signal_snapshots_older_than(days=7) == 0
