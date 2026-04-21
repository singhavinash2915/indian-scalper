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

# Install deps into .venv + project itself as an editable package
uv sync

# Generate default config on first run
uv run scalper-bootstrap
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

## Running

Local Python process (paper mode, scheduler + dashboard in one process):

```bash
uv sync              # installs the project as a package — one-time.
uv run scalper       # starts scheduler + dashboard on 127.0.0.1:8080

# Useful siblings:
uv run scalper-preflight             # 11-check launch gate
uv run scalper-bootstrap             # writes config.yaml on first run
uv run python -m serve               # identical to `uv run scalper`
```

## Deployment

### Docker

```bash
# Build + run with the shipped compose file.
docker compose up --build
# or a plain docker run
docker build -t indian-scalper:latest .
docker run --rm -it \
  -p 127.0.0.1:8080:8080 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/config.yaml:/app/config.yaml" \
  indian-scalper:latest
```

The image is multi-stage (uv-resolved venv → slim runtime), runs as a
non-root user (`scalper:1001`), and the `HEALTHCHECK` hits
`/health` every 30 s.

### systemd (bare-metal / Raspberry Pi)

1. Clone this repo to `/opt/indian-scalper`.
2. `cd /opt/indian-scalper && uv sync --no-dev`
3. Create the user and fix ownership:
   ```bash
   sudo useradd --system --home /opt/indian-scalper scalper
   sudo chown -R scalper:scalper /opt/indian-scalper
   ```
4. Install the unit file and enable:
   ```bash
   sudo cp deploy/indian-scalper.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now indian-scalper
   journalctl -u indian-scalper -f
   ```

### Live trading gate

Before flipping `mode: live` in `config.yaml`:

1. Set `UPSTOX_ACCESS_TOKEN` (and friends) in `.env`.
2. Export `LIVE_TRADING_ACKNOWLEDGED=yes`.
3. On the first interactive run, type `LIVE` at the prompt to confirm.

Both gates must pass or the process exits with code 2.
