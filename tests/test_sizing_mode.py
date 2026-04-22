"""Tests for equal_bucket vs cash_aware sizing modes + bucket_slots validation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config.settings import CONFIG_YAML_TEMPLATE, RiskCfg, Settings
from scheduler.scan_loop import _resolve_max_notional


# --------------------------------------------------------------------- #
# RiskCfg validators                                                    #
# --------------------------------------------------------------------- #

def test_riskcfg_defaults_to_equal_bucket_equity_slots():
    cfg = RiskCfg()
    assert cfg.sizing_mode == "equal_bucket"
    assert cfg.bucket_slots == "equity"
    assert cfg.bucket_safety_margin == 0.95


@pytest.mark.parametrize("value,expected", [
    ("auto", "auto"),
    ("equity", "equity"),
    ("AUTO", "auto"),            # case-insensitive
    ("  equity  ", "equity"),    # stripped
    ("4", "4"),                  # int override
    (7, "7"),                    # tolerates ints passed as ints
])
def test_bucket_slots_normalises(value, expected):
    assert RiskCfg(bucket_slots=value).bucket_slots == expected


@pytest.mark.parametrize("bad", ["0", "-1", "nonsense"])
def test_bucket_slots_rejects_invalid(bad):
    with pytest.raises(Exception):
        RiskCfg(bucket_slots=bad)


def test_bucket_safety_margin_bounds():
    RiskCfg(bucket_safety_margin=0.95)   # ok
    RiskCfg(bucket_safety_margin=1.0)    # ok (inclusive)
    with pytest.raises(Exception):
        RiskCfg(bucket_safety_margin=1.5)
    with pytest.raises(Exception):
        RiskCfg(bucket_safety_margin=0.3)


@pytest.mark.parametrize("slots,max_eq,max_fno,expected", [
    ("auto", 3, 2, 5),
    ("equity", 3, 2, 3),
    ("4", 3, 2, 4),
    ("auto", 1, 0, 1),   # degenerate safe clamp
])
def test_resolve_bucket_slots(slots, max_eq, max_fno, expected):
    cfg = RiskCfg(
        bucket_slots=slots, max_equity_positions=max_eq, max_fno_positions=max_fno,
    )
    assert cfg.resolve_bucket_slots() == expected


# --------------------------------------------------------------------- #
# _resolve_max_notional — scan-loop integration                         #
# --------------------------------------------------------------------- #

def _fake_ctx(settings: Settings):
    ctx = MagicMock()
    ctx.settings = settings
    return ctx


def _settings_with(tmp_path: Path, overrides: dict) -> Settings:
    cfg = tmp_path / "config.yaml"
    text = CONFIG_YAML_TEMPLATE
    for k, v in overrides.items():
        text = text.replace(f"{k}: equity", f"{k}: {v}") if k == "bucket_slots" else text
        text = text.replace(f"{k}: equal_bucket", f"{k}: {v}") if k == "sizing_mode" else text
    cfg.write_text(text)
    return Settings.load(cfg)


def test_cash_aware_mode_uses_available_cash_only(tmp_path: Path):
    s = _settings_with(tmp_path, {"sizing_mode": "cash_aware"})
    ctx = _fake_ctx(s)
    funds = {"equity": 500_000, "available": 400_000}
    # cash_aware: 95% of cash
    assert _resolve_max_notional(ctx, funds) == pytest.approx(400_000 * 0.95)


def test_equal_bucket_equity_mode(tmp_path: Path):
    """Default scenario: 3 equity slots, 500k capital → ~158k per slot."""
    s = _settings_with(tmp_path, {})   # defaults: equal_bucket + equity slots
    ctx = _fake_ctx(s)
    funds = {"equity": 500_000, "available": 500_000}
    expected_bucket = 500_000 / 3 * 0.95     # 158,333
    assert _resolve_max_notional(ctx, funds) == pytest.approx(expected_bucket, rel=1e-6)


def test_equal_bucket_auto_mode(tmp_path: Path):
    """auto = 3 equity + 2 F&O = 5 slots → 95k per slot."""
    s = _settings_with(tmp_path, {"bucket_slots": "auto"})
    ctx = _fake_ctx(s)
    funds = {"equity": 500_000, "available": 500_000}
    expected = 500_000 / 5 * 0.95    # 95,000
    assert _resolve_max_notional(ctx, funds) == pytest.approx(expected, rel=1e-6)


def test_equal_bucket_caps_at_available_cash(tmp_path: Path):
    """Bucket size 158k but only 50k cash left → cap falls back to 95% of cash."""
    s = _settings_with(tmp_path, {})
    ctx = _fake_ctx(s)
    funds = {"equity": 500_000, "available": 50_000}
    # min(158333, 50000 * 0.95) = 47500
    assert _resolve_max_notional(ctx, funds) == pytest.approx(47_500, rel=1e-6)


def test_equal_bucket_fixed_integer_slots(tmp_path: Path):
    """User overrides to 4 slots explicitly."""
    s = _settings_with(tmp_path, {"bucket_slots": "4"})
    ctx = _fake_ctx(s)
    funds = {"equity": 500_000, "available": 500_000}
    expected = 500_000 / 4 * 0.95     # 118,750
    assert _resolve_max_notional(ctx, funds) == pytest.approx(expected, rel=1e-6)
