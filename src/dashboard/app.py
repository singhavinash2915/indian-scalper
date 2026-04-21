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


class ModePrepareBody(BaseModel):
    target: str = Field(..., description="watch_only | paper | live")


class ModeApplyBody(BaseModel):
    target: str
    token: str


class KillApplyBody(BaseModel):
    token: str


def create_app(
    broker: PaperBroker,
    settings: Settings,
    log_file: str | Path | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a live ``PaperBroker``."""
    app = FastAPI(title="Indian Scalper Dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    state = DashboardState(
        broker=broker,
        settings=settings,
        log_file=Path(log_file) if log_file else None,
        confirm_tokens=ConfirmTokenRegistry(),
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
        "audit": store.load_operator_audit(limit=20),
    }


def _kpis_context(state: DashboardState) -> dict:
    broker = state.broker
    settings = state.settings

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
