"""Application entry point.

``uv run python -m main`` (with ``pythonpath=["src"]``) or
``uv run python src/main.py``.

Responsibilities, in order:
  1. Write ``config.yaml`` from the embedded template if it's missing.
  2. Load + validate settings (Pydantic).
  3. Configure logging.
  4. Construct the broker based on ``broker:`` in config.
  5. Hand off to the scan loop.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from brokers.base import BrokerBase
from brokers.paper import PaperBroker
from config.logging_config import setup_logging
from config.settings import CONFIG_YAML_TEMPLATE, Settings
from scheduler.scan_loop import run_scan_loop


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

    broker: BrokerBase
    if settings.broker == "paper":
        broker = PaperBroker(settings)
    elif settings.broker == "upstox":
        raise NotImplementedError(
            "UpstoxBroker — Deliverable 9. Use broker: paper until then."
        )
    else:  # pragma: no cover — Literal type guarantees this is unreachable
        raise ValueError(f"Unknown broker: {settings.broker}")

    run_scan_loop(settings, broker)


if __name__ == "__main__":
    main()
