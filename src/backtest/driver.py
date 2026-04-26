"""High-level walk-forward backtest driver.

One function ``run_walk_forward()`` that handles the whole pipeline:

  fetch historical bars → seed BacktestCandleFetcher → build PaperBroker
  → wire ScanContext → run BacktestHarness → emit BacktestResult

Designed for the ``scalper-backtest`` CLI but importable directly.

Lookahead-prevented: BacktestCandleFetcher.set_now() ensures every
``get_candles()`` call only sees bars at-or-before the simulated tick
time. Strategy + risk code paths are byte-identical to live — same
run_tick(), same scoring, same stops.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from loguru import logger

from backtest.harness import BacktestConfig, BacktestHarness, BacktestResult
from backtest.historical import fetch_history_bulk
from brokers.base import Candle
from brokers.paper import PaperBroker
from config.settings import Settings
from data.universe import UniverseRegistry
from scheduler.scan_loop import ScanContext

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class WalkForwardConfig:
    """Tuning knobs for one walk-forward run."""
    symbols: list[str]
    from_date: date
    to_date: date
    starting_capital: float = 500_000.0
    options_capital: float = 200_000.0
    interval: str = "15m"     # 15m | day
    cache_dir: str = "data/backtest"
    base_config_path: str = "config.yaml"
    stop_at_ts: datetime | None = None


def _interval_to_unit_value(interval: str) -> tuple[str, int]:
    s = interval.lower()
    if s.endswith("m"):
        return "minutes", int(s[:-1])
    if s in {"d", "1d", "day", "daily"}:
        return "days", 1
    raise ValueError(f"unsupported interval: {interval!r}")


def _build_settings(
    base_path: str,
    starting_capital: float,
    options_capital: float,
    interval: str,
    db_path: Path,
) -> Settings:
    """Load base config, override the bits that matter for backtest."""
    raw = yaml.safe_load(Path(base_path).read_text())
    raw["capital"]["starting_inr"] = starting_capital
    raw["capital"]["options_inr"] = options_capital
    raw["strategy"]["candle_interval"] = interval
    raw["storage"]["db_path"] = str(db_path)
    # Trade mode must be paper for the broker to actually place orders;
    # initial_trade_mode is what gets seeded on first DB init.
    raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    # Backtest scheduler must be 'running' so run_tick doesn't bail.
    cfg_path = db_path.parent / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    return Settings.load(cfg_path)


def run_walk_forward(cfg: WalkForwardConfig) -> BacktestResult:
    """Run a complete walk-forward replay. Returns the BacktestResult.

    Output side effects:
      - Caches fetched candles under ``cache_dir`` (CSV per symbol)
      - Persists trade ledger + equity curve to a fresh SQLite at
        ``cache_dir/<run_id>/scalper.db`` so the report can read them
    """
    unit, value = _interval_to_unit_value(cfg.interval)

    run_id = f"{cfg.from_date.isoformat()}_{cfg.to_date.isoformat()}_{cfg.interval}"
    run_dir = Path(cfg.cache_dir) / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "scalper.db"
    if db_path.exists():
        db_path.unlink()   # fresh ledger per run
    instruments_db = "data/scalper.db"   # existing live DB has the universe metadata

    # 1. Fetch history.
    logger.info(
        "[backtest] fetch start: {} symbols, {} → {}, interval={}",
        len(cfg.symbols), cfg.from_date, cfg.to_date, cfg.interval,
    )
    series = fetch_history_bulk(
        cfg.symbols, cfg.from_date, cfg.to_date,
        unit=unit, interval=value,
        cache_dir=cfg.cache_dir, instruments_db=instruments_db,
    )
    n_bars = sum(len(v) for v in series.values())
    nonempty = sum(1 for v in series.values() if v)
    logger.info(
        "[backtest] history loaded: {} symbols with bars ({} total bars)",
        nonempty, n_bars,
    )
    if not n_bars:
        raise RuntimeError("backtest has no bars — fetch failed entirely")

    # 2. Build settings + broker pointing at the run-local SQLite.
    settings = _build_settings(
        cfg.base_config_path, cfg.starting_capital, cfg.options_capital,
        cfg.interval, db_path,
    )

    # 3. Wire the harness fetcher + broker.
    fetcher = BacktestHarness.prepare_fetcher(series)
    broker = PaperBroker(
        settings=settings, db_path=db_path,
        candle_fetcher=fetcher,
    )

    # 4. Force scheduler state to running so run_tick processes ticks.
    broker.store.set_flag("scheduler_state", "running", actor="backtest")
    broker.store.set_flag("kill_switch", "armed", actor="backtest")
    broker.store.set_flag("trade_mode", "paper", actor="backtest")

    # 5. Seed universe with the requested symbols (so effective_universe()
    #    returns them).
    from data.instruments import InstrumentMaster, Segment
    instruments = InstrumentMaster(
        db_path=db_path, cache_dir=run_dir / "instruments",
    )
    # Copy instrument rows from the live DB so segment lookup works.
    import sqlite3
    if Path(instruments_db).exists():
        with sqlite3.connect(instruments_db) as src, sqlite3.connect(db_path) as dst:
            cur = src.execute(
                f"SELECT symbol, exchange, segment, tick_size, lot_size, "
                f"expiry, strike, option_type, name, isin, series, updated_at "
                f"FROM instruments WHERE symbol IN ({','.join(['?']*len(cfg.symbols))})",
                cfg.symbols,
            )
            dst.executemany(
                "INSERT OR REPLACE INTO instruments(symbol, exchange, segment, "
                "tick_size, lot_size, expiry, strike, option_type, name, isin, "
                "series, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                cur.fetchall(),
            )
    universe = UniverseRegistry(broker.store, instruments=instruments)
    for sym in cfg.symbols:
        if sym in ("NIFTY", "BANKNIFTY"):
            continue   # indices traded via options stack, not equity universe
        try:
            universe.add(symbol=sym, segment="EQ", enabled=True, actor="backtest")
        except Exception:
            # Symbol may already exist if we copied from live — toggle it on.
            try:
                universe.set_enabled(sym, True, actor="backtest")
            except Exception as exc:
                logger.warning("backtest universe add {}: {}", sym, exc)

    ctx = ScanContext(
        broker=broker, settings=settings,
        instruments=instruments,
        universe_registry=universe,
        calendar=None,
        pending_stops={},
    )

    # 6. Run.
    harness = BacktestHarness(ctx, fetcher)
    result = harness.run(BacktestConfig(stop_at_ts=cfg.stop_at_ts))
    logger.info("[backtest] complete\n{}", result.summary())
    return result
