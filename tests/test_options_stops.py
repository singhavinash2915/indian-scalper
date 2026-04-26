"""Tests for the 5-stop ladder + breakeven trail."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import brokers  # noqa: F401  — triggers package init
from config.settings import StrategyCfg
from risk.options_stops import (
    check_options_exit,
    update_high_water_and_breakeven,
)

IST = ZoneInfo("Asia/Kolkata")


def _cfg() -> StrategyCfg:
    """Default options-enabled config."""
    return StrategyCfg(
        candle_interval="15m", scan_interval_seconds=300, min_score=6,
        rsi_upper_block=78, rsi_entry_range=(55, 75), adx_min=22,
        volume_surge_multiplier=2.0, ema_fast=5, ema_mid=13, ema_slow=34,
        ema_trend=50, supertrend_period=10, supertrend_multiplier=3.0,
        options_enabled=True,
        options_premium_stop_pct=35.0,
        options_premium_breakeven_pct=35.0,
        options_trailing_premium_pct=30.0,
        options_underlying_stop_pts_nifty=50.0,
        options_underlying_stop_pts_banknifty=125.0,
        options_time_stop_minutes=45,
        options_eod_squareoff="15:05",
    )


def _pos(**overrides) -> dict:
    base = {
        "contract_key": "NIFTY26MAY24500CE",
        "underlying": "NIFTY",
        "option_type": "CE",
        "strike": 24500.0,
        "expiry": "2026-05-26",
        "lot_size": 65,
        "qty_lots": 1,
        "entry_premium": 400.0,
        "entry_spot": 24500.0,
        "high_water_premium": 400.0,
        "breakeven_locked": 0,
        "opened_at": datetime(2026, 4, 27, 9, 30, tzinfo=IST).isoformat(),
        "last_premium": 400.0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- #
# Phase 1 — initial 35% premium SL                                      #
# --------------------------------------------------------------------- #

def test_phase1_premium_sl_fires_at_minus_35pct():
    pos = _pos()
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    # Entry 400 → SL at 260 (–35%)
    decision = check_options_exit(pos, current_premium=259, current_spot=24500, now=now, cfg=_cfg())
    assert decision is not None
    assert decision.reason == "premium_stop"


def test_phase1_premium_holds_at_minus_30pct():
    pos = _pos()
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    decision = check_options_exit(pos, current_premium=280, current_spot=24500, now=now, cfg=_cfg())
    assert decision is None


# --------------------------------------------------------------------- #
# Phase 2 — breakeven lock at 1:1                                        #
# --------------------------------------------------------------------- #

def test_breakeven_flips_when_high_water_crosses_one_to_one():
    pos = _pos()
    cfg = _cfg()
    # Premium hits 540 (+35%) → breakeven_locked
    new_high, be = update_high_water_and_breakeven(pos, 541, cfg)
    assert new_high == 541
    assert be is True


def test_breakeven_never_flips_back():
    pos = _pos(high_water_premium=600, breakeven_locked=1)
    cfg = _cfg()
    # Premium drops back to 500 — breakeven_locked stays True (one-way ratchet)
    new_high, be = update_high_water_and_breakeven(pos, 500, cfg)
    assert new_high == 600   # high water doesn't fall
    assert be is True


def test_breakeven_locked_sl_equals_entry_when_no_higher_high():
    pos = _pos(high_water_premium=540, breakeven_locked=1)   # at 1:1
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    cfg = _cfg()
    # 540 × 0.7 = 378 → max(entry=400, 378) = 400 = breakeven floor
    # Premium at 401 → safe
    decision = check_options_exit(pos, current_premium=401, current_spot=24500, now=now, cfg=cfg)
    assert decision is None
    # Premium at 399 → triggers trail_stop at the breakeven floor
    decision = check_options_exit(pos, current_premium=399, current_spot=24500, now=now, cfg=cfg)
    assert decision is not None
    assert decision.reason == "trail_stop"


def test_phase3_trail_ratchets_up_with_high_water():
    pos = _pos(high_water_premium=800, breakeven_locked=1)   # high at +100%
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    cfg = _cfg()
    # 800 × 0.7 = 560 → trail SL = max(entry=400, 560) = 560
    # Premium at 600 → safe
    decision = check_options_exit(pos, current_premium=600, current_spot=24500, now=now, cfg=cfg)
    assert decision is None
    # Premium at 555 → trail fires
    decision = check_options_exit(pos, current_premium=555, current_spot=24500, now=now, cfg=cfg)
    assert decision is not None
    assert decision.reason == "trail_stop"


# --------------------------------------------------------------------- #
# Underlying point cap                                                   #
# --------------------------------------------------------------------- #

def test_nifty_call_exits_when_spot_drops_50_pts():
    pos = _pos()   # CE entry at 24500
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    decision = check_options_exit(pos, current_premium=350, current_spot=24449, now=now, cfg=_cfg())
    assert decision is not None
    assert decision.reason == "underlying_pts"


def test_nifty_call_holds_at_minus_30_pts():
    pos = _pos()
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    decision = check_options_exit(pos, current_premium=370, current_spot=24470, now=now, cfg=_cfg())
    assert decision is None


def test_nifty_put_exits_when_spot_rises_50_pts():
    pos = _pos(option_type="PE", contract_key="NIFTY26MAY24500PE")
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    decision = check_options_exit(pos, current_premium=350, current_spot=24551, now=now, cfg=_cfg())
    assert decision is not None
    assert decision.reason == "underlying_pts"


def test_banknifty_uses_125_pt_cap():
    pos = _pos(
        underlying="BANKNIFTY", contract_key="BANKNIFTY26MAY50000CE",
        strike=50000, entry_spot=50000,
    )
    now = datetime(2026, 4, 27, 10, 0, tzinfo=IST)
    # –124 pts: holds
    d = check_options_exit(pos, current_premium=350, current_spot=49876, now=now, cfg=_cfg())
    assert d is None
    # –126 pts: exits
    d = check_options_exit(pos, current_premium=350, current_spot=49874, now=now, cfg=_cfg())
    assert d is not None
    assert d.reason == "underlying_pts"


# --------------------------------------------------------------------- #
# EOD square-off                                                        #
# --------------------------------------------------------------------- #

def test_eod_squareoff_at_15_05():
    pos = _pos()
    now = datetime(2026, 4, 27, 15, 5, tzinfo=IST)
    decision = check_options_exit(pos, current_premium=380, current_spot=24500, now=now, cfg=_cfg())
    assert decision is not None
    assert decision.reason == "eod_squareoff"


def test_no_eod_before_15_05():
    pos = _pos()
    now = datetime(2026, 4, 27, 15, 4, tzinfo=IST)
    decision = check_options_exit(pos, current_premium=380, current_spot=24500, now=now, cfg=_cfg())
    assert decision is None


# --------------------------------------------------------------------- #
# Time stop                                                             #
# --------------------------------------------------------------------- #

def test_time_stop_fires_after_45min_with_stalled_underlying():
    opened = datetime(2026, 4, 27, 9, 30, tzinfo=IST)
    pos = _pos(opened_at=opened.isoformat())
    now = opened + timedelta(minutes=46)
    # Underlying ATR=20, move=8 (< 0.5×ATR=10) → stalled → exit
    decision = check_options_exit(pos, current_premium=380, current_spot=24508,
                                  now=now, cfg=_cfg(), underlying_atr=20)
    assert decision is not None
    assert decision.reason == "time_stop"


def test_time_stop_holds_when_underlying_moved_significantly():
    opened = datetime(2026, 4, 27, 9, 30, tzinfo=IST)
    pos = _pos(opened_at=opened.isoformat())
    now = opened + timedelta(minutes=46)
    # Move > 0.5×ATR → not stalled → no time stop
    decision = check_options_exit(pos, current_premium=380, current_spot=24515,
                                  now=now, cfg=_cfg(), underlying_atr=20)
    assert decision is None


def test_time_stop_disabled_when_atr_zero():
    """Caller may pass atr=0 to disable the stalled gate (tests, fallback)."""
    opened = datetime(2026, 4, 27, 9, 30, tzinfo=IST)
    pos = _pos(opened_at=opened.isoformat())
    now = opened + timedelta(minutes=120)
    decision = check_options_exit(pos, current_premium=380, current_spot=24500,
                                  now=now, cfg=_cfg(), underlying_atr=0)
    assert decision is None
