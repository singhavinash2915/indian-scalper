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

---

## 9. Deployment

### macOS launchd (laptop, always on while you're logged in)

Starts the scheduler + dashboard at login; restarts on crash. Logs to
`~/Library/Logs/indian-scalper.log`.

```bash
# 1. Edit deploy/macos/com.indianscalper.service.plist — replace each
#    /Users/avinashsingh/... path if your clone is elsewhere.
cp deploy/macos/com.indianscalper.service.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.indianscalper.service.plist

# Verify
launchctl list | grep indianscalper
tail -f ~/Library/Logs/indian-scalper.log
```

Uninstall:
```bash
launchctl unload -w ~/Library/LaunchAgents/com.indianscalper.service.plist
rm ~/Library/LaunchAgents/com.indianscalper.service.plist
```

### Auto-resume at 09:14 IST weekdays (optional)

After a week or so of comfortable manual operation:

```bash
cp deploy/macos/com.indianscalper.auto-resume.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.indianscalper.auto-resume.plist
```

Then opt in via the dashboard: **Controls panel → `auto-resume 09:14
IST` checkbox**. Flag persists across restarts.

The agent fires `scalper-auto-resume` every minute; the script itself
decides whether to act based on IST wall-clock + five guards:

1. **Opt-in flag** set to `1`
2. Today is a **weekday**
3. Today is **not** an NSE holiday
4. **Kill switch** is `armed`
5. **Trade mode** is `paper` or `live`

At 15:30 IST the same agent fires `--action pause` so the scheduler
stops ticking after market close.

Disable without touching launchd: uncheck the dashboard toggle. The
agent still fires but the script exits immediately when
`auto_resume_enabled=0`.

### Mobile access via Tailscale

Reach the dashboard from your phone without public exposure. Five-minute
setup; free for personal use.

```bash
# 1. Install Tailscale on the Mac running the bot:
brew install tailscale
sudo tailscale up
# Follow the login URL in the browser that opens.

# 2. Install Tailscale on your iPhone/Android from the App Store,
#    log in with the same account → phone joins the tailnet.

# 3. Find the Mac's tailnet hostname or IPv4:
tailscale ip -4
# → something like 100.101.102.103

# 4. Tell scalper to bind to the tailnet interface:
export SCALPER_TAILSCALE_ONLY=yes
uv run scalper

# 5. On your phone, open Safari/Chrome:
#    http://100.101.102.103:8080/m/
#    (bookmark the /m/ mobile route; desktop / is also reachable)
```

Or bake it into the launchd plist (`deploy/macos/com.indianscalper.service.plist`):

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>SCALPER_TAILSCALE_ONLY</key><string>yes</string>
    <!-- existing PATH / TZ here too -->
</dict>
```

Safety guarantees:
- Bot refuses to start if `SCALPER_TAILSCALE_ONLY=yes` but Tailscale
  isn't up. No accidental public binding.
- Tailscale ACLs are identity-based (your tailnet devices only); no
  password/API key to rotate.
- To fall back to loopback, unset the env var and restart.

### Mobile UI at `/m/`

Dedicated route at `http://<tailnet-ip>:8080/m/`. Full controls
(Pause, Resume, KILL with 3-sec hold, Re-arm, flip to watch_only),
KPIs, and the most recent `entered` + `watch_only_logged` signals.
The universe edit + chart drawer live on `/` (desktop) — anything
you'd normally do on the phone is on `/m/`.

### Position sizing — `equal_bucket` vs `cash_aware`

Controls whether the first entry of the day can monopolise capital.
Flip in `config.yaml` under `risk:` — no code change, no restart needed
beyond the next scan tick.

```yaml
risk:
  sizing_mode: equal_bucket    # default — safe
  bucket_slots: equity         # equity | auto | <int>
  bucket_safety_margin: 0.95
```

**`equal_bucket` (default)** — each of N slots gets
`starting_capital / bucket_slots × safety_margin` rupees max. With 3-slot
equity mode on ₹5,00,000 capital, each position is capped at ₹1,58,333
regardless of how much cash is idle. Prevents a single big entry from
starving later ones.

  - `bucket_slots: equity`  → `max_equity_positions` only (3 default)
  - `bucket_slots: auto`    → `max_equity + max_fno` (5 default)
  - `bucket_slots: "4"`     → fixed override

