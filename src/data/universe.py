"""Universe membership — per-symbol eligibility + per-symbol watch-only.

The ``universe_membership`` table is the scheduler's source of truth for
"which symbols do we scan this tick". Config defines the *initial* set
(on first-run seed); the table is authoritative thereafter. Every
mutation appends an ``operator_audit`` row so the history of what
changed, when, and by whom is inspectable.

Key concept: ``watch_only_override``. Even when the global
``trade_mode = paper``, a symbol with this flag set still gets SCORED
(and, Slice 3 onward, its score recorded in ``signal_snapshots``) but
never ORDERED. Useful for "I want to track INFY for a week before I'm
comfortable letting the bot trade it".
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from brokers.base import Segment
from data.instruments import InstrumentMaster
from data.presets import list_available_presets, load_preset_symbols
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")

# ``none`` + ``all`` are logical operations over the existing universe
# and need no shipped symbol list. Index presets load their list from
# ``src/data/presets/{name}.yaml`` — see ``data.presets``.
LOGICAL_PRESETS: tuple[str, ...] = ("none", "all")
KNOWN_PRESETS: tuple[str, ...] = tuple(
    sorted(set(LOGICAL_PRESETS) | set(list_available_presets()))
)
IMPLEMENTED_PRESETS: tuple[str, ...] = KNOWN_PRESETS


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    segment: str
    enabled: bool
    watch_only_override: bool
    added_at: str
    added_by: str


class UnknownSymbolError(ValueError):
    """Raised when add() is called with a symbol the instruments master
    doesn't know about — refusing prevents typos from silently breaking
    the universe."""


class PresetNotImplementedError(NotImplementedError):
    """Returned as a 501 from the dashboard when an index-preset has no
    shipped symbol list yet."""


class UniverseRegistry:
    """Thin wrapper over the ``universe_membership`` table plus the
    ``instruments`` master.

    Every mutation (toggle / watch-only / add / bulk / preset) writes
    an ``operator_audit`` row before returning.
    """

    def __init__(self, store: StateStore, instruments: InstrumentMaster) -> None:
        self.store = store
        self.instruments = instruments

    # ---------------- reads ---------------- #

    def count(self) -> int:
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            (n,) = c.execute("SELECT COUNT(*) FROM universe_membership").fetchone()
        return int(n)

    def get(self, symbol: str, segment: str | Segment = Segment.EQUITY) -> UniverseEntry | None:
        seg = _seg(segment)
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            row = c.execute(
                "SELECT symbol, segment, enabled, watch_only_override, added_at, added_by "
                "FROM universe_membership WHERE symbol = ? AND segment = ?",
                (symbol, seg),
            ).fetchone()
        return _row_to_entry(row) if row else None

    def list_entries(
        self,
        segment: str | Segment | None = None,
        enabled_only: bool = False,
    ) -> list[UniverseEntry]:
        sql = (
            "SELECT symbol, segment, enabled, watch_only_override, "
            "added_at, added_by FROM universe_membership"
        )
        where: list[str] = []
        args: list[Any] = []
        if segment is not None:
            where.append("segment = ?")
            args.append(_seg(segment))
        if enabled_only:
            where.append("enabled = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY symbol"
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            rows = c.execute(sql, args).fetchall()
        return [_row_to_entry(r) for r in rows]

    def enabled_symbols(self, segment: str | Segment | None = None) -> list[str]:
        return [e.symbol for e in self.list_entries(segment=segment, enabled_only=True)]

    def is_enabled(self, symbol: str, segment: str | Segment = Segment.EQUITY) -> bool:
        entry = self.get(symbol, segment)
        return bool(entry and entry.enabled)

    def has_watch_only_override(
        self, symbol: str, segment: str | Segment = Segment.EQUITY,
    ) -> bool:
        entry = self.get(symbol, segment)
        return bool(entry and entry.watch_only_override)

    # ---------------- seeding ---------------- #

    def seed_if_empty(
        self,
        symbols: Iterable[str],
        segment: str | Segment = Segment.EQUITY,
        actor: str = "system_init",
    ) -> int:
        """Insert rows for ``symbols`` if the table is empty. Returns
        the number of rows actually inserted. Enabled by default;
        watch_only_override defaults to 0."""
        if self.count() > 0:
            return 0
        seg = _seg(segment)
        now = datetime.now(IST).isoformat()
        rows = [(s, seg, 1, 0, now, actor) for s in symbols]
        if not rows:
            return 0
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            c.executemany(
                "INSERT OR IGNORE INTO universe_membership"
                "(symbol, segment, enabled, watch_only_override, added_at, added_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        self.store.append_operator_audit(
            "universe.seed",
            actor=actor,
            payload={"segment": seg, "count": len(rows)},
        )
        logger.info("Universe seeded: {} {} symbols (actor={})", len(rows), seg, actor)
        return len(rows)

    # ---------------- single-row mutations ---------------- #

    def toggle(
        self,
        symbol: str,
        segment: str | Segment = Segment.EQUITY,
        actor: str = "web",
    ) -> UniverseEntry:
        entry = self.get(symbol, segment)
        if entry is None:
            raise KeyError(f"{symbol}/{_seg(segment)} not in universe")
        new_val = not entry.enabled
        return self.set_enabled(symbol, segment, new_val, actor=actor)

    def set_enabled(
        self,
        symbol: str,
        segment: str | Segment,
        enabled: bool,
        actor: str = "web",
    ) -> UniverseEntry:
        seg = _seg(segment)
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            c.execute(
                "UPDATE universe_membership SET enabled = ? "
                "WHERE symbol = ? AND segment = ?",
                (1 if enabled else 0, symbol, seg),
            )
            if c.total_changes == 0:
                raise KeyError(f"{symbol}/{seg} not in universe")
        self.store.append_operator_audit(
            "universe.toggle",
            actor=actor,
            payload={"symbol": symbol, "segment": seg, "enabled": enabled},
        )
        result = self.get(symbol, seg)
        assert result is not None
        return result

    def set_watch_only_override(
        self,
        symbol: str,
        segment: str | Segment,
        on: bool,
        actor: str = "web",
    ) -> UniverseEntry:
        seg = _seg(segment)
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            c.execute(
                "UPDATE universe_membership SET watch_only_override = ? "
                "WHERE symbol = ? AND segment = ?",
                (1 if on else 0, symbol, seg),
            )
            if c.total_changes == 0:
                raise KeyError(f"{symbol}/{seg} not in universe")
        self.store.append_operator_audit(
            "universe.watch_only_override",
            actor=actor,
            payload={"symbol": symbol, "segment": seg, "on": on},
        )
        result = self.get(symbol, seg)
        assert result is not None
        return result

    def add(
        self,
        symbol: str,
        segment: str | Segment = Segment.EQUITY,
        actor: str = "web",
    ) -> UniverseEntry:
        """Insert a new row. Validates the symbol against the
        instruments master — refuses unknown tickers so a typo can't
        silently add a phantom row that never scores anything."""
        seg = _seg(segment)
        inst = self.instruments.get(symbol)
        if inst is None:
            raise UnknownSymbolError(
                f"{symbol!r} not found in instruments master — refresh the "
                "master or check the ticker"
            )
        now = datetime.now(IST).isoformat()
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            c.execute(
                "INSERT INTO universe_membership"
                "(symbol, segment, enabled, watch_only_override, added_at, added_by) "
                "VALUES (?, ?, 1, 0, ?, ?) "
                "ON CONFLICT(symbol, segment) DO UPDATE SET enabled = 1, added_by = excluded.added_by",
                (symbol, seg, now, actor),
            )
        self.store.append_operator_audit(
            "universe.add",
            actor=actor,
            payload={"symbol": symbol, "segment": seg},
        )
        result = self.get(symbol, seg)
        assert result is not None
        return result

    # ---------------- bulk / presets ---------------- #

    def bulk_update(
        self,
        operations: list[dict[str, Any]],
        actor: str = "web",
    ) -> dict[str, int]:
        """Apply a batch of per-symbol changes atomically.

        Each op is ``{"symbol": ..., "segment": ..., "enabled"?: bool,
        "watch_only_override"?: bool}``. Returns a summary
        ``{enabled, disabled, watch_set, watch_cleared, missing}``.
        """
        summary = {
            "enabled": 0, "disabled": 0,
            "watch_set": 0, "watch_cleared": 0, "missing": 0,
        }
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            for op in operations:
                symbol = op["symbol"]
                seg = _seg(op.get("segment", Segment.EQUITY))
                exists = c.execute(
                    "SELECT 1 FROM universe_membership WHERE symbol=? AND segment=?",
                    (symbol, seg),
                ).fetchone()
                if not exists:
                    summary["missing"] += 1
                    continue
                if "enabled" in op:
                    c.execute(
                        "UPDATE universe_membership SET enabled = ? "
                        "WHERE symbol = ? AND segment = ?",
                        (1 if op["enabled"] else 0, symbol, seg),
                    )
                    summary["enabled" if op["enabled"] else "disabled"] += 1
                if "watch_only_override" in op:
                    c.execute(
                        "UPDATE universe_membership SET watch_only_override = ? "
                        "WHERE symbol = ? AND segment = ?",
                        (1 if op["watch_only_override"] else 0, symbol, seg),
                    )
                    summary["watch_set" if op["watch_only_override"] else "watch_cleared"] += 1
        self.store.append_operator_audit(
            "universe.bulk_update",
            actor=actor,
            payload={"op_count": len(operations), **summary},
        )
        return summary

    def apply_preset(self, preset: str, actor: str = "web") -> dict[str, int]:
        """Atomically apply a named preset.

        * ``none`` — disable every current row.
        * ``all``  — enable every current row.
        * Index presets (``nifty_50`` / ``nifty_100`` / ``nifty_next_50``
          / ``bank_nifty_only`` / any other YAML in
          ``src/data/presets/``): insert any listed symbols that aren't
          yet in ``universe_membership`` (validated against the
          instruments master — missing symbols are counted + returned
          without aborting the rest), enable the listed ones, disable
          everything NOT in the list. The whole thing runs inside a
          single SQLite transaction.

        Raises ``PresetNotImplementedError`` only if the operator asks
        for a name that's not shipped AS a YAML AND isn't a logical
        preset — which is now only true if someone adds a new preset
        name to ``KNOWN_PRESETS`` without shipping the file. In
        practice the module auto-discovers ``*.yaml`` so this is a
        belt-and-braces guard.
        """
        if preset not in KNOWN_PRESETS:
            raise ValueError(f"unknown preset {preset!r}; known: {KNOWN_PRESETS}")

        if preset in LOGICAL_PRESETS:
            return self._apply_logical_preset(preset, actor)

        # Symbol-list preset — load the YAML.
        try:
            symbols = load_preset_symbols(preset)
        except FileNotFoundError as exc:
            raise PresetNotImplementedError(str(exc)) from exc

        summary = {
            "preset": preset,
            "listed": len(symbols),
            "missing_from_instruments": 0,
            "inserted": 0,
            "enabled": 0,
            "disabled_non_members": 0,
        }
        now = datetime.now(IST).isoformat()
        # Partition preset symbols: those in the instruments master
        # (addable) vs those not (counted + skipped).
        known_symbols: list[str] = []
        for sym in symbols:
            if self.instruments.get(sym) is not None:
                known_symbols.append(sym)
            else:
                summary["missing_from_instruments"] += 1
        known_set = set(known_symbols)

        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            # Snapshot enabled-count BEFORE any mutation so the
            # diagnostic "disabled_non_members" count is accurate.
            pre_row = c.execute(
                "SELECT COUNT(*) FROM universe_membership "
                "WHERE segment = 'EQ' AND enabled = 1",
            ).fetchone()
            pre_enabled = int(pre_row[0]) if pre_row else 0

            # Insert / enable each preset symbol we can validate.
            for sym in known_symbols:
                existed = c.execute(
                    "SELECT enabled FROM universe_membership "
                    "WHERE symbol = ? AND segment = 'EQ'",
                    (sym,),
                ).fetchone()
                if not existed:
                    c.execute(
                        "INSERT INTO universe_membership"
                        "(symbol, segment, enabled, watch_only_override,"
                        " added_at, added_by) "
                        "VALUES (?, 'EQ', 1, 0, ?, ?)",
                        (sym, now, actor),
                    )
                    summary["inserted"] += 1
                else:
                    c.execute(
                        "UPDATE universe_membership SET enabled = 1 "
                        "WHERE symbol = ? AND segment = 'EQ'",
                        (sym,),
                    )
            summary["enabled"] = len(known_symbols)

            # Disable every existing EQ row NOT in the preset. We keep
            # the rows (soft-delete) so their history is preserved and
            # a later re-enable keeps the original added_at.
            if known_set:
                placeholders = ",".join("?" for _ in known_set)
                c.execute(
                    f"UPDATE universe_membership SET enabled = 0 "
                    f"WHERE segment = 'EQ' AND symbol NOT IN ({placeholders})",
                    list(known_set),
                )
            else:
                c.execute(
                    "UPDATE universe_membership SET enabled = 0 WHERE segment = 'EQ'",
                )
            post_row = c.execute(
                "SELECT COUNT(*) FROM universe_membership "
                "WHERE segment = 'EQ' AND enabled = 1",
            ).fetchone()
            post_enabled = int(post_row[0]) if post_row else 0
            # Rows that were enabled before the preset but aren't now =
            # symbols kicked out of the universe by this preset.
            # max(0, ...) guards against small accounting drift from the
            # insert phase bumping pre-count indirectly.
            summary["disabled_non_members"] = max(
                0, pre_enabled + summary["inserted"] - post_enabled,
            )

        self.store.append_operator_audit(
            "universe.apply_preset", actor=actor, payload=summary,
        )
        logger.info(
            "Applied preset {} — listed={} inserted={} missing={}",
            preset, summary["listed"], summary["inserted"],
            summary["missing_from_instruments"],
        )
        return summary

    def _apply_logical_preset(self, preset: str, actor: str) -> dict[str, int]:
        with self.store._conn() as c:  # pyright: ignore[reportPrivateUsage]
            if preset == "none":
                c.execute("UPDATE universe_membership SET enabled = 0")
            elif preset == "all":
                c.execute("UPDATE universe_membership SET enabled = 1")
            changed = c.total_changes
        summary = {"preset": preset, "affected": int(changed)}
        self.store.append_operator_audit(
            "universe.apply_preset", actor=actor, payload=summary,
        )
        logger.info("Applied preset {} affecting {} rows", preset, changed)
        return {"affected": int(changed)}


# --------------------------------------------------------------------- #
# Internals                                                              #
# --------------------------------------------------------------------- #

def _seg(s: str | Segment) -> str:
    """Normalise either a ``Segment`` enum or a raw string to the
    storage form (``"EQ"`` / ``"FUT"`` / ``"OPT"``)."""
    if isinstance(s, Segment):
        return s.value
    return str(s)


def _row_to_entry(row: sqlite3.Row) -> UniverseEntry:
    return UniverseEntry(
        symbol=row["symbol"],
        segment=row["segment"],
        enabled=bool(row["enabled"]),
        watch_only_override=bool(row["watch_only_override"]),
        added_at=row["added_at"],
        added_by=row["added_by"],
    )
