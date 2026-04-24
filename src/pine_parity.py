"""scalper-pine-parity — dump bot-computed scores per bar to a CSV.

Use case: you've pasted the Pine indicator into TradingView on the
same symbol + timeframe the bot is running on. The bot produces a
score per bar; TV shows its own score. Run this tool to dump the
bot's numbers into a CSV you can manually cross-check against what
TradingView renders.

    uv run scalper-pine-parity --symbol RELIANCE --interval 15m --out reliance.csv

Output columns:

    ts, close, score, blocked, <8 factor bools>

Open the CSV alongside TradingView and diff mentally. Divergences on
last-N-bars after warmup are where you care; early bars will differ
by 0–1 points.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scalper-pine-parity", description=__doc__)
    ap.add_argument("--symbol", required=True, help="e.g. RELIANCE")
    ap.add_argument("--interval", default=None,
                    help="default = strategy.candle_interval (15m)")
    ap.add_argument("--lookback", type=int, default=120,
                    help="number of bars to fetch + score")
    ap.add_argument("--out", type=Path, required=True,
                    help="CSV output path")
    ap.add_argument("--config", default="config.yaml", type=Path)
    ap.add_argument(
        "--side", default="long", choices=["long", "short", "both"],
        help="which scorer to dump: long | short | both (default long)",
    )
    args = ap.parse_args(argv)

    # Deferred imports to dodge circulars.
    import brokers.base  # noqa: F401
    import pandas as pd
    from brokers.paper import PaperBroker
    from config.settings import Settings
    from data.instruments import InstrumentMaster
    from data.market_data import YFinanceFetcher
    from strategy.scoring import MIN_LOOKBACK_BARS, score_symbol, score_symbol_short

    settings = Settings.load(args.config)
    interval = args.interval or settings.strategy.candle_interval

    # Load instruments so PaperBroker + fetcher work consistently.
    db_path = settings.raw.get("storage", {}).get("db_path", "data/scalper.db")
    instruments = InstrumentMaster(
        db_path=db_path, cache_dir=Path(db_path).parent / "instruments",
    )
    if instruments.get(args.symbol) is None:
        print(f"warning: {args.symbol} not in instruments master — continuing", file=sys.stderr)

    broker = PaperBroker(settings, db_path=db_path, instruments=instruments)
    # ensure broker is only used for its fetcher; no orders flow here
    fetcher = broker.fetcher if broker.fetcher else YFinanceFetcher()
    candles = fetcher.get_candles(args.symbol, interval, lookback=args.lookback)
    if not candles:
        print(f"error: no candles returned for {args.symbol}", file=sys.stderr)
        return 1

    # Walk bar-by-bar and score the trailing window at each step.
    rows: list[dict] = []
    for i in range(MIN_LOOKBACK_BARS, len(candles) + 1):
        window = candles[:i]
        df = pd.DataFrame(
            {
                "open":   [c.open   for c in window],
                "high":   [c.high   for c in window],
                "low":    [c.low    for c in window],
                "close":  [c.close  for c in window],
                "volume": [c.volume for c in window],
            },
            index=pd.DatetimeIndex([c.ts for c in window], name="ts"),
        )
        last = window[-1]
        row: dict = {"ts": last.ts.isoformat(), "close": last.close}
        if args.side in ("long", "both"):
            s_long = score_symbol(df, settings.strategy)
            row["long_score"] = s_long.total
            row["long_blocked"] = int(s_long.blocked)
            for k, v in s_long.breakdown.items():
                row[f"long_f_{k}"] = int(v)
            row["long_reason"] = s_long.block_reason or ""
        if args.side in ("short", "both"):
            s_short = score_symbol_short(df, settings.strategy)
            row["short_score"] = s_short.total
            row["short_blocked"] = int(s_short.blocked)
            for k, v in s_short.breakdown.items():
                row[f"short_f_{k}"] = int(v)
            row["short_reason"] = s_short.block_reason or ""
        # Back-compat: when only one side is requested, expose plain
        # ``score`` / ``blocked`` / ``reason`` + ``f_<factor>`` columns
        # (matches the pre-side-flag CSV format).
        if args.side == "long":
            row["score"] = row["long_score"]
            row["blocked"] = row["long_blocked"]
            row["reason"] = row["long_reason"]
            for k in list(row):
                if k.startswith("long_f_"):
                    row[k.replace("long_f_", "f_")] = row[k]
        elif args.side == "short":
            row["score"] = row["short_score"]
            row["blocked"] = row["short_blocked"]
            row["reason"] = row["short_reason"]
            for k in list(row):
                if k.startswith("short_f_"):
                    row[k.replace("short_f_", "f_")] = row[k]
        rows.append(row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        if not rows:
            f.write("# no rows produced — lookback too small?\n")
            return 1
        fieldnames = list(rows[0].keys())
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {len(rows)} scored bars → {args.out}", file=sys.stderr)
    # Echo tail to stdout so the operator sees the recent-bars summary
    # without opening the file.
    tail = rows[-5:]
    print("\nLast 5 bars:")
    if args.side == "both":
        print(f"  {'ts':<25s} {'close':>10s}  long  short")
        for r in tail:
            print(
                f"  {r['ts']:<25s} {r['close']:>10.2f}   "
                f"{r['long_score']}/8   {r['short_score']}/8"
            )
    else:
        print(f"  {'ts':<25s} {'close':>10s}  score  blocked")
        for r in tail:
            blk = "yes" if r["blocked"] else ""
            print(f"  {r['ts']:<25s} {r['close']:>10.2f}    {r['score']}/8   {blk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
