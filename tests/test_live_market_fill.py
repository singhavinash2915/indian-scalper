"""Tests for the live_market fill mode on PaperBroker.

Verifies that when ``paper.fill_on: live_market`` is active, MARKET orders
are filled IMMEDIATELY at the live LTP + slippage (no waiting for
next_candle_open). Back-compat case (``next_candle_open``) still works
via the existing settle_on_candle path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brokers.base import OrderType, Side
from brokers.paper import PaperBroker
from config.settings import CONFIG_YAML_TEMPLATE, Settings
from data.instruments import InstrumentMaster


class _LtpStubFetcher:
    """Stand-in fetcher with get_ltp — mimics UpstoxFetcher for tests."""

    def __init__(self, prices: dict[str, float]):
        self._prices = dict(prices)
        self.calls: list[list[str]] = []

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        self.calls.append(list(symbols))
        return {s: self._prices[s] for s in symbols if s in self._prices}

    def get_candles(self, *a, **kw):  # not exercised
        return []


class _NoLtpFetcher:
    """Stand-in for a fetcher that doesn't support get_ltp (e.g. backtest)."""

    def get_candles(self, *a, **kw):
        return []


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "config.yaml"
    # Override trade_mode=paper so entries aren't blocked by the watch_only default.
    text = CONFIG_YAML_TEMPLATE.replace(
        "initial_trade_mode: watch_only", "initial_trade_mode: paper"
    )
    cfg.write_text(text)
    return Settings.load(cfg)


@pytest.fixture
def instruments(tmp_path: Path) -> InstrumentMaster:
    return InstrumentMaster(db_path=tmp_path / "t.db", cache_dir=tmp_path / "cache")


# --------------------------------------------------------------------- #
# live_market happy path                                                #
# --------------------------------------------------------------------- #

def test_live_market_fills_buy_immediately(tmp_path, settings, instruments):
    fetcher = _LtpStubFetcher({"RELIANCE": 1350.0})
    broker = PaperBroker(
        settings=settings,
        db_path=tmp_path / "broker.db",
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    assert broker.fill_mode == "live_market"

    order = broker.place_order(
        "RELIANCE", qty=10, side=Side.BUY, order_type=OrderType.MARKET,
    )
    # Should be FILLED immediately — no waiting for settle().
    assert order.status == "FILLED"
    assert order.filled_qty == 10
    # BUY slippage adds to price: 1350 × (1 + 0.0005) = 1350.675
    assert order.avg_price == pytest.approx(1350.675, rel=1e-6)
    # Position actually opened
    positions = broker.get_positions()
    assert {p.symbol for p in positions} == {"RELIANCE"}


def test_live_market_fills_sell_with_negative_slippage(tmp_path, settings, instruments):
    fetcher = _LtpStubFetcher({"RELIANCE": 1400.0})
    broker = PaperBroker(
        settings=settings,
        db_path=tmp_path / "broker.db",
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    # Open a long position first (via live fill).
    broker.place_order("RELIANCE", qty=10, side=Side.BUY, order_type=OrderType.MARKET)
    # Now close.
    fetcher._prices["RELIANCE"] = 1420.0
    exit_order = broker.place_order(
        "RELIANCE", qty=10, side=Side.SELL, order_type=OrderType.MARKET,
    )
    assert exit_order.status == "FILLED"
    # SELL slippage subtracts: 1420 × (1 - 0.0005) = 1419.29
    assert exit_order.avg_price == pytest.approx(1419.29, rel=1e-6)


# --------------------------------------------------------------------- #
# Fallbacks                                                             #
# --------------------------------------------------------------------- #

def test_backtest_fetcher_without_get_ltp_stays_pending(
    tmp_path, settings, instruments,
):
    """Backtest fetcher has no get_ltp — order stays PENDING so the existing
    settle_on_candle path handles the fill on the next simulated tick.
    Keeps replay + backtest determinism intact."""
    fetcher = _NoLtpFetcher()
    broker = PaperBroker(
        settings=settings,
        db_path=tmp_path / "broker.db",
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    broker._ltp["RELIANCE"] = 2500.0   # cache present but irrelevant

    order = broker.place_order(
        "RELIANCE", qty=5, side=Side.BUY, order_type=OrderType.MARKET,
    )
    assert order.status == "PENDING"   # legacy path preserved


def test_live_market_leaves_order_pending_when_no_ltp_available(
    tmp_path, settings, instruments,
):
    """Neither get_ltp nor cache has the symbol — order stays PENDING so the
    next settle() cycle can still fill it."""
    fetcher = _NoLtpFetcher()
    broker = PaperBroker(
        settings=settings,
        db_path=tmp_path / "broker.db",
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    order = broker.place_order(
        "UNKNOWN", qty=1, side=Side.BUY, order_type=OrderType.MARKET,
    )
    assert order.status == "PENDING"
    assert order.filled_qty == 0


def test_next_candle_open_mode_preserves_legacy_behavior(tmp_path, instruments):
    """Setting fill_on: next_candle_open must queue the order (not live-fill)."""
    cfg = tmp_path / "config.yaml"
    text = CONFIG_YAML_TEMPLATE.replace(
        "fill_on: live_market", "fill_on: next_candle_open",
    ).replace("initial_trade_mode: watch_only", "initial_trade_mode: paper")
    cfg.write_text(text)
    settings = Settings.load(cfg)

    fetcher = _LtpStubFetcher({"RELIANCE": 1350.0})   # has LTP, should still be ignored
    broker = PaperBroker(
        settings=settings,
        db_path=tmp_path / "broker.db",
        candle_fetcher=fetcher,
        instruments=instruments,
    )
    assert broker.fill_mode == "next_candle_open"

    order = broker.place_order(
        "RELIANCE", qty=10, side=Side.BUY, order_type=OrderType.MARKET,
    )
    assert order.status == "PENDING"   # must NOT fill immediately
    assert fetcher.calls == []          # live LTP never called
