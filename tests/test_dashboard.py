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
    """Kill-switch tile reflects the control_flags value verbatim —
    ``ARMED`` when safe, ``TRIPPED`` when the emergency halt is on."""
    client.broker.set_kill_switch(True)  # type: ignore[attr-defined]
    html = client.get("/partials/kpis").text
    assert "TRIPPED" in html
    # Safe-state label is NOT present when the switch is flipped on.
    assert "ARMED" not in html.split("Kill Switch")[1][:200]


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


def test_auto_resume_toggle_round_trip(client: TestClient) -> None:
    """The auto-resume toggle persists to control_flags and surfaces
    in /api/control/state."""
    # Default: off.
    state_before = client.get("/api/control/state").json()
    assert state_before["auto_resume_enabled"] is False

    r = client.post(
        "/api/control/auto_resume", json={"enabled": True},
    )
    assert r.status_code == 200
    assert r.json()["auto_resume_enabled"] is True

    state_after = client.get("/api/control/state").json()
    assert state_after["auto_resume_enabled"] is True


def test_auto_resume_toggle_refuses_when_kill_tripped(client: TestClient) -> None:
    """Enabling auto-resume with a tripped kill switch is a 409 — the
    operator must rearm first, explicitly."""
    client.broker.set_kill_switch(True, actor="test")  # type: ignore[attr-defined]
    r = client.post(
        "/api/control/auto_resume", json={"enabled": True},
    )
    assert r.status_code == 409
    # Disabling is always allowed — no guard for turning it off.
    r = client.post(
        "/api/control/auto_resume", json={"enabled": False},
    )
    assert r.status_code == 200


def test_controls_partial_renders_auto_resume_toggle(client: TestClient) -> None:
    html = client.get("/partials/controls").text
    assert 'id="auto-resume-toggle"' in html
    assert "auto-resume" in html


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
# Universe endpoints (D11 Slice 2)                                  #
# ---------------------------------------------------------------- #

def test_universe_page_renders(client: TestClient) -> None:
    r = client.get("/universe")
    assert r.status_code == 200
    html = r.text
    assert 'id="universe-host"' in html
    assert "+ Add symbol" in html
    # Nav bar shows Universe active.
    assert 'href="/universe"' in html


def test_api_universe_empty_initial_state(client: TestClient) -> None:
    r = client.get("/api/universe")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["entries"] == []
    # Presets are always listed.
    for p in ("none", "all", "nifty_50", "nifty_100"):
        assert p in body["presets"]


def test_api_universe_returns_seeded_entries(client: TestClient) -> None:
    # Seed directly through the registry the dashboard owns.
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE", "TCS"])
    body = client.get("/api/universe").json()
    assert body["count"] == 2
    symbols = {e["symbol"] for e in body["entries"]}
    assert symbols == {"RELIANCE", "TCS"}
    assert all(e["enabled"] for e in body["entries"])


def test_universe_table_partial_renders_rows(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])
    html = client.get("/partials/universe_table").text
    assert "RELIANCE" in html
    assert 'data-action="toggle-enabled"' in html
    assert 'data-action="toggle-watch"' in html


def test_universe_table_partial_search_filter(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE", "TCS", "INFY"])
    html = client.get("/partials/universe_table?q=tcs").text
    assert "TCS" in html
    assert "RELIANCE" not in html
    assert "INFY" not in html


# ---- toggle ----

def test_toggle_prepare_requires_existing_row(client: TestClient) -> None:
    r = client.post(
        "/api/universe/toggle/prepare", json={"symbol": "NOPE", "segment": "EQ"},
    )
    assert r.status_code == 404


def test_toggle_flow_flips_enabled(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])

    prep = client.post(
        "/api/universe/toggle/prepare", json={"symbol": "RELIANCE", "segment": "EQ"},
    ).json()
    assert prep["preview"]["current"] is True
    r = client.post(
        "/api/universe/toggle/apply",
        json={"symbol": "RELIANCE", "segment": "EQ", "token": prep["token"]},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert reg.is_enabled("RELIANCE") is False


def test_toggle_apply_rejects_stale_token(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])
    r = client.post(
        "/api/universe/toggle/apply",
        json={"symbol": "RELIANCE", "segment": "EQ", "token": "999.deadbeef"},
    )
    assert r.status_code == 403


