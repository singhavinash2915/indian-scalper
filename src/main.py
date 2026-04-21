"""Application entry point.

``uv run python -m main`` (with ``pythonpath=["src"]``) or
``uv run python src/main.py``.

Responsibilities, in order:
  1. Write ``config.yaml`` from the embedded template if it's missing.
  2. Load + validate settings (Pydantic).
  3. Configure logging.
  4. Enforce mode/broker safety gates (explicit acknowledgement for live).
  5. Construct the broker based on ``broker:`` in config.
  6. Hand off to the scan loop.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from brokers.base import BrokerBase
from brokers.paper import PaperBroker
from brokers.upstox import UpstoxBroker
from config.logging_config import setup_logging
from config.settings import CONFIG_YAML_TEMPLATE, Settings
from data.instruments import InstrumentMaster
from scheduler.scan_loop import ScanContext, run_scan_loop


def main() -> None:
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        cfg_path.write_text(CONFIG_YAML_TEMPLATE)
        print("Wrote default config.yaml — review it and re-run.")
        return

    settings = Settings.load(cfg_path)
    setup_logging(settings)
    logger.info(
        "Loaded settings | starting_capital=₹{:,.0f}",
        settings.capital.starting_inr,
    )

    _assert_live_mode_acknowledged(settings)

    broker: BrokerBase
    if settings.broker == "paper":
        broker = PaperBroker(settings)
    elif settings.broker == "upstox":
        storage_cfg = settings.raw.get("storage", {})
        db_path = storage_cfg.get("db_path", "data/scalper.db")
        instruments = InstrumentMaster(
            db_path=db_path, cache_dir=Path(db_path).parent / "instruments",
        )
        broker = UpstoxBroker(settings, instruments=instruments, db_path=db_path)
    else:  # pragma: no cover — Literal type guarantees this is unreachable
        raise ValueError(f"Unknown broker: {settings.broker}")

    # The scan loop currently drives PaperBroker specifically (it calls
    # broker.settle / set_position_stops). Live UpstoxBroker integration
    # requires order-status polling + bracket orders — a separate
    # deliverable. For now, swapping to broker: upstox builds the live
    # client but doesn't auto-run the scan loop against it.
    if settings.broker != "paper":
        logger.warning(
            "UpstoxBroker constructed; the scan loop is paper-only for now. "
            "Exiting after broker init — run the dashboard separately "
            "against this broker for live monitoring."
        )
        return

    assert isinstance(broker, PaperBroker)  # type narrowing for the scan loop
    ctx = ScanContext(
        settings=settings,
        broker=broker,
        universe=[],  # populate from InstrumentMaster.filter() in prod
        instruments=broker.instruments,
    )
    run_scan_loop(ctx)


def _assert_live_mode_acknowledged(settings: Settings) -> None:
    """Refuse to start in ``mode: live`` unless the operator has typed
    the acknowledgement env var PROMPT.md mandates."""
    if settings.mode != "live":
        return
    ack = os.environ.get("LIVE_TRADING_ACKNOWLEDGED", "").strip().lower()
    if ack != "yes":
        logger.error(
            "mode: live requires LIVE_TRADING_ACKNOWLEDGED=yes in the environment. "
            "Aborting."
        )
        sys.exit(2)
    # Interactive confirmation — belt and braces per PROMPT.md compliance.
    if sys.stdin.isatty():
        print("You are about to start LIVE trading. Type 'LIVE' to continue:")
        if input().strip() != "LIVE":
            logger.error("live trading not confirmed; aborting.")
            sys.exit(2)


if __name__ == "__main__":
    main()
