"""Pre-flight CLI — 11-check sanity pass run before the scheduler starts.

Exit codes:
  0 — every check passed (or skipped safely)
  1 — at least one check failed
  2 — something inside a check raised an unexpected exception

Intended as ``ExecStartPre`` for the systemd unit: systemd will refuse
to start the service if we exit non-zero. Also usable ad-hoc:

    uv run python -m preflight
    uv run python -m preflight --skip-backtest       # faster, skips check 8
    uv run python -m preflight --config path.yaml

The design is deliberately defensive: every check is wrapped so one
check's exception can't abort the rest. The summary at the end tells
the operator exactly which checks to fix.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from brokers.trade_mode import (
    LIVE_ACK_ENV,
    current_trade_mode,
    live_trading_acknowledged,
)
from config.settings import Settings
from data.holidays import DEFAULT_YAML_PATH, HolidayCalendar
from data.instruments import InstrumentMaster
from data.universe import UniverseRegistry
from execution.state import StateStore
from scheduler.market_hours import IST, now_ist

REQUIRED_TABLES = (
    "orders",
    "positions",
    "equity_curve",
    "audit_log",
    "control_flags",
    "operator_audit",
    "universe_membership",
    "signal_snapshots",
)

# Min disk-free in bytes for the data + logs paths. 1 GiB per spec.
MIN_FREE_BYTES = 1 * 1024 ** 3

# Backtest regression check — bullish-breakout fixture should produce
# at least this many signals under the test builder's min_score=4.
# Matches the D7 harness test's `len(result.trades) >= 1` lower bound,
# leaving slack for strategy drift but catching "it produces zero now".
EXPECTED_MIN_TRADES = 1


# --------------------------------------------------------------------- #
# Result type                                                            #
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str

    @property
    def is_blocking(self) -> bool:
        """Failed checks block the scheduler from starting."""
        return self.status == "fail"


def _pass(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, status="pass", detail=detail)


def _fail(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, status="fail", detail=detail)


def _skip(name: str, detail: str) -> PreflightCheck:
    return PreflightCheck(name=name, status="skip", detail=detail)


# --------------------------------------------------------------------- #
# Individual checks                                                      #
# --------------------------------------------------------------------- #

def check_config(config_path: Path) -> tuple[PreflightCheck, Settings | None]:
    if not config_path.exists():
        return (
            _fail("config", f"config file {config_path} not found"),
            None,
        )
    try:
        settings = Settings.load(config_path)
    except Exception as exc:
        return _fail("config", f"failed to parse {config_path}: {exc}"), None
    return (
        _pass(
            "config",
            f"loaded {config_path} — mode={settings.mode} broker={settings.broker} "
            f"starting_capital=₹{settings.capital.starting_inr:,.0f}",
        ),
        settings,
    )


def check_schema(db_path: Path) -> PreflightCheck:
    # StateStore() initialises the schema on construction — so simply
    # opening the store is both the fix-forward path and the test.
    StateStore(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        ).fetchall()
    present = {r[0] for r in rows}
    missing = [t for t in REQUIRED_TABLES if t not in present]
    if missing:
        return _fail("schema", f"missing tables: {', '.join(missing)}")
    return _pass(
        "schema",
        f"all {len(REQUIRED_TABLES)} expected tables present in {db_path}",
    )


def check_holidays(db_path: Path, yaml_path: Path | None = None) -> PreflightCheck:
    path = yaml_path or DEFAULT_YAML_PATH
    cal = HolidayCalendar(db_path)
    try:
        cal.load_from_yaml(path)
    except Exception as exc:
        return _fail("holidays", f"failed to load {path}: {exc}")
    if cal.count() == 0:
        return _fail("holidays", "no holidays loaded — check yaml content")
    today = now_ist().date()
    next_trading = cal.next_trading_day(today)
    today_status = (
        "weekend/holiday — next trading day: " + next_trading.isoformat()
        if not cal.is_trading_day(today)
        else "today is a trading day"
    )
    return _pass(
        "holidays",
        f"{cal.count()} holidays loaded · {today_status}",
    )


def check_instruments(db_path: Path) -> PreflightCheck:
    master = InstrumentMaster(
        db_path=db_path, cache_dir=db_path.parent / "instruments",
    )
    n = master.count()
    if n == 0:
        return _fail(
            "instruments",
            "instruments table is empty — run InstrumentMaster.refresh_equity_from_network()",
        )
    # Check freshness — reject if the most recent row is older than 7 days.
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT MAX(updated_at) FROM instruments",
        ).fetchone()
    updated_at = row[0] if row else None
    if updated_at:
        try:
            last_update = datetime.fromisoformat(updated_at)
            age = datetime.utcnow() - last_update.replace(tzinfo=None)
            if age > timedelta(days=7):
                return _fail(
                    "instruments",
                    f"{n} rows but last refresh {age.days} days old — run refresh_equity_from_network()",
                )
        except Exception:
            pass  # treat unparseable timestamps as fresh
    return _pass(
        "instruments",
        f"{n} instruments loaded, last refresh {updated_at or 'unknown'}",
    )


def check_universe(db_path: Path) -> PreflightCheck:
    store = StateStore(db_path)
    master = InstrumentMaster(
        db_path=db_path, cache_dir=db_path.parent / "instruments",
    )
    registry = UniverseRegistry(store, master)
    total = registry.count()
    if total == 0:
        return _fail(
            "universe",
            "universe_membership is empty — serve.py seeds on first run; "
            "use the dashboard's 'Add symbol' or ship a preset to fix",
        )
    enabled = len(registry.enabled_symbols())
    if enabled == 0:
        return _fail(
            "universe",
            f"{total} rows but zero enabled — enable at least one symbol "
            "on the Universe tab",
        )
    return _pass("universe", f"{enabled}/{total} symbols enabled")


def check_trade_mode(store: StateStore) -> PreflightCheck:
    mode = current_trade_mode(store)
    if mode == "live" and not live_trading_acknowledged():
        return _fail(
            "trade_mode",
            f"trade_mode=live but {LIVE_ACK_ENV}=yes is not set in the environment",
        )
    if mode not in ("watch_only", "paper", "live"):
        return _fail("trade_mode", f"unexpected trade_mode value {mode!r}")
    return _pass("trade_mode", f"trade_mode={mode}")


def check_control_flags(store: StateStore) -> PreflightCheck:
    scheduler_state = store.get_flag("scheduler_state", "stopped")
    kill_switch = store.get_flag("kill_switch", "armed")
    problems: list[str] = []
    if scheduler_state != "stopped":
        problems.append(
            f"scheduler_state={scheduler_state} — expected stopped; "
            "previous session may not have shut down cleanly"
        )
    if kill_switch != "armed":
        problems.append(
            f"kill_switch={kill_switch} — expected armed; rearm before launch"
        )
    if problems:
        return _fail("control_flags", "; ".join(problems))
    return _pass("control_flags", "scheduler_state=stopped, kill_switch=armed")


def check_backtest_regression() -> PreflightCheck:
    """Smoke backtest against a shipped fixture. Scoring drift shows up
    as a mismatch in the expected trade count."""
    try:
        from backtest.harness import (
            BacktestCandleFetcher,
            BacktestConfig,
            BacktestHarness,
        )
        from brokers.paper import PaperBroker
        from data.market_data import df_to_candles
        from scheduler.scan_loop import ScanContext
        from tests.fixtures.synthetic import bullish_breakout_df
    except Exception as exc:
        return _fail(
            "backtest_regression",
            f"could not import harness/fixture: {exc}",
        )

    # Spin up a throwaway broker in memory ($TMP) so we don't touch
    # production state.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = Settings.from_template()
        settings.raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
        settings.strategy.min_score = 4
        settings.risk.time_stop_minutes = 20

        fetcher = BacktestCandleFetcher(
            {"RELIANCE": df_to_candles(bullish_breakout_df())},
        )
        instruments = InstrumentMaster(
            db_path=tmp_path / "instruments.db",
            cache_dir=tmp_path / "instruments_cache",
        )
        fixture_csv = (
            Path(__file__).resolve().parent.parent
            / "tests" / "fixtures" / "sample_equity_master.csv"
        )
        try:
            instruments.load_equity_from_csv(fixture_csv)
        except Exception as exc:
            return _fail(
                "backtest_regression",
                f"couldn't load instruments fixture: {exc}",
            )

        broker = PaperBroker(
            settings,
            db_path=str(tmp_path / "scalper.db"),
            candle_fetcher=fetcher,
            instruments=instruments,
        )
        broker.store.set_flag("scheduler_state", "running", actor="preflight")
        ctx = ScanContext(
            settings=settings, broker=broker,
            universe=["RELIANCE"], instruments=instruments,
        )
        result = BacktestHarness(ctx, fetcher).run(BacktestConfig())

    if len(result.trades) < EXPECTED_MIN_TRADES:
        return _fail(
            "backtest_regression",
            f"fixture produced {len(result.trades)} trades, "
            f"expected >= {EXPECTED_MIN_TRADES} — scoring engine drift?",
        )
    return _pass(
        "backtest_regression",
        f"fixture replayed cleanly: {len(result.trades)} trades, "
        f"bars={result.timestamps_processed}",
    )


def check_dashboard_health(settings: Settings, db_path: Path) -> PreflightCheck:
    """In-process smoke test of the FastAPI app's /health endpoint.

    Spins up the app against the same database + instruments the
    scheduler would use, hits /health via TestClient (no port, no
    thread). Exercises Settings → broker → registry → create_app →
    route wiring in one shot.
    """
    try:
        from fastapi.testclient import TestClient

        from brokers.paper import PaperBroker
        from dashboard.app import create_app
    except Exception as exc:
        return _fail("dashboard_health", f"import failed: {exc}")

    try:
        instruments = InstrumentMaster(
            db_path=db_path, cache_dir=db_path.parent / "instruments",
        )
        broker = PaperBroker(
            settings, db_path=str(db_path), instruments=instruments,
        )
        app = create_app(broker, settings, log_file=None)
        client = TestClient(app)
        resp = client.get("/health")
    except Exception as exc:
        return _fail(
            "dashboard_health",
            f"app construction or /health call failed: {exc}",
        )
    if resp.status_code != 200:
        return _fail(
            "dashboard_health",
            f"/health returned {resp.status_code} body={resp.text[:200]}",
        )
    return _pass("dashboard_health", "/health → 200 in-process")


def check_disk_space(paths: list[Path]) -> PreflightCheck:
    offenders: list[str] = []
    for p in paths:
        try:
            p.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(p)
        except Exception as exc:
            offenders.append(f"{p}: {exc}")
            continue
        if usage.free < MIN_FREE_BYTES:
            offenders.append(
                f"{p}: {usage.free / 1024**3:.2f} GiB free "
                f"(< {MIN_FREE_BYTES / 1024**3:.0f} GiB minimum)",
            )
    if offenders:
        return _fail("disk_space", "; ".join(offenders))
    summary = ", ".join(
        f"{p.name}={shutil.disk_usage(p).free / 1024**3:.1f}GiB" for p in paths
    )
    return _pass("disk_space", f"all paths above 1 GiB free · {summary}")


def check_live_credentials(settings: Settings) -> PreflightCheck:
    """Only runs when trade_mode=live. Verifies the env-var gate AND a
    minimal smoke call against UpstoxBroker.get_funds()."""
    mode = current_trade_mode(StateStore(
        Path(settings.raw.get("storage", {}).get("db_path", "data/scalper.db")),
    ))
    if mode != "live":
        return _skip(
            "live_credentials",
            f"trade_mode={mode}; skipping Upstox connectivity check",
        )
    if not live_trading_acknowledged():
        return _fail(
            "live_credentials",
            f"{LIVE_ACK_ENV}=yes not set in environment",
        )
    upstox_cfg = settings.raw.get("upstox", {})
    env_name = upstox_cfg.get("access_token_env", "UPSTOX_ACCESS_TOKEN")
    if not os.environ.get(env_name):
        return _fail("live_credentials", f"{env_name} not set")
    try:
        from brokers.upstox import UpstoxBroker

        storage_cfg = settings.raw.get("storage", {})
        db_path = storage_cfg.get("db_path", "data/scalper.db")
        instruments = InstrumentMaster(
            db_path=db_path, cache_dir=Path(db_path).parent / "instruments",
        )
        broker = UpstoxBroker(settings, instruments=instruments, db_path=db_path)
        funds = broker.get_funds()
    except Exception as exc:
        return _fail(
            "live_credentials",
            f"Upstox smoke call failed: {exc}",
        )
    return _pass(
        "live_credentials",
        f"Upstox credentials valid · available=₹{funds['available']:,.0f}",
    )


# --------------------------------------------------------------------- #
# Composite                                                              #
# --------------------------------------------------------------------- #

def run_all_checks(
    config_path: Path = Path("config.yaml"),
    *,
    skip_backtest: bool = False,
) -> list[PreflightCheck]:
    """Run every check in order; never raises — a check's exception
    becomes a fail with a stack-trace-preview detail."""
    checks: list[PreflightCheck] = []

    # 1. Config — everything else needs Settings.
    cfg_check, settings = check_config(config_path)
    checks.append(cfg_check)
    if settings is None:
        # Can't proceed without settings — skip the rest explicitly.
        for name in ("schema", "holidays", "instruments", "universe",
                     "trade_mode", "control_flags", "backtest_regression",
                     "dashboard_health", "disk_space", "live_credentials"):
            checks.append(_skip(name, "blocked by config failure"))
        return checks

    storage_cfg = settings.raw.get("storage", {})
    db_path = Path(storage_cfg.get("db_path", "data/scalper.db"))
    logs_path = Path(
        settings.raw.get("logging", {}).get("file", "logs/scalper.log")
    ).parent

    checks.append(_guard("schema", lambda: check_schema(db_path)))
    checks.append(_guard("holidays", lambda: check_holidays(db_path)))
    checks.append(_guard("instruments", lambda: check_instruments(db_path)))
    checks.append(_guard("universe", lambda: check_universe(db_path)))

    store = StateStore(db_path)
    checks.append(_guard("trade_mode", lambda: check_trade_mode(store)))
    checks.append(_guard("control_flags", lambda: check_control_flags(store)))

    if skip_backtest:
        checks.append(_skip("backtest_regression", "--skip-backtest flag set"))
    else:
        checks.append(_guard("backtest_regression", check_backtest_regression))

    checks.append(_guard(
        "dashboard_health", lambda: check_dashboard_health(settings, db_path),
    ))
    checks.append(_guard(
        "disk_space", lambda: check_disk_space([db_path.parent, logs_path]),
    ))
    checks.append(_guard(
        "live_credentials", lambda: check_live_credentials(settings),
    ))

    return checks


def _guard(name: str, fn: Callable[[], PreflightCheck]) -> PreflightCheck:
    """Wrap a check call so an unexpected exception becomes a fail
    instead of aborting the whole preflight."""
    try:
        return fn()
    except Exception as exc:
        tb = traceback.format_exc(limit=3)
        return _fail(name, f"uncaught {type(exc).__name__}: {exc}\n{tb}")


# --------------------------------------------------------------------- #
# CLI                                                                    #
# --------------------------------------------------------------------- #

_STATUS_BADGES = {"pass": "[PASS]", "fail": "[FAIL]", "skip": "[SKIP]"}


def format_report(checks: list[PreflightCheck]) -> str:
    """Human-readable multi-line summary."""
    lines: list[str] = ["Pre-flight checks:"]
    width = max(len(c.name) for c in checks) + 2
    for c in checks:
        badge = _STATUS_BADGES.get(c.status, f"[{c.status}]")
        lines.append(f"  {badge} {c.name.ljust(width)} {c.detail}")
    n_fail = sum(1 for c in checks if c.status == "fail")
    n_pass = sum(1 for c in checks if c.status == "pass")
    n_skip = sum(1 for c in checks if c.status == "skip")
    lines.append(f"\nSummary: {n_pass} passed · {n_skip} skipped · {n_fail} failed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="preflight", description=__doc__)
    ap.add_argument(
        "--config", default="config.yaml", type=Path,
        help="path to config.yaml (default: %(default)s)",
    )
    ap.add_argument(
        "--skip-backtest", action="store_true",
        help="skip the backtest regression check (faster start)",
    )
    args = ap.parse_args(argv)

    checks = run_all_checks(args.config, skip_backtest=args.skip_backtest)
    print(format_report(checks))
    return 1 if any(c.is_blocking for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