def test_toggle_token_not_portable_across_symbols(client: TestClient) -> None:
    """(action, target) binding: token minted for RELIANCE must NOT
    verify against TCS."""
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE", "TCS"])
    prep = client.post(
        "/api/universe/toggle/prepare", json={"symbol": "RELIANCE", "segment": "EQ"},
    ).json()
    r = client.post(
        "/api/universe/toggle/apply",
        json={"symbol": "TCS", "segment": "EQ", "token": prep["token"]},
    )
    assert r.status_code == 403


# ---- watch-only override ----

def test_watch_only_override_flow(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])
    prep = client.post(
        "/api/universe/watch_only_override/prepare",
        json={"symbol": "RELIANCE", "segment": "EQ"},
    ).json()
    r = client.post(
        "/api/universe/watch_only_override/apply",
        json={"symbol": "RELIANCE", "segment": "EQ", "token": prep["token"]},
    )
    assert r.status_code == 200
    assert r.json()["watch_only_override"] is True
    assert reg.has_watch_only_override("RELIANCE")


# ---- add ----

def test_add_rejects_unknown_symbol(client: TestClient) -> None:
    r = client.post(
        "/api/universe/add/prepare",
        json={"symbol": "NOT_A_REAL_SYMBOL", "segment": "EQ"},
    )
    assert r.status_code == 400
    assert "instruments master" in r.json()["detail"].lower()


def test_add_rejects_existing_symbol(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])
    r = client.post(
        "/api/universe/add/prepare",
        json={"symbol": "RELIANCE", "segment": "EQ"},
    )
    assert r.status_code == 409


