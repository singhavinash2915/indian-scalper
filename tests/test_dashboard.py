"""FastAPI dashboard — route-level tests via TestClient.

We use a real PaperBroker (with a FakeCandleFetcher + a seeded
InstrumentMaster) so the tests exercise the actual queries the UI
would render. Every test uses ``tmp_path`` for full isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from brokers.base import Candle, OrderType, Side
from brokers.paper import PaperBroker
from config.settings import Settings
from dashboard.app import create_app
from data.instruments import InstrumentMaster
from tests.fixtures import paper_mode
from data.market_data import FakeCandleFetcher

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


def _seeded_broker(tmp_path: Path) -> tuple[PaperBroker, Settings]:
    settings = paper_mode(Settings.from_template())
    instruments = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    instruments.load_equity_from_csv(
        Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    )
    fetcher = FakeCandleFetcher({"RELIANCE": [
        Candle(ts=T0, open=1000, high=1005, low=995, close=1000, volume=1000),
    ]})
    broker = PaperBroker(
        settings,
        db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    return broker, settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    broker, settings = _seeded_broker(tmp_path)
    app = create_app(broker, settings)
    c = TestClient(app)
    c.broker = broker  # type: ignore[attr-defined] — so tests can mutate state
    c.settings = settings  # type: ignore[attr-defined]
    return c


# ---------------------------------------------------------------- #
# Page shell                                                        #
# ---------------------------------------------------------------- #

def test_home_page_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "PAPER TRADING // NOT FINANCIAL ADVICE" in html
    # HTMX + Plotly are loaded from CDN.
    assert "htmx.org" in html
    assert "plotly" in html.lower()
    # Placeholder targets for partial-loading exist.
    assert 'id="kpis"' in html
    assert 'id="positions"' in html


def test_health_endpoint(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["mode"] == "paper"
    assert body["broker"] == "paper"
    assert body["kill_switch"] is False
    assert body["positions"] == 0


# ---------------------------------------------------------------- #
# KPI partial                                                       #
# ---------------------------------------------------------------- #

def test_kpis_partial_shows_starting_capital(client: TestClient) -> None:
    r = client.get("/partials/kpis")
    assert r.status_code == 200
    html = r.text
    # ₹500,000 comes from CONFIG_YAML_TEMPLATE.
    assert "500,000" in html
    assert "Equity" in html
    assert "Cash" in html
    assert "Kill Switch" in html


def test_kpis_partial_reflects_kill_switch(client: TestClient) -> None:
    client.broker.set_kill_switch(True)  # type: ignore[attr-defined]
    html = client.get("/partials/kpis").text
    assert "ON · HALTED" in html


# ---------------------------------------------------------------- #
# Positions partial                                                 #
# ---------------------------------------------------------------- #

def test_positions_partial_empty_state(client: TestClient) -> None:
    html = client.get("/partials/positions").text
    assert "No open positions" in html


def test_positions_partial_renders_open_position(client: TestClient) -> None:
    broker: PaperBroker = client.broker  # type: ignore[attr-defined]
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    broker.settle(
        "RELIANCE",
        Candle(ts=T0 + timedelta(minutes=15), open=1000, high=1001, low=999,
               close=1000.5, volume=2000),
    )
    broker.set_position_stops("RELIANCE", stop_loss=990.0, take_profit=1020.0)
    broker.mark_to_market({"RELIANCE": 1010.0})

    html = client.get("/partials/positions").text
    assert "RELIANCE" in html
    # Qty + avg_price + ltp + stop + TP all surface in the markup.
    assert "10" in html
    assert "₹1,000.50" in html or "₹1,000.5" in html
    assert "₹990.00" in html
    assert "₹1,020.00" in html


# ---------------------------------------------------------------- #
# Trades partial                                                    #
# ---------------------------------------------------------------- #

def test_trades_partial_empty_state(client: TestClient) -> None:
    html = client.get("/partials/trades").text
    assert "No closed trades" in html


def test_trades_partial_shows_closed_round_trip(client: TestClient) -> None:
    broker: PaperBroker = client.broker  # type: ignore[attr-defined]
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    broker.settle("RELIANCE",
                  Candle(T0, open=1000, high=1001, low=999, close=1000.5, volume=1000))
    broker.place_order("RELIANCE", 10, Side.SELL, OrderType.MARKET, ts=T0 + timedelta(minutes=30))
    broker.settle("RELIANCE",
                  Candle(T0 + timedelta(minutes=30), open=1050, high=1055, low=1045,
                         close=1052, volume=1500))

    html = client.get("/partials/trades").text
    assert "RELIANCE" in html
    # Positive P&L class highlighted.
    assert "class=\"good\"" in html


# ---------------------------------------------------------------- #
# Equity curve JSON                                                 #
# ---------------------------------------------------------------- #

def test_equity_json_empty_initial_state(client: TestClient) -> None:
    body = client.get("/api/equity.json").json()
    assert body["x"] == []
    assert body["y"] == []
    assert body["starting"] == client.settings.capital.starting_inr  # type: ignore[attr-defined]


def test_equity_json_reflects_snapshots(client: TestClient) -> None:
    broker: PaperBroker = client.broker  # type: ignore[attr-defined]
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET, ts=T0)
    broker.settle("RELIANCE",
                  Candle(T0, open=1000, high=1001, low=999, close=1000.5, volume=1000))
    body = client.get("/api/equity.json").json()
    assert len(body["x"]) >= 1
    assert len(body["y"]) == len(body["x"])
    assert all(v > 0 for v in body["y"])


# ---------------------------------------------------------------- #
# Kill-switch actions                                               #
# ---------------------------------------------------------------- #

def test_action_kill_sets_flag(client: TestClient) -> None:
    r = client.post("/actions/kill")
    assert r.status_code == 200
    assert r.json()["kill_switch"] is True
    assert client.broker.is_kill_switch_on() is True  # type: ignore[attr-defined]


def test_action_unkill_clears_flag(client: TestClient) -> None:
    client.broker.set_kill_switch(True)  # type: ignore[attr-defined]
    r = client.post("/actions/unkill")
    assert r.json()["kill_switch"] is False
    assert client.broker.is_kill_switch_on() is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------- #
# Trade-mode endpoints (D11 Slice 0)                                #
# ---------------------------------------------------------------- #

def test_api_mode_returns_current_mode(client: TestClient) -> None:
    # Test client fixture uses paper_mode() so start state is paper.
    r = client.get("/api/mode")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper"
    assert "watch_only" in body["modes"]
    assert "live" in body["modes"]


def test_mode_pill_partial_renders_active_button(client: TestClient) -> None:
    html = client.get("/partials/mode_pill").text
    assert "PAPER" in html
    assert 'data-target="watch_only"' in html
    assert 'data-target="paper"' in html
    assert 'data-target="live"' in html
    # The paper button is marked active; live is disabled without env ack.
    assert "mode-btn active mode-paper" in html
    assert "disabled" in html  # live is disabled without LIVE_TRADING_ACKNOWLEDGED


def test_mode_prepare_returns_token_and_context(client: TestClient) -> None:
    r = client.post("/api/mode/prepare", json={"target": "watch_only"})
    assert r.status_code == 200
    body = r.json()
    assert body["current_mode"] == "paper"
    assert body["target_mode"] == "watch_only"
    assert body["token"]
    assert body["expires_at"] > 0
    assert body["open_positions_count"] == 0
    assert body["requires_typed_confirm"] is False


def test_mode_prepare_rejects_invalid_target(client: TestClient) -> None:
    r = client.post("/api/mode/prepare", json={"target": "not_a_mode"})
    assert r.status_code == 400


def test_mode_prepare_refuses_live_without_env_ack(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("LIVE_TRADING_ACKNOWLEDGED", raising=False)
    r = client.post("/api/mode/prepare", json={"target": "live"})
    assert r.status_code == 400
    assert "LIVE_TRADING_ACKNOWLEDGED" in r.json()["detail"]


def test_mode_prepare_allows_live_with_env_ack(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TRADING_ACKNOWLEDGED", "yes")
    r = client.post("/api/mode/prepare", json={"target": "live"})
    assert r.status_code == 200
    body = r.json()
    assert body["requires_typed_confirm"] is True
    # Warnings flag the LIVE confirmation step.
    assert any("LIVE" in w for w in body["warnings"])


def test_mode_apply_flips_trade_mode(client: TestClient) -> None:
    prep = client.post("/api/mode/prepare", json={"target": "watch_only"}).json()
    r = client.post("/api/mode/apply", json={"target": "watch_only", "token": prep["token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["current_mode"] == "watch_only"
    assert body["previous_mode"] == "paper"
    # Persisted.
    assert client.broker.store.get_flag("trade_mode") == "watch_only"  # type: ignore[attr-defined]


def test_mode_apply_rejects_stale_token(client: TestClient) -> None:
    r = client.post("/api/mode/apply", json={"target": "paper", "token": "123.deadbeef"})
    assert r.status_code == 403


def test_mode_apply_rejects_token_from_different_target(client: TestClient) -> None:
    prep = client.post("/api/mode/prepare", json={"target": "watch_only"}).json()
    # Re-use the watch_only token to try to flip to paper — must fail.
    r = client.post("/api/mode/apply", json={"target": "paper", "token": prep["token"]})
    assert r.status_code == 403


def test_mode_apply_writes_audit_row(client: TestClient) -> None:
    prep = client.post("/api/mode/prepare", json={"target": "watch_only"}).json()
    client.post("/api/mode/apply", json={"target": "watch_only", "token": prep["token"]})
    audit = client.broker.store.load_operator_audit(limit=10)  # type: ignore[attr-defined]
    latest = next(r for r in audit if r["action"] == "flag_set:trade_mode")
    assert latest["actor"] == "web"
    assert latest["payload"]["value"] == "watch_only"
    assert latest["payload"]["previous"] == "paper"


# ---------------------------------------------------------------- #
# Control endpoints (D11 Slice 1)                                   #
# ---------------------------------------------------------------- #

def test_control_state_default_shape(client: TestClient) -> None:
    r = client.get("/api/control/state")
    assert r.status_code == 200
    body = r.json()
    # First-run defaults after broker init.
    assert body["scheduler_state"] == "stopped"
    assert body["kill_switch"] == "armed"
    assert body["status"] == "STOPPED"
    # Button-enable hints match the status.
    assert body["can_resume"] is True
    assert body["can_pause"] is False
    assert body["can_kill"] is True
    assert body["can_rearm"] is False
    assert "audit" in body


def test_control_state_reports_killed_status(client: TestClient) -> None:
    client.broker.set_kill_switch(True, actor="test")  # type: ignore[attr-defined]
    body = client.get("/api/control/state").json()
    assert body["status"] == "KILLED"
    assert body["kill_switch"] == "tripped"
    assert body["can_rearm"] is True
    assert body["can_kill"] is False  # already tripped


def test_controls_partial_renders_buttons(client: TestClient) -> None:
    html = client.get("/partials/controls").text
    assert "STOPPED" in html
    assert 'data-action="pause"' in html
    assert 'data-action="resume"' in html
    assert 'data-action="kill"' in html
    assert 'data-action="rearm"' in html


def test_audit_partial_shows_recent_rows(client: TestClient) -> None:
    # Seed an audit row via a flag write.
    client.broker.store.set_flag("trade_mode", "watch_only", actor="web")  # type: ignore[attr-defined]
    html = client.get("/partials/audit").text
    assert "flag_set:trade_mode" in html
    assert "web" in html


# ---- pause / resume ----

def test_pause_flips_scheduler_state(client: TestClient) -> None:
    client.broker.store.set_flag("scheduler_state", "running", actor="test")  # type: ignore[attr-defined]
    r = client.post("/api/control/pause")
    assert r.status_code == 200
    assert r.json()["scheduler_state"] == "paused"
    assert client.broker.store.get_flag("scheduler_state") == "paused"  # type: ignore[attr-defined]


def test_resume_flips_scheduler_state(client: TestClient) -> None:
    r = client.post("/api/control/resume")
    assert r.status_code == 200
    assert r.json()["scheduler_state"] == "running"


def test_resume_rejected_when_kill_tripped(client: TestClient) -> None:
    client.broker.set_kill_switch(True, actor="test")  # type: ignore[attr-defined]
    r = client.post("/api/control/resume")
    assert r.status_code == 409
    assert "kill" in r.json()["detail"].lower()


def test_pause_rejected_when_kill_tripped(client: TestClient) -> None:
    client.broker.set_kill_switch(True, actor="test")  # type: ignore[attr-defined]
    r = client.post("/api/control/pause")
    assert r.status_code == 409


# ---- kill flow ----

def test_kill_prepare_returns_token_and_warnings(client: TestClient) -> None:
    r = client.post("/api/control/kill/prepare")
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["expires_at"] > 0
    assert any("squared off" in w.lower() for w in body["warnings"])


def test_kill_apply_flips_kill_switch(client: TestClient) -> None:
    prep = client.post("/api/control/kill/prepare").json()
    r = client.post("/api/control/kill/apply", json={"token": prep["token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["kill_switch"] == "tripped"
    assert client.broker.is_kill_switch_on() is True  # type: ignore[attr-defined]


def test_kill_apply_rejects_bad_token(client: TestClient) -> None:
    r = client.post("/api/control/kill/apply", json={"token": "123.deadbeef"})
    assert r.status_code == 403


def test_kill_apply_rejects_mode_change_token(client: TestClient) -> None:
    """A confirm token minted for mode_change must not verify against
    the kill endpoint — (action, target) binding is what stops replay."""
    mode_prep = client.post("/api/mode/prepare", json={"target": "watch_only"}).json()
    r = client.post("/api/control/kill/apply", json={"token": mode_prep["token"]})
    assert r.status_code == 403


# ---- rearm ----

def test_rearm_clears_kill_but_does_not_resume(client: TestClient) -> None:
    """Per spec: rearm clears the flag, scheduler_state stays put —
    operator must press Resume consciously."""
    # Simulate a prior kill cycle.
    client.broker.set_kill_switch(True, actor="test")  # type: ignore[attr-defined]
    client.broker.store.set_flag("scheduler_state", "stopped", actor="test")  # type: ignore[attr-defined]

    r = client.post("/api/control/rearm")
    assert r.status_code == 200
    body = r.json()
    assert body["kill_switch"] == "armed"
    assert body["scheduler_state"] == "stopped"  # NOT auto-resumed
    assert client.broker.is_kill_switch_on() is False  # type: ignore[attr-defined]


def test_every_control_action_appends_operator_audit(client: TestClient) -> None:
    """Each kv flag write goes through set_flag, which already audits.
    Rearm (via broker.set_kill_switch) must also leave an audit trail."""
    client.post("/api/control/pause")
    client.post("/api/control/resume")

    prep = client.post("/api/control/kill/prepare").json()
    client.post("/api/control/kill/apply", json={"token": prep["token"]})
    client.post("/api/control/rearm")

    audit = client.broker.store.load_operator_audit(limit=50)  # type: ignore[attr-defined]
    actions = [r["action"] for r in audit]
    # Each state change wrote a flag_set:scheduler_state / flag_set:kill_switch row.
    assert any("flag_set:scheduler_state" in a for a in actions)
    assert any("flag_set:kill_switch" in a for a in actions)
    # The actor for UI actions is "web".
    web_rows = [r for r in audit if r["actor"] == "web"]
    assert len(web_rows) >= 3


# ---------------------------------------------------------------- #
# Log tail                                                          #
# ---------------------------------------------------------------- #

def test_logs_partial_no_file_configured(client: TestClient) -> None:
    html = client.get("/partials/logs").text
    assert "No log file configured" in html


def test_logs_partial_tails_file(tmp_path: Path) -> None:
    """Dashboard with a configured log file must surface its last lines."""
    broker, settings = _seeded_broker(tmp_path)
    log_file = tmp_path / "scalper.log"
    log_file.write_text("line-one\nline-two\nline-three\n")
    app = create_app(broker, settings, log_file=log_file)
    client = TestClient(app)

    html = client.get("/partials/logs").text
    assert "line-one" in html
    assert "line-three" in html
