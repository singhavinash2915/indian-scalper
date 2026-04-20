"""
Indian Equity Momentum Scalper — Bootstrap File
================================================

Hand this file to Claude Code alongside PROMPT.md. It contains:
  1. config.yaml template (as a string for reference)
  2. Pydantic settings loader
  3. BrokerBase abstract class (the contract)
  4. PaperBroker stub (fill in the TODOs)
  5. A minimal scan-loop skeleton
  6. Logging + market-hours helpers

Once Claude Code scaffolds the project, split this into the proper
module layout described in PROMPT.md (src/brokers/, src/config/, etc.).

NOT FINANCIAL ADVICE. Paper trade only until thoroughly validated.
"""

from __future__ import annotations

import os
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from loguru import logger
from pydantic import BaseModel, Field, field_validator


# ============================================================================
# 1. CONFIG TEMPLATE (save as config.yaml in project root)
# ============================================================================

CONFIG_YAML_TEMPLATE = """
mode: paper                      # paper | live
broker: paper                    # paper | upstox
capital:
  starting_inr: 500000           # ₹5 lakh paper capital
  currency: INR

market:
  timezone: Asia/Kolkata
  session_start: "09:15"
  session_end:   "15:30"
  entry_cutoff:  "15:00"
  eod_squareoff: "15:20"
  skip_first_minutes: 15         # no entries 09:15–09:30

universe:
  equity:
    source: nifty_100            # nifty_50 | nifty_100 | custom
    custom_symbols: []
    min_price_inr: 100
    min_avg_turnover_cr: 10
  futures:
    enabled: true
    instruments: [NIFTY, BANKNIFTY, FINNIFTY]
    expiry: current              # current | next | both
  options:
    enabled: false               # enable once equity + futures are stable
    instruments: [NIFTY, BANKNIFTY]
    strikes_around_atm: 3
    expiry: weekly

strategy:
  candle_interval: 15m
  scan_interval_seconds: 300     # scan every 5 min
  min_score: 6                   # out of 8 factors
  rsi_upper_block: 78
  rsi_entry_range: [55, 75]
  adx_min: 22
  volume_surge_multiplier: 2.0
  ema_fast: 5
  ema_mid: 13
  ema_slow: 34
  ema_trend: 50
  supertrend_period: 10
  supertrend_multiplier: 3

risk:
  risk_per_trade_pct: 2.0
  stop_atr_multiplier: 1.0
  trailing_atr_multiplier_low_vol: 2.5
  trailing_atr_multiplier_high_vol: 1.8
  take_profit_atr_multiplier: 3.0
  max_equity_positions: 3
  max_fno_positions: 2
  daily_loss_limit_pct: 3.0
  drawdown_circuit_breaker_pct: 10.0
  time_stop_minutes: 90
  eod_squareoff_intraday: true
  never_hold_options_overnight: true

paper:
  slippage_pct: 0.05
  fill_on: next_candle_open      # next_candle_open | current_close

upstox:
  api_key_env: UPSTOX_API_KEY
  api_secret_env: UPSTOX_API_SECRET
  redirect_uri: http://localhost:8080/callback
  access_token_env: UPSTOX_ACCESS_TOKEN

dashboard:
  host: 127.0.0.1
  port: 8080
  refresh_seconds: 5

storage:
  db_path: data/scalper.db
  candles_cache_dir: data/candles

logging:
  level: INFO
  file: logs/scalper.log
  rotation: "50 MB"
  retention: "14 days"
"""


# ============================================================================
# 2. PYDANTIC SETTINGS
# ============================================================================

class CapitalCfg(BaseModel):
    starting_inr: float
    currency: str = "INR"


class MarketCfg(BaseModel):
    timezone: str = "Asia/Kolkata"
    session_start: str
    session_end: str
    entry_cutoff: str
    eod_squareoff: str
    skip_first_minutes: int = 15


class StrategyCfg(BaseModel):
    candle_interval: str = "15m"
    scan_interval_seconds: int = 300
    min_score: int = 6
    rsi_upper_block: float = 78
    rsi_entry_range: tuple[float, float] = (55, 75)
    adx_min: float = 22
    volume_surge_multiplier: float = 2.0
    ema_fast: int = 5
    ema_mid: int = 13
    ema_slow: int = 34
    ema_trend: int = 50
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0


