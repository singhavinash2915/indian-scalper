# Indian Equity Momentum Scalper — Build Specification

## Project Overview
Build a Python-based momentum scalping bot for the Indian market (NSE/BSE) that trades **equity cash, Nifty/Bank Nifty futures, and F&O (options)**. Development will start with **paper trading** on macOS; production will migrate to **Upstox API**. Keep the broker layer abstracted so we can swap paper → Upstox without rewriting strategy code.

A companion file `bootstrap.py` in this repo contains the initial config template, Pydantic settings, domain types, `BrokerBase` contract, `PaperBroker` skeleton, and market-hours helpers. Use it as the source of truth for types and contracts — split it into the proper module layout described below rather than rewriting it from scratch.

## Tech Stack
- Python 3.11+
- `uv` for dependency management (preferred) or `poetry`
- `pandas`, `numpy`, `pandas-ta` (pure-Python, easier than TA-Lib on Mac) for indicators
- `upstox-python-sdk` for live broker (later phase)
- `apscheduler` for the 5-minute scan loop
- `fastapi` + `uvicorn` + HTMX for a local dashboard (port 8080)
- `sqlite3` (stdlib) for trade history, positions, equity curve, logs
- `loguru` for structured logging
- `pydantic` v2 for config validation
- `pytest` + `pytest-asyncio` for tests
- `httpx` for any HTTP calls (retry with `tenacity`)
- Dockerfile for eventual Raspberry Pi / cloud deployment

## Architecture
Use a clean modular layout:

```
indian-scalper/
├── src/
│   ├── __init__.py
│   ├── main.py               # entry point
│   ├── brokers/
│   │   ├── base.py           # BrokerBase abstract
│   │   ├── paper.py          # PaperBroker
│   │   └── upstox.py         # UpstoxBroker (phase 2)
│   ├── data/
│   │   ├── market_data.py    # candle fetching + caching
│   │   ├── instruments.py    # NSE instrument master loader
│   │   └── holidays.py       # NSE holiday calendar
│   ├── strategy/
│   │   ├── indicators.py     # EMA, VWAP, MACD, RSI, ADX, BB, Supertrend, ATR
│   │   ├── scoring.py        # 8-factor scoring engine
│   │   └── filters.py        # circuit-limit, illiquidity, gap filters
│   ├── risk/
│   │   ├── position_sizing.py
│   │   ├── stops.py          # ATR stop, trailing stop, time stop
│   │   └── circuit_breaker.py
│   ├── execution/
│   │   ├── order_manager.py
│   │   └── state.py          # SQLite persistence
│   ├── scheduler/
│   │   ├── scan_loop.py
│   │   └── market_hours.py
│   ├── dashboard/
│   │   ├── app.py            # FastAPI app
│   │   └── templates/        # HTMX templates
│   └── config/
│       └── settings.py       # Pydantic settings
├── tests/
│   ├── fixtures/             # known-good candle data for indicator tests
│   ├── test_indicators.py
│   ├── test_scoring.py
│   ├── test_risk.py
│   └── test_paper_broker.py
├── data/                     # sqlite db, cached candles (gitignored)
├── logs/                     # gitignored
├── config.yaml               # generated on first run
├── .env.example              # UPSTOX_API_KEY, UPSTOX_API_SECRET, etc.
├── Dockerfile
├── pyproject.toml
├── README.md
└── CHANGELOG.md
```

## Market Specifics (India)
- **Trading hours**: 09:15–15:30 IST (NSE equity). Entry cutoff 15:00. No entries in first 15 min (09:15–09:30) to avoid opening volatility.
- **F&O hours**: same as equity for index options.
- **Holidays**: fetch NSE holiday calendar; skip those days. Cache to SQLite yearly.
- **Lot sizes**: critical for F&O — fetch from Upstox instruments master, never hardcode.
- **Tick size**: respect ₹0.05 tick size for equity rounding (₹0.01 for some stocks).
- **Circuit limits**: skip stocks in upper/lower circuit (±5%, ±10%, ±20% bands).
- **Currency**: all P&L in INR.
- **Timezone**: everything in Asia/Kolkata (IST). Never use naive datetimes.
- **T+1 settlement**: not relevant for intraday, but be aware.

