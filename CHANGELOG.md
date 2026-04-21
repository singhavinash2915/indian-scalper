# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/) with
conventional-commits style entries.

## [Unreleased]

### Deliverable 11 — Slice 4 — Pre-flight CLI + RUNBOOK (Wednesday-launch gate)

- **feat(preflight):** ``src/preflight.py`` — 11-check gate runnable
  as ``uv run python -m preflight``. Each check returns a
  ``PreflightCheck(name, status, detail)``; ``run_all_checks``
  collects them without short-circuiting so the operator gets a full
  pass/fail matrix rather than "the first one blew up". Exit code 0
  on all pass, 1 on any fail. ``_guard`` wraps every check so an
  unexpected exception becomes a ``fail`` with a stack-trace preview
  instead of crashing the whole preflight.
- **feat(preflight):** checks implemented
  per PROMPT §4 —
    1. ``config`` loads + validates,
    2. ``schema`` — every table in ``REQUIRED_TABLES`` exists,
    3. ``holidays`` — YAML loads + today's trading-day status +
       next trading day printed,
    4. ``instruments`` — count > 0 AND ``MAX(updated_at)`` <
       7 days old (rejects stale masters),
    5. ``universe`` — table populated AND at least one row
       enabled,
    6. ``trade_mode`` — value is sane; ``live`` fails without
       ``LIVE_TRADING_ACKNOWLEDGED``,
    7. ``control_flags`` — ``scheduler_state=stopped`` AND
       ``kill_switch=armed`` (catches ungraceful shutdown from a
       prior session),
    8. ``backtest_regression`` — replays the shipped bullish
       fixture through ``BacktestHarness``; fails if the trade
       count drops below the expected floor (scoring drift guard),
    9. ``dashboard_health`` — in-process ``TestClient`` hit on
       ``/health`` (no port binding, no thread) so we exercise
       Settings → broker → registry → create_app → route wiring
       before systemd releases the real port,
   10. ``disk_space`` — both ``data/`` and ``logs/`` paths must have
       ≥ 1 GiB free,
   11. ``live_credentials`` — only when ``trade_mode=live``: env-var
       gate + smoke call to ``UpstoxBroker.get_funds()``. Skipped
       (not failed) in ``watch_only``/``paper``.
- **feat(preflight):** CLI flags — ``--config PATH`` and
  ``--skip-backtest`` (trims ExecStartPre start-up to < 2s by
  skipping check 8; recommended for systemd, run full preflight
  manually once a day).
- **feat(deploy):** ``deploy/indian-scalper.service`` gains
  ``ExecStartPre=/opt/indian-scalper/.venv/bin/python -m preflight
  --skip-backtest``. systemd refuses to start the scheduler if any
  gate fails — bad state never reaches the network.
- **docs(runbook):** ``RUNBOOK.md`` at repo root, 8 sections per
  PROMPT + the Wednesday 09:15 launch plan:
    1. First-run procedure (clean clone → bot watching market).
    2. Daily startup checklist (pre-09:15 IST).
    3. Mode transitions (watch_only ↔ paper ↔ live, every click +
       CLI fallback).
    4. Mid-day intervention (pause / kill / re-arm / per-symbol
       override, with a shell-level kill command for when the UI
       is down).
    5. End of day (EOD square-off verification, log rotation,
       equity snapshot archival).
    6. Incident response (broker down, network drops, dashboard
       unresponsive, daily-loss halt, drawdown circuit, SQLite
       corruption).
    7. First live-money checklist (15 boxes before flipping
       ``live``).
    8. Wednesday 09:15 launch plan.
- **test(preflight):** 28 tests covering every check individually
  (happy + unhappy paths, including stale-instruments, empty-
  universe, all-disabled-universe, live-mode-without-env-ack,
  scheduler-not-stopped, kill-switch-tripped), composite
  ``run_all_checks`` green-path + config-failure-skip-rest +
  backtest-skip-flag + universe-fail short-circuit, CLI exit codes
  (0 green / 1 any fail), ``_guard``'s uncaught-exception
  handling, and a pin that the systemd unit wires
  ``ExecStartPre=preflight`` so the D11 contract stays honoured.

### Deliverable 11 — Slice 3 — Signals view + per-symbol charts

- **schema(state):** ``signal_snapshots(id, ts, symbol, score,
  breakdown_json, action, reason, trace_id, trade_mode)``. Indexed on
  ``ts`` / ``symbol`` / ``action``. One row per scored symbol per
  tick — the ground truth the Signals UI + counterfactual queries
  read from.
- **feat(state):** StateStore gains ``append_signal_snapshot``,
  ``load_recent_signals`` (filters: ``limit``, ``min_score``,
  ``actions``, ``trade_modes``), ``load_signals_for_symbol`` (24-hour
  lookback default), ``prune_signal_snapshots_older_than(days=7)``.
  Breakdown stored as JSON; the loader re-hydrates to a dict.
- **feat(scheduler):** every decision branch in ``_evaluate_symbol``
  writes a snapshot via a new ``_record_snapshot`` helper — covers
  ``entered``, ``watch_only_logged`` (per-symbol override + global
  broker rejection), ``skipped_score``, ``skipped_filter`` (short
  history, RSI hard block, ATR non-positive), ``skipped_position_cap``
  (already-long + position-cap gate), ``skipped_risk`` (sized-to-zero).
  Trade mode captured at decision time so "what would have happened
  if we'd been live on day X?" is a SQL query away.