class RiskCfg(BaseModel):
    risk_per_trade_pct: float = 2.0
    stop_atr_multiplier: float = 1.0
    trailing_atr_multiplier_low_vol: float = 2.5
    trailing_atr_multiplier_high_vol: float = 1.8
    take_profit_atr_multiplier: float = 3.0
    max_equity_positions: int = 3
    max_fno_positions: int = 2
    daily_loss_limit_pct: float = 3.0
    drawdown_circuit_breaker_pct: float = 10.0
    time_stop_minutes: int = 90
    eod_squareoff_intraday: bool = True
    never_hold_options_overnight: bool = True

    @field_validator("risk_per_trade_pct", "daily_loss_limit_pct")
    @classmethod
    def sane_pct(cls, v: float) -> float:
        if not 0 < v < 100:
            raise ValueError("percent must be between 0 and 100")
        return v


class Settings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    broker: Literal["paper", "upstox"] = "paper"
    capital: CapitalCfg
    market: MarketCfg
    strategy: StrategyCfg
    risk: RiskCfg
    # universe, paper, upstox, dashboard, storage, logging — add as needed
    raw: dict = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(
            mode=raw["mode"],
            broker=raw["broker"],
            capital=CapitalCfg(**raw["capital"]),
            market=MarketCfg(**raw["market"]),
            strategy=StrategyCfg(**raw["strategy"]),
            risk=RiskCfg(**raw["risk"]),
            raw=raw,
        )


# ============================================================================
# 3. DOMAIN TYPES
# ============================================================================

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class Segment(str, Enum):
    EQUITY = "EQ"
    FUTURES = "FUT"
    OPTIONS = "OPT"


@dataclass
class Instrument:
    symbol: str                  # e.g. "RELIANCE", "NIFTY26APR25000CE"
    exchange: str                # "NSE" | "NFO" | "BSE"
    segment: Segment
    tick_size: float = 0.05
    lot_size: int = 1
    expiry: datetime | None = None
    strike: float | None = None
    option_type: Literal["CE", "PE"] | None = None


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class Order:
    id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    price: float | None = None
    trigger_price: float | None = None
    status: str = "PENDING"
    filled_qty: int = 0
    avg_price: float = 0.0
    ts: datetime = field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Kolkata")))


@dataclass
class Position:
    symbol: str
    qty: int                     # +long, -short
    avg_price: float
    ltp: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    trail_stop: float | None = None
    opened_at: datetime | None = None

    @property
    def pnl(self) -> float:
        return (self.ltp - self.avg_price) * self.qty

    @property
    def pnl_pct(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return (self.ltp - self.avg_price) / self.avg_price * 100


# ============================================================================
# 4. BROKER ABSTRACTION (the contract)
# ============================================================================

class BrokerBase(ABC):
    """Every broker (paper, Upstox, future Zerodha) implements this.

    Strategy and risk code MUST only call these methods — never broker SDKs
    directly. This keeps the paper → Upstox swap clean.
    """

    @abstractmethod
    def get_instruments(self) -> list[Instrument]: ...

    @abstractmethod
    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]: ...

    @abstractmethod
    def get_ltp(self, symbols: list[str]) -> dict[str, float]: ...

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        qty: int,
        side: Side,
        order_type: OrderType,
        price: float | None = None,
        trigger_price: float | None = None,
    ) -> Order: ...

    @abstractmethod
    def modify_order(self, order_id: str, **kwargs) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_funds(self) -> dict[str, float]:
        """Returns {'available': ..., 'used': ..., 'equity': ...} in INR."""


# ============================================================================
# 5. PAPER BROKER (stub — Claude Code will flesh this out)
# ============================================================================

