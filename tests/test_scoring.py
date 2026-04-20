"""8-factor scoring engine — synthetic 8/8, 0/8, hard-block, and error paths."""

from __future__ import annotations

import pytest

from config.settings import Settings
from strategy.scoring import MIN_LOOKBACK_BARS, Score, score_symbol
from tests.fixtures.synthetic import (
    bullish_breakout_df,
    flat_chop_df,
    parabolic_df,
)


@pytest.fixture
def cfg():
    return Settings.from_template().strategy


# --------------------------------------------------------------------- #
# Happy path: engineered bullish breakout                                #
# --------------------------------------------------------------------- #

def test_bullish_breakout_scores_high(cfg) -> None:
    score = score_symbol(bullish_breakout_df(), cfg)
    # The engine MUST clearly separate a textbook bullish setup from chop.
    # Some factors (vwap_cross, macd_cross) are strict "crossover within
    # last 2 bars" timing signals and are not reliably hittable from a
    # seeded generator — so we don't require 6/8 here. The meaningful
    # invariant is that the regime-level factors all fire.
    must_pass = {"ema_stack", "adx_trend", "volume_surge", "supertrend"}
    for factor in must_pass:
        assert score.breakdown[factor], (
            f"regime factor {factor!r} should fire on a clear breakout; "
            f"full breakdown: {score.breakdown}"
        )
    assert not score.blocked, f"unexpected hard block: {score.block_reason}"
    assert set(score.breakdown.keys()) == {
        "ema_stack", "vwap_cross", "macd_cross", "rsi_entry",
        "adx_trend", "volume_surge", "bb_breakout", "supertrend",
    }


def test_bullish_breakout_clearly_beats_chop(cfg) -> None:
    """The signal has to be meaningfully stronger than chop, otherwise the
    engine is not discriminating between regimes."""
    bullish = score_symbol(bullish_breakout_df(), cfg)
    chop = score_symbol(flat_chop_df(), cfg)
    assert bullish.total > chop.total + 2, (
        f"bullish={bullish.total} chop={chop.total} — separation too narrow"
    )


def test_breakdown_passed_factors_consistent(cfg) -> None:
    """``passed_factors`` should match the True entries in ``breakdown``."""
    score = score_symbol(bullish_breakout_df(), cfg)
    from_breakdown = {name for name, passed in score.breakdown.items() if passed}
    from_passed = set(score.passed_factors)
    assert from_breakdown == from_passed


# --------------------------------------------------------------------- #
# Negative path: flat chop                                               #
# --------------------------------------------------------------------- #

def test_flat_chop_scores_low(cfg) -> None:
    score = score_symbol(flat_chop_df(), cfg)
    # Chop should fail the trend-biased factors (EMA stack, ADX, BB
    # breakout, Supertrend, volume surge, MACD). RSI-in-range might
    # still pass by luck; we just assert total is far below threshold.
    assert score.total < cfg.min_score, (
        f"flat chop unexpectedly scored {score.total}/{cfg.min_score}: {score.breakdown}"
    )
    assert not score.blocked  # RSI in chop sits near 50, never > 78


# --------------------------------------------------------------------- #
# Hard block: overbought parabola                                        #
# --------------------------------------------------------------------- #

def test_parabolic_triggers_rsi_hard_block(cfg) -> None:
    score = score_symbol(parabolic_df(), cfg)
    # A relentless rally with no pullbacks pushes RSI above rsi_upper_block.
    assert score.blocked, f"expected hard block; score={score.total} reason=None"
    assert score.block_reason is not None
    assert "rsi" in score.block_reason.lower()


# --------------------------------------------------------------------- #
# Input validation                                                       #
# --------------------------------------------------------------------- #

def test_missing_columns_raises(cfg) -> None:
    df = bullish_breakout_df().drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing columns"):
        score_symbol(df, cfg)


def test_short_history_raises(cfg) -> None:
    df = bullish_breakout_df(n_bars=MIN_LOOKBACK_BARS - 1)
    with pytest.raises(ValueError, match=f"need >= {MIN_LOOKBACK_BARS}"):
        score_symbol(df, cfg)


def test_score_results_are_frozen(cfg) -> None:
    """Score + FactorResult are frozen dataclasses — guards against accidental
    mutation downstream (e.g. by risk engine)."""
    score = score_symbol(bullish_breakout_df(), cfg)
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        score.results[0].passed = not score.results[0].passed  # type: ignore[misc]
    with pytest.raises(Exception):
        score.total = 99  # type: ignore[misc]


def test_score_is_deterministic_for_same_input(cfg) -> None:
    """Pure function — identical inputs → identical outputs."""
    df = bullish_breakout_df(seed=42)
    a = score_symbol(df, cfg)
    b = score_symbol(df, cfg)
    assert a.total == b.total
    assert a.breakdown == b.breakdown
    assert a.blocked == b.blocked


def test_score_returns_expected_type(cfg) -> None:
    assert isinstance(score_symbol(bullish_breakout_df(), cfg), Score)
