"""FastAPI + HTMX dashboard.

Single-page local dashboard. Polls partials every few seconds via HTMX
``hx-trigger="every Ns"`` — no SSE, no websockets, no frontend build.
All data comes from an in-memory ``PaperBroker`` and its underlying
``StateStore`` (so the dashboard and scan loop must run in the same
process; use APScheduler's ``BackgroundScheduler`` in production).

Route map:
  GET  /                        page shell
  GET  /partials/kpis           KPI tiles (polled)
  GET  /partials/positions      open-positions table
  GET  /partials/trades         last-50 closed trades
  GET  /partials/logs           tail of loguru log file
  GET  /api/equity.json         Plotly-ready equity curve series
  POST /actions/kill            flip kill switch on
  POST /actions/unkill          flip kill switch off
  GET  /health                  smoke check
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from backtest.trades import extract_trades
from brokers.base import Segment
from brokers.paper import PaperBroker
from brokers.trade_mode import (
    VALID_TRADE_MODES,
    current_trade_mode,
    live_trading_acknowledged,
)
from config.settings import Settings
from dashboard.confirm import ConfirmTokenRegistry
from data.universe import (
    KNOWN_PRESETS,
    PresetNotImplementedError,
    UniverseRegistry,
    UnknownSymbolError,
)
from strategy import indicators as ind
from risk.circuit_breaker import (
    peak_equity_from_curve,
    start_of_day_equity,
)
from scheduler.market_hours import now_ist

TEMPLATES_DIR = Path(__file__).parent / "templates"
MAX_LOG_LINES = 200


@dataclass
class DashboardState:
    broker: PaperBroker
    settings: Settings
    log_file: Path | None
    confirm_tokens: ConfirmTokenRegistry
    universe_registry: UniverseRegistry | None


class ModePrepareBody(BaseModel):
    target: str = Field(..., description="watch_only | paper | live")


class ModeApplyBody(BaseModel):
    target: str
    token: str


class KillApplyBody(BaseModel):
    token: str


class AutoResumeToggleBody(BaseModel):
    enabled: bool


# D11 Slice 2 — universe mutations
class UniverseSingleBody(BaseModel):
    symbol: str
    segment: str = "EQ"


class UniverseApplyBody(UniverseSingleBody):
    token: str
    value: bool | None = None  # for watch_only_override apply


class UniverseBulkPrepBody(BaseModel):
    operations: list[dict[str, Any]]


class UniverseBulkApplyBody(UniverseBulkPrepBody):
    token: str


class UniversePresetBody(BaseModel):
    preset: str


class UniversePresetApplyBody(UniversePresetBody):
    token: str


def create_app(
    broker: PaperBroker,
    settings: Settings,
    log_file: str | Path | None = None,
    universe_registry: UniverseRegistry | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a live ``PaperBroker``.

    ``universe_registry`` is optional: when omitted the D11 Slice 2
    universe endpoints still work by constructing a lazy registry over
    the broker's store + instruments master — so the dashboard always
    has a universe surface even if the caller forgot to pass one.
    """
    app = FastAPI(title="Indian Scalper Dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    if universe_registry is None:
        universe_registry = UniverseRegistry(broker.store, broker.instruments)

    state = DashboardState(
        broker=broker,
        settings=settings,
        log_file=Path(log_file) if log_file else None,
        confirm_tokens=ConfirmTokenRegistry(),
        universe_registry=universe_registry,
    )
    app.state.dashboard = state

    # ---------------- Pages ----------------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"banner": "PAPER TRADING // NOT FINANCIAL ADVICE"},
        )

    # ---------------- Partials (HTMX polling) ----------------

    @app.get("/partials/kpis", response_class=HTMLResponse)
    def kpis_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/kpis.html", _kpis_context(state)
        )

    @app.get("/partials/positions", response_class=HTMLResponse)
    def positions_partial(request: Request) -> HTMLResponse:
        # Live-refresh LTP for open positions so P&L updates on every
        # dashboard tick (not just every scheduler scan).
        try:
            state.broker.refresh_live_ltp()
        except Exception:
            pass
        positions = state.broker.get_positions()
        enriched = [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "ltp": state.broker._ltp.get(p.symbol, p.avg_price),
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "trail_stop": p.trail_stop,
                "opened_at": p.opened_at,
                "pnl": (state.broker._ltp.get(p.symbol, p.avg_price) - p.avg_price) * p.qty,
            }
            for p in positions
        ]
        return templates.TemplateResponse(
            request, "partials/positions.html", {"positions": enriched},
        )

    @app.get("/partials/trades", response_class=HTMLResponse)
    def trades_partial(request: Request) -> HTMLResponse:
        orders = state.broker.store.load_orders()
        trades = extract_trades(orders)[-50:]  # newest 50
        return templates.TemplateResponse(
            request, "partials/trades.html", {"trades": list(reversed(trades))},
        )

    @app.get("/partials/logs", response_class=HTMLResponse)
    def logs_partial(request: Request) -> HTMLResponse:
        lines = _tail_log(state.log_file, MAX_LOG_LINES)
        return templates.TemplateResponse(
            request, "partials/logs.html", {"lines": lines},
        )

    # ---------------- API ----------------

    @app.get("/api/equity.json")
    def equity_json() -> JSONResponse:
        curve = state.broker.store.load_equity_curve()
        return JSONResponse(
            {
                "x": [row["ts"] for row in curve],
                "y": [float(row["equity"]) for row in curve],
                "starting": state.settings.capital.starting_inr,
            }
        )

    # ---------------- Actions (POST) ----------------

    @app.post("/actions/kill")
    def action_kill() -> JSONResponse:
        state.broker.set_kill_switch(True, actor="web")
        return JSONResponse({"ok": True, "kill_switch": True})

    @app.post("/actions/unkill")
    def action_unkill() -> JSONResponse:
        state.broker.set_kill_switch(False, actor="web")
        return JSONResponse({"ok": True, "kill_switch": False})

    # ---------------- Trade mode (D11 Slice 0) ----------------

    @app.get("/api/mode")
    def mode_current() -> JSONResponse:
        return JSONResponse(_mode_status_context(state))

    @app.post("/api/mode/prepare")
    def mode_prepare(body: ModePrepareBody) -> JSONResponse:
        target = body.target
        if target not in VALID_TRADE_MODES:
            raise HTTPException(400, f"invalid target {target!r}")
        current = current_trade_mode(state.broker.store)
        # Refuse to even issue a confirm token for live when the env-var
        # gate isn't set — the UI shows this as an error rather than a
        # modal.
        if target == "live" and not live_trading_acknowledged():
            raise HTTPException(
                400,
                "live mode requires LIVE_TRADING_ACKNOWLEDGED=yes in the "
                "environment",
            )
        token, expires_at = state.confirm_tokens.issue("mode_change", target)
        positions = state.broker.get_positions()
        warnings: list[str] = []
        if target == "watch_only" and positions:
            warnings.append(
                f"{len(positions)} open position(s) — stops/trails will still "
                "manage them, but no new entries will be placed."
            )
        if target == "live":
            warnings.append("LIVE trading. Type the word LIVE to confirm.")
        return JSONResponse(
            {
                "current_mode": current,
                "target_mode": target,
                "token": token,
                "expires_at": expires_at,
                "open_positions_count": len(positions),
                "warnings": warnings,
                "requires_typed_confirm": target == "live",
            }
        )

    @app.post("/api/mode/apply")
    def mode_apply(body: ModeApplyBody) -> JSONResponse:
        target = body.target
        if target not in VALID_TRADE_MODES:
            raise HTTPException(400, f"invalid target {target!r}")
        if not state.confirm_tokens.verify("mode_change", target, body.token):
            raise HTTPException(403, "confirm token invalid or expired")
        if target == "live" and not live_trading_acknowledged():
            raise HTTPException(
                400,
                "live mode requires LIVE_TRADING_ACKNOWLEDGED=yes in the "
                "environment",
            )
        previous = current_trade_mode(state.broker.store)
        state.broker.store.set_flag("trade_mode", target, actor="web")
        return JSONResponse(
            {
                "ok": True,
                "previous_mode": previous,
                "current_mode": target,
            }
        )

    @app.get("/partials/mode_pill", response_class=HTMLResponse)
    def mode_pill_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/mode_pill.html", _mode_status_context(state),
        )

    # ---------------- Mobile (D12 Session 2) ----------------
    #
    # /m/ prefix is a completely separate template tree tuned for
    # 375 px phone screens. Reuses the same _control_state_context /
    # _kpis_context helpers so there's one source of truth for
    # state; two layouts, one model.

    @app.get("/m", response_class=HTMLResponse)
    @app.get("/m/", response_class=HTMLResponse)
    def mobile_home(request: Request) -> HTMLResponse:
        ctx = {
            **_kpis_context(state),
            **_control_state_context(state),
            **_mode_status_context(state),
            "banner": "PAPER TRADING — NOT FINANCIAL ADVICE",
        }
        return templates.TemplateResponse(request, "mobile.html", ctx)

    @app.get("/m/partials/overview", response_class=HTMLResponse)
    def mobile_overview(request: Request) -> HTMLResponse:
        ctx = {
            **_kpis_context(state),
            **_control_state_context(state),
            **_mode_status_context(state),
        }
        return templates.TemplateResponse(
            request, "mobile_overview.html", ctx,
        )

    @app.get("/m/partials/signals", response_class=HTMLResponse)
    def mobile_signals(request: Request, limit: int = 20) -> HTMLResponse:
        rows = state.broker.store.load_recent_signals(
            limit=limit, actions=["entered", "watch_only_logged"],
        )
        return templates.TemplateResponse(
            request, "mobile_signals.html", {"rows": rows},
        )

    # ---------------- Controls (D11 Slice 1) ----------------

    @app.get("/api/control/state")
    def control_state() -> JSONResponse:
        return JSONResponse(_control_state_context(state))

    @app.get("/partials/controls", response_class=HTMLResponse)
    def controls_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/controls.html", _control_state_context(state),
        )

    @app.get("/partials/audit", response_class=HTMLResponse)
    def audit_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/audit_drawer.html",
            {"rows": state.broker.store.load_operator_audit(limit=20)},
        )

    @app.post("/api/control/pause")
    def control_pause() -> JSONResponse:
        # Refuse to pause when kill has latched — operator must rearm
        # + resume consciously, not pause the dead scheduler.
        if state.broker.is_kill_switch_on():
            raise HTTPException(409, "kill switch is tripped; rearm first")
        state.broker.store.set_flag("scheduler_state", "paused", actor="web")
        return JSONResponse({"ok": True, "scheduler_state": "paused"})

    @app.post("/api/control/resume")
    def control_resume() -> JSONResponse:
        if state.broker.is_kill_switch_on():
            raise HTTPException(
                409, "kill switch is tripped; rearm before resuming",
            )
        state.broker.store.set_flag("scheduler_state", "running", actor="web")
        return JSONResponse({"ok": True, "scheduler_state": "running"})

    @app.post("/api/control/rearm")
    def control_rearm() -> JSONResponse:
        """Clear the kill switch. Explicitly does NOT resume — the
        operator must press Resume separately, on purpose."""
        state.broker.set_kill_switch(False, actor="web")
        return JSONResponse(
            {
                "ok": True,
                "kill_switch": "armed",
                "scheduler_state": state.broker.store.get_flag(
                    "scheduler_state", "stopped",
                ),
            }
        )

    @app.post("/api/control/auto_resume")
    def control_auto_resume(body: AutoResumeToggleBody) -> JSONResponse:
        """Flip the auto-resume opt-in flag. The launchd agent fires
        every minute; this flag is what controls whether those fires
        translate into scheduler state changes. Refuses to enable when
        kill_switch is tripped (forces a conscious re-arm first)."""
        target = "1" if body.enabled else "0"
        if body.enabled and state.broker.is_kill_switch_on():
            raise HTTPException(
                409, "kill switch is tripped; rearm before enabling auto-resume",
            )
        state.broker.store.set_flag(
            "auto_resume_enabled", target, actor="web",
        )
        return JSONResponse(
            {"ok": True, "auto_resume_enabled": body.enabled},
        )

    @app.post("/api/control/kill/prepare")
    def control_kill_prepare() -> JSONResponse:
        token, expires_at = state.confirm_tokens.issue("control_kill", "kill")
        positions = state.broker.get_positions()
        return JSONResponse(
            {
                "token": token,
                "expires_at": expires_at,
                "open_positions_count": len(positions),
                "warnings": [
                    "All open positions will be squared off at market immediately.",
                    "Scheduler will be stopped. Rearm + Resume to restart.",
                ],
            }
        )

    @app.post("/api/control/kill/apply")
    def control_kill_apply(body: KillApplyBody) -> JSONResponse:
        if not state.confirm_tokens.verify("control_kill", "kill", body.token):
            raise HTTPException(403, "confirm token invalid or expired")
        # Flip the flag — scan loop's next tick does the square-off.
        # Dashboard is decoupled from scheduler per architectural
        # principle ("UI publishes intent by writing to SQLite").
        state.broker.set_kill_switch(True, actor="web")
        return JSONResponse(
            {
                "ok": True,
                "kill_switch": "tripped",
                "note": "scheduler will square off open positions + stop on next tick",
            }
        )

    # ---------------- Universe (D11 Slice 2) ----------------

    @app.get("/api/universe")
    def api_universe(segment: str | None = None, enabled_only: bool = False) -> JSONResponse:
        reg = state.universe_registry
        assert reg is not None
        entries = reg.list_entries(segment=segment, enabled_only=enabled_only)
        ltp_cache = state.broker._ltp
        return JSONResponse(
            {
                "count": len(entries),
                "presets": list(KNOWN_PRESETS),
                "entries": [
                    {
                        "symbol": e.symbol,
                        "segment": e.segment,
                        "enabled": e.enabled,
                        "watch_only_override": e.watch_only_override,
                        "added_at": e.added_at,
                        "added_by": e.added_by,
                        "ltp": ltp_cache.get(e.symbol),
                        # Liquidity / score / last_scanned populated by
                        # Slice 3 signal_snapshots — "—" placeholder.
                        "avg_turnover_cr": None,
                        "last_score": None,
                        "last_scanned_at": None,
                    }
                    for e in entries
                ],
            }
        )

    @app.get("/universe", response_class=HTMLResponse)
    def page_universe(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "universe.html",
            {"banner": "PAPER TRADING // NOT FINANCIAL ADVICE"},
        )

    @app.get("/partials/universe_table", response_class=HTMLResponse)
    def universe_table_partial(
        request: Request,
        q: str = "",
        segment: str | None = None,
        enabled_only: bool = False,
    ) -> HTMLResponse:
        reg = state.universe_registry
        assert reg is not None
        entries = reg.list_entries(segment=segment, enabled_only=enabled_only)
        if q:
            ql = q.strip().lower()
            entries = [e for e in entries if ql in e.symbol.lower()]
        ltp_cache = state.broker._ltp
        return templates.TemplateResponse(
            request,
            "partials/universe_table.html",
            {
                "entries": entries,
                "ltp_cache": ltp_cache,
                "presets": KNOWN_PRESETS,
                "query": q,
            },
        )

    # ---- toggle ----

    @app.post("/api/universe/toggle/prepare")
    def universe_toggle_prepare(body: UniverseSingleBody) -> JSONResponse:
        reg = state.universe_registry
        assert reg is not None
        entry = reg.get(body.symbol, body.segment)
        if entry is None:
            raise HTTPException(404, f"{body.symbol}/{body.segment} not in universe")
        target = f"{body.symbol}:{body.segment}"
        token, expires_at = state.confirm_tokens.issue("universe_toggle", target)
        return JSONResponse({
            "token": token,
            "expires_at": expires_at,
            "preview": {
                "symbol": body.symbol, "segment": body.segment,
                "current": entry.enabled,
                "next": not entry.enabled,
            },
        })

    @app.post("/api/universe/toggle/apply")
    def universe_toggle_apply(body: UniverseApplyBody) -> JSONResponse:
        target = f"{body.symbol}:{body.segment}"
        if not state.confirm_tokens.verify("universe_toggle", target, body.token):
            raise HTTPException(403, "confirm token invalid or expired")
        reg = state.universe_registry
        assert reg is not None
        try:
            entry = reg.toggle(body.symbol, body.segment, actor="web")
        except KeyError:
            raise HTTPException(404, f"{body.symbol}/{body.segment} not in universe")
        return JSONResponse({"ok": True, "enabled": entry.enabled})

    # ---- watch-only override ----

    @app.post("/api/universe/watch_only_override/prepare")
    def universe_watch_prepare(body: UniverseSingleBody) -> JSONResponse:
        reg = state.universe_registry
        assert reg is not None
        entry = reg.get(body.symbol, body.segment)
        if entry is None:
            raise HTTPException(404, f"{body.symbol}/{body.segment} not in universe")
        target = f"{body.symbol}:{body.segment}"
        token, expires_at = state.confirm_tokens.issue(
            "universe_watch_only_override", target,
        )
        return JSONResponse({
            "token": token,
            "expires_at": expires_at,
            "preview": {
                "symbol": body.symbol, "segment": body.segment,
                "current": entry.watch_only_override,
                "next": not entry.watch_only_override,
            },
        })

    @app.post("/api/universe/watch_only_override/apply")
    def universe_watch_apply(body: UniverseApplyBody) -> JSONResponse:
        target = f"{body.symbol}:{body.segment}"
        if not state.confirm_tokens.verify(
            "universe_watch_only_override", target, body.token,
        ):
            raise HTTPException(403, "confirm token invalid or expired")
        reg = state.universe_registry
        assert reg is not None
        entry = reg.get(body.symbol, body.segment)
        if entry is None:
            raise HTTPException(404, f"{body.symbol}/{body.segment} not in universe")
        # Flip the current value (matches the preview the prepare step
        # generated). If the caller really wants absolute set, they can
        # use /bulk.
        new_value = not entry.watch_only_override
        try:
            out = reg.set_watch_only_override(
                body.symbol, body.segment, new_value, actor="web",
            )
        except KeyError:
            raise HTTPException(404, f"{body.symbol}/{body.segment} not in universe")
        return JSONResponse({"ok": True, "watch_only_override": out.watch_only_override})

    # ---- bulk ----

    @app.post("/api/universe/bulk/prepare")
    def universe_bulk_prepare(body: UniverseBulkPrepBody) -> JSONResponse:
        ops = body.operations
        token, expires_at = state.confirm_tokens.issue(
            "universe_bulk", f"count:{len(ops)}",
        )
        enabled_count = sum(1 for o in ops if o.get("enabled") is True)
        disabled_count = sum(1 for o in ops if o.get("enabled") is False)
        watch_count = sum(1 for o in ops if "watch_only_override" in o)
        return JSONResponse({
            "token": token,
            "expires_at": expires_at,
            "preview": {
                "op_count": len(ops),
                "enable": enabled_count,
                "disable": disabled_count,
                "watch_changes": watch_count,
            },
        })

    @app.post("/api/universe/bulk/apply")
    def universe_bulk_apply(body: UniverseBulkApplyBody) -> JSONResponse:
        target = f"count:{len(body.operations)}"
        if not state.confirm_tokens.verify("universe_bulk", target, body.token):
            raise HTTPException(403, "confirm token invalid or expired")
        reg = state.universe_registry
        assert reg is not None
        summary = reg.bulk_update(body.operations, actor="web")
        return JSONResponse({"ok": True, "summary": summary})

    # ---- add ----

    @app.post("/api/universe/add/prepare")
    def universe_add_prepare(body: UniverseSingleBody) -> JSONResponse:
        reg = state.universe_registry
        assert reg is not None
        # Fail fast if the instrument isn't known — saves an unnecessary
        # confirm round-trip.
        if state.broker.instruments.get(body.symbol) is None:
            raise HTTPException(
                400,
                f"{body.symbol!r} not in instruments master — refresh it or "
                "check the ticker",
            )
        if reg.get(body.symbol, body.segment) is not None:
            raise HTTPException(
                409, f"{body.symbol}/{body.segment} already in universe",
            )
        target = f"{body.symbol}:{body.segment}"
        token, expires_at = state.confirm_tokens.issue("universe_add", target)
        return JSONResponse({
            "token": token,
            "expires_at": expires_at,
            "preview": {"symbol": body.symbol, "segment": body.segment},
        })

    @app.post("/api/universe/add/apply")
    def universe_add_apply(body: UniverseApplyBody) -> JSONResponse:
        target = f"{body.symbol}:{body.segment}"
        if not state.confirm_tokens.verify("universe_add", target, body.token):
            raise HTTPException(403, "confirm token invalid or expired")
        reg = state.universe_registry
        assert reg is not None
        try:
            entry = reg.add(body.symbol, body.segment, actor="web")
        except UnknownSymbolError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({
            "ok": True, "symbol": entry.symbol, "segment": entry.segment,
        })

    # ---- preset ----

    @app.post("/api/universe/preset/prepare")
    def universe_preset_prepare(body: UniversePresetBody) -> JSONResponse:
        if body.preset not in KNOWN_PRESETS:
            raise HTTPException(400, f"unknown preset {body.preset!r}")
        token, expires_at = state.confirm_tokens.issue(
            "universe_preset", body.preset,
        )
        return JSONResponse({
            "token": token,
            "expires_at": expires_at,
            "preview": {"preset": body.preset},
        })

    @app.post("/api/universe/preset/apply")
    def universe_preset_apply(body: UniversePresetApplyBody) -> JSONResponse:
        if not state.confirm_tokens.verify(
            "universe_preset", body.preset, body.token,
        ):
            raise HTTPException(403, "confirm token invalid or expired")
        reg = state.universe_registry
        assert reg is not None
        try:
            summary = reg.apply_preset(body.preset, actor="web")
        except PresetNotImplementedError as exc:
            raise HTTPException(501, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return JSONResponse({"ok": True, "summary": summary})

    # ---------------- Signals (D11 Slice 3) ----------------

    @app.get("/signals", response_class=HTMLResponse)
    def page_signals(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "signals.html",
            {"banner": "PAPER TRADING // NOT FINANCIAL ADVICE"},
        )

    @app.get("/api/signals/recent")
    def api_signals_recent(
        limit: int = 100,
        min_score: int = 0,
        actions: str | None = None,
        trade_modes: str | None = None,
    ) -> JSONResponse:
        action_list = [a.strip() for a in actions.split(",")] if actions else None
        mode_list = [m.strip() for m in trade_modes.split(",")] if trade_modes else None
        rows = state.broker.store.load_recent_signals(
            limit=limit,
            min_score=min_score,
            actions=action_list,
            trade_modes=mode_list,
        )
        return JSONResponse({"count": len(rows), "signals": rows})

    @app.get("/api/signals/symbol/{symbol}")
    def api_signals_symbol(symbol: str, lookback_hours: int = 24) -> JSONResponse:
        rows = state.broker.store.load_signals_for_symbol(
            symbol, lookback_hours=lookback_hours,
        )
        return JSONResponse({"symbol": symbol, "count": len(rows), "signals": rows})

    @app.get("/partials/signals_table", response_class=HTMLResponse)
    def signals_table_partial(
        request: Request,
        limit: int = 100,
        min_score: int = 0,
        hide_skipped: bool = False,
        hide_watch_only: bool = False,
        segment: str | None = None,
    ) -> HTMLResponse:
        actions: list[str] | None = None
        if hide_skipped and hide_watch_only:
            actions = ["entered"]
        elif hide_skipped:
            actions = ["entered", "watch_only_logged"]
        elif hide_watch_only:
            actions = [
                "entered", "skipped_score", "skipped_filter",
                "skipped_risk", "skipped_position_cap",
            ]
        rows = state.broker.store.load_recent_signals(
            limit=limit, min_score=min_score, actions=actions,
        )
        # Segment filter (client-side via instruments master).
        if segment:
            inst_segments = {}
            for row in rows:
                sym = row["symbol"]
                if sym not in inst_segments:
                    inst = state.broker.instruments.get(sym)
                    inst_segments[sym] = inst.segment.value if inst else "EQ"
            rows = [r for r in rows if inst_segments.get(r["symbol"]) == segment]

        return templates.TemplateResponse(
            request, "partials/signals_table.html",
            {
                "rows": rows,
                "min_score": min_score,
                "hide_skipped": hide_skipped,
                "hide_watch_only": hide_watch_only,
                "segment": segment,
            },
        )

    # ---------------- Chart (D11 Slice 3) ----------------

    @app.get("/api/charts/{symbol}")
    def api_chart(symbol: str, interval: str | None = None, lookback: int = 100) -> JSONResponse:
        """OHLCV + indicator series for the per-symbol chart drawer.

        Uses the same ``src.strategy.indicators`` functions as the
        scoring engine — chart and scorer see identical numbers.
        Divergence here would be a silent bug magnet.
        """
        import pandas as pd

        iv = interval or state.settings.strategy.candle_interval
        # Ask the broker for enough candles to warm the longest
        # indicator (EMA 50 / MACD 26/9 / Supertrend 10). 120 is the
        # scoring engine's own lookback.
        try:
            candles = state.broker.get_candles(
                symbol, iv, lookback=max(lookback, 120),
            )
        except KeyError as exc:
            # FakeCandleFetcher-style "never seeded this symbol". Live
            # fetchers would return [] instead — handled right below.
            raise HTTPException(404, f"unknown symbol {symbol!r}") from exc
        if not candles:
            raise HTTPException(404, f"no candles available for {symbol!r}")

        df = pd.DataFrame(
            {
                "open": [c.open for c in candles],
                "high": [c.high for c in candles],
                "low": [c.low for c in candles],
                "close": [c.close for c in candles],
                "volume": [c.volume for c in candles],
            },
            index=pd.DatetimeIndex([c.ts for c in candles], name="ts"),
        )

        strat = state.settings.strategy
        series: dict[str, list] = {
            "ema_fast": _series_to_list(ind.ema(df["close"], strat.ema_fast)),
            "ema_mid":  _series_to_list(ind.ema(df["close"], strat.ema_mid)),
            "ema_slow": _series_to_list(ind.ema(df["close"], strat.ema_slow)),
            "ema_trend": _series_to_list(ind.ema(df["close"], strat.ema_trend)),
            "vwap": _series_to_list(ind.vwap(df)),
            "rsi": _series_to_list(ind.rsi(df["close"])),
            "atr": _series_to_list(ind.atr(df["high"], df["low"], df["close"])),
            "volume_sma_20": _series_to_list(ind.volume_sma(df["volume"], 20)),
        }
        macd = ind.macd(df["close"])
        series["macd"] = _series_to_list(macd["macd"])
        series["macd_signal"] = _series_to_list(macd["signal"])
        series["macd_hist"] = _series_to_list(macd["hist"])

        adx = ind.adx(df["high"], df["low"], df["close"])
        series["adx"] = _series_to_list(adx["adx"])

        bb = ind.bbands(df["close"], length=20, std=2.0)
        series["bb_upper"] = _series_to_list(bb["upper"])
        series["bb_middle"] = _series_to_list(bb["middle"])
        series["bb_lower"] = _series_to_list(bb["lower"])

        st = ind.supertrend(
            df["high"], df["low"], df["close"],
            length=strat.supertrend_period, multiplier=strat.supertrend_multiplier,
        )
        series["supertrend_line"] = _series_to_list(st["line"])
        series["supertrend_direction"] = _series_to_list(st["direction"])

        # Markers: timestamps where the scoring engine fired an
        # "entered" snapshot, and filled-order events (position open/close).
        signals = state.broker.store.load_signals_for_symbol(
            symbol, lookback_hours=24 * 30, limit=500,
        )
        strong_signal_ts = [
            s["ts"] for s in signals
            if s["score"] >= strat.min_score and s["action"] == "entered"
        ]
        watch_logged_ts = [
            s["ts"] for s in signals
            if s["score"] >= strat.min_score and s["action"] == "watch_only_logged"
        ]

        orders = state.broker.store.load_orders(status="FILLED")
        position_events = [
            {
                "ts": o.ts.isoformat(),
                "side": o.side.value,
                "price": o.avg_price,
                "qty": o.filled_qty,
            }
            for o in orders if o.symbol == symbol
        ]

        return JSONResponse(
            {
                "symbol": symbol,
                "interval": iv,
                "candles": [
                    {
                        "ts": c.ts.isoformat(),
                        "open": c.open, "high": c.high,
                        "low": c.low, "close": c.close,
                        "volume": c.volume,
                    }
                    for c in candles
                ],
                "indicators": series,
                "markers": {
                    "strong_signal_ts": strong_signal_ts,
                    "watch_logged_ts": watch_logged_ts,
                    "position_events": position_events,
                },
                "thresholds": {
                    "rsi_upper_block": strat.rsi_upper_block,
                    "rsi_entry_range": list(strat.rsi_entry_range),
                    "adx_min": strat.adx_min,
                    "volume_surge_multiplier": strat.volume_surge_multiplier,
                    "min_score": strat.min_score,
                },
            }
        )

    # ---------------- Health ----------------

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "mode": state.settings.mode,
                "broker": state.settings.broker,
                "kill_switch": state.broker.is_kill_switch_on(),
                "positions": len(state.broker.get_positions()),
            }
        )

    # ---------------- Upstox web re-auth ----------------
    from dashboard.auth_routes import register_routes as _register_auth_routes
    _register_auth_routes(app, state, templates)

    return app