class PaperBroker(BrokerBase):
    """In-memory paper broker that simulates fills at next-candle-open + slippage.

    Persists state to SQLite so restart is safe.
    """

    def __init__(self, settings: Settings, db_path: str = "data/scalper.db"):
        self.settings = settings
        self.cash: float = settings.capital.starting_inr
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, Order] = {}
        self.slippage_pct = settings.raw.get("paper", {}).get("slippage_pct", 0.05)
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, symbol TEXT, side TEXT, qty INTEGER,
                    order_type TEXT, price REAL, trigger_price REAL,
                    status TEXT, filled_qty INTEGER, avg_price REAL, ts TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY, qty INTEGER, avg_price REAL,
                    stop_loss REAL, take_profit REAL, trail_stop REAL, opened_at TEXT
                );
                CREATE TABLE IF NOT EXISTS equity_curve (
                    ts TEXT PRIMARY KEY, equity REAL, cash REAL, pnl REAL
                );
                """
            )

    # TODO (Claude Code): implement all abstract methods below
    def get_instruments(self) -> list[Instrument]:
        raise NotImplementedError("Load from cached NSE master CSV")

    def get_candles(self, symbol: str, interval: str, lookback: int) -> list[Candle]:
        raise NotImplementedError(
            "For paper mode, fetch historical candles from Upstox public API "
            "or yfinance (.NS suffix) and cache to data/candles/"
        )

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        raise NotImplementedError("Return last close of most recent candle")

    def place_order(self, symbol, qty, side, order_type, price=None, trigger_price=None):
        raise NotImplementedError(
            "Create Order, persist to sqlite, fill at next candle open + slippage. "
            "Update self.cash and self.positions."
        )

    def modify_order(self, order_id, **kwargs):
        raise NotImplementedError

    def cancel_order(self, order_id):
        raise NotImplementedError

    def get_positions(self) -> list[Position]:
        return list(self.positions.values())

    def get_funds(self) -> dict[str, float]:
        used = sum(abs(p.qty) * p.avg_price for p in self.positions.values())
        pnl = sum(p.pnl for p in self.positions.values())
        return {
            "available": self.cash,
            "used": used,
            "equity": self.cash + used + pnl,
        }


# ============================================================================
# 6. MARKET HOURS HELPER
# ============================================================================

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def is_market_open(settings: Settings, ts: datetime | None = None) -> bool:
    ts = ts or now_ist()
    if ts.weekday() >= 5:                         # Sat/Sun
        return False
    # TODO: plug in NSE holiday calendar check here
    start = parse_hhmm(settings.market.session_start)
    end = parse_hhmm(settings.market.session_end)
    return start <= ts.time() <= end


def can_enter_new_trade(settings: Settings, ts: datetime | None = None) -> bool:
    ts = ts or now_ist()
    if not is_market_open(settings, ts):
        return False
    session_start = parse_hhmm(settings.market.session_start)
    cutoff = parse_hhmm(settings.market.entry_cutoff)
    earliest = (
        datetime.combine(ts.date(), session_start)
        + timedelta(minutes=settings.market.skip_first_minutes)
    ).time()
    return earliest <= ts.time() <= cutoff


# ============================================================================
# 7. SCAN LOOP SKELETON
# ============================================================================

def run_scan_loop(settings: Settings, broker: BrokerBase) -> None:
    """Main loop. Claude Code will wire this to APScheduler and the
    scoring engine + risk engine."""
    logger.info("Scan loop started | mode={} broker={}", settings.mode, settings.broker)
    while True:
        ts = now_ist()
        if not is_market_open(settings, ts):
            logger.debug("Market closed — sleeping 60s")
            time.sleep(60)
            continue

        # 1. fetch candles for each symbol in universe
        # 2. run indicator + scoring engine → list of signals
        # 3. apply risk engine (position limits, circuit breaker, sizing)
        # 4. place orders via broker
        # 5. manage open positions (trail stop, time stop, EOD squareoff)
        # 6. persist equity curve snapshot

        time.sleep(settings.strategy.scan_interval_seconds)


# ============================================================================
# 8. ENTRY POINT
# ============================================================================

def setup_logging(settings: Settings) -> None:
    log_cfg = settings.raw.get("logging", {})
    logger.remove()
    logger.add(
        lambda m: print(m, end=""),
        level=log_cfg.get("level", "INFO"),
    )
    log_file = log_cfg.get("file", "logs/scalper.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_file,
        level=log_cfg.get("level", "INFO"),
        rotation=log_cfg.get("rotation", "50 MB"),
        retention=log_cfg.get("retention", "14 days"),
        serialize=True,
    )


def main() -> None:
    # On first run, write the config template if it doesn't exist
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        cfg_path.write_text(CONFIG_YAML_TEMPLATE.strip() + "\n")
        print("Wrote default config.yaml — review it and re-run.")
        return

    settings = Settings.load(cfg_path)
    setup_logging(settings)
    logger.info("Loaded settings | starting_capital=₹{:,.0f}",
                settings.capital.starting_inr)

    broker: BrokerBase
    if settings.broker == "paper":
        broker = PaperBroker(settings)
    elif settings.broker == "upstox":
        raise NotImplementedError("UpstoxBroker — to be built after paper is stable")
    else:
        raise ValueError(f"Unknown broker: {settings.broker}")

    run_scan_loop(settings, broker)


if __name__ == "__main__":
    main()
