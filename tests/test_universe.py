"""UniverseRegistry — unit tests for the D11 Slice 2 DAO.

These tests exercise the registry directly (no dashboard, no scan
loop). Integration with scan_loop is covered in test_scan_loop.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brokers.base import Segment
from data.instruments import InstrumentMaster
from data.universe import (
    IMPLEMENTED_PRESETS,
    KNOWN_PRESETS,
    PresetNotImplementedError,
    UniverseRegistry,
    UnknownSymbolError,
)
from execution.state import StateStore


# ---------------- Fixtures ---------------- #

def _setup(tmp_path: Path) -> UniverseRegistry:
    store = StateStore(tmp_path / "scalper.db")
    instruments = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    return UniverseRegistry(store, instruments)


# ---------------- Seeding ---------------- #

def test_seed_if_empty_inserts_rows(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    assert reg.count() == 0
    inserted = reg.seed_if_empty(["RELIANCE", "TCS", "INFY"])
    assert inserted == 3
    assert reg.count() == 3
    entries = reg.list_entries()
    assert {e.symbol for e in entries} == {"RELIANCE", "TCS", "INFY"}
    # All enabled by default; no watch_only_override.
    assert all(e.enabled for e in entries)
    assert not any(e.watch_only_override for e in entries)


def test_seed_if_empty_noop_when_populated(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE"])
    assert reg.seed_if_empty(["TCS", "INFY"]) == 0
    assert reg.count() == 1


def test_seed_audits_itself(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE", "TCS"], actor="system_init")
    audit = reg.store.load_operator_audit()
    assert any(r["action"] == "universe.seed" for r in audit)
    row = next(r for r in audit if r["action"] == "universe.seed")
    assert row["payload"]["count"] == 2


# ---------------- Toggle + watch-only ---------------- #

def test_toggle_flips_enabled(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE"])
    assert reg.is_enabled("RELIANCE")
    after = reg.toggle("RELIANCE")
    assert after.enabled is False
    assert reg.enabled_symbols() == []
    # Toggle back.
    reg.toggle("RELIANCE")
    assert reg.is_enabled("RELIANCE")


def test_toggle_unknown_raises(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    with pytest.raises(KeyError):
        reg.toggle("NOT_SEEDED")


def test_set_watch_only_override(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE"])
    assert not reg.has_watch_only_override("RELIANCE")
    out = reg.set_watch_only_override("RELIANCE", Segment.EQUITY, True)
    assert out.watch_only_override is True
    assert reg.has_watch_only_override("RELIANCE") is True
    # Flag is orthogonal to enabled — watch_only_override True does not
    # disable the row.
    assert out.enabled is True


def test_every_mutation_audits(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE"])
    reg.toggle("RELIANCE")
    reg.set_watch_only_override("RELIANCE", "EQ", True)
    audit = reg.store.load_operator_audit()
    actions = {r["action"] for r in audit}
    assert "universe.seed" in actions
    assert "universe.toggle" in actions
    assert "universe.watch_only_override" in actions


# ---------------- Add ---------------- #

def test_add_rejects_unknown_symbol(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    with pytest.raises(UnknownSymbolError):
        reg.add("NOT_A_REAL_SYMBOL")


def test_add_accepts_known_symbol(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    entry = reg.add("RELIANCE")
    assert entry.symbol == "RELIANCE"
    assert entry.enabled is True
    assert reg.count() == 1


def test_add_is_upsert(tmp_path: Path) -> None:
    """Adding an already-disabled symbol re-enables it and records who
    did it. Doesn't error out."""
    reg = _setup(tmp_path)
    reg.add("RELIANCE", actor="system")
    reg.set_enabled("RELIANCE", "EQ", False)
    out = reg.add("RELIANCE", actor="web")
    assert out.enabled is True
    assert out.added_by == "web"


# ---------------- Bulk ---------------- #