- **feat(scheduler):** ``run_tick`` prunes snapshots older than 7 days
  at most once per calendar day via a kv-flag guard
  (``last_signal_prune_date``) — no DELETE on every 5-min tick.
- **feat(dashboard):** ``GET /signals`` page with an HTMX-polled
  signals table (5s) + filter controls: min-score slider, hide-skipped
  toggle, hide-watch-only toggle, segment filter. Row click opens the
  chart drawer.
- **feat(dashboard):** ``GET /partials/signals_table`` — colour-coded
  by score (green ≥6, amber 4–5, grey <4) and action (entered,
  watch-only-logged, skipped-*). ``GET /api/signals/recent`` + ``GET
  /api/signals/symbol/{symbol}`` return the raw JSON for scripting
  and counterfactual queries.
- **feat(dashboard):** ``GET /api/charts/{symbol}`` — OHLCV + every
  indicator series ``src.strategy.indicators`` emits (EMA 5/13/34/50,
  VWAP, MACD + signal + histogram, RSI, ADX, Bollinger Bands,
  Supertrend line + direction, ATR, volume SMA-20). Uses the same
  indicator functions as the scoring engine — no parallel
  implementation that could drift. Also returns marker timestamps
  for strong-score snapshots + filled-order events so the chart can
  annotate where the engine acted.
- **feat(dashboard):** full-width chart drawer (``signals.html``)
  rendered with Plotly. Candlestick + EMAs + VWAP + Bollinger Bands
  + Supertrend overlay, plus sub-panels for MACD histogram, RSI
  (with entry-range + hard-block lines), ADX (with min-threshold
  line), Volume (with ``volume_surge_multiplier × SMA 20`` line).
  Refresh button + auto-refresh toggle (off by default — heavy).
- **feat(dashboard):** Signals tab now enabled in the nav bar (D11
  Slice 2 had it disabled).
- **test(state):** 10 new tests in ``tests/test_signal_snapshots.py``
  — append + load round-trip, newest-first ordering, limit cap,
  ``min_score`` + ``actions`` + ``trade_modes`` filters, per-symbol
  scoping, 24-hour lookback cutoff, 7-day pruning, counterfactual
  query ("score ≥ 6 + watch_only_logged + trade_mode=watch_only").
- **test(scheduler):** 6 new ``tests/test_scan_loop.py`` tests —
  snapshot written for ``entered`` + ``skipped_score`` +
  ``watch_only_logged`` (per-symbol override) + global-watch-only
  broker rejection + insufficient-candles ``skipped_filter`` + the
  daily-prune guard.
- **test(dashboard):** 12 new tests — page renders with nav,
  ``/api/signals/recent`` empty + populated + ``min_score`` +
  ``actions`` filters, ``/api/signals/symbol/{symbol}`` scope, table
  partial hide-skipped + hide-watch + empty-message,
  ``/api/charts/{symbol}`` candle + indicator shape, **chart
  indicator parity with ``ind.rsi`` + ``ind.macd`` to
  1e-9 tolerance** (the PROMPT's "do NOT recompute differently here"
  guarantee), 404 on unknown symbol, thresholds carry config values.

### Deliverable 11 — Slice 2 — Universe picker

- **schema(state):** ``universe_membership(symbol, segment, enabled,
  watch_only_override, added_at, added_by, PK (symbol, segment))`` is
  now the scheduler's source of truth for which symbols to scan.
  Config provides the *initial* set (seeded on first run); the table
  is authoritative thereafter.
- **feat(data):** ``src/data/universe.py`` — ``UniverseRegistry`` DAO.
  Surface: ``seed_if_empty`` (idempotent first-run seed),
  ``list_entries`` / ``enabled_symbols`` / ``is_enabled`` /
  ``has_watch_only_override`` (reads), ``toggle`` / ``set_enabled`` /
  ``set_watch_only_override`` / ``add`` / ``bulk_update`` /
  ``apply_preset`` (writes). ``add`` validates against the instruments
  master — refuses unknown tickers so a typo can't silently create a
  phantom row.
- **feat(data):** watch-only *override* is a per-symbol flag orthogonal
  to ``enabled``. When set, scan-loop still scores the symbol but
  never places an order, even when global ``trade_mode = paper`` —
  "I want to shadow INFY for a week before letting the bot trade it."
- **feat(data):** presets. ``none`` disables every row; ``all`` enables
  every row. The named-index presets (``nifty_50``, ``nifty_100``,
  ``nifty_next_50``, ``bank_nifty_only``) raise
  ``PresetNotImplementedError`` (dashboard returns 501) with a
  pointer to the shipped-symbol-list follow-up.
- **feat(scheduler):** ``ScanContext`` grows ``universe_registry:
  UniverseRegistry | None``. Scan loop calls
  ``ctx.effective_universe()`` which queries the registry every tick
  (table > static list). When no registry is attached or the table
  is empty, falls back to the ``universe: list[str]`` field — keeps
  bootstrap and tests working.
- **feat(scheduler):** ``_evaluate_symbol`` now consults
  ``registry.has_watch_only_override(symbol)`` before ``place_order``;
  when set, logs a ``watch_only_override — signal logged, no order``
  line and returns without generating a SignalReport. Defense in
  depth complements the global ``trade_mode`` check in the broker.
- **feat(dashboard):** universe page at ``GET /universe`` with a nav
  bar on every page (Dashboard | Universe | Signals-disabled |
  Logs-disabled) for D11 Slice 3 to fill out.
