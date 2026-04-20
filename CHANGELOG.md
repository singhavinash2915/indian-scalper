# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/) with
conventional-commits style entries.

## [Unreleased]

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
