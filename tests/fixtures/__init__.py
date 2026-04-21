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


def running_scheduler(broker, actor: str = "test") -> None:
    """Flip ``scheduler_state`` to ``running`` on a freshly-built
    broker so ``run_tick`` doesn't short-circuit on the initial
    ``stopped`` default (per D11 Slice 1). Call immediately after
    broker construction in tests that exercise the full tick pipeline.
    Tests that specifically verify the stopped/paused branches should
    omit this helper.
    """
    broker.store.set_flag("scheduler_state", "running", actor=actor)