- **feat(dashboard):** universe endpoints (all mutations use the
  existing HMAC confirm-token flow from Slice 0):
    * ``GET /api/universe`` — JSON ``{count, presets, entries}``.
      Each entry carries ``{symbol, segment, enabled,
      watch_only_override, added_at, added_by, ltp, avg_turnover_cr?,
      last_score?, last_scanned_at?}``. The last three are ``null``
      placeholders until Slice 3 signal snapshots land.
    * ``GET /partials/universe_table`` — HTMX-refreshed table with
      search (``?q=``), segment filter, enabled-only filter.
    * ``POST /api/universe/{toggle, watch_only_override, add, bulk,
      preset}/{prepare, apply}`` — (action, target) HMAC tokens, same
      pattern as mode-change in Slice 0. ``apply`` writes + audits;
      ``prepare`` mints the token + returns a preview payload for
      the UI to show in the confirm modal.
    * Single-symbol mutations (toggle / watch-only override) flow
      invisibly through prepare + apply on each click — no modal,
      two quick round-trips. Bulk / preset / add use the confirm
      modal.
- **feat(serve):** ``build_context`` now seeds the universe table
  from the instruments master on first init and attaches a live
  ``UniverseRegistry`` to the scan context + the dashboard app.
- **feat(templates):** ``universe.html`` — search box, segment
  filter, bulk-action buttons (Enable / Disable / Toggle watch-only
  for selected rows), preset dropdown, add-symbol modal.
  ``partials/universe_table.html`` — sortable table with row-level
  toggles and selection checkboxes. Nav bar CSS added to
  ``base.html``.
- **test(data):** 18 new tests in ``tests/test_universe.py`` — seed
  idempotency, seed auditing, toggle round-trip, unknown-symbol
  rejection, audit-row-per-mutation, add-upserts, bulk summary math +
  one-audit-row-per-batch, preset none/all behaviour,
  not-implemented presets, unknown-preset rejection,
  cross-instance persistence.
- **test(scheduler):** 4 new tests — registry-enabled universe drives
  effective_universe, watch-only-override blocks entry even in paper
  mode, mid-session toggle takes effect next tick, empty registry
  falls back to static universe.
- **test(dashboard):** 18 new tests — page renders with nav, ``GET
  /api/universe`` empty + populated, search-filtered partial, toggle
  prepare 404 on unknown row, toggle full flow, stale-token
  rejection, (action, target) token-replay rejection across
  symbols, watch-only-override full flow, add rejects unknown + 409
  on existing + full-flow insert, bulk flow, preset ``none`` works,
  named-index preset 501, unknown-preset 400, every mutation audits
  as ``actor="web"``.

### Deliverable 11 — Slice 1 — Scheduler controls (pause / resume / kill / rearm)

- **feat(scheduler):** ``run_tick`` now obeys a full state machine
  driven by two control_flags keys:

    ``kill_switch``      — ``armed`` (default) → ``tripped``.
                            When tripped on a running scheduler, the
                            current tick squares off *every* open
                            position with ``intent="exit"`` (bypassing
                            watch-only so the exits actually land) and
                            pins ``scheduler_state = "stopped"``. Later
                            ticks while already stopped short-circuit
                            silently.
    ``scheduler_state``  — ``stopped`` (first-run default) | ``paused``
                            | ``running``. ``stopped`` skips the tick
                            entirely. ``paused`` refreshes LTPs (new
                            ``_refresh_ltps`` heartbeat — fetch last
                            candle + mark-to-market + equity snapshot,
                            no ``settle``, no management, no orders)
                            then returns. ``running`` runs the full
                            original pipeline.
- **feat(scheduler):** the drawdown-circuit breach now squares off
  positions **in the same tick** rather than waiting for the next
  cycle — removes a 5-minute exposure window on a severe breach.
  The inline exits are tagged ``reason="drawdown_circuit"`` in
  ``ExitReport`` so the dashboard / audit trail distinguishes them
  from EOD or manual kills.
- **feat(scheduler):** ``_squareoff_all`` grows a ``reason`` param
  (``"eod_squareoff"`` / ``"kill_switch"`` / ``"drawdown_circuit"``).
  ``TickReport.exits`` surfaces the specific reason for downstream
  display.
- **feat(dashboard):** control endpoints.
    * ``GET /api/control/state`` — JSON ``{status, scheduler_state,
      kill_switch, can_pause, can_resume, can_kill, can_rearm, audit}``.
      Status pill is derived here (``RUNNING`` / ``PAUSED`` /
      ``STOPPED`` / ``KILLED``) so the UI renders a single label
      without client-side state-machine logic.
    * ``POST /api/control/pause`` / ``POST /api/control/resume`` —
      single-step. Both 409 if ``kill_switch = tripped``.
    * ``POST /api/control/kill/prepare`` → confirm token + warnings +
      open-positions count. ``POST /api/control/kill/apply`` →
      verifies token, flips the flag (scan loop does the rest on
      next tick). The UI adds a 3-second hold between modal open
      and Confirm-enabled.
    * ``POST /api/control/rearm`` — clears kill_switch but does NOT
      auto-resume. Operator presses Resume separately.
- **feat(dashboard):** controls partial (``GET /partials/controls``)
  + audit drawer partial (``GET /partials/audit``). Controls panel
  polls every 2s, drawer every 5s + on ``controls-changed`` /
  ``mode-changed`` custom events. Audit drawer collapsed by default.
