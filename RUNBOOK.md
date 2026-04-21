# RUNBOOK — Indian Scalper

Operator-facing, IST-timezone-aware. Assumes you're on the host the
bot runs on (or shelled into it).

> **Paper trading only unless you've explicitly flipped every live
> gate.** The system ships with `trade_mode = watch_only` on first
> boot. Promotions require conscious action described below.

---

## 1. First-run procedure (clean clone → bot watching the market)

```bash
# Clone + install (macOS / Linux)
git clone https://github.com/singhavinash2915/indian-scalper.git
cd indian-scalper

# uv is required. Install once: https://docs.astral.sh/uv/
uv sync                                            # runtime deps
uv sync --group dev                                # add pytest / ruff / mypy
uv run pytest                                      # sanity — all tests green

# First run writes config.yaml from the embedded template then exits.
uv run scalper-bootstrap
# → edit config.yaml (universe, capital, risk thresholds, logging)

# Refresh the NSE instruments master (one-off, ~5s).
uv run python -c "\
from data.instruments import InstrumentMaster; \
InstrumentMaster('data/scalper.db').refresh_equity_from_network()"

# Populate NSE holidays for this + next year.
#   src/data/nse_holidays.yaml already has the fixed-date nationals;
#   add moveable holidays (Holi, Diwali, Eid, Ram Navami …) from the
#   NSE circular before going live.

# Pre-flight — refuses to start unless every gate is green.
uv run scalper-preflight

# Launch (scheduler + dashboard in one process, port 8080).
uv run scalper
# → http://127.0.0.1:8080
#
# Equivalent: `uv run python -m serve`. Both Just Work after uv sync —
# no PYTHONPATH needed.

# Flip trade_mode watch_only → paper via the dashboard's three-way
# switch (confirm modal, HMAC token). No restart required.
```

Docker alternative — same flow, but:

```bash
docker compose up --build
# ExecStartPre equivalent not wired for compose; run preflight
# manually from inside the container the first time:
docker compose exec scalper python -m preflight
```

---

## 2. Daily startup (before 09:15 IST)

Run this before every trading day. Takes ≲ 30 seconds.

- [ ] **Host checks** — disk > 1 GiB free on `data/` and `logs/`, clock in sync
  (NSE only trades IST; a 10-min clock drift confuses market-hours gating).
- [ ] **Pull upstream + redeploy** if a new release landed overnight.
- [ ] **Pre-flight**: `uv run python -m preflight` returns exit code 0.
- [ ] **Dashboard up**: hit `http://127.0.0.1:8080/health` → `{"ok": true, ...}`.
- [ ] **Controls panel** shows `STOPPED · armed` (scheduler not started, kill not tripped).
- [ ] **Mode pill** shows the mode you intended to start in (`PAPER`
  for Wednesday launch; `WATCH ONLY` for strategy-tweak days).
- [ ] **Universe tab** — confirm the symbol count matches expectations.
  Disable any you don't want scanning today.
- [ ] At **09:14 IST**, press **Resume** in the Controls panel.
- [ ] At **09:15 IST**, market opens. First scan tick fires after the
  `skip_first_minutes` guard (09:30 by default).
- [ ] Watch the Signals tab. Anything weird → see §4.

---

## 3. Mode transitions

| From          | To            | Click path + notes                                      |
|---------------|---------------|----------------------------------------------------------|
| `watch_only`  | `paper`       | Mode switch → `Paper` → confirm modal. Scheduler continues scanning; the next scored signal will place a MARKET order. |
| `paper`       | `watch_only`  | Mode switch → `Watch`. Open positions stay — exits still fire. No new entries. |
| `paper`       | `live`        | **Only after §7 checklist.** Set `LIVE_TRADING_ACKNOWLEDGED=yes` → restart process → mode switch → `Live` → type **`LIVE`** → confirm. Broker flips to `UpstoxBroker`. |
| any           | (emergency)   | **KILL** button in Controls panel → 3-second hold → Confirm. Squares off everything at market, pins scheduler=`stopped`. |

Mid-day mode flips are safe: the scan loop reads `trade_mode` from
SQLite on every `place_order` call — no scheduler restart needed.

---

