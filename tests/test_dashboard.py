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
from data.market_data import FakeCandleFetcher

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


def _seeded_broker(tmp_path: Path) -> tuple[PaperBroker, Settings]:
    settings = Settings.from_template()
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
