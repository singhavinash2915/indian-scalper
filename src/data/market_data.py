"""Candle data source abstraction for paper mode.

The ``CandleFetcher`` protocol is what ``PaperBroker`` and the scan loop
depend on — never a concrete yfinance/Upstox client. Swapping between
``YFinanceFetcher`` (default in paper mode), ``UpstoxFetcher`` (later,
for live mode), and ``FakeCandleFetcher`` (tests + backtest) is a
constructor swap.

Interval parsing is liberal: the strategy config uses Reddit-like strings
(``"15m"``, ``"5m"``, ``"1h"``, ``"1d"``). Concrete fetchers map them to
their own conventions internally.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from loguru import logger

from brokers.base import Candle

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------- #
# Protocol                                                               #
# --------------------------------------------------------------------- #

class CandleFetcher(Protocol):
    """Every backend implements exactly this surface."""

    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]: ...


# --------------------------------------------------------------------- #
# FakeCandleFetcher — deterministic, used by tests & dry-run             #
# --------------------------------------------------------------------- #

class FakeCandleFetcher:
    """Serves pre-seeded candle series. Raises if asked for a symbol it
    wasn't seeded with — a fake that silently returns [] would mask test
    bugs."""

    def __init__(self, series: dict[str, list[Candle]] | None = None) -> None:
        self._series: dict[str, list[Candle]] = dict(series or {})

    def seed(self, symbol: str, candles: list[Candle]) -> None:
        self._series[symbol] = list(candles)

    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]:
        if symbol not in self._series:
            raise KeyError(f"FakeCandleFetcher: no candles seeded for {symbol!r}")
        series = self._series[symbol]
        return series[-lookback:] if lookback > 0 else list(series)


# --------------------------------------------------------------------- #
# YFinanceFetcher — default paper-mode backend                           #
# --------------------------------------------------------------------- #

_YF_INTERVAL_MAP = {
    "1m": "1m",
    "2m": "2m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "1h": "1h",
    "1d": "1d",
    "daily": "1d",
}

_YF_LOOKBACK_PERIOD = {
    "1m": "7d",     # yfinance caps 1m at 7 days
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "1h": "730d",
    "1d": "2y",
    "daily": "2y",
}


class YFinanceFetcher:
    """Thin wrapper over the ``yfinance`` library. Adds ``.NS`` suffix
    automatically for NSE equities.

    ``yfinance`` is imported lazily on the first ``get_candles`` call so
    that constructing ``PaperBroker`` (which instantiates this fetcher by
    default) stays fast and doesn't force a yfinance import on tests
    that never fetch.
    """

    def __init__(self, exchange_suffix: str = ".NS") -> None:
        self.exchange_suffix = exchange_suffix

    def get_candles(
        self, symbol: str, interval: str, lookback: int
    ) -> list[Candle]:
        try:
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "YFinanceFetcher requires the 'yfinance' package. "
                "Install with: uv add yfinance"
            ) from exc

        yf_interval = _YF_INTERVAL_MAP.get(interval)
        if yf_interval is None:
            raise ValueError(f"Unsupported interval: {interval!r}")
        period = _YF_LOOKBACK_PERIOD[interval]

        ticker = symbol if symbol.endswith(self.exchange_suffix) else f"{symbol}{self.exchange_suffix}"
        logger.debug("yfinance fetch {} interval={} period={}", ticker, yf_interval, period)
        df = yf.download(
            tickers=ticker,
            interval=yf_interval,
            period=period,
            progress=False,
            auto_adjust=False,
        )
        if df is None or df.empty:
            return []

        # yfinance returns MultiIndex columns when there's one ticker but
        # group_by defaults vary across versions. Flatten if needed.
        if hasattr(df.columns, "levels"):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        candles: list[Candle] = []
        for ts, row in df.tail(lookback).iterrows():
            # yfinance returns tz-naive for most intervals; localise to IST.
            if getattr(ts, "tzinfo", None) is None:
                ts_ist = ts.to_pydatetime().replace(tzinfo=IST)
            else:
                ts_ist = ts.to_pydatetime().astimezone(IST)
            candles.append(
                Candle(
                    ts=ts_ist,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=int(row.get("Volume", 0) or 0),
                )
            )
        return candles


# --------------------------------------------------------------------- #
# UpstoxFetcher — live NSE feed via Upstox REST v2                       #
# --------------------------------------------------------------------- #

_UPSTOX_BASE = "https://api.upstox.com/v2"

# Upstox intraday endpoint supports only 1minute + 30minute. Anything
# else (5m, 15m) is produced by resampling 1-minute bars on our side.
_UPSTOX_NATIVE_INTERVALS = {"1m", "30m", "1d", "daily"}
_UPSTOX_INTERVAL_MAP = {
    "1m":    ("intraday", "1minute"),
    "30m":   ("intraday", "30minute"),
    "1d":    ("historical", "day"),
    "daily": ("historical", "day"),
}


def _interval_to_minutes(interval: str) -> int:
    """Parse '5m' / '15m' / '30m' / '60m' / '1h' into minutes. Raises ValueError."""
    s = interval.strip().lower()
    if s.endswith("m"):
        return int(s[:-1])
    if s.endswith("h"):
        return int(s[:-1]) * 60
    raise ValueError(f"can't convert {interval!r} to minutes")


class UpstoxFetcher:
    """Real-time NSE candles via Upstox REST API.

    Auth: ``UPSTOX_ACCESS_TOKEN`` env var (set via ``.env``; refresh daily
    before 03:30 IST via ``scalper-upstox-auth``). Instrument-key resolution
    goes through ``InstrumentMaster`` (ISIN column) — Upstox identifies NSE
    equities as ``NSE_EQ|<isin>``.

    Interval handling:
        * ``1m`` / ``30m`` / ``1d`` — pass through to Upstox's native endpoint.
        * ``5m`` / ``15m`` / ``60m`` — fetch 1-minute bars and resample with
          pandas. Upstox intraday only serves today's session, so multi-day
          warmup is topped up from the historical daily endpoint when
          ``lookback`` exceeds what's available.
    """

    def __init__(
        self,
        access_token: str | None = None,
        instruments: "InstrumentMaster | None" = None,   # type: ignore[name-defined]
        base_url: str = _UPSTOX_BASE,
        ltp_cache_ttl: float = 1.0,
    ) -> None:
        import os
        self.access_token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        if not self.access_token:
            raise RuntimeError(
                "UPSTOX_ACCESS_TOKEN not set — cannot use UpstoxFetcher. "
                "Add it to .env (see RUNBOOK §Upstox live feed)."
            )
        self.instruments = instruments
        self.base_url = base_url
        # Short TTL cache: when multiple dashboard panels render in the
        # same second they share one Upstox round-trip. Not a long-term
        # cache — anything > 1s would feel stale on the live-P&L view.
        self._ltp_cache_ttl = ltp_cache_ttl
        self._ltp_cache: dict[str, tuple[float, float]] = {}  # symbol → (ts, price)

    # -- key resolution ------------------------------------------------- #

    # Hardcoded index instrument keys — used by the options stack to
    # fetch NIFTY/BANKNIFTY 15m candles + spot LTP. Stable for years.
    _INDEX_KEYS = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    }

    def _instrument_key(self, symbol: str) -> str:
        # Special-case indices first — their keys aren't in the instruments DB.
        if symbol in self._INDEX_KEYS:
            return self._INDEX_KEYS[symbol]
        if self.instruments is None:
            raise RuntimeError("UpstoxFetcher requires InstrumentMaster for symbol→ISIN lookup")
        # Instrument dataclass doesn't expose isin; read it directly from the
        # SQLite row. Cache per-symbol to avoid repeated queries.
        if not hasattr(self, "_isin_cache"):
            self._isin_cache: dict[str, str] = {}
        isin = self._isin_cache.get(symbol)
        if isin is None:
            import sqlite3
            db_path = getattr(self.instruments, "db_path", None) or "data/scalper.db"
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT isin FROM instruments WHERE symbol = ?", (symbol,)
                ).fetchone()
            if not row or not row[0]:
                raise ValueError(f"no ISIN in instruments master for {symbol!r}")
            isin = row[0]
            self._isin_cache[symbol] = isin
        return f"NSE_EQ|{isin}"

    # -- HTTP layer (isolated so tests can monkey-patch) ---------------- #

    def _http_get(self, url: str, params: dict | None = None) -> dict:
        import httpx
        r = httpx.get(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "accept": "application/json",
            },
            timeout=10.0,
        )
        r.raise_for_status()
        return r.json()

    # -- candle fetches ------------------------------------------------- #

    def _fetch_intraday_1m(self, ikey: str) -> list[list]:
        from urllib.parse import quote
        url = f"{self.base_url}/historical-candle/intraday/{quote(ikey, safe='')}/1minute"
        return self._http_get(url).get("data", {}).get("candles", [])

    def _fetch_historical(self, ikey: str, interval: str, to_date: str, from_date: str) -> list[list]:
        from urllib.parse import quote
        url = (
            f"{self.base_url}/historical-candle/"
            f"{quote(ikey, safe='')}/{interval}/{to_date}/{from_date}"
        )
        return self._http_get(url).get("data", {}).get("candles", [])

    # -- public API ----------------------------------------------------- #

    def get_ltp_by_keys(self, instrument_keys: list[str]) -> dict[str, float]:
        """Real-time LTP for arbitrary Upstox instrument keys (any segment).

        Used by the options stack: keys come in as ``NSE_FO|<token>``
        (option contracts) or ``NSE_INDEX|Nifty 50`` (index spot).
        Returns ``{instrument_key: last_price}``. Fail-open returns {}.
        """
        if not instrument_keys:
            return {}
        url = f"{self.base_url}/market-quote/ltp"
        params = {"instrument_key": ",".join(instrument_keys)}
        try:
            data = self._http_get(url, params=params).get("data", {})
        except Exception as exc:
            logger.warning("UpstoxFetcher get_ltp_by_keys failed: {}", exc)
            return {}
        out: dict[str, float] = {}
        for _resp_key, payload in data.items():
            tok = payload.get("instrument_token", "")
            if tok:
                out[tok] = float(payload.get("last_price", 0.0))
        return out

    def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        """Batched real-time LTP fetch. Returns ``{symbol: last_price}``.

        Caches each symbol for ``ltp_cache_ttl`` seconds — the dashboard's
        positions + KPI partials fire within the same tick, so we coalesce
        their calls into one HTTP round-trip.
        """
        import time
        now = time.monotonic()
        fresh: dict[str, float] = {}
        stale: list[str] = []
        for sym in symbols:
            hit = self._ltp_cache.get(sym)
            if hit and now - hit[0] < self._ltp_cache_ttl:
                fresh[sym] = hit[1]
            else:
                stale.append(sym)
        if not stale:
            return fresh

        # Resolve ISINs for anything not cached.
        keys: list[str] = []
        key_to_sym: dict[str, str] = {}
        for sym in stale:
            try:
                k = self._instrument_key(sym)
            except Exception as exc:
                logger.warning("UpstoxFetcher LTP skip {} ({})", sym, exc)
                continue
            keys.append(k)
            key_to_sym[f"NSE_EQ:{k.split('|', 1)[1]}"] = sym  # Upstox response key format
        if not keys:
            return fresh

        url = f"{self.base_url}/market-quote/ltp"
        params = {"instrument_key": ",".join(keys)}
        try:
            data = self._http_get(url, params=params).get("data", {})
        except Exception as exc:
            logger.warning("UpstoxFetcher LTP fetch failed: {}", exc)
            return fresh

        for response_key, payload in data.items():
            sym = key_to_sym.get(response_key)
            if sym is None:
                # Fallback: find by instrument_token suffix match.
                tok = payload.get("instrument_token", "")
                for k, v in key_to_sym.items():
                    if k == f"NSE_EQ:{tok.split('|', 1)[-1]}":
                        sym = v
                        break
            if sym is None:
                continue
            price = float(payload.get("last_price", 0.0))
            if price <= 0:
                # Upstox occasionally returns last_price=0 at session
                # boundaries / illiquid moments. Don't poison the cache —
                # let the caller's fallback (entry price, last good LTP)
                # take over.
                logger.debug("UpstoxFetcher: dropping zero LTP for {}", sym)
                continue
            fresh[sym] = price
            self._ltp_cache[sym] = (now, price)
        return fresh

    def get_candles(self, symbol: str, interval: str, lookback: int) -> list[Candle]:
        import pandas as pd

        ikey = self._instrument_key(symbol)
        target_min = _interval_to_minutes(interval) if interval not in {"1d", "daily"} else None

        # Daily / weekly — pass through historical endpoint.
        if interval in {"1d", "daily"}:
            from datetime import date, timedelta
            to_d = date.today().isoformat()
            from_d = (date.today() - timedelta(days=max(lookback * 2, 60))).isoformat()
            raw = self._fetch_historical(ikey, "day", to_d, from_d)
            raw = list(reversed(raw))   # Upstox returns newest-first
            return _rows_to_candles(raw)[-lookback:]

        # Intraday.
        raw = self._fetch_intraday_1m(ikey)
        raw = list(reversed(raw))   # oldest-first
        candles_1m = _rows_to_candles(raw)

        # If we need more history than today's session provides, top up from
        # historical 30-minute and stitch. For 15m / 5m targets that usually
        # means the warmup bars come from 30m (~same resolution) then fresh
        # current-session from 1m — acceptable for scoring continuity.
        needed_bars_1m = lookback * (target_min or 1)
        if len(candles_1m) < needed_bars_1m:
            from datetime import date, timedelta
            days = max(1, needed_bars_1m // 375 + 2)   # ~375 1m bars per session
            to_d = (date.today() - timedelta(days=1)).isoformat()
            from_d = (date.today() - timedelta(days=days + 3)).isoformat()
            try:
                hist_30m = self._fetch_historical(ikey, "30minute", to_d, from_d)
                hist_30m = list(reversed(hist_30m))
                hist_candles = _rows_to_candles(hist_30m)
                # Expand each 30m candle into synthetic 1m placeholders? No —
                # simpler to resample hist 30m into target interval, then
                # concat with resampled today-1m.
                if target_min and target_min >= 30:
                    # Target is 30m or larger; use hist 30m directly.
                    merged = hist_candles + _resample_candles(candles_1m, target_min)
                    return merged[-lookback:]
                else:
                    # Target < 30m — we can't upsample, so accept coarser
                    # warmup bars then switch to fine resolution today.
                    hist_resampled = _resample_candles_30m_to(hist_candles, target_min or 15)
                    fine_today = _resample_candles(candles_1m, target_min or 15)
                    merged = hist_resampled + fine_today
                    return merged[-lookback:]
            except Exception as exc:   # historical backfill is best-effort
                logger.warning("UpstoxFetcher historical backfill failed: {}", exc)

        # Happy path — resample today's 1m bars to target.
        if target_min is None or target_min == 1:
            return candles_1m[-lookback:]
        return _resample_candles(candles_1m, target_min)[-lookback:]


def _rows_to_candles(rows: list[list]) -> list[Candle]:
    """Upstox row format: [ts_iso, open, high, low, close, volume, oi]."""
    out: list[Candle] = []
    for row in rows:
        ts_str, o, h, lo, c, v, *_ = row
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        else:
            ts = ts.astimezone(IST)
        out.append(Candle(ts=ts, open=float(o), high=float(h), low=float(lo),
                          close=float(c), volume=int(v)))
    return out


def _resample_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    """Resample 1-minute candles to ``minutes``-minute bars using pandas OHLCV rules."""
    if not candles:
        return []
    import pandas as pd
    df = pd.DataFrame(
        {
            "open":   [c.open   for c in candles],
            "high":   [c.high   for c in candles],
            "low":    [c.low    for c in candles],
            "close":  [c.close  for c in candles],
            "volume": [c.volume for c in candles],
        },
        index=pd.DatetimeIndex([c.ts for c in candles], name="ts"),
    )
    agg = df.resample(f"{minutes}min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    out: list[Candle] = []
    for ts, row in agg.iterrows():
        py_ts = ts.to_pydatetime()
        if py_ts.tzinfo is None:
            py_ts = py_ts.replace(tzinfo=IST)
        out.append(Candle(
            ts=py_ts, open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]), volume=int(row["volume"]),
        ))
    return out


def _resample_candles_30m_to(candles_30m: list[Candle], target_min: int) -> list[Candle]:
    """Downsample 30m history to a finer target (15m, 5m) by repeating the bar.

    Not a true resample (we don't have the intra-30m detail) — used only for
    warmup continuity before today's 1m-sourced bars kick in. Volume is
    split evenly across the synthetic sub-bars.
    """
    if target_min >= 30 or not candles_30m:
        return list(candles_30m)
    out: list[Candle] = []
    from datetime import timedelta
    reps = 30 // target_min
    for c in candles_30m:
        vol = c.volume // reps
        for i in range(reps):
            out.append(Candle(
                ts=c.ts + timedelta(minutes=i * target_min),
                open=c.open, high=c.high, low=c.low, close=c.close, volume=vol,
            ))
    return out


# --------------------------------------------------------------------- #
# CSV cache helpers (used by paper broker to persist fetched candles)    #
# --------------------------------------------------------------------- #

def candles_to_csv(candles: list[Candle], path: str | Path) -> None:
    """Persist candles to CSV — used by PaperBroker's candle cache."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume])


def candles_from_csv(path: str | Path) -> list[Candle]:
    out: list[Candle] = []
    with Path(path).open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            out.append(
                Candle(
                    ts=datetime.fromisoformat(row["ts"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                )
            )
    return out


def save_candles_bulk(
    series: dict[str, list[Candle]],
    cache_dir: str | Path,
) -> dict[str, Path]:
    """Write one CSV per symbol to ``cache_dir``. Returns a dict
    ``symbol → written_path``. Used by the replay CLI so historical
    days can be cached locally and replayed without hitting the
    network again."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for symbol, candles in series.items():
        if not candles:
            continue
        safe = symbol.replace("/", "_").replace("\\", "_")
        path = cache_dir / f"{safe}.csv"
        candles_to_csv(candles, path)
        written[symbol] = path
    return written


def load_candles_bulk(
    cache_dir: str | Path,
    symbols: list[str] | None = None,
) -> dict[str, list[Candle]]:
    """Read every ``*.csv`` under ``cache_dir`` (or the listed subset)
    back into a ``{symbol: [Candle, …]}`` dict ready for
    ``BacktestCandleFetcher``."""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return {}
    out: dict[str, list[Candle]] = {}
    for path in sorted(cache_dir.glob("*.csv")):
        symbol = path.stem
        if symbols is not None and symbol not in symbols:
            continue
        out[symbol] = candles_from_csv(path)
    return out


def df_to_candles(df) -> list[Candle]:
    """Convert an OHLCV DataFrame (with a DatetimeIndex) into a list of
    ``Candle``. Mostly useful for wiring strategy-engine fixtures into
    the ``FakeCandleFetcher``.
    """
    out: list[Candle] = []
    for ts, row in df.iterrows():
        py_ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        out.append(
            Candle(
                ts=py_ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            )
        )
    return out


def build_synthetic_candles(
    start: datetime,
    interval_minutes: int,
    closes: list[float],
    volumes: list[int] | None = None,
) -> list[Candle]:
    """Helper for tests — turn a close series into believable OHLCV candles."""
    out: list[Candle] = []
    vols = volumes or [1000] * len(closes)
    prev = closes[0]
    for i, (c, v) in enumerate(zip(closes, vols, strict=True)):
        ts = start + timedelta(minutes=interval_minutes * i)
        o = prev
        h = max(o, c) + 0.2
        low_p = min(o, c) - 0.2
        out.append(Candle(ts=ts, open=o, high=h, low=low_p, close=c, volume=v))
        prev = c
    return out
