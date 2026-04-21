"""Pre-flight CLI — check-level + composite + exit-code tests.

Each check is a pure function. We exercise happy + unhappy paths in
isolation, plus ``run_all_checks`` end-to-end against a freshly-built
DB to confirm that a green system flies through all 11 checks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import preflight
from brokers.base import Side  # noqa: F401 — defeats the circular from execution
from brokers.paper import PaperBroker
from config.settings import CONFIG_YAML_TEMPLATE, Settings
from data.instruments import InstrumentMaster
from data.universe import UniverseRegistry
from execution.state import StateStore

IST = ZoneInfo("Asia/Kolkata")


# ---------------- builders ---------------- #

def _write_config(tmp_path: Path, **overrides) -> Path:
    """Config file pinning every path inside tmp_path so checks don't
    touch the real filesystem."""
    import yaml

    raw = yaml.safe_load(CONFIG_YAML_TEMPLATE)
    raw["storage"]["db_path"] = str(tmp_path / "scalper.db")
    raw["storage"]["candles_cache_dir"] = str(tmp_path / "candles")
    raw["logging"]["file"] = str(tmp_path / "logs" / "scalper.log")
    raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    for k, v in overrides.items():
        raw[k] = v
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _seed_dashboard_ready(tmp_path: Path) -> tuple[Path, StateStore]:
    """Fully-seeded DB the preflight should flip every check green on.

    - Instruments fixture loaded.
    - Universe seeded + enabled.
    - Broker-init + _seed_control_flags fire → scheduler_state=stopped,
      kill_switch=armed, trade_mode=paper.
    """
    cfg_path = _write_config(tmp_path)
    settings = Settings.load(cfg_path)

    db_path = Path(settings.raw["storage"]["db_path"])

    instruments = InstrumentMaster(
        db_path=db_path, cache_dir=db_path.parent / "instruments",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )

    # Broker constructor seeds control_flags + initialises StateStore.
    broker = PaperBroker(settings, db_path=str(db_path), instruments=instruments)

    registry = UniverseRegistry(broker.store, instruments)
    registry.seed_if_empty(["RELIANCE", "TCS"])

    return cfg_path, broker.store


# ---------------- individual checks ---------------- #

def test_check_config_missing_file(tmp_path: Path) -> None:
    check, settings = preflight.check_config(tmp_path / "nope.yaml")
    assert check.status == "fail"
    assert settings is None


def test_check_config_happy(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    check, settings = preflight.check_config(cfg_path)
    assert check.status == "pass"
    assert settings is not None
    assert settings.broker == "paper"


def test_check_schema_creates_required_tables(tmp_path: Path) -> None:
    check = preflight.check_schema(tmp_path / "fresh.db")
    assert check.status == "pass"
    # All REQUIRED_TABLES mentioned in detail when passing.
    assert "8 expected tables" in check.detail


def test_check_holidays_populates_from_yaml(tmp_path: Path) -> None:
    check = preflight.check_holidays(tmp_path / "scalper.db")
    assert check.status == "pass"
    assert "holidays loaded" in check.detail


def test_check_instruments_fails_when_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "scalper.db"
    StateStore(db_path)
    InstrumentMaster(db_path=db_path, cache_dir=tmp_path / "instruments")
    check = preflight.check_instruments(db_path)
    assert check.status == "fail"
    assert "empty" in check.detail


def test_check_instruments_passes_when_populated(tmp_path: Path) -> None:
    db_path = tmp_path / "scalper.db"
    master = InstrumentMaster(db_path=db_path, cache_dir=tmp_path / "instruments")
    master.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    check = preflight.check_instruments(db_path)
    assert check.status == "pass"
    assert "instruments loaded" in check.detail


def test_check_instruments_fails_when_stale(tmp_path: Path) -> None:
    """Instruments populated but last refresh > 7 days old → fail."""
    import sqlite3

    db_path = tmp_path / "scalper.db"
    master = InstrumentMaster(db_path=db_path, cache_dir=tmp_path / "instruments")
    master.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    stale_ts = (datetime.utcnow() - timedelta(days=10)).isoformat()
    with sqlite3.connect(str(db_path)) as c:
        c.execute("UPDATE instruments SET updated_at = ?", (stale_ts,))
    check = preflight.check_instruments(db_path)
    assert check.status == "fail"
    assert "days old" in check.detail


def test_check_universe_fails_when_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "scalper.db"
    StateStore(db_path)
    InstrumentMaster(db_path=db_path, cache_dir=tmp_path / "instruments")
    check = preflight.check_universe(db_path)
    assert check.status == "fail"


def test_check_universe_fails_when_all_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "scalper.db"
    store = StateStore(db_path)
    master = InstrumentMaster(db_path=db_path, cache_dir=tmp_path / "instruments")
    master.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    registry = UniverseRegistry(store, master)
    registry.seed_if_empty(["RELIANCE"])
    registry.set_enabled("RELIANCE", "EQ", False)
    check = preflight.check_universe(db_path)
    assert check.status == "fail"
    assert "zero enabled" in check.detail


def test_check_universe_passes(tmp_path: Path) -> None:
    _, store = _seed_dashboard_ready(tmp_path)
    check = preflight.check_universe(Path(store._db_path))  # pyright: ignore[reportPrivateUsage]
    assert check.status == "pass"


def test_check_trade_mode_live_without_env_ack(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "scalper.db")
    store.set_flag("trade_mode", "live", actor="test")
    monkeypatch.delenv("LIVE_TRADING_ACKNOWLEDGED", raising=False)
    check = preflight.check_trade_mode(store)
    assert check.status == "fail"
    assert "LIVE_TRADING_ACKNOWLEDGED" in check.detail


def test_check_trade_mode_live_with_env_ack(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path / "scalper.db")
    store.set_flag("trade_mode", "live", actor="test")
    monkeypatch.setenv("LIVE_TRADING_ACKNOWLEDGED", "yes")
    check = preflight.check_trade_mode(store)
    assert check.status == "pass"


def test_check_control_flags_passes_on_stopped_armed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "scalper.db")
    store.set_flag("scheduler_state", "stopped", actor="test")
    store.set_flag("kill_switch", "armed", actor="test")
    check = preflight.check_control_flags(store)
    assert check.status == "pass"


def test_check_control_flags_fails_on_running(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "scalper.db")
    store.set_flag("scheduler_state", "running", actor="test")
    store.set_flag("kill_switch", "armed", actor="test")
    check = preflight.check_control_flags(store)
    assert check.status == "fail"
    assert "scheduler_state=running" in check.detail


def test_check_control_flags_fails_on_tripped(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "scalper.db")
    store.set_flag("scheduler_state", "stopped", actor="test")
    store.set_flag("kill_switch", "tripped", actor="test")
    check = preflight.check_control_flags(store)
    assert check.status == "fail"
    assert "kill_switch=tripped" in check.detail


def test_check_dashboard_health_passes(tmp_path: Path) -> None:
    cfg_path, _ = _seed_dashboard_ready(tmp_path)
    settings = Settings.load(cfg_path)
    check = preflight.check_dashboard_health(
        settings, Path(settings.raw["storage"]["db_path"]),
    )
    assert check.status == "pass", check.detail
    assert "200" in check.detail


def test_check_disk_space_fail_is_rendered_when_path_impossible(tmp_path: Path) -> None:
    # Sanity-only — we can't force a "< 1 GiB free" without heroics,
    # so just verify the pass branch works.
    check = preflight.check_disk_space([tmp_path])
    assert check.status in ("pass", "fail")
    assert "GiB" in check.detail


def test_check_live_credentials_skipped_in_paper(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    settings = Settings.load(cfg_path)
    # Broker constructor seeds trade_mode=paper.
    InstrumentMaster(
        db_path=settings.raw["storage"]["db_path"],
        cache_dir=tmp_path / "instruments",
    )
    PaperBroker(settings, db_path=settings.raw["storage"]["db_path"])
    check = preflight.check_live_credentials(settings)
    assert check.status == "skip"


def test_check_backtest_regression_passes() -> None:
    check = preflight.check_backtest_regression()
    assert check.status == "pass"
    assert "trades" in check.detail


# ---------------- run_all_checks composite ---------------- #

def test_run_all_green_path(tmp_path: Path) -> None:
    cfg_path, _ = _seed_dashboard_ready(tmp_path)
    checks = preflight.run_all_checks(cfg_path, skip_backtest=True)
    failed = [c for c in checks if c.status == "fail"]
    assert failed == [], "\n".join(
        f"{c.name}: {c.detail}" for c in failed
    )


def test_run_all_skips_rest_on_config_failure(tmp_path: Path) -> None:
    bogus_path = tmp_path / "does_not_exist.yaml"
    checks = preflight.run_all_checks(bogus_path, skip_backtest=True)
    assert checks[0].name == "config"
    assert checks[0].status == "fail"
    # Every downstream check is explicitly skipped.
    for c in checks[1:]:
        assert c.status == "skip"
        assert "config failure" in c.detail


def test_run_all_skips_backtest_when_flagged(tmp_path: Path) -> None:
    cfg_path, _ = _seed_dashboard_ready(tmp_path)
    checks = preflight.run_all_checks(cfg_path, skip_backtest=True)
    bt = next(c for c in checks if c.name == "backtest_regression")
    assert bt.status == "skip"
    assert "--skip-backtest" in bt.detail


def test_run_all_fails_when_universe_empty(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path)
    settings = Settings.load(cfg_path)
    instruments = InstrumentMaster(
        db_path=settings.raw["storage"]["db_path"],
        cache_dir=tmp_path / "instruments",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    # Broker creates control_flags but no universe_membership seeded.
    PaperBroker(settings, db_path=settings.raw["storage"]["db_path"])

    checks = preflight.run_all_checks(cfg_path, skip_backtest=True)
    universe = next(c for c in checks if c.name == "universe")
    assert universe.status == "fail"


# ---------------- CLI exit codes ---------------- #

def test_main_exits_0_on_green(tmp_path: Path) -> None:
    cfg_path, _ = _seed_dashboard_ready(tmp_path)
    rc = preflight.main(["--config", str(cfg_path), "--skip-backtest"])
    assert rc == 0


def test_main_exits_1_on_any_failure(tmp_path: Path) -> None:
    # Missing config → whole thing fails.
    rc = preflight.main([
        "--config", str(tmp_path / "nope.yaml"), "--skip-backtest",
    ])
    assert rc == 1


def test_format_report_includes_all_checks(tmp_path: Path) -> None:
    cfg_path, _ = _seed_dashboard_ready(tmp_path)
    checks = preflight.run_all_checks(cfg_path, skip_backtest=True)
    text = preflight.format_report(checks)
    for name in ("config", "schema", "universe", "dashboard_health"):
        assert name in text
    assert "[PASS]" in text or "[SKIP]" in text


# ---------------- guard wrapper ---------------- #

def test_guard_catches_unexpected_exceptions() -> None:
    def boom() -> preflight.PreflightCheck:
        raise RuntimeError("boom")

    check = preflight._guard("demo", boom)
    assert check.status == "fail"
    assert "boom" in check.detail
    assert "RuntimeError" in check.detail


# ---------------- systemd unit integration ---------------- #

def test_systemd_unit_calls_preflight_via_ExecStartPre() -> None:
    """The deployment unit must run preflight before starting the
    service so a bad state prevents the scheduler from coming up."""
    text = (
        Path(__file__).resolve().parents[1]
        / "deploy" / "indian-scalper.service"
    ).read_text()
    assert "ExecStartPre" in text
    assert "preflight" in text