def test_bulk_update_counts(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE", "TCS", "INFY"])
    summary = reg.bulk_update([
        {"symbol": "RELIANCE", "enabled": False},
        {"symbol": "TCS", "enabled": False, "watch_only_override": True},
        {"symbol": "INFY", "watch_only_override": True},
        {"symbol": "MISSING_SYM", "enabled": False},  # not in table
    ])
    assert summary["disabled"] == 2
    assert summary["enabled"] == 0
    assert summary["watch_set"] == 2
    assert summary["missing"] == 1
    assert reg.enabled_symbols() == ["INFY"]


def test_bulk_update_audits_once(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE", "TCS"])
    reg.bulk_update([
        {"symbol": "RELIANCE", "enabled": False},
        {"symbol": "TCS", "enabled": False},
    ])
    audit = reg.store.load_operator_audit(limit=20)
    bulk_rows = [r for r in audit if r["action"] == "universe.bulk_update"]
    assert len(bulk_rows) == 1  # one batched audit row, not N individual


# ---------------- Presets ---------------- #

def test_preset_none_disables_all(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE", "TCS", "INFY"])
    out = reg.apply_preset("none")
    assert out["affected"] == 3
    assert reg.enabled_symbols() == []


def test_preset_all_enables_all(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE", "TCS"])
    reg.bulk_update([
        {"symbol": "RELIANCE", "enabled": False},
        {"symbol": "TCS", "enabled": False},
    ])
    assert reg.enabled_symbols() == []
    reg.apply_preset("all")
    assert set(reg.enabled_symbols()) == {"RELIANCE", "TCS"}


def test_named_index_presets_load_shipped_lists(tmp_path: Path) -> None:
    """Every named preset shipped in src/data/presets/ should apply
    cleanly. The fixture instruments master has 4 EQ symbols
    (RELIANCE / TCS / HDFCBANK / INFY); any of them appearing in a
    preset count as resolvable, the rest go into
    ``missing_from_instruments``."""
    reg = _setup(tmp_path)
    reg.seed_if_empty(["RELIANCE"])
    for preset in ("nifty_50", "nifty_100", "nifty_next_50", "bank_nifty_only"):
        summary = reg.apply_preset(preset)
        # Shipped preset has a non-empty symbol list.
        assert summary["listed"] > 0
        # Accounting invariant: enabled + missing_from_instruments == listed
        assert summary["enabled"] + summary["missing_from_instruments"] == summary["listed"]
        # Can't have negative row counts.
        assert summary["missing_from_instruments"] >= 0
        assert summary["enabled"] >= 0


def test_preset_symbols_enabled_and_others_disabled(tmp_path: Path) -> None:
    """Mini-preset test: seed the instruments master with a handful of
    symbols, apply a preset that covers some of them, verify enabled/
    disabled partitioning."""
    reg = _setup(tmp_path)
    # Pre-populate instruments master with 4 symbols (2 in preset, 2 not).
    import sqlite3
    from datetime import datetime
    with sqlite3.connect(reg.instruments._db_path) as c:  # pyright: ignore[reportPrivateUsage]
        for sym in ("RELIANCE", "TCS", "NOTINPRESET1", "NOTINPRESET2"):
            c.execute(
                "INSERT OR REPLACE INTO instruments"
                "(symbol, exchange, segment, tick_size, lot_size,"
                " name, isin, series, updated_at)"
                " VALUES (?, 'NSE', 'EQ', 0.05, 1, ?, ?, 'EQ', ?)",
                (sym, sym, f"ISIN_{sym}", datetime.utcnow().isoformat()),
            )
    reg.seed_if_empty(["RELIANCE", "TCS", "NOTINPRESET1", "NOTINPRESET2"])

    # nifty_50 contains RELIANCE + TCS.
    summary = reg.apply_preset("nifty_50")
    enabled = set(reg.enabled_symbols())
    assert "RELIANCE" in enabled
    assert "TCS" in enabled
    assert "NOTINPRESET1" not in enabled
    assert "NOTINPRESET2" not in enabled
    assert summary["missing_from_instruments"] > 0  # rest of Nifty 50 not in our master


def test_preset_rejects_unknown(tmp_path: Path) -> None:
    reg = _setup(tmp_path)
    with pytest.raises(ValueError):
        reg.apply_preset("totally_fake_preset")


def test_known_and_implemented_presets_documented() -> None:
    """All spec-named presets are now shipped. IMPLEMENTED == KNOWN."""
    for preset in ("none", "all", "nifty_50", "nifty_100", "nifty_next_50", "bank_nifty_only"):
        assert preset in KNOWN_PRESETS
    # Every KNOWN preset is now runnable.
    assert set(IMPLEMENTED_PRESETS) == set(KNOWN_PRESETS)


def test_preset_not_implemented_error_path(tmp_path: Path) -> None:
    """If someone manually adds a preset to KNOWN_PRESETS without
    shipping the YAML, apply_preset should raise
    PresetNotImplementedError. Demonstrates the guard is still live."""
    reg = _setup(tmp_path)
    # Monkey-patch a synthetic preset into KNOWN_PRESETS for this test.
    import data.universe as u
    original_known = u.KNOWN_PRESETS
    u.KNOWN_PRESETS = (*original_known, "nonexistent_preset")
    try:
        with pytest.raises(PresetNotImplementedError):
            reg.apply_preset("nonexistent_preset")
    finally:
        u.KNOWN_PRESETS = original_known


def test_preset_loader_rejects_bad_yaml(tmp_path: Path) -> None:
    """load_preset_symbols should surface a ValueError for malformed
    YAML content."""
    from data.presets import PRESETS_DIR, load_preset_symbols

    bad = PRESETS_DIR / "__broken_test_preset.yaml"
    try:
        bad.write_text("name: broken\nsymbols: not_a_list\n")
        with pytest.raises(ValueError):
            load_preset_symbols("__broken_test_preset")
    finally:
        if bad.exists():
            bad.unlink()


# ---------------- Persistence ---------------- #

def test_state_persists_across_registry_instances(tmp_path: Path) -> None:
    """The table IS the source of truth — spin up a fresh registry
    against the same DB and everything is still there."""
    reg1 = _setup(tmp_path)
    reg1.seed_if_empty(["RELIANCE", "TCS"])
    reg1.set_watch_only_override("RELIANCE", "EQ", True)
    reg1.toggle("TCS")  # disable TCS

    store2 = StateStore(tmp_path / "scalper.db")
    instruments2 = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    reg2 = UniverseRegistry(store2, instruments2)
    assert reg2.has_watch_only_override("RELIANCE") is True
    assert reg2.is_enabled("TCS") is False
    assert reg2.enabled_symbols() == ["RELIANCE"]
