"""Smoke test: PaperBroker instantiates + SQLite schema initialises.

Order placement / fill simulation lands in Deliverable 4.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from brokers.paper import PaperBroker
from config.settings import Settings


def test_paper_broker_initialises(tmp_path: Path) -> None:
    settings = Settings.from_template()
    db_path = tmp_path / "scalper.db"

    broker = PaperBroker(settings, db_path=str(db_path))

    assert broker.cash == settings.capital.starting_inr
    assert broker.positions == {}
    assert broker.orders == {}
    assert broker.slippage_pct == 0.05


def test_paper_broker_creates_tables(tmp_path: Path) -> None:
    settings = Settings.from_template()
    db_path = tmp_path / "scalper.db"

    PaperBroker(settings, db_path=str(db_path))

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()

    assert {r[0] for r in rows} == {"orders", "positions", "equity_curve"}


def test_get_funds_initial_state(tmp_path: Path) -> None:
    settings = Settings.from_template()
    broker = PaperBroker(settings, db_path=str(tmp_path / "scalper.db"))

    funds = broker.get_funds()

    assert funds["available"] == settings.capital.starting_inr
    assert funds["used"] == 0.0
    assert funds["equity"] == settings.capital.starting_inr
