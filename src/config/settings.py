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
  starting_inr: 500000           # ₹5L equity bucket
  options_inr: 200000            # ₹2L options bucket (separate accounting)
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
  # Regime filter — skip new entries when proxy symbol's ADX is below min.
  regime_filter_enabled: false
  regime_filter_proxy_symbol: RELIANCE
  regime_filter_min_adx: 22.0
  # Per-symbol cooldown — block re-entry after a stop_loss / trail_stop
  # / time_stop exit for N minutes. 0 = disabled.
  cooldown_minutes: 0
  # Short-side intraday — off by default. Opt in after paper testing.
  enable_shorts: false
  short_rsi_entry_low: 25
  short_rsi_entry_high: 45
  short_rsi_hard_block: 22
  # ---- Options trading ----
  options_enabled: false                       # flip true to activate
  options_underlyings: [NIFTY, BANKNIFTY]
  options_min_days_to_expiry: 7                # roll forward when < this
  options_risk_per_trade_pct: 5.0              # 5% × options bucket per trade
  options_premium_stop_pct: 35.0               # initial premium SL
  options_premium_breakeven_pct: 35.0          # gain that triggers BE trail (1:1)
  options_trailing_premium_pct: 30.0           # trail by 30% of high-water in phase 3
  options_underlying_stop_pts_nifty: 50.0      # hard pts cap for NIFTY
  options_underlying_stop_pts_banknifty: 125.0 # hard pts cap for BANKNIFTY
  options_time_stop_minutes: 45
  options_max_lots_per_signal: 1
  options_premium_cap_per_lot: 35000           # skip if premium/lot exceeds
  options_eod_squareoff: "15:05"               # earlier than equity 15:20

  # Earnings-calendar filter — see RUNBOOK §earnings.
  #   off | exclude | restrict_to
  earnings_filter: "off"
  earnings_calendar_path: data/earnings/today.csv

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
    options_inr: float = 0.0   # separate bucket for F&O options trading
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

    # ---- Regime filter (NIFTY-style ADX gate on a proxy symbol) ----
    # Blocks new entries when the broad market isn't trending. Default off.
    regime_filter_enabled: bool = False
    regime_filter_proxy_symbol: str = "RELIANCE"
    regime_filter_min_adx: float = 22.0

    # ---- Per-symbol cooldown after a stop-out ----
    # After stop_loss / trail_stop / time_stop exit, block re-entry on the
    # same symbol for N minutes. 0 disables the cooldown entirely.
    cooldown_minutes: int = 0

    # ---- Short-side intraday trading ----
    # Mirrors the 8-factor scorer for bearish setups. Disabled by default
    # — opt in after paper-testing and after whitelisting symbols that
    # allow intraday shorts on Upstox.
    enable_shorts: bool = False
    short_rsi_entry_low: float = 25.0
    short_rsi_entry_high: float = 45.0
    short_rsi_hard_block: float = 22.0   # RSI below this = too oversold to short

    # ---- Options trading (NIFTY/BANKNIFTY · monthly · ATM · single-leg) ----
    options_enabled: bool = False
    options_underlyings: list[str] = Field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    options_min_days_to_expiry: int = 7         # roll forward when current expiry < this
    options_risk_per_trade_pct: float = 5.0     # 5% × options bucket = max loss per trade
    options_premium_stop_pct: float = 35.0      # initial premium SL
    options_premium_breakeven_pct: float = 35.0 # gain at which trail flips to breakeven (1:1)
    options_trailing_premium_pct: float = 30.0  # trail by 30% of high-water-mark in phase 3
    options_underlying_stop_pts_nifty: float = 50.0
    options_underlying_stop_pts_banknifty: float = 125.0
    options_time_stop_minutes: int = 45
    options_max_lots_per_signal: int = 1
    options_premium_cap_per_lot: float = 35000.0   # skip signal if premium per lot exceeds
    options_eod_squareoff: str = "15:05"        # earlier than equity 15:20

    # ---- Earnings-calendar filter ----
    # Restrict trading based on which symbols have results today.
    #   off          — no filter (default)
    #   exclude      — skip symbols whose results are today (safer; avoids gap risk)
    #   restrict_to  — ONLY trade symbols whose results are today (event-driven)
    # Earnings list is read from earnings_calendar_path — one symbol per line,
    # # comments allowed. Update before market open (manually or via NSE scraper).
    earnings_filter: Literal["off", "exclude", "restrict_to"] = "off"
    earnings_calendar_path: str = "data/earnings/today.csv"


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