- **feat(dashboard):** kill modal with a 3-second progress-bar
  countdown before Confirm enables. Cancelling the modal clears the
  minted token; apply round-trips ``kill/apply``.
- **refactor(dashboard):** retired the D8 "Kill switch ON/OFF" inline
  panel in favour of the new state-pill + four-button controls. The
  legacy ``/actions/{kill,unkill}`` endpoints still exist for
  external callers (they write the same flag, audited as ``actor=
  "web"``).
- **test(scheduler):** 6 new scan-loop tests — stopped short-circuit,
  paused skips scoring/management/settle but updates LTPs + equity,
  paused does NOT fill pending orders, kill-tick squares off + pins
  stopped, already-killed-and-stopped skips silently, drawdown-breach
  squares off inline in the same tick.
- **test(scheduler):** updated 2 prior tests to the new
  ``skipped_reason == "killed"`` label (was ``"kill_switch"``) and
  the new auto-stop-on-kill contract.
- **test(dashboard):** 14 new control-endpoint tests — default state
  shape, killed state + rearm availability, controls partial HTML,
  audit partial rows, pause/resume flips, resume/pause 409 when kill
  tripped, kill prepare/apply flow + token replay across actions
  (mode_change token fails against kill — (action, target) binding
  is what stops replay), rearm clears flag but doesn't resume,
  every control action writes ``operator_audit`` with ``actor="web"``.
- **test(fixtures):** ``tests/fixtures/running_scheduler(broker)``
  helper so tests that expect ``run_tick`` to exercise the full
  pipeline can opt into the scheduler running state. Threaded
  through ``_build_ctx`` + backtest ``_build``.

### Deliverable 11 — Slice 0 — Trade mode (watch_only / paper / live)

- **feat(trade-mode):** `src/brokers/trade_mode.py` — single module that
  owns the trade-mode vocabulary, the env-var gate, and
  ``check_and_maybe_reject`` (shared between PaperBroker and
  UpstoxBroker). Reads ``trade_mode`` from ``control_flags`` at every
  ``place_order`` call — no caching, so a UI flip takes effect on the
  next tick with no scheduler restart.
- **feat(brokers):** defense-in-depth enforcement. When
  ``trade_mode = watch_only`` and ``intent="entry"``, both brokers
  log a warning, append an ``operator_audit`` row
  (``order_blocked_by_trade_mode``), and return a synthetic Order with
  ``status = "REJECTED_BY_TRADE_MODE"`` and id ``blocked-<uuid>``.
  Never raises — the scheduler treats the rejection as a non-fill and
  moves on. Exits (``intent="exit"``) flow through so stops / trails /
  EOD square-off keep managing existing positions after a mid-day
  flip to watch_only.
- **feat(config):** ``runtime.initial_trade_mode`` config key (defaults
  to ``watch_only`` per PROMPT). Used only on first DB init via
  ``StateStore.ensure_initial_flags`` — after that, the dashboard is
  the source of truth.
- **feat(broker-init):** every ``PaperBroker`` / ``UpstoxBroker``
  construction seeds ``control_flags`` (``trade_mode``,
  ``scheduler_state="stopped"``, ``kill_switch="armed"``) if absent.
  Existing flags are preserved across restarts.
- **feat(dashboard):** confirm-token helper
  (``src/dashboard/confirm.py``) — HMAC-signed ``{exp}.{sig}`` tokens
  bound to ``(action, target)`` with a 30 s TTL. Per-process secret
  — dashboard restart invalidates outstanding tokens (fine — they
  expire quickly anyway).
- **feat(dashboard):** ``GET /api/mode`` / ``POST /api/mode/prepare``
  / ``POST /api/mode/apply`` endpoints. ``prepare`` mints a token +
  returns ``{current_mode, target_mode, open_positions_count,
  warnings, requires_typed_confirm}``. ``apply`` verifies the token
  then writes the flag via ``StateStore.set_flag`` (which audits
  automatically). Both refuse ``live`` without
  ``LIVE_TRADING_ACKNOWLEDGED=yes`` in the environment.
- **feat(dashboard):** ``GET /partials/mode_pill`` — colour-coded
  three-way switch (blue watch-only / amber paper / red blinking
  live). KPI partial + dashboard page poll every 3 s; a
  ``mode-changed`` event after ``apply`` triggers an immediate
  refresh. Confirm modal handles prepare → apply flow and enforces
  typed ``LIVE`` confirmation for live mode.
- **test(trade-mode):** 18 tests in ``tests/test_trade_mode.py`` —
  first-run defaults, config-driven seed, invalid-mode fallback,
  persistence across restarts, paper + Upstox entry blocking in
  watch_only, exit-through for existing-position management,
  default-entry-intent safety, mode reversibility, audit-row content,
  env-var gate, ``check_and_maybe_reject`` unit tests.
- **test(confirm-tokens):** 8 tests in
  ``tests/test_confirm_tokens.py`` — round-trip, (action, target)
  binding, TTL expiry, tampered signature rejection, malformed
  tokens, per-registry secret isolation, non-positive TTL guard.
- **test(dashboard):** 10 new tests for the mode endpoints — current
  mode, pill partial rendering, prepare-token shape, invalid target,
  live without env ack / with env ack, apply flips the flag,
  stale-token rejection, cross-target token-replay rejection, audit
  row on apply.