## 4. Mid-day intervention

- **Pause** — scan loop keeps refreshing LTPs + equity curve, does
  NOT score/enter/exit. Use when you need to think. Resume at will.
- **Kill** — emergency. Two-step with 3-second hold. Squares off all
  positions at market + pins scheduler=`stopped`. Requires explicit
  **Re-arm → Resume** sequence to restart.
- **Re-arm** — clears the kill flag but does NOT auto-resume. You
  must press Resume consciously.
- **Per-symbol watch-only override** — Universe tab, per-row Watch
  toggle. Symbol still scored (signals appear in the Signals tab
  with `watch_only_logged` action) but no order placed, even in
  `paper` mode.
- **Universe toggle** — flip any symbol off. Next tick drops it.

If the dashboard is unresponsive but the scheduler is healthy, use
the CLI:

```bash
uv run python -c "\
from execution.state import StateStore; \
s = StateStore('data/scalper.db'); \
s.set_flag('kill_switch', 'tripped', actor='cli-emergency')"
```

---

## 5. End of day

- **15:20 IST** — configurable `eod_squareoff`. Scan loop
  auto-squares-off every intraday position at market.
- **Verify** no open positions after 15:25: Dashboard → Open Positions
  panel should be empty.
- **Equity snapshot** is automatic on every settle; `GET
  /api/equity.json` serialises it. Archive the day's curve if you want
  historical reference:
  ```bash
  sqlite3 data/scalper.db \
    "SELECT * FROM equity_curve WHERE ts >= date('now','-1 day')" \
    > archive/equity-$(date +%Y-%m-%d).csv
  ```
- **Logs** rotate automatically (loguru, 50 MB files, 14-day retention).
  No manual rotation needed.
- **Signal snapshots** are pruned by the scheduler once per calendar
  day. If you want to inspect a bad decision before prune kicks in,
  query `signal_snapshots` directly.
- **Keep the service running overnight** with scheduler=`stopped` —
  faster start-of-day, preserves operator audit + equity history.

---

## 6. Incident response

### Broker API down (live)

Upstox returns 5xx or times out:

- Tenacity retries up to 3 attempts with exponential backoff.
- After that, the SDK raises — the scheduler's `run_tick` catches
  broker exceptions at the fetch layer and logs them; subsequent
  ticks keep trying.
- If errors persist, **Pause** the scheduler. Open positions keep
  their stops at the exchange (in live mode with bracket orders —
  TODO). In current paper-integration mode, exits queue on next
  successful tick.

### Network drops

- Dashboard (localhost) is unaffected.
- Broker calls fail until connectivity returns. Tenacity retries.
- `/api/control/state` and the audit drawer continue to render
  correctly — they're SQLite-backed.

### Dashboard unresponsive

- Scheduler keeps running — all state is in SQLite.
- `systemctl restart indian-scalper` — service comes back up with
  state intact. Pre-flight runs before service restart, catches any
  DB corruption.

### Daily loss limit hit

- `check_daily_loss_limit` (3% default) returns a blocking `RiskGate`.
- Scan loop reports `skipped_reason=halted:daily loss ...`.
- No new entries for the rest of the day; existing positions still
  managed normally.
- Auto-releases at the next session.

### Drawdown circuit breaker tripped

- `check_drawdown_circuit` (10% default) returns a blocking
  `RiskGate` — this is the "something is seriously wrong" signal.
- Scan loop immediately squares off all positions + latches the
  kill switch + pins scheduler=`stopped` in the *same* tick.
- Does NOT auto-release. Inspect the equity curve + trades in the
  last 24 hours, reason about what went wrong, decide explicitly
  to resume:
  ```
  Dashboard → Controls → Re-arm → Resume
  ```

### SQLite lock / corruption

Rare but possible on ungraceful shutdown.

```bash
# Sanity-check
sqlite3 data/scalper.db "PRAGMA integrity_check"

# If it's corrupt — restore from latest backup, or:
cp data/scalper.db data/scalper.db.bak
sqlite3 data/scalper.db ".dump" | sqlite3 data/scalper.db.fixed
mv data/scalper.db.fixed data/scalper.db
```

---

## 7. First live-money checklist (go/no-go for flipping to `live`)

