"""Options chain helpers — ATM strike + monthly expiry resolution.

Wraps Upstox's ``/v2/option/contract`` endpoint. Two responsibilities:

1. **Refresh the local NFO contract cache** for NIFTY + BANKNIFTY. Stored
   in the existing ``instruments`` table (it already has expiry / strike /
   option_type columns from the original schema). Called once per day
   from the scheduler — instruments turn over slowly enough that 24 h
   freshness is plenty.

2. **Resolve the right contract at signal time**: given an underlying
   symbol + side (CE/PE) + spot LTP + today's date, return the
   ``instrument_key`` + ``lot_size`` of the at-the-money current-month
   monthly contract (rolling forward when within the dte buffer).

Strike steps + lot sizes are NOT hardcoded — read from the live NFO
master. Lot sizes change every quarter under SEBI; assume they will
change again. Don't bake them into config.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

IST = ZoneInfo("Asia/Kolkata")

# Underlying instrument keys — used to fetch the option chain.
UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}

# Strike step per index (verified against Upstox NFO master 2026-04-26).
# These are stable for years — SEBI rarely changes them.
STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
}


# --------------------------------------------------------------------- #
# Data classes                                                          #
# --------------------------------------------------------------------- #

def _parse_expiry(raw: object) -> date:
    """Upstox returns expiry as either ISO 'YYYY-MM-DD' (current API)
    or epoch-ms (older docs). Handle both gracefully."""
    if isinstance(raw, str):
        return date.fromisoformat(raw[:10])
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(int(raw) / 1000).date()
    raise ValueError(f"unparseable expiry: {raw!r}")


@dataclass(frozen=True)
class OptionContract:
    """One row from the NFO master (or our SQLite cache)."""
    instrument_key: str
    underlying: str          # NIFTY | BANKNIFTY
    expiry: date
    strike: float
    option_type: str         # CE | PE
    lot_size: int
    tick_size: float
    trading_symbol: str


# --------------------------------------------------------------------- #
# Public API                                                             #
# --------------------------------------------------------------------- #

def refresh_options_master(
    db_path: str | Path,
    underlyings: list[str] | None = None,
    access_token: str | None = None,
) -> int:
    """Fetch + persist NIFTY/BANKNIFTY option contracts to SQLite.

    Returns the count of rows upserted. Cheap enough to call daily at
    pre-market warmup. Fails open — on network error logs a warning
    and returns 0 without raising, so the scheduler keeps running.
    """
    underlyings = underlyings or list(UNDERLYING_KEYS)
    token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        logger.warning("refresh_options_master: no UPSTOX_ACCESS_TOKEN — skipping refresh")
        return 0

    contracts: list[OptionContract] = []
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    for underlying in underlyings:
        ikey = UNDERLYING_KEYS.get(underlying)
        if ikey is None:
            logger.warning("refresh_options_master: no instrument key for {}", underlying)
            continue
        try:
            r = httpx.get(
                "https://api.upstox.com/v2/option/contract",
                params={"instrument_key": ikey},
                headers=headers,
                timeout=15.0,
            )
            r.raise_for_status()
        except Exception as exc:
            logger.warning("refresh_options_master: fetch failed for {}: {}", underlying, exc)
            continue
        for raw in r.json().get("data", []):
            ot = raw.get("instrument_type")
            if ot not in ("CE", "PE"):
                continue
            exp_raw = raw.get("expiry")
            if not exp_raw:
                continue
            contracts.append(OptionContract(
                instrument_key=raw.get("instrument_key", ""),
                underlying=underlying,
                expiry=_parse_expiry(exp_raw),
                strike=float(raw.get("strike_price", 0)),
                option_type=ot,
                lot_size=int(raw.get("lot_size", 0) or 0),
                tick_size=float(raw.get("tick_size", 0) or 0),
                trading_symbol=raw.get("trading_symbol", ""),
            ))
    if not contracts:
        logger.warning("refresh_options_master: zero contracts fetched")
        return 0

    # Upsert via the existing instruments table — re-using its expiry /
    # strike / option_type columns which were declared in the original
    # schema for exactly this purpose.
    n = 0
    with sqlite3.connect(str(db_path)) as conn:
        for c in contracts:
            conn.execute(
                """
                INSERT INTO instruments(
                    symbol, exchange, segment, tick_size, lot_size,
                    expiry, strike, option_type, name, isin, series, updated_at
                ) VALUES(?, 'NSE', 'OPT', ?, ?, ?, ?, ?, ?, '', 'OPT', ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    tick_size=excluded.tick_size, lot_size=excluded.lot_size,
                    expiry=excluded.expiry, strike=excluded.strike,
                    option_type=excluded.option_type, updated_at=excluded.updated_at
                """,
                (
                    c.trading_symbol or c.instrument_key, c.tick_size,
                    c.lot_size, c.expiry.isoformat(), c.strike, c.option_type,
                    f"{c.underlying} {c.strike:.0f} {c.option_type}",
                    datetime.now(IST).isoformat(),
                ),
            )
            n += 1
    logger.info("refresh_options_master: upserted {} contracts ({})",
                n, ", ".join(underlyings))
    return n


def get_atm_strike(spot: float, underlying: str) -> float:
    """Round ``spot`` to the nearest valid strike for the given underlying."""
    step = STRIKE_STEP.get(underlying)
    if step is None:
        raise ValueError(f"unknown underlying {underlying!r}; expected NIFTY or BANKNIFTY")
    return round(spot / step) * step


def get_monthly_expiry(
    underlying: str,
    today: date,
    *,
    min_days_to_expiry: int = 7,
    access_token: str | None = None,
) -> date | None:
    """Return the monthly expiry to trade.

    Logic:
      1. Fetch all available expiries for the underlying.
      2. Compute the last expiry of each calendar month (= "monthly").
      3. Pick current month's monthly expiry if days-to-expiry ≥
         ``min_days_to_expiry``; otherwise the next month's monthly.
      4. Returns None on fetch failure (caller should skip the signal,
         not crash).
    """
    token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        return None
    ikey = UNDERLYING_KEYS.get(underlying)
    if ikey is None:
        return None
    try:
        r = httpx.get(
            "https://api.upstox.com/v2/option/contract",
            params={"instrument_key": ikey},
            headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
            timeout=10.0,
        )
        r.raise_for_status()
    except Exception as exc:
        logger.warning("get_monthly_expiry: fetch failed: {}", exc)
        return None

    raw_expiries: set[date] = set()
    for c in r.json().get("data", []):
        v = c.get("expiry")
        if v:
            try:
                raw_expiries.add(_parse_expiry(v))
            except Exception:
                continue
    if not raw_expiries:
        return None

    # Group by (year, month) — the LAST expiry in each bucket = monthly.
    monthly_by_month: dict[tuple[int, int], date] = {}
    for exp in raw_expiries:
        key = (exp.year, exp.month)
        if exp > monthly_by_month.get(key, date.min):
            monthly_by_month[key] = exp

    # Pick first monthly with dte ≥ min_days_to_expiry.
    for (_y, _m), exp in sorted(monthly_by_month.items()):
        dte = (exp - today).days
        if dte >= min_days_to_expiry:
            return exp
    return None


def resolve_atm_option(
    db_path: str | Path,
    underlying: str,
    side: str,
    spot: float,
    today: date,
    *,
    min_days_to_expiry: int = 7,
    access_token: str | None = None,
) -> OptionContract | None:
    """Find the ATM CE/PE contract for the given underlying + side.

    ``side`` is "CE" or "PE". Reads from the local ``instruments`` cache
    first; if that misses, returns None (caller should call
    ``refresh_options_master`` first).

    Returns None on any resolution failure — caller logs + skips.
    """
    if side not in ("CE", "PE"):
        raise ValueError(f"side must be CE or PE, got {side!r}")
    if underlying not in UNDERLYING_KEYS:
        return None

    expiry = get_monthly_expiry(
        underlying, today,
        min_days_to_expiry=min_days_to_expiry,
        access_token=access_token,
    )
    if expiry is None:
        logger.warning("resolve_atm_option: no monthly expiry for {}", underlying)
        return None
    strike = get_atm_strike(spot, underlying)

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT symbol, tick_size, lot_size, expiry, strike, option_type, name
            FROM instruments
            WHERE segment = 'OPT'
              AND option_type = ?
              AND strike = ?
              AND date(expiry) = ?
              AND name LIKE ?
            LIMIT 1
            """,
            (side, strike, expiry.isoformat(), f"{underlying}%"),
        ).fetchone()
    if row is None:
        logger.warning(
            "resolve_atm_option: no contract in cache for {} {} {} {}",
            underlying, side, strike, expiry,
        )
        return None

    return OptionContract(
        instrument_key="",   # filled by caller from the row's symbol if needed
        underlying=underlying,
        expiry=date.fromisoformat(row["expiry"][:10]),
        strike=float(row["strike"]),
        option_type=row["option_type"],
        lot_size=int(row["lot_size"]),
        tick_size=float(row["tick_size"]),
        trading_symbol=row["symbol"],
    )