- **test(fixtures):** ``tests/fixtures/paper_mode()`` helper so tests
  that expect trades to flow can opt into ``paper`` mode explicitly.

### prep for D11 — StateStore control_flags + operator_audit

No behaviour change visible to the existing 240 tests.

- **schema(state):** ``control_flags(key PK, value, updated_at,
  updated_by)`` and ``operator_audit(id PK, ts, actor, action,
  payload_json, trace_id)`` added. Legacy ``kv`` table kept in
  schema but no code writes to it any more.
- **feat(state):** ``set_flag(key, value, actor="system", *,
  trace_id=None)`` writes to ``control_flags`` AND an
  ``operator_audit`` row with action ``flag_set:{key}`` + payload
  ``{value, previous}``. ``get_flag`` reads ``control_flags``.
  ``load_control_flags``, ``ensure_initial_flags``,
  ``append_operator_audit``, ``load_operator_audit`` added for the
  Slice 0/1 dashboard needs.
- **refactor(brokers):** ``set_kill_switch(on, actor="system")`` on
  both brokers — value migrated from ``"1"`` / ``"0"`` to
  ``"tripped"`` / ``"armed"``. External API unchanged.
- **feat(brokers):** ``BrokerBase.place_order`` gains
  ``intent: Literal["entry", "exit"] = "entry"``. Scan loop passes
  ``intent="entry"`` from ``_evaluate_symbol`` and
  ``intent="exit"`` from ``_close_position``. Default ``"entry"`` —
  unannotated callers err on the side of blocking.
- **refactor(scheduler):** scan loop only stashes ``pending_stops``
  if the returned order's status is ``"PENDING"`` (guards against
  Slice-0 rejections polluting the pending-stops dict).
- **refactor(scheduler):** drawdown-circuit latch threads
  ``actor="drawdown_circuit"``; dashboard kill/unkill actions thread
  ``actor="web"``.

### Deliverable 10 — Dockerfile + systemd deployment

- **feat(serve):** `src/serve.py` — single-process production entry
  point. Loads config → configures loguru → builds
  `PaperBroker` + `InstrumentMaster` + `ScanContext` → starts
  APScheduler's `BackgroundScheduler` (tick every
  `scan_interval_seconds`, `max_instances=1`, `coalesce=True`) →
  runs uvicorn serving the FastAPI dashboard. Composition is split
  into `load_or_create_config`, `build_context`, `build_scheduler`
  so tests assemble the pieces without binding a port. Refuses to
  start against `broker: upstox` — live execution needs a separate
  orchestration layer (D9 notes).
- **feat(docker):** `Dockerfile` — multi-stage build
  (python:3.12-slim-bookworm).
    * Stage 1 uses uv to resolve deps from a frozen `uv.lock` with
      `--no-dev` so the runtime image carries no pytest/ruff/mypy.
    * Stage 2 runs as a non-root `scalper:1001` user, `EXPOSE 8080`,
      sets `TZ=Asia/Kolkata`, and ships a `HEALTHCHECK` that hits
      `/health` every 30s (stdlib `urllib.request`, no extra deps).
    * `CMD ["python", "-m", "serve"]`.
- **feat(docker):** `.dockerignore` keeps the build context tight —
  excludes `.venv/`, `data/`, `logs/`, `.git/`, caches, config.yaml,
  .env, tests/, CHANGELOG, bootstrap.py.
- **feat(docker):** `docker-compose.yml` — binds `127.0.0.1:8080:8080`
  by default (no auth → no public exposure), mounts `./data`,
  `./logs`, `./config.yaml`, optional `./.env`, ships a
  dashboard-aware healthcheck.
- **feat(deploy):** `deploy/indian-scalper.service` — systemd unit for
  bare-metal / Raspberry Pi installs. Runs as a dedicated user,
  `Restart=on-failure` with a start-limit burst, `EnvironmentFile=-`
  picks up `.env` if present, and a hardening block
  (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
  `ProtectHome`, `ReadWritePaths=/opt/indian-scalper/data
  /opt/indian-scalper/logs`, `LockPersonality`).
- **docs(README):** extended with Running + Docker + systemd +
  live-trading-gate sections.
- **test(serve):** 20 new tests covering:
    * `build_context` returns a paper ScanContext; refuses
      `broker: upstox` with a clear error.
    * `build_scheduler` registers `scan_tick` with the configured
      interval, `max_instances=1`, `coalesce=True`, and is not
      started automatically.
    * `load_or_create_config` materialises the template on first run
      and preserves operator edits on subsequent runs.
    * Dockerfile is multi-stage, runs non-root (`useradd` + `USER
      scalper`), `EXPOSE 8080`, has `HEALTHCHECK` hitting `/health`,
      uses `uv sync --frozen --no-dev`, `CMD` invokes `serve`.
    * docker-compose binds loopback by default (non-comment lines
      only — documentation comments mentioning 0.0.0.0 don't trip the
      assertion), mounts state volumes, has a healthcheck.
    * systemd unit runs as the dedicated user, restarts on failure,
      includes the hardening block, and `ExecStart` points at the
      venv's Python.
    * `.dockerignore` excludes `.venv/`, `data/`, `logs/`, `.git/`,
      `__pycache__/`, `config.yaml`, `.env`.

### Deliverable 9 — UpstoxBroker (live broker, feature-parity with PaperBroker)