## Universe (configurable, start here)
- **Equity**: Nifty 50 + Nifty Next 50 (top 100 liquid stocks)
- **Futures**: Nifty, Bank Nifty, FinNifty (current month + next month)
- **Options**: ATM ± 3 strikes for Nifty/Bank Nifty weekly expiry
- **Filters**: min ₹100 price (Indian equivalent of the $5 filter), min avg daily turnover ₹10 Cr

## Strategy: 8-Factor Scoring System
Port the Reddit momentum strategy with Indian market tweaks. Scan every 5 minutes using 15-minute candles.

1. **EMA stack** (5/13/34) with 50 EMA trend filter — price above all, stacked bullish
2. **VWAP crossover** — price above VWAP, crossover within last 2 candles
3. **MACD histogram cross** — positive cross above zero line
4. **RSI filter** — between 55 and 75 (hard block above 78 for Indian markets — they're whippier than US)
5. **ADX ≥ 22** (slightly relaxed from 25 for Indian volatility)
6. **Volume surge** ≥ 2× 20-period average
7. **Bollinger squeeze breakout** — BB width expansion after contraction
8. **Supertrend confirmation** (10, 3) — works well on Indian indices

Each factor = 1 point. Require score ≥ 6/8 to enter. All thresholds live in `config.yaml` — no magic numbers in code.

## Risk Management
- **Capital risk per trade**: 2% (lower than Reddit's 6% — Indian markets have higher gap risk)
- **Stop-loss**: 1× ATR(14)
- **Trailing stop**: 1.8–2.5× ATR, tighter in high-volatility regime (use ATR percentile over last 50 candles)
- **Take-profit**: 3× ATR (1:3 R:R)
- **Max simultaneous positions**: 3 equity + 2 F&O = 5 total
- **Daily loss limit**: 3% of capital → halt for the day
- **Drawdown circuit breaker**: 10% from peak equity → halt until manual reset
- **Time stop**: 90 min for dead positions (no movement ± 0.5× ATR)
- **EOD square-off**: for intraday mode, auto-close all positions at 15:20 IST
- **F&O specific**: never hold options overnight unless explicitly configured — theta decay kills you
- **Position sizing**: `qty = (capital × risk_pct) / (entry_price − stop_price)`, rounded down to lot size for F&O

## Broker Abstraction (critical)
`BrokerBase` (already defined in `bootstrap.py`) is the contract. Strategy and risk modules MUST only depend on this interface — never import broker SDKs directly. This keeps the paper → Upstox swap a one-line config change.

Implement in this order:
1. **PaperBroker** — simulates fills at next candle open + configurable slippage (default 0.05%). Persists orders, positions, and equity curve to SQLite. Must be idempotent: restart mid-session recovers state.
2. **UpstoxBroker** — thin wrapper around `upstox-python-sdk`. Every call wrapped with retry + exponential backoff (`tenacity`). Access token refresh handled automatically.

## Dashboard Features (FastAPI + HTMX, no React needed)
Mirror the Reddit screenshot aesthetic:
- Equity curve (plotly, dark theme)
- Open positions table with live P&L (refreshes every 5 sec via HTMX SSE)
- Trade history (last 50)
- Live log stream (tail of loguru file via SSE)
- KPIs: Equity, Cash, Day P&L, vs. Start, Position count (n/max)
- Manual kill switch button (sets a flag in SQLite; scan loop checks it every tick)
- Prominent "PAPER TRADING // NOT FINANCIAL ADVICE" banner

## Config
Externalize everything in `config.yaml`: mode (paper/live), broker, capital, universe, strategy thresholds, risk params, market hours, log level. Validate with Pydantic on startup. See `bootstrap.py` for the full template.

Secrets (`UPSTOX_API_KEY`, etc.) live in `.env`, loaded via `python-dotenv`. Never commit `.env`. Provide `.env.example`.

## Testing
- **Unit tests** for each indicator against known-good fixtures (use a saved day of Nifty data as reference).
- **Scoring engine tests** — feed synthetic candles that should score 8/8 and 0/8.
- **Risk engine tests** — verify position sizing math, stop calculations, circuit breaker trips.
- **PaperBroker tests** — place order → fill simulation → position update → P&L calculation.
- **Backtest harness** — separate module that feeds historical candles through the same strategy + risk engine. Reports Sharpe, max DD, win rate, avg R:R.
- **Dry-run mode** — tick through a saved day of candles at 10× speed to validate end-to-end before paper-trading live.

## Deliverables (build in this order)
1. **Project skeleton** — pyproject.toml with uv, directory structure, .gitignore, .env.example, README stub, logging setup. Move types and contracts from `bootstrap.py` into proper modules. First commit.
2. **Instruments + market hours + holiday calendar** — fetch NSE master, cache to SQLite, implement `is_market_open` and `can_enter_new_trade` with holiday awareness.
3. **Indicator library + scoring engine** — all 8 indicators as pure functions, scoring engine that returns `(score, breakdown_dict)`. Unit tests against fixtures.
4. **PaperBroker + order manager + state persistence** — implement all abstract methods, SQLite schema, idempotent recovery.
5. **Risk engine** — position sizing, stop calculation, trailing stop updates, circuit breakers, daily loss limit, time stop.
6. **Scan loop** — APScheduler wiring, market-hours gating, end-to-end flow: fetch → score → filter → size → order → manage.
7. **Backtest harness + dry-run mode** — validate strategy on historical data before any live paper trading.
8. **FastAPI dashboard** — KPIs, equity curve, positions, log stream, kill switch.
9. **UpstoxBroker** — behind `broker: upstox` config flag, feature-parity with PaperBroker.
10. **Dockerfile + systemd unit** — for later Raspberry Pi / cloud deployment. Multi-stage build, non-root user, healthcheck.

## Constraints
- **No look-ahead bias** — only use closed candles. The current forming candle is off-limits for signals.
- **Idempotent** — restarting mid-day must recover state from SQLite without duplicating orders or losing positions.
- **Defensive** — every broker/network call wrapped with retry + exponential backoff. Fail-closed on broker errors (don't open new positions if broker is flaky).
- **Observable** — structured JSON logs via loguru, every decision traced with `trace_id` per scan cycle.
- **Type-safe** — full type hints, `mypy --strict` passes, Pydantic for all external data.
- **Timezone-aware** — every datetime is tz-aware (IST). Assert on boundaries.
- **No magic numbers** — all thresholds in `config.yaml`.
- **Kill switch first** — before any live trading, the manual kill switch and daily loss limit must be tested end-to-end.

## Non-Goals (for now)
- Machine learning / neural nets — keep the strategy interpretable
- Options Greeks-based strategies — start with directional only
- Multi-user / auth — this is a personal tool
- Mobile app — the web dashboard is enough
- High-frequency (sub-minute) trading — 5-min scan is the floor

## Compliance & Safety
- This is for **personal use and educational paper trading**.
- Prominent "NOT FINANCIAL ADVICE // PAPER TRADING" banner on the dashboard.
- Before switching `mode: live`, require a CLI confirmation prompt AND a `LIVE_TRADING_ACKNOWLEDGED=yes` env var.
- Log every order to an immutable append-only audit table in SQLite.
- Never log API keys, access tokens, or PII.

## Getting Started (instructions for Claude Code)
1. Read `bootstrap.py` first — it has the contracts and config template already.
2. Start with **Deliverable 1 only**. Do not implement strategy logic yet.
3. After scaffolding, show me the directory tree and wait for me to review before moving to Deliverable 2.
4. Update `CHANGELOG.md` after each deliverable.
5. Commit after every green test run with a conventional-commits message (`feat:`, `fix:`, `test:`, `chore:`).
6. When stuck or ambiguous, ask — don't guess.
