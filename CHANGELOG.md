# Changelog

All notable changes to this project are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/) with
conventional-commits style entries.

## [Unreleased]

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
