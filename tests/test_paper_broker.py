"""PaperBroker — end-to-end lifecycle, recovery, and the BrokerBase contract."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brokers.base import BrokerBase, Candle, OrderType, Segment, Side
from brokers.paper import PaperBroker
from config.settings import Settings
from data.instruments import InstrumentMaster
from data.market_data import FakeCandleFetcher, build_synthetic_candles
from tests.fixtures import paper_mode

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 9, 30, tzinfo=IST)


@pytest.fixture
def settings() -> Settings:
    return paper_mode(Settings.from_template())


@pytest.fixture
def fake_fetcher() -> FakeCandleFetcher:
    candles = build_synthetic_candles(
        start=T0, interval_minutes=15,
        closes=[1000.0, 1001.0, 1002.0, 1003.0, 1004.0],
    )
    return FakeCandleFetcher({"RELIANCE": candles, "TCS": candles})


@pytest.fixture
def instruments(tmp_path: Path) -> InstrumentMaster:
    m = InstrumentMaster(
        db_path=tmp_path / "instruments.db",
        cache_dir=tmp_path / "instruments_cache",
    )
    fixture_csv = Path(__file__).parent / "fixtures" / "sample_equity_master.csv"
    m.load_equity_from_csv(fixture_csv)
    return m


@pytest.fixture
def broker(
    tmp_path: Path,
    settings: Settings,
    fake_fetcher: FakeCandleFetcher,
    instruments: InstrumentMaster,
) -> PaperBroker:
    return PaperBroker(
        settings,
        db_path=str(tmp_path / "scalper.db"),
        candle_fetcher=fake_fetcher,
        instruments=instruments,
    )


# ---------------------------------------------------------------- #
# Construction + BrokerBase contract                                #
# ---------------------------------------------------------------- #

def test_is_broker_base(broker: PaperBroker) -> None:
    assert isinstance(broker, BrokerBase)


def test_initial_funds(broker: PaperBroker, settings: Settings) -> None:
    funds = broker.get_funds()
    assert funds["available"] == settings.capital.starting_inr
    assert funds["used"] == 0.0
    assert funds["equity"] == settings.capital.starting_inr


def test_get_instruments_delegates_to_master(broker: PaperBroker) -> None:
    instruments = broker.get_instruments()
    symbols = {i.symbol for i in instruments}
    assert "RELIANCE" in symbols
    assert all(i.segment == Segment.EQUITY for i in instruments)


# ---------------------------------------------------------------- #
# Order lifecycle                                                   #
# ---------------------------------------------------------------- #

def test_place_order_is_pending_until_settled(broker: PaperBroker) -> None:
    order = broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    assert order.status == "PENDING"
    assert broker.get_positions() == []
    assert broker.orders[order.id].status == "PENDING"


def test_settle_fills_market_order_and_updates_position(broker: PaperBroker) -> None:
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    candle = Candle(
        ts=T0 + timedelta(minutes=15),
        open=1000.0, high=1005.0, low=995.0, close=1002.0, volume=5000,
    )
    filled = broker.settle("RELIANCE", candle)

    assert len(filled) == 1
    assert filled[0].status == "FILLED"
    assert filled[0].avg_price == pytest.approx(1000.0 * 1.0005)

    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "RELIANCE"
    assert positions[0].qty == 10
    # LTP is set to candle.close after settle.
    assert positions[0].ltp == 1002.0


def test_settle_writes_equity_curve_row(broker: PaperBroker) -> None:
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    candle = Candle(
        ts=T0 + timedelta(minutes=15),
        open=1000.0, high=1005.0, low=995.0, close=1002.0, volume=5000,
    )
    broker.settle("RELIANCE", candle)

    curve = broker.store.load_equity_curve()
    assert len(curve) == 1
    assert curve[0]["cash"] == pytest.approx(
        broker.settings.capital.starting_inr - (1000.0 * 1.0005) * 10
    )


def test_cancel_pending_order(broker: PaperBroker) -> None:
    order = broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    assert broker.cancel_order(order.id) is True
    assert order.id not in broker.orders


def test_modify_pending_limit(broker: PaperBroker) -> None:
    order = broker.place_order(
        "RELIANCE", 10, Side.BUY, OrderType.LIMIT, price=990.0,
    )
    new = broker.modify_order(order.id, price=985.0)
    assert new.price == 985.0


# ---------------------------------------------------------------- #
# get_ltp                                                           #
# ---------------------------------------------------------------- #

def test_get_ltp_uses_cache_after_settle(broker: PaperBroker) -> None:
    broker.settle(
        "RELIANCE",
        Candle(T0, open=1000, high=1001, low=999, close=1000.5, volume=1000),
    )
    ltps = broker.get_ltp(["RELIANCE"])
    assert ltps["RELIANCE"] == 1000.5


def test_get_ltp_cold_hits_fetcher(broker: PaperBroker) -> None:
    """No prior settle — LTP must be derived from the fetcher's last candle."""
    ltps = broker.get_ltp(["RELIANCE"])
    assert ltps["RELIANCE"] == 1004.0  # last close from the seeded series


# ---------------------------------------------------------------- #
# Kill switch                                                       #
# ---------------------------------------------------------------- #

def test_kill_switch_persists(broker: PaperBroker) -> None:
    assert broker.is_kill_switch_on() is False
    broker.set_kill_switch(True)
    assert broker.is_kill_switch_on() is True
    broker.set_kill_switch(False)
    assert broker.is_kill_switch_on() is False


# ---------------------------------------------------------------- #
# Recovery — the critical invariant                                 #
# ---------------------------------------------------------------- #

def test_restart_recovers_pending_orders_and_positions(
    tmp_path: Path, settings: Settings, fake_fetcher: FakeCandleFetcher,
    instruments: InstrumentMaster,
) -> None:
    db_path = str(tmp_path / "scalper.db")

    # Session 1.
    b1 = PaperBroker(
        settings, db_path=db_path, candle_fetcher=fake_fetcher, instruments=instruments,
    )
    b1.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    b1.settle("RELIANCE", Candle(T0, 1000, 1001, 999, 1000.5, 1000))
    pending = b1.place_order("TCS", 5, Side.BUY, OrderType.LIMIT, price=3000.0)
    cash_after_s1 = b1.cash

    # Session 2 — brand-new broker, same db_path.
    b2 = PaperBroker(
        settings, db_path=db_path, candle_fetcher=fake_fetcher, instruments=instruments,
    )
    assert b2.cash == pytest.approx(cash_after_s1)
    # Pending TCS order came back.
    assert pending.id in b2.orders
    # RELIANCE long position came back.
    reliance = next((p for p in b2.get_positions() if p.symbol == "RELIANCE"), None)
    assert reliance is not None
    assert reliance.qty == 10


# ---------------------------------------------------------------- #
# Audit trail                                                       #
# ---------------------------------------------------------------- #

def test_audit_log_records_every_state_change(broker: PaperBroker) -> None:
    order = broker.place_order("RELIANCE", 10, Side.BUY, OrderType.MARKET)
    broker.settle("RELIANCE", Candle(T0, 1000, 1001, 999, 1000.5, 1000))
    broker.place_order("RELIANCE", 10, Side.BUY, OrderType.LIMIT, price=995.0)

    log = broker.store.load_audit()
    actions = [row["action"] for row in log]
    assert "order_submitted" in actions
    assert "order_filled" in actions
    # The first submit corresponds to our first order.
    first_submit = next(r for r in log if r["action"] == "order_submitted")
    assert first_submit["order_id"] == order.id