# --------------------------------------------------------------------- #
# Partial-building helpers                                               #
# --------------------------------------------------------------------- #

def _mode_status_context(state: DashboardState) -> dict:
    """Shared context for the mode pill partial + the /api/mode endpoint."""
    mode = current_trade_mode(state.broker.store)
    return {
        "mode": mode,
        "live_acknowledged": live_trading_acknowledged(),
        "modes": list(VALID_TRADE_MODES),
    }


def _control_state_context(state: DashboardState) -> dict:
    """Shared context for the controls partial + /api/control/state.

    Derives a single ``status`` label (``RUNNING`` / ``PAUSED`` /
    ``STOPPED`` / ``KILLED``) that the UI pill renders directly, plus
    the raw flags so the endpoint JSON is useful to non-HTMX clients,
    plus the last 20 operator-audit rows for the drawer.
    """
    store = state.broker.store
    scheduler_state = store.get_flag("scheduler_state", "stopped")
    kill = store.get_flag("kill_switch", "armed")

    if kill == "tripped":
        status = "KILLED"
    elif scheduler_state == "running":
        status = "RUNNING"
    elif scheduler_state == "paused":
        status = "PAUSED"
    else:
        status = "STOPPED"

    # Which buttons to enable in the UI.
    can_pause = status == "RUNNING"
    can_resume = kill == "armed" and scheduler_state in ("stopped", "paused")
    can_kill = kill == "armed"
    can_rearm = kill == "tripped"

    return {
        "status": status,
        "scheduler_state": scheduler_state,
        "kill_switch": kill,
        "can_pause": can_pause,
        "can_resume": can_resume,
        "can_kill": can_kill,
        "can_rearm": can_rearm,
        "auto_resume_enabled": store.get_flag("auto_resume_enabled", "0") == "1",
        "audit": store.load_operator_audit(limit=20),
    }


