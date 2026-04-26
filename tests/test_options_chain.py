"""Tests for src/data/options_chain.py — pure logic (no live HTTP)."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

import brokers  # noqa: F401  — triggers package init to avoid circular import
from data.options_chain import (
    STRIKE_STEP,
    UNDERLYING_KEYS,
    get_atm_strike,
    get_monthly_expiry,
    refresh_options_master,
    resolve_atm_option,
)


# --------------------------------------------------------------------- #
# get_atm_strike                                                        #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("spot,expected", [
    (24523, 24500),     # rounds down — closer to 24500 than 24550
    (24574, 24550),     # rounds up
    (24525, 24500),     # exactly half — banker's rounding may go either way; we use Python round
    (25000, 25000),     # exact strike
    (22451, 22450),
])
def test_atm_strike_nifty(spot, expected):
    assert get_atm_strike(spot, "NIFTY") == expected


@pytest.mark.parametrize("spot,expected", [
    (50140, 50100),
    (50051, 50100),
    (51000, 51000),
    (52349, 52300),
])
def test_atm_strike_banknifty(spot, expected):
    assert get_atm_strike(spot, "BANKNIFTY") == expected


def test_atm_strike_unknown_raises():
    with pytest.raises(ValueError, match="unknown underlying"):
        get_atm_strike(100, "FINNIFTY")


def test_strike_step_constants():
    """Snapshot the step sizes — if SEBI changes them, this test fails
    loudly so we update the constants."""
    assert STRIKE_STEP["NIFTY"] == 50
    assert STRIKE_STEP["BANKNIFTY"] == 100
    assert set(UNDERLYING_KEYS) == {"NIFTY", "BANKNIFTY"}


# --------------------------------------------------------------------- #
# get_monthly_expiry                                                    #
# --------------------------------------------------------------------- #

def _stub_response(expiries: list[str]):
    """Build a fake httpx.Response with the given expiry list (ISO dates)."""
    import time
    payload = {
        "data": [
            {
                "expiry": int(time.mktime(
                    date.fromisoformat(e).timetuple()
                )) * 1000,
                "instrument_type": "CE",
                "strike_price": 24500,
                "lot_size": 65,
                "tick_size": 5,
                "trading_symbol": f"NIFTY{e}",
                "instrument_key": f"NSE_FO|{e}",
            } for e in expiries
        ]
    }

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload
    return _Resp()


def test_monthly_picks_current_month_when_dte_ok():
    """Today 2026-04-26, expiries [2026-04-28, 2026-05-26, 2026-06-30].
    Current-month is 2026-04-28 but only 2 days out → roll to 2026-05-26."""
    today = date(2026, 4, 26)
    with patch("data.options_chain.httpx.get", return_value=_stub_response(
        ["2026-04-28", "2026-05-26", "2026-06-30"]
    )), patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "x"}):
        result = get_monthly_expiry("NIFTY", today, min_days_to_expiry=7)
    assert result == date(2026, 5, 26)


def test_monthly_keeps_current_month_with_room():
    """Today 2026-04-10, current month last expiry 2026-04-28 (18 days out).
    Should use that, not roll forward."""
    today = date(2026, 4, 10)
    with patch("data.options_chain.httpx.get", return_value=_stub_response(
        ["2026-04-28", "2026-05-26"]
    )), patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "x"}):
        result = get_monthly_expiry("NIFTY", today, min_days_to_expiry=7)
    assert result == date(2026, 4, 28)


def test_monthly_picks_last_per_month_skipping_weeklies():
    """Mix of weekly + monthly — only the LAST per (year, month) counts."""
    today = date(2026, 4, 10)
    with patch("data.options_chain.httpx.get", return_value=_stub_response(
        ["2026-04-14", "2026-04-21", "2026-04-28",   # 3 weeklies in April
         "2026-05-05", "2026-05-26"]                  # 2 in May
    )), patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "x"}):
        result = get_monthly_expiry("NIFTY", today, min_days_to_expiry=7)
    # April monthly = 2026-04-28 (last in April), 18 days out → use that.
    assert result == date(2026, 4, 28)


def test_monthly_returns_none_on_fetch_failure():
    today = date(2026, 4, 26)
    with patch("data.options_chain.httpx.get", side_effect=RuntimeError("network")):
        result = get_monthly_expiry("NIFTY", today, access_token="x")
    assert result is None


def test_monthly_returns_none_without_token():
    """No token → fail-open with None (caller handles)."""
    import os
    saved = os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
    try:
        result = get_monthly_expiry("NIFTY", date(2026, 4, 26))
        assert result is None
    finally:
        if saved is not None:
            os.environ["UPSTOX_ACCESS_TOKEN"] = saved


def test_monthly_returns_none_for_unknown_underlying():
    result = get_monthly_expiry("FINNIFTY", date(2026, 4, 26), access_token="x")
    assert result is None


# --------------------------------------------------------------------- #
# resolve_atm_option (integrates with SQLite cache)                     #
# --------------------------------------------------------------------- #

def _seed_instruments_db(tmp_path: Path) -> Path:
    db = tmp_path / "scalper.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE instruments (
                symbol TEXT PRIMARY KEY, exchange TEXT, segment TEXT,
                tick_size REAL, lot_size INTEGER,
                expiry TEXT, strike REAL, option_type TEXT,
                name TEXT, isin TEXT, series TEXT, updated_at TEXT
            );
        """)
        # Seed two NIFTY contracts and one BANKNIFTY at the May 2026 monthly.
        rows = [
            ("NIFTY26MAY24500CE", "NSE", "OPT", 5, 65, "2026-05-26", 24500, "CE",
             "NIFTY 24500 CE", "", "OPT", "2026-04-26"),
            ("NIFTY26MAY24500PE", "NSE", "OPT", 5, 65, "2026-05-26", 24500, "PE",
             "NIFTY 24500 PE", "", "OPT", "2026-04-26"),
            ("BANKNIFTY26MAY50000CE", "NSE", "OPT", 5, 30, "2026-05-26", 50000, "CE",
             "BANKNIFTY 50000 CE", "", "OPT", "2026-04-26"),
        ]
        conn.executemany(
            "INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return db


def test_resolve_atm_option_happy_path(tmp_path):
    db = _seed_instruments_db(tmp_path)
    with patch("data.options_chain.get_monthly_expiry", return_value=date(2026, 5, 26)):
        out = resolve_atm_option(
            db, "NIFTY", "CE", spot=24523, today=date(2026, 4, 26),
            access_token="x",
        )
    assert out is not None
    assert out.strike == 24500
    assert out.lot_size == 65
    assert out.option_type == "CE"
    assert out.expiry == date(2026, 5, 26)


def test_resolve_atm_option_picks_pe_for_short_signal(tmp_path):
    db = _seed_instruments_db(tmp_path)
    with patch("data.options_chain.get_monthly_expiry", return_value=date(2026, 5, 26)):
        out = resolve_atm_option(
            db, "NIFTY", "PE", spot=24523, today=date(2026, 4, 26),
            access_token="x",
        )
    assert out is not None
    assert out.option_type == "PE"


def test_resolve_atm_option_returns_none_when_strike_missing(tmp_path):
    db = _seed_instruments_db(tmp_path)
    # Spot 30000 → ATM strike 30000, but DB only has 24500.
    with patch("data.options_chain.get_monthly_expiry", return_value=date(2026, 5, 26)):
        out = resolve_atm_option(
            db, "NIFTY", "CE", spot=30000, today=date(2026, 4, 26),
            access_token="x",
        )
    assert out is None


def test_resolve_atm_option_rejects_invalid_side(tmp_path):
    db = _seed_instruments_db(tmp_path)
    with pytest.raises(ValueError, match="CE or PE"):
        resolve_atm_option(
            db, "NIFTY", "BUY", spot=24523, today=date(2026, 4, 26),
            access_token="x",
        )


def test_resolve_atm_option_unknown_underlying(tmp_path):
    db = _seed_instruments_db(tmp_path)
    out = resolve_atm_option(
        db, "FINNIFTY", "CE", spot=24523, today=date(2026, 4, 26),
        access_token="x",
    )
    assert out is None


# --------------------------------------------------------------------- #
# refresh_options_master                                                #
# --------------------------------------------------------------------- #

def test_refresh_writes_rows_to_existing_table(tmp_path):
    db = _seed_instruments_db(tmp_path)
    fake_payload = {
        "data": [
            {
                "instrument_key": "NSE_FO|FAKE",
                "expiry": int(__import__("time").mktime(
                    date(2026, 6, 30).timetuple()
                )) * 1000,
                "instrument_type": "CE",
                "strike_price": 25000,
                "lot_size": 65,
                "tick_size": 5,
                "trading_symbol": "NIFTY26JUN25000CE",
            }
        ]
    }

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return fake_payload

    with patch("data.options_chain.httpx.get", return_value=_Resp()), \
         patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "x"}):
        n = refresh_options_master(db, underlyings=["NIFTY"])
    assert n == 1

    with sqlite3.connect(db) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM instruments WHERE segment='OPT' AND symbol='NIFTY26JUN25000CE'"
        ).fetchone()[0]
    assert cnt == 1


def test_refresh_fails_open_without_token(tmp_path):
    db = _seed_instruments_db(tmp_path)
    import os
    saved = os.environ.pop("UPSTOX_ACCESS_TOKEN", None)
    try:
        n = refresh_options_master(db)
        assert n == 0
    finally:
        if saved is not None:
            os.environ["UPSTOX_ACCESS_TOKEN"] = saved


def test_refresh_fails_open_on_network_error(tmp_path):
    db = _seed_instruments_db(tmp_path)
    with patch("data.options_chain.httpx.get", side_effect=RuntimeError("dns fail")), \
         patch.dict("os.environ", {"UPSTOX_ACCESS_TOKEN": "x"}):
        n = refresh_options_master(db)
    assert n == 0
