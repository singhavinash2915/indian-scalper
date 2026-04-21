"""SQLite-backed persistence for broker state.

This is the DAO layer. Business logic (order lifecycle, fill simulation)
lives in ``src/execution/order_manager.py``; this module only knows how
to push rows in and pull them out.

Schema:

* ``orders``         — every Order ever placed (PENDING → FILLED/CANCELLED/REJECTED).
* ``positions``      — open positions, one row per symbol. Row is DELETED
                       when qty returns to zero; full history lives in audit.
* ``equity_curve``   — one row per mark-to-market snapshot.
* ``audit_log``      — append-only journal of broker / order state changes.
* ``control_flags``  — operator control plane: ``trade_mode``,
                       ``scheduler_state``, ``kill_switch`` and friends.
                       UI publishes intent by writing here; the scheduler +
                       brokers read at call-time.
* ``operator_audit`` — append-only journal of operator actions (mode
                       changes, pause/resume, kill). Separate from
                       ``audit_log`` so trade-side and operator-side
                       history don't tangle.
* ``kv``             — legacy simple key/value store. Kept for backward
                       compatibility; new code should use control_flags.

Timestamps are stored as ISO-8601 strings. Prices and quantities are
floats and ints respectively. Nothing fancy — this is a local file, not
a production database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from brokers.base import Order, OrderType, Position, Side

IST = ZoneInfo("Asia/Kolkata")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    price REAL,
    trigger_price REAL,
    status TEXT NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0.0,
    ts TEXT NOT NULL,
    filled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    trail_stop REAL,
    opened_at TEXT
);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    pnl REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    order_id TEXT,
    symbol TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_flags (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS operator_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT,
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_operator_audit_ts ON operator_audit(ts);
"""


