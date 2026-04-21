"""Back-compat re-export shim. The actual implementation now lives at
``src/backtest/fixtures.py`` so the preflight backtest-regression
check can import the generators from the installed wheel.
"""

from __future__ import annotations

from backtest.fixtures import (  # noqa: F401
    bullish_breakout_df,
    flat_chop_df,
    parabolic_df,
)
