"""Earnings-calendar filter.

Reads a one-symbol-per-line CSV listing companies that have results today.
The scan loop consults this list when ``strategy.earnings_filter`` is set
to ``exclude`` (skip results-today stocks — avoid gap risk) or
``restrict_to`` (ONLY trade results-today stocks — event-driven mode).

File format (``data/earnings/today.csv`` by default):

    # Optional comment lines start with #
    # Update each morning before 09:15 IST.
    # One NSE symbol per line, no quotes.
    RELIANCE
    INFY
    TCS

Missing file is treated as empty — bot logs a warning + falls back to
"no symbols match" behaviour. Caller decides whether that means
"trade nothing" (restrict_to) or "trade everything" (exclude).
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger


def load_earnings_today(path: str | Path) -> set[str]:
    """Return the set of symbols with results today.

    Empty set when the file is missing, empty, or contains only comments.
    Lines are stripped + uppercased for normalisation.
    """
    p = Path(path)
    if not p.exists():
        logger.warning("earnings calendar not found at {} — treating as empty", p)
        return set()
    out: set[str] = set()
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate CSV with extra columns: take only the first.
        sym = line.split(",")[0].strip().upper()
        if sym:
            out.add(sym)
    return out


def symbol_passes_earnings_filter(
    symbol: str, earnings_set: set[str], mode: str,
) -> tuple[bool, str | None]:
    """Decide whether ``symbol`` is allowed to trade given the filter mode.

    Returns ``(passes, reason)`` where ``reason`` is None on pass or a
    human-readable explanation on block.
    """
    mode = (mode or "off").lower()
    if mode == "off":
        return True, None
    if mode == "exclude":
        if symbol in earnings_set:
            return False, "earnings_today"
        return True, None
    if mode == "restrict_to":
        if not earnings_set:
            return False, "earnings_calendar_empty"
        if symbol not in earnings_set:
            return False, "not_in_earnings_calendar"
        return True, None
    # Unknown mode — fail open (don't silently halt the bot).
    logger.warning("unknown earnings_filter mode {!r}; passing through", mode)
    return True, None
