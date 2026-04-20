# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/) with
conventional-commits style entries.

## [Unreleased]

### Deliverable 3 — Indicator library + 8-factor scoring engine

- **feat(strategy):** `src/strategy/indicators.py` — pure-function wrappers
  over pandas-ta with stable output-column names (`macd`, `hist`,
  `signal` / `adx`, `dmp`, `dmn` / `lower`, `middle`, `upper`,
  `bandwidth`, `percent` / `line`, `direction`, `long`, `short`).
  Exports: `ema`, `rsi`, `atr`, `volume_sma`, `macd`, `adx`, `bbands`,
  `supertrend`, `vwap`. Intraday VWAP is hand-rolled with daily reset
  via `df.index.normalize()` groupby; zero-volume bars guarded via
  `Series.where(cum_vol != 0)` so the output stays float-dtype.
- **feat(strategy):** `src/strategy/scoring.py` — the 8-factor scoring
  engine. `score_symbol(df, cfg)` returns a frozen `Score` dataclass
  with `total` (0–8), per-factor `results` tuple, `breakdown` dict,
  `blocked` flag + `block_reason`. Hard block fires when
  `RSI > rsi_upper_block`, killing the signal even on 8/8. Every
  threshold comes from `StrategyCfg` — no magic numbers in the engine.
  Factors: EMA stack, VWAP cross (within last 2 bars), MACD histogram
  zero-line cross, RSI in entry range, ADX ≥ min, volume surge vs.
  SMA-20, Bollinger squeeze→breakout (bandwidth ≥ 1.5× rolling-min +
  expanding + close above middle band), Supertrend bullish direction
  with close above line.
- **feat(strategy):** input validation — `ValueError` on missing OHLCV
  columns, `ValueError` on < `MIN_LOOKBACK_BARS` (60, covers EMA 50 +
  MACD 12/26/9 warm-up).
- **test(strategy):** 17 new tests. `tests/fixtures/synthetic.py` ships
  three seeded OHLCV generators: `bullish_breakout_df` (regime factors
  fire, no hard block), `flat_chop_df` (scores far below `min_score`),
  `parabolic_df` (RSI > 78 → hard block). `tests/test_indicators.py`
  covers every wrapper with invariant-level checks (EMA of constant,
  RSI extrema, MACD sign on accelerating trend, ADX trend-vs-chop,
  Supertrend direction, ATR scale, VWAP daily reset, DatetimeIndex
  requirement). `tests/test_scoring.py` covers 8/8 regime firing,
  chop-vs-bullish separation, hard-block trigger, missing-columns and
  short-history rejection, deterministic purity, and dataclass
  immutability.

### Deliverable 2 — Instruments + holiday calendar + market-hours awareness

- **feat(data):** `src/data/holidays.py` — `HolidayCalendar`, SQLite-backed.
  Loads NSE trading holidays from YAML, provides `is_trading_holiday`,
  `is_trading_day`, `next_trading_day`, `holidays_for_year`. Idempotent
  upsert on reload.
- **feat(data):** `src/data/nse_holidays.yaml` — shipped with fixed-date
  national holidays (Republic Day, Maharashtra Day, Independence Day,
  Gandhi Jayanti, Christmas) for 2025 + 2026. Moveable holidays (Holi,
  Diwali, Mahashivratri, Eid, Good Friday, Ram Navami, etc.) are flagged
  as TODO — must be populated annually from the NSE circular.
- **feat(data):** `src/data/instruments.py` — `InstrumentMaster`,
  SQLite-backed. `load_equity_from_csv` parses NSE `EQUITY_L.csv` format
  (EQ-series only, skips BE/BL/BT illiquid segments).
  `refresh_equity_from_network` fetches the live CSV from
  `archives.nseindia.com` via httpx + tenacity exponential-backoff retry.
- **feat(scheduler):** `is_market_open` and `can_enter_new_trade` now
  accept an optional `HolidayCalendar`. Back-compat preserved — callers
  without a calendar get weekend-only gating as before.
- **test(data):** 8 tests for `HolidayCalendar` (fixture loader,
  idempotency, trading-day queries, invalid-YAML rejection, shipped
  default parseability).
- **test(data):** 7 tests for `InstrumentMaster` (EQ filtering, get,
  segment/exchange filters, upsert idempotency, empty-CSV rejection).
- **test(scheduler):** 3 new tests covering holiday-closes-session,
  entry-blocked-on-holiday, and non-holiday passthrough.
- **fixtures:** `tests/fixtures/sample_holidays.yaml`,
  `tests/fixtures/sample_equity_master.csv`.

### Deliverable 1 — Project skeleton

- **feat(scaffold):** project layout per `PROMPT.md` — `src/brokers`, `src/config`,
  `src/scheduler`, `src/strategy`, `src/risk`, `src/execution`, `src/data`,
  `src/dashboard`, `tests/`.
- **feat(config):** `pyproject.toml` with `uv`, Python 3.12 pin (bumped from
  3.11 because `pandas-ta` now requires `>=3.12`; PROMPT.md's "3.11+" is
  still satisfied), runtime + dev dependency groups, pytest/ruff/mypy config.
- **feat(config):** moved `Settings` / `CapitalCfg` / `MarketCfg` / `StrategyCfg` /
  `RiskCfg` Pydantic models from `bootstrap.py` into `src/config/settings.py`.
  Config YAML template preserved as a module constant.
- **feat(brokers):** moved `BrokerBase` abstract and domain types
  (`Instrument`, `Candle`, `Order`, `Position`, `Side`, `OrderType`, `Segment`)
  from `bootstrap.py` into `src/brokers/base.py`.
- **feat(brokers):** moved `PaperBroker` skeleton into `src/brokers/paper.py`
  (still stubs — implementation deferred to Deliverable 4).
- **feat(scheduler):** moved `now_ist`, `is_market_open`, `can_enter_new_trade`
  helpers into `src/scheduler/market_hours.py`. Scan loop skeleton moved to
  `src/scheduler/scan_loop.py`.
- **feat(logging):** loguru setup extracted to `src/config/logging_config.py`.
- **chore:** `.gitignore`, `.env.example`, `.python-version`, README stub.
- **test(smoke):** `tests/test_config.py` validates the embedded config
  template parses into a `Settings` instance; `tests/test_market_hours.py`
  covers weekend + session-window gating.
