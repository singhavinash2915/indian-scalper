"""NSE instrument master, SQLite-backed.

Two entry points:

* ``InstrumentMaster.load_equity_from_csv(path)`` — parse a local NSE
  ``EQUITY_L.csv`` (the CSV format NSE archives publishes daily) and
  upsert into SQLite. This is what tests hit.

* ``InstrumentMaster.refresh_equity_from_network()`` — fetch
  ``EQUITY_L.csv`` with httpx + tenacity retry, save to the cache dir,
  then delegate to the loader. Intended as a one-shot CLI / scheduled job.

F&O (futures + options) instrument loading is deferred to the F&O
deliverable — the table schema already has the required columns so we
don't have to migrate later.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from brokers.base import Instrument, Segment

NSE_EQUITY_MASTER_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

# NSE serves its archives with a User-Agent filter — a browser-ish UA is
# required. This is not evasion; it's NSE's documented expectation.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/csv,application/csv,*/*;q=0.8",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    tick_size REAL NOT NULL DEFAULT 0.05,
    lot_size INTEGER NOT NULL DEFAULT 1,
    expiry TEXT,
    strike REAL,
    option_type TEXT,
    name TEXT,
    isin TEXT,
    series TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instruments_segment ON instruments(segment);
CREATE INDEX IF NOT EXISTS idx_instruments_exchange ON instruments(exchange);
"""


class InstrumentMaster:
    """SQLite-backed instrument master. One row per tradeable symbol."""

    def __init__(
        self,
        db_path: str | Path,
        cache_dir: str | Path = "data/instruments",
    ) -> None:
        self._db_path = str(db_path)
        self._cache_dir = Path(cache_dir)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # Loading                                                             #
    # ------------------------------------------------------------------ #

    def load_equity_from_csv(self, csv_path: str | Path) -> int:
        """Parse NSE ``EQUITY_L.csv`` format and upsert into SQLite.

        NSE columns (as of 2026, order stable since 2014):
            SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,
            MARKET LOT, ISIN NUMBER, FACE VALUE

        We only keep EQ series (regular equity; skip BE/BL/BT/SM and
        similar illiquid segments).
        """
        rows = list(self._parse_equity_csv(Path(csv_path)))
        if not rows:
            raise ValueError(f"no EQ rows parsed from {csv_path}")

        updated_at = datetime.utcnow().isoformat(timespec="seconds")
        with sqlite3.connect(self._db_path) as conn:
            conn.executemany(
                """
                INSERT INTO instruments(
                    symbol, exchange, segment, tick_size, lot_size,
                    expiry, strike, option_type, name, isin, series, updated_at
                ) VALUES (?, 'NSE', ?, 0.05, ?, NULL, NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    exchange=excluded.exchange,
                    segment=excluded.segment,
                    lot_size=excluded.lot_size,
                    name=excluded.name,
                    isin=excluded.isin,
                    series=excluded.series,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        r["symbol"],
                        Segment.EQUITY.value,
                        r["lot_size"],
                        r["name"],
                        r["isin"],
                        r["series"],
                        updated_at,
                    )
                    for r in rows
                ],
            )
        logger.info("Loaded {} NSE equity instruments from {}", len(rows), csv_path)
        return len(rows)

    @staticmethod
    def _parse_equity_csv(path: Path) -> Iterable[dict]:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            # NSE CSV uses inconsistent whitespace in headers — normalise.
            field_map = {name: (name.strip() if name else name) for name in reader.fieldnames or []}
            for raw in reader:
                row = {field_map[k]: (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k}
                series = row.get("SERIES", "")
                if series != "EQ":
                    continue
                try:
                    lot = int(row.get("MARKET LOT") or "1")
                except ValueError:
                    lot = 1
                yield {
                    "symbol": row["SYMBOL"],
                    "name": row.get("NAME OF COMPANY", ""),
                    "series": series,
                    "lot_size": lot,
                    "isin": row.get("ISIN NUMBER", ""),
                }

    # ------------------------------------------------------------------ #
    # Network refresh                                                     #
    # ------------------------------------------------------------------ #

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _download_equity_master(self) -> Path:
        logger.info("Fetching NSE equity master from {}", NSE_EQUITY_MASTER_URL)
        dest = self._cache_dir / "EQUITY_L.csv"
        with httpx.Client(timeout=30.0, headers=_FETCH_HEADERS, follow_redirects=True) as client:
            resp = client.get(NSE_EQUITY_MASTER_URL)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return dest

    def refresh_equity_from_network(self) -> int:
        """Download EQUITY_L.csv + load. Intended for CLI / scheduled refresh."""
        csv_path = self._download_equity_master()
        return self.load_equity_from_csv(csv_path)

    # ------------------------------------------------------------------ #
    # Queries                                                             #
    # ------------------------------------------------------------------ #

    def get(self, symbol: str) -> Instrument | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT symbol, exchange, segment, tick_size, lot_size, "
                "expiry, strike, option_type FROM instruments WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return _row_to_instrument(row) if row else None

    def filter(
        self,
        segment: Segment | None = None,
        exchange: str | None = None,
    ) -> list[Instrument]:
        sql = (
            "SELECT symbol, exchange, segment, tick_size, lot_size, "
            "expiry, strike, option_type FROM instruments"
        )
        where: list[str] = []
        args: list[str] = []
        if segment is not None:
            where.append("segment = ?")
            args.append(segment.value)
        if exchange is not None:
            where.append("exchange = ?")
            args.append(exchange)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY symbol"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, args).fetchall()
        return [i for i in (_row_to_instrument(r) for r in rows) if i is not None]

    def count(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()
        return int(n)


def _row_to_instrument(row: tuple) -> Instrument | None:
    if row is None:
        return None
    symbol, exchange, segment, tick_size, lot_size, expiry, strike, option_type = row
    return Instrument(
        symbol=symbol,
        exchange=exchange,
        segment=Segment(segment),
        tick_size=float(tick_size),
        lot_size=int(lot_size),
        expiry=datetime.fromisoformat(expiry) if expiry else None,
        strike=float(strike) if strike is not None else None,
        option_type=option_type,  # type: ignore[arg-type]
    )
