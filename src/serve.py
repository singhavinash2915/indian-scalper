"""Production entry point — scan loop + dashboard in one process.

``main.py`` is the paper/live dispatcher; ``serve.py`` is the long-running
paper-trading process that Docker + systemd both wrap:

1. Load ``config.yaml`` (materialise from template on first run).
2. Configure loguru.
3. Build a ``PaperBroker`` + ``InstrumentMaster`` + ``ScanContext``.
4. Start APScheduler's **BackgroundScheduler** driving ``run_tick`` at
   ``scan_interval_seconds``.
5. Start uvicorn serving the FastAPI dashboard on the config host/port.

Live broker (``broker: upstox``) is rejected here — driving live orders
from this simple single-process loop isn't ready yet (see D9 notes).

The scheduler / uvicorn startup is split into helpers so tests can
assemble the context without actually binding a port.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from brokers.paper import PaperBroker
from config.logging_config import setup_logging
from config.settings import CONFIG_YAML_TEMPLATE, Settings
from dashboard.app import create_app
from data.instruments import InstrumentMaster
from scheduler.market_hours import IST
from scheduler.scan_loop import ScanContext, run_tick


# --------------------------------------------------------------------- #
# Composition helpers                                                   #
# --------------------------------------------------------------------- #

def load_or_create_config(path: str | Path = "config.yaml") -> Path:
    """Materialise the embedded config template on first run."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        cfg_path.write_text(CONFIG_YAML_TEMPLATE)
        logger.info("Wrote default config.yaml — edit and restart to take effect.")
    return cfg_path


def build_context(settings: Settings) -> tuple[ScanContext, PaperBroker]:
    """Construct broker + scan context from a loaded Settings."""
    if settings.broker != "paper":
        raise RuntimeError(
            f"serve.py only drives broker: paper (got {settings.broker!r}). "
            "Live Upstox execution needs order-status polling + websocket fills "
            "— see D9 notes."
        )

    storage_cfg = settings.raw.get("storage", {})
    db_path = storage_cfg.get("db_path", "data/scalper.db")

    instruments = InstrumentMaster(
        db_path=db_path,
        cache_dir=Path(db_path).parent / "instruments",
    )
    broker = PaperBroker(settings, db_path=db_path, instruments=instruments)

    # Universe resolution — default to whatever is in the instruments
    # table. Operators can override via a future config.yaml ``universe``
    # block once the universe-builder lands.
    universe = [i.symbol for i in instruments.filter()]

    ctx = ScanContext(
        settings=settings,
        broker=broker,
        universe=universe,
        instruments=instruments,
    )
    return ctx, broker


def build_scheduler(ctx: ScanContext) -> BackgroundScheduler:
    """APScheduler wired to the scan loop. Not started yet — caller calls
    ``.start()`` so tests can inspect the job config first."""
    scheduler = BackgroundScheduler(timezone=str(IST))
    scheduler.add_job(
        lambda: run_tick(ctx),
        IntervalTrigger(seconds=ctx.settings.strategy.scan_interval_seconds),
        id="scan_tick",
        max_instances=1,  # don't pile up ticks if one is slow
        coalesce=True,    # drop backlog if the scheduler falls behind
    )
    return scheduler


# --------------------------------------------------------------------- #
# Entry point                                                           #
# --------------------------------------------------------------------- #

def main() -> None:
    cfg_path = load_or_create_config()
    settings = Settings.load(cfg_path)
    setup_logging(settings)

    logger.info(
        "Starting indian-scalper | mode={} broker={} starting_capital=₹{:,.0f}",
        settings.mode, settings.broker, settings.capital.starting_inr,
    )

    ctx, broker = build_context(settings)
    scheduler = build_scheduler(ctx)
    scheduler.start()
    logger.info(
        "BackgroundScheduler up — tick every {}s",
        settings.strategy.scan_interval_seconds,
    )

    log_file = settings.raw.get("logging", {}).get("file", "logs/scalper.log")
    app = create_app(broker, settings, log_file=log_file)

    dashboard_cfg = settings.raw.get("dashboard", {})
    # DASHBOARD_HOST env wins over config.yaml — Docker defaults to
    # 0.0.0.0 so port mapping works; bare-metal installs stay on
    # 127.0.0.1 from config.
    host = os.environ.get("DASHBOARD_HOST") or dashboard_cfg.get("host", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT") or dashboard_cfg.get("port", 8080))
    log_level = settings.raw.get("logging", {}).get("level", "info").lower()

    try:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    finally:
        logger.info("Shutting down scheduler…")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
