"""Loguru setup — console + rotating JSON file sink."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from config.settings import Settings


def setup_logging(settings: Settings) -> None:
    """Configure loguru based on the ``logging:`` block in config.yaml."""
    log_cfg = settings.raw.get("logging", {})
    level = log_cfg.get("level", "INFO")

    logger.remove()
    logger.add(sys.stderr, level=level, enqueue=True)

    log_file = log_cfg.get("file", "logs/scalper.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_file,
        level=level,
        rotation=log_cfg.get("rotation", "50 MB"),
        retention=log_cfg.get("retention", "14 days"),
        serialize=True,
        enqueue=True,
    )
