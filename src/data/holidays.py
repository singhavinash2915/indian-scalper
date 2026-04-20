"""NSE trading-holiday calendar, SQLite-backed.

The YAML shipped at ``src/data/nse_holidays.yaml`` is the authoritative
source. ``HolidayCalendar.load_from_yaml`` upserts it into SQLite so the
rest of the app gets a cheap ``is_trading_holiday`` lookup without touching
the filesystem on every call.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import yaml
from loguru import logger

DEFAULT_YAML_PATH = Path(__file__).parent / "nse_holidays.yaml"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS holidays (
    date TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    year INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holidays_year ON holidays(year);
"""


class HolidayCalendar:
    """SQLite-backed NSE trading holiday calendar."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # Loading                                                             #
    # ------------------------------------------------------------------ #

    def load_from_yaml(self, yaml_path: str | Path | None = None) -> int:
        """Upsert holidays from YAML. Returns the number of rows written."""
        path = Path(yaml_path) if yaml_path else DEFAULT_YAML_PATH
        raw = yaml.safe_load(path.read_text()) or {}

        rows: list[tuple[str, str, int]] = []
        for year, entries in raw.items():
            if not isinstance(year, int):
                raise ValueError(f"holiday YAML year key must be int, got {year!r}")
            for entry in entries or []:
                d_str = str(entry["date"])
                # Round-trip through date so the DB always holds ISO-formatted strings.
                d = date.fromisoformat(d_str)
                rows.append((d.isoformat(), str(entry["name"]), d.year))

        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(
                "INSERT INTO holidays(date, name, year) VALUES (?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET name=excluded.name, year=excluded.year",
                rows,
            )
        logger.info("Loaded {} NSE holidays from {}", len(rows), path)
        return len(rows)

    # ------------------------------------------------------------------ #
    # Queries                                                             #
    # ------------------------------------------------------------------ #

    def is_trading_holiday(self, d: date) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM holidays WHERE date = ?", (d.isoformat(),)
            ).fetchone()
        return row is not None

    def is_trading_day(self, d: date) -> bool:
        """Weekday AND not a holiday."""
        if d.weekday() >= 5:
            return False
        return not self.is_trading_holiday(d)

    def next_trading_day(self, d: date) -> date:
        """First trading day strictly after ``d`` (skips weekends + holidays)."""
        candidate = d + timedelta(days=1)
        # Cap the search window at 15 days — if we can't find a trading day
        # in two weeks, something is very wrong (or the calendar is stale).
        for _ in range(15):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise RuntimeError(
            f"No trading day found within 15 days after {d.isoformat()} — "
            "holiday calendar may be corrupt or stale."
        )

    def holidays_for_year(self, year: int) -> list[tuple[date, str]]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT date, name FROM holidays WHERE year = ? ORDER BY date",
                (year,),
            ).fetchall()
        return [(date.fromisoformat(d), name) for d, name in rows]

    def count(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM holidays").fetchone()
        return int(n)
