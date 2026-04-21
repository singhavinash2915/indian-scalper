"""Shipped named-index presets for the universe picker.

Each preset is a YAML file:
    src/data/presets/{preset_name}.yaml

with the shape:

    name: nifty_100
    source: "NSE index constituents — <url or circular date>"
    reviewed_at: "YYYY-MM-DD"
    symbols:
      - RELIANCE
      - TCS
      ...

The symbols list is operator-owned data — indices rebalance, symbols
change. Every file ships with a disclaimer comment at the top.
Operators are expected to re-verify against the current NSE
circular before using in live trading.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PRESETS_DIR = Path(__file__).parent


def list_available_presets() -> list[str]:
    """Every ``*.yaml`` file in the presets dir is an available preset."""
    return sorted(p.stem for p in PRESETS_DIR.glob("*.yaml"))


def load_preset_symbols(name: str) -> list[str]:
    """Load the symbol list for ``name``. Raises ``FileNotFoundError``
    if the preset isn't shipped, ``ValueError`` if the YAML is
    malformed."""
    path = PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"preset {name!r} has no shipped symbol list at {path}",
        )
    raw = yaml.safe_load(path.read_text()) or {}
    symbols = raw.get("symbols")
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError(
            f"preset {name!r} YAML at {path} must have a `symbols:` list of strings",
        )
    # Dedup, strip whitespace, uppercase.
    seen: set[str] = set()
    out: list[str] = []
    for s in symbols:
        key = s.strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out