- **feat(brokers):** `src/brokers/upstox.py` — `UpstoxBroker`
  implementing every `BrokerBase` method via the upstox-python-sdk v2
  APIs. Constructor accepts injected API objects so tests never touch
  the real SDK; `_init_sdk()` is the production-only path that reads
  `UPSTOX_ACCESS_TOKEN` from the env var named in `config.yaml`.
- **feat(brokers):** every SDK call is wrapped with a tenacity retry
  decorator — `stop_after_attempt(3)` + exponential backoff (min=1s,
  max=8s). `_is_retryable(exc)` retries on network errors
  (ConnectionError / TimeoutError / OSError) and Upstox `ApiException`
  with HTTP ≥ 500 or 429; fails fast on any other 4xx (bad request,
  auth, not-found).
- **feat(brokers):** `place_order` maps our `Side` / `OrderType` to
  Upstox's string codes, builds a `PlaceOrderRequest` (product=I
  intraday by default), persists the resulting order + audit row to
  `StateStore`. `modify_order` resolves missing fields from the cached
  order so callers can supply a partial update (the SDK rejects
  `None` on validity / price / order_type / trigger_price).
  `cancel_order` flips the stored status to `CANCELLED`.
- **feat(brokers):** `get_positions`, `get_funds`, `get_ltp`,
  `get_candles` parse the SDK's model/dict hybrid responses through
  `_extract_data` + `_field` helpers that tolerate both shapes.
  `_parse_candle_response` handles epoch-seconds + ISO-string +
  native-datetime timestamps.
- **feat(brokers):** symbol → Upstox `instrument_key` (`NSE_EQ|{isin}`)
  via a pluggable `key_resolver`. Default resolver reads the ISIN
  column InstrumentMaster already stores; callers can inject their
  own for F&O keys.
- **feat(brokers):** local kill switch via `StateStore.kv` flag — same
  API as PaperBroker, so the dashboard halts entries identically in
  live mode. Optional `update_server_kill_switch(segment, on)` also
  flips Upstox's server-side segment-level halt.
- **feat(main):** `src/main.py` now constructs UpstoxBroker when
  `broker: upstox` is set. Adds `_assert_live_mode_acknowledged`
  guard — refuses to start in `mode: live` without
  `LIVE_TRADING_ACKNOWLEDGED=yes` env var (PROMPT.md compliance) and
  a terminal `LIVE` confirmation on a TTY. Scan-loop integration with
  live Upstox is deliberately deferred (requires order-status polling
  / websocket fills / bracket orders) — D9 ships the broker class +
  safety gates, not live scan-loop execution.
- **test(brokers):** 23 mock-based tests in `tests/test_upstox_broker.py`
  covering:
    * retry policy (server errors, rate-limit, network errors retried;
      4xx not retried; retry+succeed flow; fail-fast flow),
    * symbol → instrument_key resolution (ISIN lookup, unknown symbol,
      custom resolver),
    * place/cancel/modify order (request body shape, api_version,
      partial modify merges with cached state, SL-M trigger_price
      handling, non-positive-qty rejection),
    * get_positions / get_funds / get_ltp / get_candles response
      parsing (model + dict shapes, intraday endpoint routing,
      unsupported interval rejection, empty LTP short-circuit,
      zero-qty position filtering),
    * kill-switch parity with PaperBroker + server-side ENABLE/DISABLE
      mapping,
    * constructor safety — missing env var raises.

### Deliverable 8 — FastAPI + HTMX dashboard

- **feat(dashboard):** `src/dashboard/app.py` — `create_app(broker,
  settings, log_file=None)` factory. Mounts Jinja templates, binds the
  live `PaperBroker` to app state, wires every route. No SPA, no
  build step — all refresh is HTMX polling over Jinja-rendered
  partials.
- **feat(dashboard):** routes — `GET /` page shell, `GET
  /partials/{kpis,positions,trades,logs}` polled fragments,
  `GET /api/equity.json` Plotly-ready series, `POST /actions/{kill,
  unkill}` kill-switch toggles, `GET /health` smoke check.
- **feat(dashboard):** templates (`src/dashboard/templates/*.html`) —
  dark-themed GitHub-ish palette, prominent `PAPER TRADING // NOT
  FINANCIAL ADVICE` banner. KPI tiles poll every 5s; positions every
  5s; trades every 10s; log tail every 3s; equity curve every 30s.
- **feat(dashboard):** Plotly equity curve (dark theme) with a dashed
  starting-capital reference line. Loaded via CDN.
- **feat(dashboard):** `MAX_LOG_LINES = 200` tail from the configured
  loguru file path. Gracefully handles missing file / empty file.
- **test(dashboard):** 14 tests via FastAPI TestClient — page shell,
  health, KPI starting-capital + kill-switch render, positions
  empty vs populated (with LTP + stop + TP), trades empty vs
  closed-round-trip, equity JSON shape + contents, kill/unkill
  actions, log tail with and without configured file.

### Deliverable 7 — Backtest harness + dry-run mode

- **feat(backtest):** `src/backtest/harness.py` — `BacktestHarness.run()`
  iterates the union of all candle timestamps, advances a
  ``BacktestCandleFetcher`` cutoff per bar (so no look-ahead into
  future candles), and drives the *same* ``run_tick`` the live scan
  loop uses. Strategy + risk code paths in a backtest are
  byte-identical to production.
- **feat(backtest):** `BacktestCandleFetcher` — ``set_now(ts)`` +
  ``get_candles`` filter. Subclass of ``FakeCandleFetcher`` so tests
  can still seed arbitrary series.
