"""scalper-backtest — walk-forward backtest CLI.

Examples
--------

  # 60-day 15m backtest on default Nifty 100 universe (free yfinance / Upstox V3)
  uv run scalper-backtest --from 2026-02-25 --to 2026-04-25 --interval 15m

  # 2-year 15m backtest (sliding-window Upstox V3 fetcher, ~40min one-time fetch)
  uv run scalper-backtest --from 2024-04-26 --to 2026-04-25 --interval 15m

  # 2-year daily backtest (one Upstox call per symbol)
  uv run scalper-backtest --from 2024-04-26 --to 2026-04-25 --interval day

  # Custom symbol set + capital
  uv run scalper-backtest --from 2026-02-25 --to 2026-04-25 \\
      --symbols RELIANCE,TCS,INFY --capital 200000
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()

    ap = argparse.ArgumentParser(
        prog="scalper-backtest", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--from", dest="from_date", required=True,
                    help="ISO date, e.g. 2024-04-26")
    ap.add_argument("--to", dest="to_date", required=True,
                    help="ISO date, e.g. 2026-04-25")
    ap.add_argument("--interval", default="15m", choices=["15m", "5m", "day"])
    ap.add_argument("--symbols", default=None,
                    help="comma-separated list (default: enabled equity universe)")
    ap.add_argument("--capital", type=float, default=500_000.0)
    ap.add_argument("--options-capital", type=float, default=200_000.0)
    ap.add_argument("--cache-dir", default="data/backtest")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out-dir", default="data/backtest/reports")
    ap.add_argument("--label", default=None,
                    help="custom report label (default: <from>_<to>_<interval>)")
    args = ap.parse_args(argv)

    # Lazy imports to keep --help fast.
    import brokers  # noqa: F401
    from backtest.driver import WalkForwardConfig, run_walk_forward
    from backtest.reporter import build_report
    from data.universe import UniverseRegistry
    from execution.state import StateStore

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        # Default: enabled equity universe from the live DB.
        store = StateStore("data/scalper.db")
        from data.instruments import InstrumentMaster
        master = InstrumentMaster(db_path="data/scalper.db", cache_dir="data/instruments")
        reg = UniverseRegistry(store, instruments=master)
        symbols = [
            e.symbol for e in reg.list_entries(segment="EQ", enabled_only=True)
        ]
    if not symbols:
        print("error: no symbols to backtest (pass --symbols or seed the universe)", file=sys.stderr)
        return 1

    cfg = WalkForwardConfig(
        symbols=symbols,
        from_date=date.fromisoformat(args.from_date),
        to_date=date.fromisoformat(args.to_date),
        starting_capital=args.capital,
        options_capital=args.options_capital,
        interval=args.interval,
        cache_dir=args.cache_dir,
        base_config_path=args.config,
    )

    print(f"\nBacktest universe: {len(symbols)} symbols")
    print(f"Window: {cfg.from_date} → {cfg.to_date} · interval={cfg.interval}")
    print(f"Capital: equity ₹{args.capital:,.0f} + options ₹{args.options_capital:,.0f}\n")

    result = run_walk_forward(cfg)
    summary = build_report(
        result,
        from_date=cfg.from_date, to_date=cfg.to_date,
        interval=cfg.interval, out_dir=args.out_dir,
        label=args.label,
    )

    # Console summary.
    print("=" * 64)
    print(f"  BACKTEST · {summary['label']}")
    print("=" * 64)
    print(f"  Capital:        ₹{summary['starting_equity']:,.0f} → ₹{summary['final_equity']:,.0f}")
    print(f"  Total return:   {summary['total_return_pct']:+.2f}%       CAGR: {summary['cagr_pct']:+.2f}%")
    print(f"  Max drawdown:   {summary['max_dd_pct']:.2f}%")
    print(f"  Sharpe:         {summary['sharpe']:.2f}")
    print(f"  Trades:         {summary['trades_total']}      Win rate: {summary['win_rate_pct']:.1f}%")
    pf = summary.get("profit_factor")
    print(f"  Profit factor:  {pf:.2f}" if pf else "  Profit factor:  ∞")
    print(f"  Avg win:        ₹{summary['avg_win']:+,.0f}     Avg loss: ₹-{summary['avg_loss']:,.0f}")
    print(f"  Max losses run: {summary['max_consecutive_losses']}")
    print()
    print("  Exit attribution:")
    for reason, a in summary["exit_attribution"].items():
        sign = "+" if a["pnl"] >= 0 else ""
        print(f"    {reason:<18} {a['count']:>4} trades ({a['count_pct']:>4.1f}%)   "
              f"₹{sign}{a['pnl']:>11,.0f}   win {a['win_rate_pct']:>4.1f}%")
    print()
    print(f"  HTML report:  {summary['artifacts']['html']}")
    print(f"  Trade ledger: {summary['artifacts']['csv']}")
    print(f"  JSON:         {summary['artifacts']['json']}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