def test_add_flow_inserts_row(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    prep = client.post(
        "/api/universe/add/prepare", json={"symbol": "TCS", "segment": "EQ"},
    ).json()
    r = client.post(
        "/api/universe/add/apply",
        json={"symbol": "TCS", "segment": "EQ", "token": prep["token"]},
    )
    assert r.status_code == 200
    assert reg.is_enabled("TCS") is True


# ---- bulk ----

def test_bulk_flow_applies_operations(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE", "TCS", "INFY"])
    ops = [
        {"symbol": "RELIANCE", "enabled": False},
        {"symbol": "TCS", "watch_only_override": True},
    ]
    prep = client.post("/api/universe/bulk/prepare", json={"operations": ops}).json()
    r = client.post(
        "/api/universe/bulk/apply",
        json={"operations": ops, "token": prep["token"]},
    )
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["disabled"] == 1
    assert summary["watch_set"] == 1
    assert reg.is_enabled("RELIANCE") is False
    assert reg.has_watch_only_override("TCS") is True


# ---- preset ----

def test_preset_none_disables_all(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE", "TCS"])
    prep = client.post("/api/universe/preset/prepare", json={"preset": "none"}).json()
    r = client.post(
        "/api/universe/preset/apply",
        json={"preset": "none", "token": prep["token"]},
    )
    assert r.status_code == 200
    assert reg.enabled_symbols() == []


def test_named_index_preset_applies_via_api(client: TestClient) -> None:
    """Named-index presets are shipped with YAML symbol lists (per the
    Tuesday-dry-run tooling), so the API should accept them. Symbols
    not in the instruments master are counted in the response summary
    but don't abort the call."""
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])
    prep = client.post(
        "/api/universe/preset/prepare", json={"preset": "nifty_50"},
    ).json()
    r = client.post(
        "/api/universe/preset/apply",
        json={"preset": "nifty_50", "token": prep["token"]},
    )
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["preset"] == "nifty_50"
    assert summary["listed"] > 40  # Nifty 50 has 50 entries give or take


def test_preset_prepare_rejects_unknown(client: TestClient) -> None:
    r = client.post("/api/universe/preset/prepare", json={"preset": "bogus"})
    assert r.status_code == 400


# ---- audit trail ----

def test_universe_mutations_audit(client: TestClient) -> None:
    reg = client.app.state.dashboard.universe_registry  # type: ignore[attr-defined]
    reg.seed_if_empty(["RELIANCE"])

    prep = client.post(
        "/api/universe/toggle/prepare", json={"symbol": "RELIANCE", "segment": "EQ"},
    ).json()
    client.post(
        "/api/universe/toggle/apply",
        json={"symbol": "RELIANCE", "segment": "EQ", "token": prep["token"]},
    )
    audit = client.broker.store.load_operator_audit(limit=20)  # type: ignore[attr-defined]
    actions = [r["action"] for r in audit]
    assert "universe.toggle" in actions
    web_rows = [r for r in audit if r["actor"] == "web"]
    assert len(web_rows) >= 1


# ---------------------------------------------------------------- #
# Signals endpoints (D11 Slice 3)                                   #
# ---------------------------------------------------------------- #

def _seed_snapshot(store, **overrides):
    """Helper for the signals-endpoint tests."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    base = dict(
        ts=datetime(2026, 4, 21, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
        symbol="RELIANCE", score=6,
        breakdown={"ema_stack": True}, action="entered",
        reason="placed MARKET BUY", trace_id="abc123", trade_mode="paper",
    )
    base.update(overrides)
    store.append_signal_snapshot(**base)


def test_signals_page_renders(client: TestClient) -> None:
    r = client.get("/signals")
    assert r.status_code == 200
    html = r.text
    assert 'id="signals-host"' in html
    assert "Hide skipped" in html
    # Nav bar highlights Signals.
    assert 'href="/signals"' in html


def test_api_signals_recent_empty(client: TestClient) -> None:
    r = client.get("/api/signals/recent")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["signals"] == []


def test_api_signals_recent_returns_rows(client: TestClient) -> None:
    _seed_snapshot(client.broker.store)  # type: ignore[attr-defined]
    body = client.get("/api/signals/recent").json()
    assert body["count"] == 1
    assert body["signals"][0]["symbol"] == "RELIANCE"
    assert body["signals"][0]["breakdown"] == {"ema_stack": True}


def test_api_signals_recent_filter_by_min_score(client: TestClient) -> None:
    store = client.broker.store  # type: ignore[attr-defined]
    _seed_snapshot(store, symbol="LOW", score=2, action="skipped_score")
    _seed_snapshot(store, symbol="HIGH", score=7)
    body = client.get("/api/signals/recent?min_score=5").json()
    symbols = {s["symbol"] for s in body["signals"]}
    assert symbols == {"HIGH"}


def test_api_signals_recent_filter_by_actions(client: TestClient) -> None:
    store = client.broker.store  # type: ignore[attr-defined]
    _seed_snapshot(store, symbol="A", action="entered")
    _seed_snapshot(store, symbol="B", action="skipped_score")
    body = client.get("/api/signals/recent?actions=entered").json()
    assert {s["symbol"] for s in body["signals"]} == {"A"}


def test_api_signals_symbol_scoped(client: TestClient) -> None:
    store = client.broker.store  # type: ignore[attr-defined]
    _seed_snapshot(store, symbol="RELIANCE")
    _seed_snapshot(store, symbol="TCS")
    body = client.get("/api/signals/symbol/RELIANCE").json()
    assert body["symbol"] == "RELIANCE"
    assert all(s["symbol"] == "RELIANCE" for s in body["signals"])


def test_signals_table_partial_hide_skipped(client: TestClient) -> None:
    store = client.broker.store  # type: ignore[attr-defined]
    _seed_snapshot(store, symbol="KEPT", action="entered")
    _seed_snapshot(store, symbol="HIDDEN", action="skipped_score")
    html = client.get("/partials/signals_table?hide_skipped=true").text
    assert "KEPT" in html
    assert "HIDDEN" not in html


def test_signals_table_partial_hide_watch_only(client: TestClient) -> None:
    store = client.broker.store  # type: ignore[attr-defined]
    _seed_snapshot(store, symbol="KEPT", action="entered")
    _seed_snapshot(store, symbol="SHADOW", action="watch_only_logged")
    html = client.get("/partials/signals_table?hide_watch_only=true").text
    assert "KEPT" in html
    assert "SHADOW" not in html


def test_signals_table_empty_message(client: TestClient) -> None:
    html = client.get("/partials/signals_table").text
    assert "No signals match" in html


# ---------------------------------------------------------------- #
# Chart endpoint — indicator-parity with scoring engine             #
# ---------------------------------------------------------------- #

def test_chart_endpoint_returns_candles_and_indicators(
    client: TestClient, tmp_path: Path,
) -> None:
    """End-to-end: seed the broker's fetcher with known candles, hit
    /api/charts/{symbol}, verify the indicator arrays match what
    src.strategy.indicators produces on the same input. This is the
    PROMPT's "do NOT recompute differently here" guarantee."""
    import math

    import pandas as pd

    from data.market_data import df_to_candles
    from strategy import indicators as ind
    from tests.fixtures.synthetic import bullish_breakout_df

    candles = df_to_candles(bullish_breakout_df())
    # Swap in our candles for RELIANCE (fetcher is seeded with short
    # single-bar series by default).
    client.broker.fetcher._series["RELIANCE"] = candles  # type: ignore[attr-defined]

    r = client.get("/api/charts/RELIANCE")
    assert r.status_code == 200
    body = r.json()

    assert body["symbol"] == "RELIANCE"
    assert body["candles"], "expected candles in response"
    # Indicator series keys match what the UI needs.
    for key in (
        "ema_fast", "ema_mid", "ema_slow", "ema_trend",
        "vwap", "macd", "macd_signal", "macd_hist",
        "rsi", "adx", "bb_upper", "bb_middle", "bb_lower",
        "supertrend_line", "supertrend_direction", "atr",
        "volume_sma_20",
    ):
        assert key in body["indicators"], f"missing {key!r}"

    # Parity: recompute RSI + MACD directly and assert last-bar match.
    df = pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low":  [c.low  for c in candles],
            "close":[c.close for c in candles],
            "volume":[c.volume for c in candles],
        },
        index=pd.DatetimeIndex([c.ts for c in candles]),
    )
    expected_rsi = float(ind.rsi(df["close"]).iloc[-1])
    returned_rsi = body["indicators"]["rsi"][-1]
    assert returned_rsi is not None
    assert math.isclose(returned_rsi, expected_rsi, rel_tol=1e-9, abs_tol=1e-9)

    expected_macd_hist = float(ind.macd(df["close"])["hist"].iloc[-1])
    returned_macd_hist = body["indicators"]["macd_hist"][-1]
    assert returned_macd_hist is not None
    assert math.isclose(returned_macd_hist, expected_macd_hist,
                        rel_tol=1e-9, abs_tol=1e-9)


def test_chart_endpoint_404_on_unknown_symbol(client: TestClient) -> None:
    # FakeCandleFetcher raises KeyError on unseeded symbols; the app
    # catches that and surfaces a 404 so the UI can show a friendly
    # error instead of a 500 stack trace.
    r = client.get("/api/charts/NOT_A_SYMBOL")
    assert r.status_code == 404
    assert "NOT_A_SYMBOL" in r.json()["detail"]


def test_chart_thresholds_carry_config_values(client: TestClient) -> None:
    from data.market_data import df_to_candles
    from tests.fixtures.synthetic import bullish_breakout_df

    client.broker.fetcher._series["RELIANCE"] = df_to_candles(bullish_breakout_df())  # type: ignore[attr-defined]
    body = client.get("/api/charts/RELIANCE").json()
    th = body["thresholds"]
    assert th["rsi_upper_block"] == 78.0
    assert th["rsi_entry_range"] == [55.0, 75.0]
    assert th["adx_min"] == 22.0
    assert th["volume_surge_multiplier"] == 2.0
    assert th["min_score"] == 6  # template default; dashboard fixture doesn't lower it


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