- **feat(backtest):** `BacktestConfig` (bars_per_year + stop_at_ts)
  and `BacktestResult` (trades, equity_curve, tick_reports, metrics,
  starting/final equity, timestamps_processed, ticks_skipped). Result
  includes a ``.summary()`` method for human-readable console output.
- **feat(backtest):** `src/backtest/trades.py` — `extract_trades()`
  FIFO-pairs filled BUY/SELL orders into closed `Trade` rows. Handles
  partial closes (one BUY → multiple Trade rows as SELLs chip away).
  Open positions at end-of-series are not reported. Long-only for now.
- **feat(backtest):** `src/backtest/metrics.py` — `compute_sharpe`
  (annualised from bar-returns with configurable ``bars_per_year``),
  `compute_max_drawdown` (peak / trough + timestamps),
  `compute_win_rate`, `compute_avg_rr` (realised |avg_win|/|avg_loss|
  ratio), `compute_total_pnl`, `compute_avg_holding_minutes`. Every
  function tolerates empty / degenerate input — returns NaN or 0.0
  rather than raising.
- **feat(backtest):** `src/backtest/dry_run.py` — `run_dry_run(ctx,
  fetcher, speed_multiplier=10)` wraps the harness loop with
  ``time.sleep`` calibrated from ``candle_interval``. ``sleep_fn`` is
  injectable so tests don't actually sleep. Rejects unsupported
  intervals + non-positive speeds.
- **fix(scheduler):** order timestamps now come from the scan loop's
  simulated ``ts`` instead of ``datetime.now(IST)``. Bug surfaced via
  backtest replay where entry and exit order ts values were
  wall-clock-milliseconds apart, destroying holding-time metrics.
  PaperBroker.place_order accepts an optional ``ts`` kwarg;
  ``run_tick`` threads the tick's ``ts`` through every entry /
  exit / EOD-close / time-stop-close call.
- **fix(scheduler):** position sizing now caps at ``available × 0.95``
  so a 100%-of-cash entry plus downstream slippage can never trip the
  InsufficientFundsError guard on re-entries after tight-stop fixtures.
- **test(backtest):** 6 tests for FIFO trade extraction
  (round-trip, losing trade, pending-order exclusion, partial close,
  open-position-at-end handling, multi-symbol independence).
- **test(backtest):** 14 tests for metrics (Sharpe sign + NaN edges,
  max drawdown on rising / falling curves, win-rate math, avg-RR
  no-losses/no-wins NaN, total P&L, avg holding).
- **test(backtest):** 11 integration tests for the harness + dry-run
  (future-masking contract, trade closure on bullish fixture, Saturday
  series skipped entirely, empty-series safety, summary rendering,
  dry-run sleep count = bars-1, bad speed rejected, unknown interval
  rejected, same result shape as harness, stop-at-ts truncation).

### Deliverable 6 — Scan loop

- **feat(scheduler):** full rewrite of `src/scheduler/scan_loop.py`.
  Integrates every earlier deliverable into a single tick pipeline:
  kill switch → market-hours + holidays (D2) → candle fetch + settle
  (D4) → attach stashed stops to filled entries → EOD square-off gate
  (D5) → position management (stops/TP/trail/time stop) →
  portfolio-level gates (daily loss, drawdown; D5) → per-symbol
  evaluate (score + size + place, D3 + D5). Every tick gets a
  trace_id stamped into the returned `TickReport` and logged on every
  decision.
- **feat(scheduler):** `run_tick(ctx, ts)` is a pure tick pass —
  deterministic enough to scenario-test without APScheduler.
  `run_scan_loop(ctx)` is the production wrapper that uses
  APScheduler `BlockingScheduler` + `IntervalTrigger` keyed on
  `scan_interval_seconds`.
- **feat(scheduler):** `ScanContext` dataclass wraps settings, broker,
  universe, instruments master, optional holiday calendar, and the
  scan loop's `pending_stops` dict (order_id → (stop, tp)). Stops are
  stashed at entry and applied to the position on the next settle —
  if the loop crashes between fill and attach, the management branch
  notices a missing `stop_loss` and rebuilds from current ATR.
- **feat(scheduler):** drawdown circuit latches the kill switch.
  When the drawdown gate blocks entries, the scan loop flips
  `StateStore.set_flag("kill_switch", "1")` so downstream ticks are
  fully locked out. Daily-loss halt does *not* latch — auto-releases
  at the next session as intended.
- **feat(brokers):** `PaperBroker.set_position_stops(symbol, stop_loss,
  take_profit, trail_stop)` — partial-update helper used by the scan
  loop to attach ATR-derived stops after a position fills and to
  ratchet trailing stops on each tick.
- **feat(data):** `data.market_data.df_to_candles(df)` — converts an
  OHLCV DataFrame (with DatetimeIndex) back to `list[Candle]`, the
  glue that lets the D3 synthetic fixtures feed the broker's
  FakeCandleFetcher in scan-loop tests.
- **test(scheduler):** 15 new scenarios in `tests/test_scan_loop.py`:
    * kill switch skips entire tick
    * market closed / holiday skips
    * bullish signal → entry placed + stops stashed
    * flat chop → no signal
    * two-tick flow: entry → settle → stops applied to position
    * no double-up on existing position
    * EOD square-off closes every position
    * stop_loss / take_profit / trail_stop / time_stop exits fire
    * daily-loss halt blocks entries
    * drawdown circuit latches kill switch → next tick locked out
    * outside entry window still manages existing positions

