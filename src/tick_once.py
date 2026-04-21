"""scalper-tick — fire ``run_tick`` once, outside the scheduler.

Used by the Tuesday dry-run playbook's Phase 3 rehearsal: the market
is closed, so the running scheduler's ticks keep falling through the
``market_closed`` gate. ``scalper-tick --ignore-market-hours``
forces a single tick against the existing DB + universe so the
Signals tab populates and the full scan pipeline is exercised
end-to-end.

Example:

    scalper-tick --ignore-market-hours
    scalper-tick --ts 2026-04-22T10:30 --ignore-market-hours

Does NOT start uvicorn / BackgroundScheduler. Writes the resulting
``TickReport`` summary to stdout then exits.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

IST = ZoneInfo("Asia/Kolkata")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scalper-tick", description=__doc__)
    ap.add_argument("--config", default="config.yaml", type=Path)
    ap.add_argument(
        "--ts", help="ISO timestamp for the simulated tick (default: now IST)",
    )
    ap.add_argument(
        "--ignore-market-hours", action="store_true",
        help="bypass the is_market_open + entry-window gates",
    )
    args = ap.parse_args(argv)

    from brokers.paper import PaperBroker
    from config.settings import Settings
    from data.instruments import InstrumentMaster
    from data.universe import UniverseRegistry
    from scheduler.scan_loop import ScanContext, run_tick

    settings = Settings.load(args.config)
    db_path = settings.raw.get("storage", {}).get("db_path", "data/scalper.db")
    instruments = InstrumentMaster(
        db_path=db_path, cache_dir=Path(db_path).parent / "instruments",
    )
    broker = PaperBroker(settings, db_path=db_path, instruments=instruments)
    registry = UniverseRegistry(broker.store, instruments)

    ctx = ScanContext(
        settings=settings, broker=broker,
        universe=registry.enabled_symbols(),
        instruments=instruments, universe_registry=registry,
    )

    # Parse --ts; default to now in IST.
    if args.ts:
        ts = datetime.fromisoformat(args.ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
    else:
        ts = datetime.now(IST)

    logger.info(
        "scalper-tick: ts={} ignore_market_hours={} universe_size={}",
        ts.isoformat(), args.ignore_market_hours, len(ctx.universe),
    )
    report = run_tick(ctx, ts, ignore_market_hours=args.ignore_market_hours)

    print("\n" + "=" * 60)
    print(f"Tick report — trace={report.trace_id}")
    print("=" * 60)
    print(f"  ts              : {report.ts.isoformat()}")
    print(f"  skipped_reason  : {report.skipped_reason or '—'}")
    print(f"  signals         : {len(report.signals)}")
    print(f"  exits           : {len(report.exits)}")
    print(f"  notes           : {', '.join(report.notes) or '—'}")
    if report.signals:
        print("  ---- signals ----")
        for s in report.signals:
            print(f"    {s.symbol:10s} score={s.score}  qty={s.qty} "
                  f"entry={s.entry:.2f} stop={s.stop:.2f} tp={s.take_profit:.2f}")
    if report.exits:
        print("  ---- exits ----")
        for e in report.exits:
            print(f"    {e.symbol:10s} reason={e.reason}")

    # Count snapshot actions so the operator can cross-check with the
    # Signals tab.
    snapshots = broker.store.load_recent_signals(limit=500)
    from collections import Counter
    action_counts = Counter(s["action"] for s in snapshots)
    if action_counts:
        print("  ---- snapshot action breakdown (last 500 rows) ----")
        for action, n in action_counts.most_common():
            print(f"    {action:24s} {n}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
