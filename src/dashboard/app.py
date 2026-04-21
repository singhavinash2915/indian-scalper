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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from backtest.trades import extract_trades
from brokers.base import Segment
from brokers.paper import PaperBroker
from config.settings import Settings
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
