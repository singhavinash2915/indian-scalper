"""Bulk historical fetcher for backtest input data.

Upstox V3 historical-candle caps each call at 30 days for the 15-minute
interval. Two-year datasets need ~24 sequential calls per symbol. This
module:

  - Walks the date range in 30-day chunks
  - Persists each symbol's full series as a CSV under
    data/backtest/<resolution>/<symbol>.csv
  - Resumes from the cache: if a file already covers the requested
    window, skip the network calls
  - Politely rate-limits at ~5 req/s to stay under Upstox limits
  - Special-cases NIFTY/BANKNIFTY indices (NSE_INDEX|...) for the
    options backtest

Output rows are ``Candle`` objects, ready for ``BacktestCandleFetcher``.
"""

from __future__ import annotations

import csv
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from brokers.base import Candle

IST = ZoneInfo("Asia/Kolkata")
UPSTOX_BASE = "https://api.upstox.com/v3"

# Upstox V3 max windows per interval (verified 2026-04-26).
_MAX_WINDOW_DAYS = {
    ("minutes", 1): 7,
    ("minutes", 5): 30,
    ("minutes", 15): 30,
    ("minutes", 30): 30,
    ("days", 1): 800,    # 2+ years works in one call
}

# Symbols → instrument keys for the indices we care about.
_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}


def _resolve_instrument_key(symbol: str, instruments_db: str | Path | None) -> str | None:
    """Map a strategy-level symbol (RELIANCE, NIFTY, BANKNIFTY) to its
    Upstox instrument_key."""
    if symbol in _INDEX_KEYS:
        return _INDEX_KEYS[symbol]
    if instruments_db is None:
        return None
    import sqlite3
    with sqlite3.connect(str(instruments_db)) as c:
        row = c.execute(
            "SELECT isin FROM instruments WHERE symbol = ? AND segment IN ('EQ','EQUITY')",
            (symbol,),
        ).fetchone()
    if row and row[0]:
        return f"NSE_EQ|{row[0]}"
    return None


def _slug(symbol: str) -> str:
    """File-system-safe filename component."""
    return symbol.replace("|", "_").replace("/", "_").replace(":", "_")


def _candle_from_row(row: list) -> Candle:
    """Upstox row format: [ts_iso, open, high, low, close, volume, oi]."""
    ts_str, o, h, lo, c, v, *_ = row
    ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    else:
        ts = ts.astimezone(IST)
    return Candle(
        ts=ts, open=float(o), high=float(h), low=float(lo),
        close=float(c), volume=int(v),
    )


def _http_get(url: str, token: str, timeout: float = 30.0) -> dict:
    r = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
        timeout=timeout,
    )
    if r.status_code == 200:
        return r.json()
    raise RuntimeError(f"upstox {r.status_code}: {r.text[:200]}")


def _fetch_window(
    instrument_key: str,
    unit: str,
    interval: int,
    from_d: date,
    to_d: date,
    token: str,
) -> list[Candle]:
    """Fetch one window from Upstox V3. Returns oldest-first list."""
    url = (
        f"{UPSTOX_BASE}/historical-candle/"
        f"{quote(instrument_key, safe='')}/{unit}/{interval}/"
        f"{to_d.isoformat()}/{from_d.isoformat()}"
    )
    data = _http_get(url, token).get("data", {})
    raw = data.get("candles", [])
    # Upstox returns newest-first.
    return [_candle_from_row(r) for r in reversed(raw)]


def _save_csv(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume])


def _load_csv(path: Path) -> list[Candle]:
    out: list[Candle] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            out.append(Candle(
                ts=ts, open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=int(row["volume"]),
            ))
    return out


def fetch_history(
    symbol: str,
    from_date: date,
    to_date: date,
    *,
    unit: str = "minutes",
    interval: int = 15,
    cache_dir: str | Path = "data/backtest",
    instruments_db: str | Path | None = None,
    token: str | None = None,
    rate_limit_sec: float = 0.2,
    force: bool = False,
) -> list[Candle]:
    """Fetch ``symbol`` candles between ``from_date`` and ``to_date``.

    Cached per-symbol. Subsequent calls with the same window return
    instantly. Window-walks the Upstox V3 endpoint at ``rate_limit_sec``
    between calls.

    ``unit/interval``: ("minutes", 15) for 15m, ("days", 1) for daily.
    """
    token = token or os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("fetch_history: UPSTOX_ACCESS_TOKEN not set")
    ikey = _resolve_instrument_key(symbol, instruments_db)
    if ikey is None:
        raise ValueError(f"fetch_history: cannot resolve instrument_key for {symbol}")

    res_label = f"{interval}{unit[0]}"
    cache_path = Path(cache_dir) / res_label / f"{_slug(symbol)}.csv"
    if not force and cache_path.exists():
        cached = _load_csv(cache_path)
        if cached:
            first = cached[0].ts.date()
            last = cached[-1].ts.date()
            if first <= from_date and last >= to_date:
                return [c for c in cached if from_date <= c.ts.date() <= to_date]

    # Sliding window walk.
    window_max = _MAX_WINDOW_DAYS.get((unit, interval), 30)
    cursor = from_date
    out: list[Candle] = []
    while cursor <= to_date:
        chunk_end = min(cursor + timedelta(days=window_max - 1), to_date)
        try:
            chunk = _fetch_window(ikey, unit, interval, cursor, chunk_end, token)
            out.extend(chunk)
        except Exception as exc:
            logger.warning("fetch_history {} {}-{}: {}", symbol, cursor, chunk_end, exc)
        cursor = chunk_end + timedelta(days=1)
        time.sleep(rate_limit_sec)

    # Dedupe + sort (overlapping windows can repeat the boundary bar).
    seen: dict[datetime, Candle] = {c.ts: c for c in out}
    cleaned = sorted(seen.values(), key=lambda c: c.ts)
    _save_csv(cache_path, cleaned)
    return cleaned


def fetch_history_bulk(
    symbols: list[str],
    from_date: date,
    to_date: date,
    *,
    unit: str = "minutes",
    interval: int = 15,
    cache_dir: str | Path = "data/backtest",
    instruments_db: str | Path | None = None,
    token: str | None = None,
    rate_limit_sec: float = 0.2,
    progress_every: int = 5,
) -> dict[str, list[Candle]]:
    """Bulk-fetch the universe. Logs progress every N symbols. Failures
    on individual symbols don't abort the run (warn + continue)."""
    out: dict[str, list[Candle]] = {}
    total = len(symbols)
    started = time.monotonic()
    for i, sym in enumerate(symbols, 1):
        try:
            out[sym] = fetch_history(
                sym, from_date, to_date,
                unit=unit, interval=interval,
                cache_dir=cache_dir, instruments_db=instruments_db,
                token=token, rate_limit_sec=rate_limit_sec,
            )
        except Exception as exc:
            logger.warning("fetch_history_bulk skip {}: {}", sym, exc)
            out[sym] = []
        if i % progress_every == 0 or i == total:
            elapsed = time.monotonic() - started
            n_bars = sum(len(v) for v in out.values())
            logger.info(
                "history fetch progress: {}/{} symbols, {:,} bars total, {:.0f}s elapsed",
                i, total, n_bars, elapsed,
            )
    return out
