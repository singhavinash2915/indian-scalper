"""Shared test helpers."""

from __future__ import annotations

from config.settings import Settings


def paper_mode(settings: Settings) -> Settings:
    """Flip a template ``Settings`` into paper mode by setting
    ``runtime.initial_trade_mode=paper``. Call this BEFORE constructing
    a broker — the broker reads ``runtime.initial_trade_mode`` at
    construction time to seed control_flags on first DB init.

    Tests that specifically verify watch-only behaviour should omit
    this helper and rely on the PROMPT-mandated default.
    """
    settings.raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    return settings