**`cash_aware` (legacy)** — sizes against available cash × 0.95. First
entry can eat up to 95% of the wallet. Useful when you're deliberately
running a single-name concentrated strategy, but unsafe for default
multi-name scalping.

### Upstox real-time data feed

Swap the default yfinance (~15 min delay) for real-time NSE candles via
Upstox REST. Works in both paper and live modes — paper keeps using
simulated orders, but the scorer sees live prices.

**One-time setup** (~5 min):

1. Create an app at https://account.upstox.com/developer/apps
   - Redirect URI: `http://127.0.0.1:8080/upstox/callback` (exact match)
2. Copy **API Key** + **API Secret** into `.env`:
   ```
   UPSTOX_API_KEY=...
   UPSTOX_API_SECRET=...
   UPSTOX_ACCESS_TOKEN=   # filled by the helper below
   ```
3. Set `data.source: auto` (default) or explicitly `upstox` in `config.yaml`.

**Every morning before 09:15 IST** — tokens expire daily at 03:30 IST:

```bash
uv run scalper-upstox-auth
# → opens browser → you log in → callback captured → .env updated
```

The helper listens on `127.0.0.1:8080/upstox/callback`, captures the
OAuth code automatically, exchanges it for an access token, and
rewrites the `UPSTOX_ACCESS_TOKEN` line in `.env` in place. Other
entries (API key, secret, comments) are preserved.

**Verify the live feed:**

```bash
uv run scalper-live-ltp RELIANCE TCS INFY --compare-yfinance
# → prints Upstox (live) vs yfinance (delayed) side-by-side.
#   During market hours you'll see a few paise gap — Upstox matches NSE.
```

**Fallback behaviour:** if `UPSTOX_ACCESS_TOKEN` is missing or expired,
the bot logs a warning and falls back to yfinance automatically — the
scheduler never crashes on a bad token.

### TradingView cross-check (Pine Script)

Paste `pine/indian-scalper-scorer.pine` into TradingView's Pine editor
to run an independent implementation of the 8-factor scorer on any
chart. Lets you verify the bot's decisions manually.

Daily workflow:

1. Bot finishes first scan at 09:30 IST → check `/signals` for the
   top 3 by score.
2. In TV, open each symbol on the 15-minute timeframe with the
   indicator applied. Score should match within ±1 once warmed up
   (~100 bars).
3. Persistent disagreement → flip bot to `watch_only`, investigate.

Dump bot scores per bar for a direct diff:

```bash
uv run scalper-pine-parity --symbol RELIANCE --out reliance.csv
```

Known divergences that are NOT bugs (warmup differences, last-bar
volume, pre-market bars) documented in `pine/README.md`.

### Web-based Upstox re-authentication (one-click daily ops)

For cloud deployments where SSH-ing in to run the CLI auth script is friction,
the dashboard now exposes a web flow that re-auths without restarting the bot:

```
http://<tailnet-ip>:8080/auth/upstox
```

Or click the **"Re-auth →"** link in the amber token-status strip on the
Dashboard tab (strip turns red when the token has expired).

Flow:
1. Open the page → a big "Log in with Upstox" button appears
2. Click → Upstox OAuth login → redirects back to the bot's `/auth/upstox/callback`
3. Bot exchanges code, **hot-swaps the access_token on the running fetcher
   in memory** + persists to `.env` — no process restart
4. Success page → back to dashboard, real-time Upstox feed is live again

**One-time Upstox app setup** — before first use, register the callback URL
shown on the `/auth/upstox` page as an allowed redirect URI at
https://account.upstox.com/developer/apps. For a cloud VM on a tailnet you'll
typically register:

```
http://127.0.0.1:8080/auth/upstox/callback       # for local Mac flow
http://<tailnet-hostname>:8080/auth/upstox/callback  # for cloud flow
```

Upstox allows multiple redirect URIs in a single app config.

