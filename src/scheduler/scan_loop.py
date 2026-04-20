"""Main scan-loop skeleton.

Deliverable 6 wires this to APScheduler + the scoring engine + risk engine.
For now the loop just gates on market hours and sleeps — strategy and order
logic land in later deliverables.
"""

from __future__ import annotations

import time

from loguru import logger

from brokers.base import BrokerBase
from config.settings import Settings
from scheduler.market_hours import is_market_open, now_ist


def run_scan_loop(settings: Settings, broker: BrokerBase) -> None:
    logger.info(
        "Scan loop started | mode={} broker={}",
        settings.mode,
        settings.broker,
    )
    while True:
        ts = now_ist()
        if not is_market_open(settings, ts):
            logger.debug("Market closed — sleeping 60s")
            time.sleep(60)
            continue

        # Deliverable 3–6 fill these in:
        # 1. fetch candles for each symbol in universe
        # 2. run indicator + scoring engine → list of signals
        # 3. apply risk engine (position limits, circuit breaker, sizing)
        # 4. place orders via broker
        # 5. manage open positions (trail stop, time stop, EOD squareoff)
        # 6. persist equity curve snapshot

        time.sleep(settings.strategy.scan_interval_seconds)