**Do not flip `live` until every box is checked.** Paper trade for a
minimum of 4 full trading weeks first. Expect to discover strategy +
infrastructure bugs — that's the point of paper.

- [ ] 20+ trading days of paper operation with no show-stopping
  bugs (no crashes, no double-orders, no zombie positions).
- [ ] Paper drawdown never breached 5% — if it did, strategy is too
  aggressive for real capital.
- [ ] Reviewed every `entered` snapshot from the last 5 trading
  days: each one made sense given the score + breakdown.
- [ ] `backtest_regression` check in preflight is green across
  your own saved fixtures (not just the synthetic one).
- [ ] Re-run pre-flight with `--skip-backtest` unchecked. Exit 0.
- [ ] Upstox account:
    - [ ] Funded with the capital you're comfortable losing
      **entirely** on day 1. Not a rupee more.
    - [ ] API key + secret generated; `UPSTOX_ACCESS_TOKEN` in env.
    - [ ] Server-side kill switch tested via
      `broker.update_server_kill_switch("EQ", True)` then back to `False`.
- [ ] `risk_per_trade_pct` dialled down from paper default.
  Start at 0.5%. Move up only after weeks of observation.
- [ ] `daily_loss_limit_pct` and `drawdown_circuit_breaker_pct`
  tightened for live — 1.5% / 5% is a reasonable first pass.
- [ ] Environment set: `LIVE_TRADING_ACKNOWLEDGED=yes`.
- [ ] Physical-presence commitment: you're at the keyboard for the
  full session on day 1. Phone charger plugged in. No meetings.
  Kill switch within arm's reach.
- [ ] Mode switch → `Live` → typed `LIVE` → confirmed.
- [ ] **First live order placed** — hit Kill immediately after the
  first fill. Verify on the Upstox app that the exit fired. If
  it didn't, something is wrong — do NOT resume.
- [ ] Only after all of the above: re-arm, resume, trade the day.

---

## 8. Wednesday 09:15 launch plan

Intended debut of paper trading on real NSE market data.

### Tuesday evening
- [ ] Full pre-flight: `uv run python -m preflight` → exit 0.
- [ ] Dashboard up and responsive.
- [ ] Universe configured (Nifty 100 or starting subset — edit the
  Universe tab, don't touch the config file).
- [ ] Leave service running overnight. `scheduler_state = stopped`,
  `trade_mode = paper`. Confirmed by the Controls + Mode pills.

### Wednesday 09:00 IST
- [ ] Open dashboard — everything green?
- [ ] Mode pill = `PAPER`.
- [ ] Controls pill = `STOPPED` (scheduler not running yet).
- [ ] Open Positions = 0.
- [ ] Equity = starting capital (₹500,000 default).
- [ ] Universe tab — enabled count matches your intention.
- [ ] Watch the Audit drawer — no unexpected flag flips since
  yesterday.

### Wednesday 09:14 IST
- [ ] Controls → **Resume** → pill turns `RUNNING`.
- [ ] Kill switch armed, untouched.
- [ ] No new log spam — loguru tail on Dashboard shows steady state.

### Wednesday 09:15 IST — market opens
- First scan tick fires at `session_start + skip_first_minutes`
  (09:30 by default). Pre-09:30 ticks land on market-closed /
  entry-window skips — that's expected.

### Wednesday 09:30 IST
- Signals tab populates with first decisions.
- Anything weird (scoring doesn't match your intuition, unknown
  symbol appears, something takes longer than 2 seconds):
  - Mode switch → **Watch** (one click, confirm, no positions
    disturbed — stops / trails still manage what's open).
  - Investigate via the Signals tab + per-symbol chart drawer.
  - Flip back to Paper when comfortable, or leave in Watch for the
    rest of the day and debug tomorrow.

### Throughout the day
- Check in every 30 min. If things are boring → the bot is doing its
  job.
- At 15:20 IST, EOD square-off fires. Verify the Open Positions
  panel goes empty within a minute or two.
- 15:30 IST — market closes. Review the Signals tab + trade history
  + equity curve. Good day / bad day, log lessons learned.
- Leave service running. Tomorrow you repeat §2.
