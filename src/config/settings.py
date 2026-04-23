"""Typed, validated configuration loaded from config.yaml.

Split out of ``bootstrap.py`` — this is now the single source of truth for
all runtime settings. The full config template lives here as
``CONFIG_YAML_TEMPLATE`` so ``src/main.py`` can materialise a default
``config.yaml`` on first run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_YAML_TEMPLATE = """\
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
  stop_atr_multiplier: 1.5              # wider stops survive opening-bar wicks
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
  # Position sizing —
  #   equal_bucket : every slot gets starting_capital / bucket_slots (default)
  #   cash_aware   : first entry can consume up to 95% of available cash
  sizing_mode: equal_bucket
  # bucket_slots (only used in equal_bucket mode):
  #   auto     = max_equity_positions + max_fno_positions (5 by default)
  #   equity   = max_equity_positions only (3 by default; ignores F&O slots)
  #   <int>    = override, e.g. "4"
  bucket_slots: equity
  bucket_safety_margin: 0.95   # 5% reserved for slippage + bar drift

runtime:
  # Seeded into control_flags.trade_mode on first DB init ONLY.
  # After that, the dashboard's mode switch is the source of truth.
  # Valid values: watch_only | paper | live.
  initial_trade_mode: watch_only

paper:
  slippage_pct: 0.05
  # Fill policy:
  #   live_market       — fetch live LTP at place-time and fill IMMEDIATELY
  #                       with slippage (mirrors a real broker's MARKET order).
  #   next_candle_open  — legacy: queue order, fill on the next 15m candle's open.
  fill_on: live_market

data:
  # Candle feed for paper/live scoring.
  #   upstox   — real-time NSE via Upstox REST (needs UPSTOX_ACCESS_TOKEN)
  #   yfinance — free delayed feed (~15 min NSE lag)
  #   auto     — upstox if UPSTOX_ACCESS_TOKEN set, else yfinance (default)
  source: auto

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
    stop_atr_multiplier: float = 1.5
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
    # Position sizing mode — see src/risk/position_sizing.py.
    #   equal_bucket (default): every slot gets starting_capital / bucket_slots.
    #                           Prevents the first entry monopolising capital.
    #   cash_aware:             first-come-first-served against available cash.
    sizing_mode: Literal["equal_bucket", "cash_aware"] = "equal_bucket"
    # Used in equal_bucket mode:
    #   auto       : bucket_slots = max_equity_positions + max_fno_positions
    #   equity     : bucket_slots = max_equity_positions only (F&O ignored)
    #   <int>      : fixed slot count (integer as a string, e.g. "4")
    bucket_slots: str = "equity"
    bucket_safety_margin: float = 0.95   # 5% reserved for slippage

    @field_validator("risk_per_trade_pct", "daily_loss_limit_pct")
    @classmethod
    def sane_pct(cls, v: float) -> float:
        if not 0 < v < 100:
            raise ValueError("percent must be between 0 and 100")
        return v

    @field_validator("bucket_safety_margin")
    @classmethod
    def sane_margin(cls, v: float) -> float:
        if not 0.5 < v <= 1.0:
            raise ValueError("bucket_safety_margin must be in (0.5, 1.0]")
        return v

    @field_validator("bucket_slots", mode="before")
    @classmethod
    def validate_slots(cls, v) -> str:
        v = str(v).strip().lower()
        if v in {"auto", "equity"}:
            return v
        try:
            n = int(v)
        except (ValueError, TypeError):
            raise ValueError("bucket_slots must be 'auto', 'equity', or an integer") from None
        if n <= 0:
            raise ValueError("bucket_slots must be > 0")
        return str(n)

    def resolve_bucket_slots(self) -> int:
        """Collapse bucket_slots (string) into the concrete per-position count."""
        v = self.bucket_slots
        if v == "auto":
            return max(1, self.max_equity_positions + self.max_fno_positions)
        if v == "equity":
            return max(1, self.max_equity_positions)
        return max(1, int(v))


class Settings(BaseModel):
    """Typed snapshot of config.yaml.

    Only sections that strategy/risk code consumes are modelled explicitly;
    everything else is preserved verbatim in ``raw`` for later deliverables
    (paper slippage, upstox creds, dashboard host, storage paths, logging).
    """

    mode: Literal["paper", "live"] = "paper"
    broker: Literal["paper", "upstox"] = "paper"
    capital: CapitalCfg
    market: MarketCfg
    strategy: StrategyCfg
    risk: RiskCfg
    raw: dict = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Settings:
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

    @classmethod
    def from_template(cls) -> Settings:
        """Parse the embedded template — useful for tests and defaults."""
        raw = yaml.safe_load(CONFIG_YAML_TEMPLATE)
        return cls(
            mode=raw["mode"],
            broker=raw["broker"],
            capital=CapitalCfg(**raw["capital"]),
            market=MarketCfg(**raw["market"]),
            strategy=StrategyCfg(**raw["strategy"]),
            risk=RiskCfg(**raw["risk"]),
            raw=raw,
        )
