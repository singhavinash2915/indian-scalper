# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/) with
conventional-commits style entries.

## [Unreleased]

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
