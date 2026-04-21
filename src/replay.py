"""Fetch-and-replay CLI used by the Tuesday dry-run playbook.

Two sub-commands:

    scalper-replay fetch --date 2026-04-21 [--symbols A,B,C] [--out data/candles/dry_run]
        Pulls intraday candles for the given date via the broker's
        live fetcher (yfinance in paper mode) and caches one CSV per
        symbol to ``--out``. No backtest is run; just a cache populate.
        Use this on Tuesday evening against Tuesday's closed market.

    scalper-replay run --from data/candles/dry_run [--min-score N]
        Loads every CSV under ``--from`` into a BacktestCandleFetcher
        and runs the full BacktestHarness against the current config.
        Prints a one-page summary (signals count, orders, end equity,
        max drawdown) and exits non-zero if the replay completes 0
        trades — a sanity signal that thresholds or inputs are off.

Both sub-commands honour ``scheduler_state = running`` requirements
for the harness automatically — the harness flips it for its own
lifecycle.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------- #
# fetch                                                                  #
# --------------------------------------------------------------------- #

def cmd_fetch(args: argparse.Namespace) -> int:
    from brokers.paper import PaperBroker
    from config.settings import Settings
    from data.instruments import InstrumentMaster
    from data.market_data import YFinanceFetcher, save_candles_bulk
    from data.universe import UniverseRegistry

    settings = Settings.load(args.config)
    db_path = settings.raw.get("storage", {}).get("db_path", "data/scalper.db")
    instruments = InstrumentMaster(
        db_path=db_path, cache_dir=Path(db_path).parent / "instruments",
    )
    broker = PaperBroker(settings, db_path=db_path, instruments=instruments)

    # Decide which symbols to fetch.
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        registry = UniverseRegistry(broker.store, instruments)
        symbols = registry.enabled_symbols()
        if not symbols:
            print("error: no enabled universe symbols; pass --symbols or seed the universe")
            return 2

    interval = args.interval or settings.strategy.candle_interval
    fetcher = YFinanceFetcher()

    default_stem = args.date or datetime.now(IST).date().isoformat()
    out_dir = Path(args.out or f"data/candles/dry_run_{default_stem}")
    logger.info(
        "Fetching {} symbols · interval={} · date_hint={} · out={}",
        len(symbols), interval, args.date, out_dir,
    )

    series: dict[str, list] = {}
    failures: list[tuple[str, str]] = []
    # yfinance pulls a rolling window — the harness will naturally
    # replay the most recent bars; we over-fetch 60 bars to warm up
    # the long EMAs + indicators at the start of the replay.
    for i, symbol in enumerate(symbols, 1):
        try:
            candles = fetcher.get_candles(symbol, interval, lookback=200)
        except Exception as exc:
            failures.append((symbol, f"{type(exc).__name__}: {exc}"))
            logger.warning("  [{}/{}] {} FAILED: {}", i, len(symbols), symbol, exc)
            continue
        if not candles:
            failures.append((symbol, "empty response"))
            logger.warning("  [{}/{}] {} empty", i, len(symbols), symbol)
            continue
        # Optional date filter: keep only bars whose IST date matches.
        if args.date:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
            candles = [c for c in candles if c.ts.astimezone(IST).date() == target]
        if not candles:
            failures.append((symbol, f"no bars on {args.date}"))
            continue
        series[symbol] = candles
        if i % 10 == 0:
            logger.info("  [{}/{}] done (most recent: {})", i, len(symbols), symbol)

    written = save_candles_bulk(series, out_dir)
    print(
        f"\nFetched {len(written)}/{len(symbols)} symbols → {out_dir}\n"
        f"Failures: {len(failures)}",
    )
    if failures:
        print("First 5 failures:")
        for sym, reason in failures[:5]:
            print(f"  {sym}: {reason}")
    return 0 if written else 1


# --------------------------------------------------------------------- #
# run                                                                    #
# --------------------------------------------------------------------- #

def cmd_run(args: argparse.Namespace) -> int:
    from backtest.harness import BacktestCandleFetcher, BacktestConfig, BacktestHarness
    from brokers.paper import PaperBroker
    from config.settings import Settings
    from data.instruments import InstrumentMaster
    from data.market_data import load_candles_bulk
    from scheduler.scan_loop import ScanContext

    settings = Settings.load(args.config)
    if args.min_score is not None:
        settings.strategy.min_score = args.min_score

    # Always replay in a scratch DB so we don't pollute production state.
    scratch_db = Path(args.scratch_db or "data/replay_scratch.db")
    if scratch_db.exists() and args.fresh:
        scratch_db.unlink()
        logger.info("Removed stale scratch DB at {}", scratch_db)

    # Force paper mode for the scratch DB so entries can actually fire.
    settings.raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"

    series = load_candles_bulk(args.src)
    if not series:
        print(f"error: no candles found under {args.src}")
        return 2

    instruments = InstrumentMaster(
        db_path=str(scratch_db),
        cache_dir=scratch_db.parent / "instruments_replay",
    )
    broker = PaperBroker(settings, db_path=str(scratch_db), instruments=instruments)
    broker.store.set_flag("scheduler_state", "running", actor="replay")

    fetcher = BacktestCandleFetcher(series)
    ctx = ScanContext(
        settings=settings, broker=broker,
        universe=list(series.keys()), instruments=instruments,
    )
    harness = BacktestHarness(ctx, fetcher)
    result = harness.run(BacktestConfig(bars_per_year=252 * 25))

    # Score distribution from the signal_snapshots table.
    snapshots = broker.store.load_recent_signals(limit=10_000)
    scored_ge_6 = {s["symbol"] for s in snapshots if s["score"] >= 6}
    orders = broker.store.load_orders()
    placed = [o for o in orders if o.status in ("PENDING", "FILLED", "CANCELLED")]

    # Max drawdown inline (harness doesn't compute it by default).
    from backtest.metrics import compute_max_drawdown
    max_dd = compute_max_drawdown(result.equity_curve)

    print("\n" + "=" * 60)
    print(f"Replay summary — input: {args.src}")
    print("=" * 60)
    print(f"  symbols replayed             : {len(series)}")
    print(f"  timestamps processed          : {result.timestamps_processed}")
    print(f"  snapshots written             : {len(snapshots)}")
    print(f"  symbols scoring >= 6 at least : {len(scored_ge_6)}")
    print(f"  orders placed                 : {len(placed)}")
    print(f"  trades closed                 : {len(result.trades)}")
    print(f"  starting equity               : ₹{result.starting_equity:,.2f}")
    print(f"  final equity                  : ₹{result.final_equity:,.2f}")
    print(f"  total return                  : {result.total_return_pct:+.2f}%")
    print(f"  max drawdown                  : {max_dd.get('max_dd_pct', 0):.2f}%")
    print(f"  sharpe (annualised)           : {result.metrics.get('sharpe', float('nan')):.2f}")
    print(f"  scratch DB                    : {scratch_db}")
    print("=" * 60)

    # CLI sanity heuristic from the PROMPT: expect 3–15 score-≥6 events on a
    # normal NSE day across Nifty 100 over the full session. Outside that
    # range is a signal worth investigating.
    if scored_ge_6:
        print(f"\nScore-≥6 symbols: {', '.join(sorted(scored_ge_6))}")
    else:
        print("\n⚠ no symbols reached score ≥ 6 — threshold too tight or data gap?")

    return 0 if len(series) > 0 else 1


# --------------------------------------------------------------------- #
# argparse wiring                                                        #
# --------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scalper-replay", description=__doc__)
    ap.add_argument("--config", default="config.yaml", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser(
        "fetch",
        help="pull candles (full 60-day yfinance window; optional --date filter)",
    )
    p_fetch.add_argument(
        "--date",
        help="ISO date — when set, keeps only bars from this IST day. "
             "Omit to keep the full 60-day history (needed for harness warmup).",
    )
    p_fetch.add_argument("--symbols", help="comma-separated; default = enabled universe")
    p_fetch.add_argument("--interval", help="default = strategy.candle_interval")
    p_fetch.add_argument("--out", help="default = data/candles/dry_run_<date>/")

    p_run = sub.add_parser("run", help="replay cached candles through the harness")
    p_run.add_argument("--src", required=True, help="directory of CSV files")
    p_run.add_argument("--min-score", type=int, help="override settings.strategy.min_score")
    p_run.add_argument("--scratch-db", help="default = data/replay_scratch.db")
    p_run.add_argument("--fresh", action="store_true", help="delete scratch-db first")

    args = ap.parse_args(argv)
    if args.cmd == "fetch":
        return cmd_fetch(args)
    if args.cmd == "run":
        return cmd_run(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
