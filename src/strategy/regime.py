"""Market-regime detection.

Momentum strategies (8-factor scorer in ``scoring.py``) bleed in ranging
markets because stops fire on normal noise. This module answers:
"is the broad market trending right now?" Scan loop consults the result
before any fresh entry.

Signal: ADX on a proxy symbol (NIFTY constituent or the index itself)
computed on the strategy's candle interval. ADX ≥ threshold → trending;
otherwise ranging.

Proxy choice: the default is the user's configured ``regime_filter.proxy_symbol``
(e.g. ``RELIANCE``, ``NIFTYBEES``, ``^NSEI``). Any symbol the data fetcher can
serve 15m candles for. If candles can't be fetched (no data, token expired),
the filter fails OPEN — i.e. entries continue — rather than silently halting.
Caller logs a warning in that case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from strategy import indicators as ind

if TYPE_CHECKING:
    from data.market_data import CandleFetcher


@dataclass(frozen=True)
class RegimeSnapshot:
    """Result of one regime probe."""
    adx: float
    trending: bool
    proxy_symbol: str
    min_adx: float
    reason: str = ""   # populated when ``trending`` is False or fetch failed


def probe_regime(
    fetcher: "CandleFetcher",
    proxy_symbol: str,
    interval: str,
    min_adx: float,
    adx_len: int = 14,
    lookback: int = 120,
) -> RegimeSnapshot:
    """Fetch the proxy's recent candles and check its ADX.

    Returns a ``RegimeSnapshot`` with ``trending=True`` when current ADX
    ≥ ``min_adx``, else False. On fetch / compute failure, ``trending``
    defaults to **True** (fail-open) so a broken feed doesn't silently
    pause the strategy.
    """
    try:
        candles = fetcher.get_candles(proxy_symbol, interval, lookback=lookback)
    except Exception as exc:
        return RegimeSnapshot(
            adx=0.0, trending=True, proxy_symbol=proxy_symbol,
            min_adx=min_adx, reason=f"fetch_error: {exc}",
        )
    if not candles or len(candles) < adx_len + 10:
        return RegimeSnapshot(
            adx=0.0, trending=True, proxy_symbol=proxy_symbol,
            min_adx=min_adx, reason=f"insufficient_candles ({len(candles) if candles else 0})",
        )
    df = pd.DataFrame(
        {
            "high":  [c.high  for c in candles],
            "low":   [c.low   for c in candles],
            "close": [c.close for c in candles],
        },
        index=pd.DatetimeIndex([c.ts for c in candles], name="ts"),
    )
    try:
        adx_frame = ind.adx(df["high"], df["low"], df["close"], length=adx_len)
    except Exception as exc:
        return RegimeSnapshot(
            adx=0.0, trending=True, proxy_symbol=proxy_symbol,
            min_adx=min_adx, reason=f"adx_error: {exc}",
        )
    # ind.adx returns a DataFrame with columns {adx, dmp, dmn} — take the adx column.
    adx_series = adx_frame["adx"] if hasattr(adx_frame, "columns") else adx_frame
    adx_val = float(adx_series.dropna().iloc[-1]) if not adx_series.dropna().empty else 0.0
    trending = adx_val >= min_adx
    reason = "" if trending else f"adx {adx_val:.1f} < {min_adx}"
    return RegimeSnapshot(
        adx=adx_val, trending=trending,
        proxy_symbol=proxy_symbol, min_adx=min_adx, reason=reason,
    )
