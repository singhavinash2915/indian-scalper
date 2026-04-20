"""Smoke test: the embedded config template parses into a valid Settings."""

from __future__ import annotations

from config.settings import CONFIG_YAML_TEMPLATE, Settings


def test_template_parses_into_settings() -> None:
    settings = Settings.from_template()

    assert settings.mode == "paper"
    assert settings.broker == "paper"
    assert settings.capital.starting_inr == 500_000
    assert settings.capital.currency == "INR"

    # Strategy block sanity
    assert settings.strategy.min_score == 6
    assert settings.strategy.rsi_entry_range == (55, 75)
    assert settings.strategy.ema_fast < settings.strategy.ema_mid < settings.strategy.ema_slow

    # Risk block sanity
    assert 0 < settings.risk.risk_per_trade_pct < 100
    assert 0 < settings.risk.daily_loss_limit_pct < 100
    assert settings.risk.max_equity_positions >= 1


def test_raw_block_preserves_unmodeled_sections() -> None:
    """Sections that aren't modelled yet (universe, paper, upstox, dashboard,
    storage, logging) still have to round-trip through ``raw`` so later
    deliverables can read them without re-parsing config.yaml."""
    settings = Settings.from_template()

    for key in ("universe", "paper", "upstox", "dashboard", "storage", "logging"):
        assert key in settings.raw, f"{key} missing from raw"

    assert settings.raw["paper"]["slippage_pct"] == 0.05
    assert settings.raw["storage"]["db_path"] == "data/scalper.db"


def test_template_string_is_nonempty() -> None:
    assert CONFIG_YAML_TEMPLATE.strip().startswith("mode:")