def _kpis_context(state: DashboardState) -> dict:
    broker = state.broker
    settings = state.settings

    # Live-refresh LTP + mark-to-market so equity + P&L reflect the
    # last traded price, not the last scan-tick's stale close.
    try:
        broker.refresh_live_ltp()
    except Exception:
        pass

    funds = broker.get_funds()
    positions = broker.get_positions()

    equity_curve = broker.store.load_equity_curve()
    peak = peak_equity_from_curve(equity_curve)
    sod = start_of_day_equity(equity_curve, now_ist())
    starting = settings.capital.starting_inr

    day_pnl = funds["equity"] - sod if sod is not None else 0.0
    total_pnl = funds["equity"] - starting
    total_pnl_pct = (total_pnl / starting * 100.0) if starting else 0.0
    day_pnl_pct = (day_pnl / sod * 100.0) if sod else 0.0

    # Per-segment position counts.
    eq_open = 0
    fno_open = 0
    for p in positions:
        inst = broker.instruments.get(p.symbol)
        seg = inst.segment if inst else Segment.EQUITY
        if seg == Segment.EQUITY:
            eq_open += 1
        else:
            fno_open += 1

    return {
        "mode": settings.mode,
        "broker_name": settings.broker,
        "trade_mode": current_trade_mode(broker.store),
        "equity": funds["equity"],
        "cash": funds["available"],
        "used": funds["used"],
        "day_pnl": day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "starting": starting,
        "peak_equity": peak,
        "eq_open": eq_open,
        "eq_max": settings.risk.max_equity_positions,
        "fno_open": fno_open,
        "fno_max": settings.risk.max_fno_positions,
        "kill_switch": broker.is_kill_switch_on(),
        "now": now_ist(),
    }


def _series_to_list(series) -> list[float | None]:
    """Convert a pandas Series to a JSON-safe list (NaN → None)."""
    import math
    out: list[float | None] = []
    for v in series.tolist():
        try:
            fv = float(v)
            if math.isnan(fv) or math.isinf(fv):
                out.append(None)
            else:
                out.append(fv)
        except (TypeError, ValueError):
            out.append(None)
    return out


def _tail_log(path: Path | None, max_lines: int) -> list[str]:
    """Return up to ``max_lines`` last lines of a loguru log file."""
    if path is None or not path.exists():
        return []
    tail: deque[str] = deque(maxlen=max_lines)
    try:
        with path.open(errors="replace") as f:
            for line in f:
                tail.append(line.rstrip("\n"))
    except OSError:
        return []
    return list(tail)
