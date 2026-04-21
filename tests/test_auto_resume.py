"""scalper-auto-resume — guard + action tests.

The script has 5 guards (opt-in, weekday, holiday, kill, trade_mode)
plus a fire-window check. Every branch pinned.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import auto_resume
from brokers.base import Side  # noqa: F401 — loads brokers, dodges circular
from config.settings import CONFIG_YAML_TEMPLATE
from execution.state import StateStore
from tests.fixtures import paper_mode  # noqa: F401 — exports shared helper


IST = ZoneInfo("Asia/Kolkata")


# ---------------- builders ---------------- #

def _write_config(tmp_path: Path, **runtime_overrides) -> Path:
    """Pin every path inside tmp_path so guards run against an
    isolated DB + fresh cache dirs."""
    import yaml

    raw = yaml.safe_load(CONFIG_YAML_TEMPLATE)
    raw["storage"]["db_path"] = str(tmp_path / "scalper.db")
    raw["storage"]["candles_cache_dir"] = str(tmp_path / "candles")
    raw["logging"]["file"] = str(tmp_path / "logs" / "scalper.log")
    raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    raw["runtime"].update(runtime_overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _prime_store(cfg_path: Path, **flags) -> StateStore:
    """Open the store at the config's db_path and set flags."""
    import yaml

    raw = yaml.safe_load(cfg_path.read_text())
    store = StateStore(raw["storage"]["db_path"])
    for k, v in flags.items():
        store.set_flag(k, v, actor="test")
    return store


# ---------------- fire window ---------------- #

def test_fire_window_on_time() -> None:
    """09:14 IST sharp is inside the resume fire window."""
    ts = datetime(2026, 4, 22, 9, 14, 0, tzinfo=IST)
    assert auto_resume._within_fire_window(ts, auto_resume.RESUME_TARGET)


def test_fire_window_within_tolerance() -> None:
    """±2 min tolerance absorbs launchd slop."""
    for offset_sec in (-119, -1, 0, 1, 119):
        ts = (
            datetime(2026, 4, 22, 9, 14, 0, tzinfo=IST)
            .replace(second=0)
        )
        assert auto_resume._within_fire_window(
            ts.replace(second=abs(offset_sec) % 60),
            auto_resume.RESUME_TARGET,
        )


def test_fire_window_outside_tolerance() -> None:
    """5 minutes from the target — no fire."""
    ts = datetime(2026, 4, 22, 9, 19, 0, tzinfo=IST)
    assert not auto_resume._within_fire_window(ts, auto_resume.RESUME_TARGET)


# ---------------- full command: quiet-exit paths ---------------- #

def test_skips_outside_fire_window_without_force(tmp_path: Path) -> None:
    """Outside ±2 min of the target time + no --force → quiet exit 0."""
    cfg = _write_config(tmp_path)
    rc = auto_resume.main([
        "--config", str(cfg),
        "--action", "resume",
        "--now", "2026-04-22T12:00:00+05:30",  # lunch hour, nowhere near
    ])
    assert rc == 0


def test_skips_when_opt_in_not_set(tmp_path: Path) -> None:
    """With --force (bypassing fire window) but no opt-in flag → exit 0,
    no state change."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        trade_mode="paper", scheduler_state="stopped", kill_switch="armed",
        # auto_resume_enabled deliberately NOT set
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-04-22T09:14:00+05:30",
    ])
    assert rc == 0
    assert store.get_flag("scheduler_state") == "stopped"


def test_skips_on_weekend(tmp_path: Path) -> None:
    """Saturday 09:14 IST — weekend guard blocks."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="stopped", kill_switch="armed",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-04-18T09:14:00+05:30",  # Saturday
    ])
    assert rc == 0
    assert store.get_flag("scheduler_state") == "stopped"


# ---------------- full command: guard-failure paths ---------------- #

def test_refuses_on_holiday(tmp_path: Path) -> None:
    """Republic Day is in the shipped YAML — exit 2."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="stopped", kill_switch="armed",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-01-26T09:14:00+05:30",  # Republic Day, Monday
    ])
    assert rc == 2
    assert store.get_flag("scheduler_state") == "stopped"


def test_refuses_when_kill_tripped(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="stopped", kill_switch="tripped",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-04-22T09:14:00+05:30",
    ])
    assert rc == 2
    assert store.get_flag("scheduler_state") == "stopped"


def test_refuses_on_watch_only(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="watch_only", scheduler_state="stopped", kill_switch="armed",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-04-22T09:14:00+05:30",
    ])
    assert rc == 2
    assert store.get_flag("scheduler_state") == "stopped"


# ---------------- full command: happy path ---------------- #

def test_resume_flips_scheduler_running(tmp_path: Path) -> None:
    """Every guard passes — scheduler flips running + audit row."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="stopped", kill_switch="armed",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-04-22T09:14:00+05:30",  # confirmed trading day in our audit
    ])
    assert rc == 0
    assert store.get_flag("scheduler_state") == "running"
    # Audit row written with actor=auto_resume.
    audit = store.load_operator_audit()
    assert any(r["action"] == "flag_set:scheduler_state"
               and r["actor"] == "auto_resume" for r in audit)


def test_resume_is_idempotent(tmp_path: Path) -> None:
    """Already running — exit 1 (noop), no second audit row."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="running", kill_switch="armed",
    )
    audit_before = len(store.load_operator_audit(limit=100))
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "2026-04-22T09:14:00+05:30",
    ])
    assert rc == 1
    audit_after = len(store.load_operator_audit(limit=100))
    assert audit_after == audit_before  # no new row


# ---------------- pause branch ---------------- #

def test_pause_fires_even_on_holiday(tmp_path: Path) -> None:
    """EOD pause should fire regardless — conservative to always stop
    the scheduler, never leave it running overnight by mistake."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="running", kill_switch="armed",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "pause", "--force",
        # Inside 15:30 window but on a holiday (Republic Day, Monday).
        "--now", "2026-01-26T15:30:00+05:30",
    ])
    assert rc == 0
    assert store.get_flag("scheduler_state") == "paused"


def test_pause_skips_on_weekend(tmp_path: Path) -> None:
    """Weekends skip both actions — scheduler shouldn't be running
    Saturday 15:30 anyway."""
    cfg = _write_config(tmp_path)
    store = _prime_store(
        cfg,
        auto_resume_enabled="1",
        trade_mode="paper", scheduler_state="running", kill_switch="armed",
    )
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "pause", "--force",
        "--now", "2026-04-18T15:30:00+05:30",  # Saturday
    ])
    assert rc == 0
    assert store.get_flag("scheduler_state") == "running"


# ---------------- bad arguments ---------------- #

def test_bad_iso_in_now_exits_3(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    rc = auto_resume.main([
        "--config", str(cfg), "--action", "resume", "--force",
        "--now", "not-a-date",
    ])
    assert rc == 3