### One-shot cloud VM bootstrap (Hetzner / DigitalOcean / any)

The `deploy/cloud/bootstrap.sh` script does everything a fresh Ubuntu
22.04/24.04 VM needs: user, firewall, Docker, Tailscale, repo, `.env`.
Idempotent — safe to re-run.

**Fastest path (≈ 10 minutes, ≈ ₹340/month)**:

1. Sign up at https://www.hetzner.com/cloud (or DigitalOcean / Vultr).
2. Get a Tailscale auth key at
   https://login.tailscale.com/admin/settings/keys (check "Reusable" +
   "Pre-approved").
3. Create a **CX11** (Hetzner) or **$4 droplet** (DO) with Ubuntu 24.04.
4. Paste the contents of `deploy/cloud/cloud-init.yaml` into the
   "User Data" / "Cloud-init" field, after replacing:
     - `<TS_AUTHKEY>` with your Tailscale key
     - `<REPO_URL>` with your fork's Git URL
5. Click Create. Wait ~5 min for the bootstrap to finish
   (`/var/log/cloud-init-output.log` on the VM for progress).
6. On your phone / laptop: open
   `http://scalper:8080/auth/upstox` (or the tailnet IPv4 if
   MagicDNS isn't enabled) — complete the web re-auth flow once.
7. Dashboard is live at `http://scalper:8080/` on your tailnet.

The script:
- Creates a `scalper` non-root user with SSH key copied from root + a
  restricted sudoers rule (docker, systemctl, journalctl only).
- Installs Docker, Tailscale, UFW.
- Joins your tailnet automatically via `TS_AUTHKEY`.
- Locks down UFW to SSH + tailnet only — **8080 never exposed publicly**.
- Clones your repo to `/opt/indian-scalper`, seeds a template `.env`.
- Leaves final start to you (`docker compose -f docker-compose.tailnet.yml up -d`)
  so you can review `.env` / `config.yaml` before first boot.

### Migrating to a Raspberry Pi or cloud VM

Graduate off the Mac when paper week feels solid. Two options:

**Option A — Raspberry Pi (one-time hardware cost ~$60, no ongoing)**

```bash
# On the Pi after a fresh Raspberry Pi OS Lite 64-bit install:
scp deploy/cloud-init/indian-scalper.yaml pi@<pi-ip>:/tmp/
ssh pi@<pi-ip>
sudo cloud-init single --name write-files --file /tmp/indian-scalper.yaml
sudo cloud-init single --name runcmd
```

**Option B — Cloud VM (DigitalOcean / Hetzner / Vultr, ~$5/mo)**

1. Provision an Ubuntu 24.04 LTS droplet / cloud instance.
2. Paste the contents of `deploy/cloud-init/indian-scalper.yaml`
   into the provider's "User data" / "Cloud-init" field before
   clicking Create.
3. Wait 3–5 minutes for the bootstrap to complete. cloud-init logs:
   `tail -f /var/log/cloud-init-output.log`

**After either option**, ssh in and finish setup:

```bash
ssh scalper@<host>
cd /opt/indian-scalper
cp .env.example.cloud .env
# Edit .env:
#   TS_AUTHKEY=...    (https://login.tailscale.com/admin/settings/keys)
#   UPSTOX_*=...      (only if flipping to live; paper doesn't need it)
# Edit config.yaml for capital + universe + thresholds.
docker compose -f docker-compose.tailnet.yml up -d
```

What cloud-init guarantees:

- `scalper` non-root user with passwordless sudo limited to
  docker/systemd status commands — no arbitrary root access.
- UFW default-deny-incoming, only SSH + Tailscale STUN open.
- 8080 is **never** exposed on the public interface; dashboard is
  reachable only via the tailnet.
- `LIVE_TRADING_ACKNOWLEDGED` stays commented out in the template.
  Flipping to live is still a fully conscious manual step.

Once running, the service appears as `scalper` on your tailnet
admin console. Dashboard URL: `http://scalper:8080/` (desktop) or
`http://scalper:8080/m/` (mobile).

### systemd / Docker — see README §Deployment
