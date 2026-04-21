"""ATR-based stops, take-profits, trailing stops, and time stops."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from brokers.base import Position, Side
from config.settings import Settings
from risk.stops import (
    atr_stop_price,
    check_time_stop,
    minutes_since,
    take_profit_price,
    trailing_multiplier,
    update_trail_stop,
)

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 4, 21, 10, 0, tzinfo=IST)


@pytest.fixture
def risk_cfg():
    return Settings.from_template().risk


# ---------------------------------------------------------------- #
# atr_stop_price + take_profit_price                                #
# ---------------------------------------------------------------- #

def test_long_atr_stop_below_entry() -> None:
    assert atr_stop_price(100.0, atr=2.0, multiplier=1.0, side=Side.BUY) == 98.0


def test_short_atr_stop_above_entry() -> None:
    assert atr_stop_price(100.0, atr=2.0, multiplier=1.0, side=Side.SELL) == 102.0


def test_long_take_profit_above_entry() -> None:
    assert take_profit_price(100.0, atr=2.0, multiplier=3.0, side=Side.BUY) == 106.0


def test_short_take_profit_below_entry() -> None:
    assert take_profit_price(100.0, atr=2.0, multiplier=3.0, side=Side.SELL) == 94.0


def test_atr_stop_rejects_nonpositive_atr() -> None:
    with pytest.raises(ValueError, match="atr"):
        atr_stop_price(100.0, atr=0.0, multiplier=1.0, side=Side.BUY)


def test_atr_stop_rejects_nonpositive_multiplier() -> None:
    with pytest.raises(ValueError, match="multiplier"):
        atr_stop_price(100.0, atr=2.0, multiplier=0.0, side=Side.BUY)


# ---------------------------------------------------------------- #
# trailing_multiplier — regime selection                            #
# ---------------------------------------------------------------- #

def test_trailing_multiplier_high_vol(risk_cfg) -> None:
    # ATR trending up: current ATR (last) is the max → above median → high-vol.
    series = pd.Series(np.linspace(1.0, 5.0, 50))
    assert trailing_multiplier(series, risk_cfg) == risk_cfg.trailing_atr_multiplier_high_vol


def test_trailing_multiplier_low_vol(risk_cfg) -> None:
    # ATR trending down: current ATR below median → low-vol regime.
    series = pd.Series(np.linspace(5.0, 1.0, 50))
    assert trailing_multiplier(series, risk_cfg) == risk_cfg.trailing_atr_multiplier_low_vol


def test_trailing_multiplier_falls_back_to_low_vol_when_short(risk_cfg) -> None:
    """Fewer than 50 ATR readings → conservative choice = low-vol (wider) multiplier."""
    series = pd.Series([1.0] * 20)
    assert trailing_multiplier(series, risk_cfg) == risk_cfg.trailing_atr_multiplier_low_vol


# ---------------------------------------------------------------- #
# update_trail_stop — ratchet only                                  #
# ---------------------------------------------------------------- #

def test_long_trail_only_ratchets_up() -> None:
    pos = Position(symbol="RELIANCE", qty=10, avg_price=100.0, stop_loss=95.0)
    # Price rises to 110, ATR=2, multiplier=2 → candidate = 106.
    new = update_trail_stop(pos, current_price=110.0, atr=2.0, multiplier=2.0)
    assert new == 106.0
    # Simulate a pullback: price drops to 105 — existing trail of 106 wins.
    pos2 = Position(symbol="RELIANCE", qty=10, avg_price=100.0, trail_stop=106.0)
    new2 = update_trail_stop(pos2, current_price=105.0, atr=2.0, multiplier=2.0)
    assert new2 == 106.0


def test_short_trail_only_ratchets_down() -> None:
    pos = Position(symbol="NIFTY26APR", qty=-50, avg_price=100.0, stop_loss=105.0)
    new = update_trail_stop(pos, current_price=90.0, atr=2.0, multiplier=2.0)
    assert new == 94.0  # 90 + 2*2
    pos2 = Position(symbol="NIFTY26APR", qty=-50, avg_price=100.0, trail_stop=94.0)
    new2 = update_trail_stop(pos2, current_price=95.0, atr=2.0, multiplier=2.0)
    assert new2 == 94.0  # no loosening


def test_trail_uses_candidate_when_no_existing_stop() -> None:
    pos = Position(symbol="RELIANCE", qty=10, avg_price=100.0)
    new = update_trail_stop(pos, current_price=110.0, atr=2.0, multiplier=2.0)
    assert new == 106.0


# ---------------------------------------------------------------- #
# Time stop                                                         #
# ---------------------------------------------------------------- #

def test_time_stop_does_not_fire_before_window(risk_cfg) -> None:
    pos = Position(
        symbol="RELIANCE", qty=10, avg_price=100.0, opened_at=T0,
    )
    now = T0 + timedelta(minutes=30)  # well under 90m
    d = check_time_stop(pos, current_price=100.1, atr=2.0, now=now, cfg=risk_cfg)
    assert d.close_now is False
    assert d.reason is not None and "threshold" in d.reason


def test_time_stop_does_not_fire_when_price_moved(risk_cfg) -> None:
    pos = Position(
        symbol="RELIANCE", qty=10, avg_price=100.0, opened_at=T0,
    )
    now = T0 + timedelta(minutes=100)
    # Moved 2.0 points; deadband is 0.5 × ATR=2.0 = 1.0.
    d = check_time_stop(pos, current_price=102.0, atr=2.0, now=now, cfg=risk_cfg)
    assert d.close_now is False
    assert d.reason is not None and "deadband" in d.reason


def test_time_stop_fires_when_stale_and_flat(risk_cfg) -> None:
    pos = Position(
        symbol="RELIANCE", qty=10, avg_price=100.0, opened_at=T0,
    )
    now = T0 + timedelta(minutes=100)
    # Moved 0.3 points; deadband is 1.0 (= 0.5 × ATR=2.0). Dead.
    d = check_time_stop(pos, current_price=100.3, atr=2.0, now=now, cfg=risk_cfg)
    assert d.close_now is True
    assert d.reason is not None and "aged out" in d.reason


def test_time_stop_skips_position_without_opened_at(risk_cfg) -> None:
    pos = Position(symbol="RELIANCE", qty=10, avg_price=100.0, opened_at=None)
    d = check_time_stop(pos, current_price=100.0, atr=2.0, now=T0, cfg=risk_cfg)
    assert d.close_now is False
    assert d.reason is not None and "opened_at" in d.reason


def test_minutes_since_requires_tz_aware() -> None:
    naive = datetime(2026, 4, 21, 10, 0)  # no tz
    with pytest.raises(ValueError, match="tz-aware"):
        minutes_since(naive, T0)
