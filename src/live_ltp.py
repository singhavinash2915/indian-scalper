"""scalper-live-ltp — fetch real-time LTP from Upstox for one or more symbols.

Reads UPSTOX_ACCESS_TOKEN from env (see .env). Resolves NSE instrument keys
(NSE_EQ|<isin>) from the instruments master.

    uv run scalper-live-ltp RELIANCE TCS INFY
    uv run scalper-live-ltp --compare-yfinance RELIANCE   # side-by-side
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx


UPSTOX_BASE = "https://api.upstox.com/v2"


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def _resolve_instrument_keys(symbols: list[str], db_path: str) -> dict[str, str]:
    import sqlite3
    conn = sqlite3.connect(db_path)
    placeholders = ",".join("?" * len(symbols))
    rows = conn.execute(
        f"SELECT symbol, isin FROM instruments WHERE symbol IN ({placeholders})",
        symbols,
    ).fetchall()
    conn.close()
    return {sym: f"NSE_EQ|{isin}" for sym, isin in rows if isin}


def fetch_upstox_ltp(keys: list[str], token: str) -> dict[str, float]:
    """Returns {instrument_key: last_price}. Batches all keys into one call."""
    url = f"{UPSTOX_BASE}/market-quote/ltp"
    params = {"instrument_key": ",".join(keys)}
    r = httpx.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}", "accept": "application/json"},
        timeout=10.0,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    out: dict[str, float] = {}
    for _short_key, payload in data.items():
        ikey = payload.get("instrument_token") or _short_key
        out[ikey] = float(payload.get("last_price", 0.0))
    return out


def fetch_yfinance_ltp(symbols: list[str]) -> dict[str, float]:
    """For comparison — returns yfinance 'regular market price' (delayed)."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out: dict[str, float] = {}
    for sym in symbols:
        t = yf.Ticker(f"{sym}.NS")
        info = t.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)
        if price:
            out[sym] = float(price)
    return out


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()

    ap = argparse.ArgumentParser(prog="scalper-live-ltp", description=__doc__)
    ap.add_argument("symbols", nargs="+", help="e.g. RELIANCE TCS INFY")
    ap.add_argument("--db", default="data/scalper.db", help="path to instruments DB")
    ap.add_argument(
        "--compare-yfinance",
        action="store_true",
        help="also fetch yfinance LTP for side-by-side comparison",
    )
    args = ap.parse_args(argv)

    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("error: UPSTOX_ACCESS_TOKEN not set (missing .env?)", file=sys.stderr)
        return 1

    sym_to_key = _resolve_instrument_keys(args.symbols, args.db)
    missing = [s for s in args.symbols if s not in sym_to_key]
    if missing:
        print(f"warning: no ISIN in DB for {missing} — skipping", file=sys.stderr)

    if not sym_to_key:
        return 1

    keys = list(sym_to_key.values())
    upstox = fetch_upstox_ltp(keys, token)

    yf_map: dict[str, float] = {}
    if args.compare_yfinance:
        yf_map = fetch_yfinance_ltp(list(sym_to_key.keys()))

    # Header
    if args.compare_yfinance:
        print(f"  {'symbol':<10}  {'upstox (live)':>15}  {'yfinance (delayed)':>20}  diff")
        print(f"  {'-'*10}  {'-'*15}  {'-'*20}  ----")
    else:
        print(f"  {'symbol':<10}  {'upstox LTP':>15}")
        print(f"  {'-'*10}  {'-'*15}")

    for sym, ikey in sym_to_key.items():
        up = upstox.get(ikey, 0.0)
        if args.compare_yfinance:
            yfp = yf_map.get(sym, 0.0)
            diff = up - yfp if yfp else 0.0
            print(f"  {sym:<10}  ₹{up:>13,.2f}  ₹{yfp:>18,.2f}  {diff:+.2f}")
        else:
            print(f"  {sym:<10}  ₹{up:>13,.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
