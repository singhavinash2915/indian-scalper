# Indian Equity Momentum Scalper

Python-based momentum scalping bot for NSE/BSE — equity cash, index futures, and F&O.
Paper-trading first on macOS, migrating to Upstox API for live trading later.

> **NOT FINANCIAL ADVICE. Paper-trade only until thoroughly validated.**

## Status

Deliverable 1 of 10 — project skeleton. Strategy, risk, scan loop, and dashboard
not yet implemented. See `PROMPT.md` for the full build plan.

## Requirements

- Python 3.11+ (managed by [`uv`](https://docs.astral.sh/uv/))
- macOS or Linux

## Setup

```bash
# Install uv once
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install deps into .venv
uv sync

# Generate default config on first run
uv run python -m main
# → writes config.yaml — edit, then re-run

# Run tests
uv run pytest
```

## Layout

See `PROMPT.md` for the target architecture. Key modules (as they land):

- `src/brokers/` — `BrokerBase` contract, `PaperBroker`, `UpstoxBroker`
- `src/config/` — Pydantic settings, logging setup
- `src/scheduler/` — market-hours helpers, scan loop
- `src/strategy/` — indicators + 8-factor scoring (D3)
- `src/risk/` — sizing, stops, circuit breakers (D5)
- `src/execution/` — order manager, SQLite state (D4)
- `src/dashboard/` — FastAPI + HTMX UI (D8)

## Configuration

All runtime behavior lives in `config.yaml` (generated on first run, gitignored).
Secrets live in `.env` (copy from `.env.example`, gitignored).