### Deliverable 5 — Risk engine

- **feat(risk):** `src/risk/position_sizing.py` — `position_size(...)`
  returns a `SizeResult` with qty, risk rupees, per-unit risk, notional,
  and an optional diagnostic note. Formula: `qty = floor((capital ×
  risk_pct / 100) / |entry − stop|)` rounded down to `lot_size` multiples.
  Returns qty=0 with a note when inputs are degenerate (entry == stop,
  zero capital, zero risk_pct). Optional `max_notional` cap enforced
  on top of the risk-based qty.
- **feat(risk):** `src/risk/stops.py` — pure functions for
  `atr_stop_price`, `take_profit_price`, `update_trail_stop`
  (ratchets only — never loosens), `trailing_multiplier` (selects
  low/high-vol multiplier by comparing current ATR to the 50-bar
  median — falls back to the conservative low-vol multiplier when
  history is too short), `check_time_stop` (aged-out deadband check,
  returns `TimeStopDecision`), and a tz-aware `minutes_since` helper.
- **feat(risk):** `src/risk/circuit_breaker.py` — entry-gate stack
  returning `RiskGate(allow_new_entries, reason)`:
    * `check_position_limits` (per-segment equity vs F&O caps),
    * `check_daily_loss_limit` (auto-releases next session),
    * `check_drawdown_circuit` (manual-reset trip),
    * `is_eod_squareoff_time` (predicate only — caller triggers
      square-off),
    * `combine_gates(...)` short-circuits on the first blocker so its
      reason surfaces to the caller,
    * `peak_equity_from_curve` + `start_of_day_equity` helpers that
      work off of `StateStore.load_equity_curve()` rows so the scan
      loop (Deliverable 6) doesn't have to re-implement reductions.
- **test(risk):** 9 tests for position sizing (equity math, F&O lot
  rounding, degenerate inputs, max-notional cap, short-side math).
- **test(risk):** 17 tests for stops (initial stops, take-profits,
  trailing-multiplier regime selection + short-history fallback,
  ratchet invariants for long and short, time-stop three-way branch
  + missing-opened_at guard + tz-aware requirement).
- **test(risk):** 18 tests for circuit breakers (equity/F&O caps,
  daily-loss threshold, drawdown threshold, EOD predicate boundary,
  gate combinator short-circuit, equity-curve reducers with and
  without matching session).

### Deliverable 4 — PaperBroker + order manager + state persistence

- **feat(execution):** `src/execution/state.py` — `StateStore`, SQLite DAO.
  Tables: `orders`, `positions`, `equity_curve`, `audit_log` (append-only),
  `kv` (kill switch + flags). Every write runs in its own transaction.
  Upsert-based so repeated saves never duplicate rows.
- **feat(execution):** `src/execution/order_manager.py` — `OrderManager`,
  paper-mode fill simulator. MARKET orders fill on next `settle(symbol,
  candle)` at `candle.open * (1 ± slippage_pct/100)`; LIMIT orders fill
  at the limit price when the candle range crosses it; SL / SL-M fill
  at `trigger * (1 ± slippage)`. Supports averaging-in, position flips,
  partial closes, and full flat-out. Enforces cash guard — BUY orders
  that exceed available cash are REJECTED (raises
  `InsufficientFundsError`) with an audit entry.
- **feat(data):** `src/data/market_data.py` — `CandleFetcher` protocol
  with three implementations: `FakeCandleFetcher` (deterministic,
  test-only, raises on unseeded symbols), `YFinanceFetcher` (lazy
  yfinance import, `.NS` suffix), and CSV cache helpers
  (`candles_to_csv`, `candles_from_csv`, `build_synthetic_candles`).
- **feat(brokers):** `src/brokers/paper.py` now fully implements
  `BrokerBase`. Composes `StateStore`, `OrderManager`,
  `InstrumentMaster`, and an injectable `CandleFetcher`. Adds
  `settle(symbol, candle)` (advances fill simulation + updates LTP
  cache + snapshots equity), `mark_to_market(prices)`, and a kill-switch
  flag persisted in SQLite.
- **feat(brokers):** **idempotent recovery** — restarting `PaperBroker`
  against an existing SQLite file reloads every pending order,
  every open position, and reconstructs cash by replaying filled-order
  cash flow. Covered by a dedicated test (`broker1.place_order(...)` →
  `broker2 = PaperBroker(same_db)` → assertions).
- **feat(audit):** every order lifecycle event (submit, modify, cancel,
  fill, reject) appends a row to `audit_log` with a JSON details blob.
- **chore(deps):** added `yfinance>=0.2.40` as a runtime dep so paper
  mode works out of the box. yfinance is lazy-imported in
  `YFinanceFetcher.get_candles` so tests don't pay the import cost.
- **test(execution):** 8 tests for `StateStore` (round-trip, idempotency,
  filtered loads, audit append-only, kill-switch flag).
- **test(execution):** 15 tests for `OrderManager` (market/limit/SL
  fills, cancel, modify, cash guard, averaging, position flips,
  mark-to-market, restart recovery).
- **test(data):** 4 tests for market_data (FakeFetcher, CSV
  round-trip, synthetic candle shapes).
- **test(brokers):** 13 tests for full PaperBroker lifecycle
  (BrokerBase conformance, order placement, settle/fill,
  equity-curve snapshot on settle, cold-vs-warm LTP, kill switch,
  audit trail, recovery).

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