class StateStore:
    """Thin DAO over SQLite. Opens a fresh connection per call (SQLite
    handles concurrent readers fine for our volumes; every write runs
    inside its own ``BEGIN``/``COMMIT`` via the context manager)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ #
    # Infrastructure                                                      #
    # ------------------------------------------------------------------ #

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Orders                                                              #
    # ------------------------------------------------------------------ #

    def save_order(self, order: Order) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO orders(
                    id, symbol, side, qty, order_type, price, trigger_price,
                    status, filled_qty, avg_price, ts, filled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    symbol=excluded.symbol,
                    side=excluded.side,
                    qty=excluded.qty,
                    order_type=excluded.order_type,
                    price=excluded.price,
                    trigger_price=excluded.trigger_price,
                    status=excluded.status,
                    filled_qty=excluded.filled_qty,
                    avg_price=excluded.avg_price,
                    ts=excluded.ts
                """,
                (
                    order.id, order.symbol, order.side.value, order.qty,
                    order.order_type.value, order.price, order.trigger_price,
                    order.status, order.filled_qty, order.avg_price,
                    order.ts.isoformat(),
                ),
            )

    def update_order_status(
        self,
        order_id: str,
        status: str,
        *,
        filled_qty: int | None = None,
        avg_price: float | None = None,
        filled_at: datetime | None = None,
    ) -> None:
        fields = ["status = ?"]
        args: list[Any] = [status]
        if filled_qty is not None:
            fields.append("filled_qty = ?")
            args.append(filled_qty)
        if avg_price is not None:
            fields.append("avg_price = ?")
            args.append(avg_price)
        if filled_at is not None:
            fields.append("filled_at = ?")
            args.append(filled_at.isoformat())
        args.append(order_id)
        with self._conn() as c:
            c.execute(f"UPDATE orders SET {', '.join(fields)} WHERE id = ?", args)

    def load_orders(self, *, status: str | None = None) -> list[Order]:
        sql = "SELECT * FROM orders"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY ts"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [_row_to_order(r) for r in rows]

    def get_order(self, order_id: str) -> Order | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return _row_to_order(row) if row else None

    # ------------------------------------------------------------------ #
    # Positions                                                           #
    # ------------------------------------------------------------------ #

    def save_position(self, pos: Position) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO positions(
                    symbol, qty, avg_price, stop_loss, take_profit,
                    trail_stop, opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    qty=excluded.qty,
                    avg_price=excluded.avg_price,
                    stop_loss=excluded.stop_loss,
                    take_profit=excluded.take_profit,
                    trail_stop=excluded.trail_stop
                """,
                (
                    pos.symbol, pos.qty, pos.avg_price, pos.stop_loss,
                    pos.take_profit, pos.trail_stop,
                    pos.opened_at.isoformat() if pos.opened_at else None,
                ),
            )

    def delete_position(self, symbol: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))

    def load_positions(self) -> list[Position]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM positions ORDER BY symbol"
            ).fetchall()
        return [_row_to_position(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Equity curve                                                        #
    # ------------------------------------------------------------------ #

    def snapshot_equity(
        self, ts: datetime, equity: float, cash: float, pnl: float
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO equity_curve(ts, equity, cash, pnl)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    equity=excluded.equity,
                    cash=excluded.cash,
                    pnl=excluded.pnl
                """,
                (ts.isoformat(), equity, cash, pnl),
            )

    def load_equity_curve(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, equity, cash, pnl FROM equity_curve ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Audit (append-only)                                                 #
    # ------------------------------------------------------------------ #

    def append_audit(
        self,
        action: str,
        *,
        order_id: str | None = None,
        symbol: str | None = None,
        details: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO audit_log(ts, action, order_id, symbol, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (ts or datetime.now(IST)).isoformat(),
                    action,
                    order_id,
                    symbol,
                    json.dumps(details) if details else None,
                ),
            )

    def load_audit(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, ts, action, order_id, symbol, details FROM audit_log ORDER BY id"
        args: tuple = ()
        if limit:
            sql += " LIMIT ?"
            args = (limit,)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            if row.get("details"):
                row["details"] = json.loads(row["details"])
            out.append(row)
        return out

    # ------------------------------------------------------------------ #
    # Control flags — operator control plane (trade_mode, scheduler      #
    # _state, kill_switch). Every set writes a row to operator_audit.    #
    # ------------------------------------------------------------------ #

    def set_flag(
        self,
        key: str,
        value: str,
        actor: str = "system",
        *,
        trace_id: str | None = None,
    ) -> None:
        """Set a control flag and append a matching operator-audit row.

        ``actor`` identifies who triggered the change — ``"web"`` for
        dashboard-originated writes, ``"scheduler"`` for latched-by-risk
        trips, ``"system"`` for init/seed.
        """
        now_iso = datetime.now(IST).isoformat()
        with self._conn() as c:
            # Snapshot previous value for the audit payload.
            prev_row = c.execute(
                "SELECT value FROM control_flags WHERE key = ?", (key,),
            ).fetchone()
            previous = prev_row["value"] if prev_row else None

            c.execute(
                """
                INSERT INTO control_flags(key, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by
                """,
                (key, value, now_iso, actor),
            )
            c.execute(
                "INSERT INTO operator_audit(ts, actor, action, payload_json, trace_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    now_iso, actor, f"flag_set:{key}",
                    json.dumps({"value": value, "previous": previous}),
                    trace_id,
                ),
            )

    def get_flag(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM control_flags WHERE key = ?", (key,),
            ).fetchone()
        return row["value"] if row else default

    def load_control_flags(self) -> dict[str, dict[str, Any]]:
        """All flags with their audit metadata — used by the UI state
        endpoint."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, value, updated_at, updated_by FROM control_flags ORDER BY key",
            ).fetchall()
        return {r["key"]: dict(r) for r in rows}

    def ensure_initial_flags(self, defaults: dict[str, str], actor: str = "system_init") -> list[str]:
        """Seed control flags that don't yet exist. Returns the list of
        keys that were actually inserted (so callers can audit first-run
        vs subsequent starts)."""
        inserted: list[str] = []
        for key, value in defaults.items():
            if self.get_flag(key) is None:
                self.set_flag(key, value, actor=actor)
                inserted.append(key)
        return inserted

    # ------------------------------------------------------------------ #
    # Operator audit (append-only) — operator control-plane actions.      #
    # Separate from ``audit_log`` which journals broker/order events.    #
    # ------------------------------------------------------------------ #

    def append_operator_audit(
        self,
        action: str,
        *,
        actor: str = "system",
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
        ts: datetime | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO operator_audit(ts, actor, action, payload_json, trace_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    (ts or datetime.now(IST)).isoformat(),
                    actor, action,
                    json.dumps(payload) if payload is not None else None,
                    trace_id,
                ),
            )

    def load_operator_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, actor, action, payload_json, trace_id "
                "FROM operator_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            if row.get("payload_json"):
                row["payload"] = json.loads(row["payload_json"])
            else:
                row["payload"] = None
            row.pop("payload_json", None)
            out.append(row)
        return out


# ---------------------------------------------------------------------- #
# Row → dataclass helpers                                                 #
# ---------------------------------------------------------------------- #

def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"],
        symbol=row["symbol"],
        side=Side(row["side"]),
        qty=int(row["qty"]),
        order_type=OrderType(row["order_type"]),
        price=row["price"],
        trigger_price=row["trigger_price"],
        status=row["status"],
        filled_qty=int(row["filled_qty"]),
        avg_price=float(row["avg_price"]),
        ts=datetime.fromisoformat(row["ts"]),
    )


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        symbol=row["symbol"],
        qty=int(row["qty"]),
        avg_price=float(row["avg_price"]),
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
        trail_stop=row["trail_stop"],
        opened_at=(
            datetime.fromisoformat(row["opened_at"])
            if row["opened_at"] else None
        ),
    )
