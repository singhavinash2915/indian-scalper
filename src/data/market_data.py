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
