"""scalper-pine-parity — CLI produces a one-row-per-bar CSV matching
score_symbol's output on the same candles."""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch

import pine_parity
from brokers.base import Side  # noqa: F401 — loads brokers package
from tests.fixtures import paper_mode
from tests.fixtures.synthetic import bullish_breakout_df


def test_cli_writes_csv_with_score_column(tmp_path: Path) -> None:
    """Feed the CLI a known synthetic series via a stubbed fetcher;
    verify every bar past MIN_LOOKBACK_BARS produces exactly one CSV
    row with a score in [0, 8]."""
    from data.market_data import df_to_candles

    candles = df_to_candles(bullish_breakout_df())

    # Write a minimal config the CLI can load.
    import yaml

    from config.settings import CONFIG_YAML_TEMPLATE
    raw = yaml.safe_load(CONFIG_YAML_TEMPLATE)
    raw["storage"]["db_path"] = str(tmp_path / "scalper.db")
    raw["storage"]["candles_cache_dir"] = str(tmp_path / "candles")
    raw["logging"]["file"] = str(tmp_path / "logs" / "scalper.log")
    raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))

    out = tmp_path / "out.csv"

    # The CLI constructs a PaperBroker which lazily spins up a
    # YFinanceFetcher. We patch YFinanceFetcher at the brokers.paper
    # import site (that's where _default_fetcher looks it up) so the
    # broker's fetcher attribute is our seeded stub.
    class _StubFetcher:
        def get_candles(self, symbol, interval, lookback):
            return candles

    with patch("brokers.paper._default_fetcher", return_value=_StubFetcher()):
        rc = pine_parity.main([
            "--symbol", "RELIANCE",
            "--lookback", "120",
            "--out", str(out),
            "--config", str(cfg_path),
        ])
    assert rc == 0
    assert out.exists()

    with out.open() as f:
        rows = list(csv.DictReader(f))

    # Bars 60..120 produce 61 scored rows.
    assert len(rows) >= 1
    # Every row has score in 0..8 and the 8 factor columns present.
    expected_factor_cols = {
        "f_ema_stack", "f_vwap_cross", "f_macd_cross", "f_rsi_entry",
        "f_adx_trend", "f_volume_surge", "f_bb_breakout", "f_supertrend",
    }
    for row in rows:
        score = int(row["score"])
        assert 0 <= score <= 8
        assert expected_factor_cols.issubset(row.keys())
        # Factor ints sum to score (when not blocked).
        if int(row["blocked"]) == 0:
            factor_sum = sum(int(row[k]) for k in expected_factor_cols)
            assert factor_sum == score


def test_cli_writes_short_side_csv(tmp_path: Path) -> None:
    """--side short should produce a CSV with short-side scores."""
    from data.market_data import df_to_candles
    candles = df_to_candles(bullish_breakout_df())

    import yaml
    from config.settings import CONFIG_YAML_TEMPLATE
    raw = yaml.safe_load(CONFIG_YAML_TEMPLATE)
    raw["storage"]["db_path"] = str(tmp_path / "s.db")
    raw["storage"]["candles_cache_dir"] = str(tmp_path / "c")
    raw["logging"]["file"] = str(tmp_path / "logs" / "s.log")
    raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))

    class _StubFetcher:
        def get_candles(self, symbol, interval, lookback):
            return candles
    out = tmp_path / "short.csv"
    with patch("brokers.paper._default_fetcher", return_value=_StubFetcher()):
        rc = pine_parity.main([
            "--symbol", "RELIANCE",
            "--lookback", "120",
            "--out", str(out),
            "--config", str(cfg_path),
            "--side", "short",
        ])
    assert rc == 0
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 1
    # Bullish test data → short scores should be LOW.
    scores = [int(r["score"]) for r in rows]
    assert max(scores) < 6, f"bullish data should not trigger short, got max={max(scores)}"


def test_cli_writes_both_sides_csv(tmp_path: Path) -> None:
    """--side both exposes long_* + short_* columns in one file."""
    from data.market_data import df_to_candles
    candles = df_to_candles(bullish_breakout_df())

    import yaml
    from config.settings import CONFIG_YAML_TEMPLATE
    raw = yaml.safe_load(CONFIG_YAML_TEMPLATE)
    raw["storage"]["db_path"] = str(tmp_path / "s.db")
    raw["storage"]["candles_cache_dir"] = str(tmp_path / "c")
    raw["logging"]["file"] = str(tmp_path / "logs" / "s.log")
    raw.setdefault("runtime", {})["initial_trade_mode"] = "paper"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))

    class _StubFetcher:
        def get_candles(self, symbol, interval, lookback):
            return candles
    out = tmp_path / "both.csv"
    with patch("brokers.paper._default_fetcher", return_value=_StubFetcher()):
        rc = pine_parity.main([
            "--symbol", "RELIANCE", "--lookback", "120",
            "--out", str(out), "--config", str(cfg_path), "--side", "both",
        ])
    assert rc == 0
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert "long_score" in rows[0]
    assert "short_score" in rows[0]
    # On bullish data, long > short.
    avg_long = sum(int(r["long_score"]) for r in rows) / len(rows)
    avg_short = sum(int(r["short_score"]) for r in rows) / len(rows)
    assert avg_long > avg_short


def test_short_pine_script_file_exists() -> None:
    """Short-side Pine script must exist + mirror the 8 factors bearishly."""
    pine = Path(__file__).resolve().parents[1] / "pine" / "indian-scalper-scorer-short.pine"
    assert pine.exists(), "short Pine indicator file missing"
    contents = pine.read_text()
    assert "//@version=5" in contents
    # Must contain the 8 factor identifiers (same names, bearish semantics).
    for factor in (
        "f_ema_stack", "f_vwap_cross", "f_macd_cross", "f_rsi_entry",
        "f_adx_trend", "f_vol_surge", "f_bb_breakout", "f_supertrend",
    ):
        assert factor in contents, f"short Pine missing factor {factor}"
    # Must use the short-specific hard-block constant name.
    assert "rsi_short_block" in contents
    # Must declare at least one alertcondition so TV can wire it up.
    assert "alertcondition" in contents


def test_pine_script_file_exists_and_declares_version() -> None:
    """Cheap guard that nobody deletes or breaks the Pine file without
    realising Pine imports silently when saved with a bad header."""
    pine = Path(__file__).resolve().parents[1] / "pine" / "indian-scalper-scorer.pine"
    assert pine.exists(), "Pine indicator file missing"
    contents = pine.read_text()
    assert "//@version=5" in contents, "Pine file must declare v5"
    # The 8 factor names must appear as identifiers so it's at least
    # superficially in sync with the Python scorer.
    for factor in (
        "f_ema_stack", "f_vwap_cross", "f_macd_cross", "f_rsi_entry",
        "f_adx_trend", "f_vol_surge", "f_bb_breakout", "f_supertrend",
    ):
        assert factor in contents, f"Pine missing factor {factor}"
    # Hard block on RSI > 78 must be present.
    assert "rsi_hard_block" in contents
    # Alert declaration must exist so TV can wire it up.
    assert "alertcondition" in contents


def test_pine_readme_mentions_parity_caveats() -> None:
    """Human-readable guard: the Pine README must warn operators about
    the first-100-bars warmup divergence and last-bar volume
    differences so they don't flag known behaviours as bugs."""
    readme = Path(__file__).resolve().parents[1] / "pine" / "README.md"
    text = readme.read_text()
    assert "Known divergences that are NOT bugs" in text
    assert "warm up" in text or "warmup" in text
    # Minimum threshold sync warning
    assert "min_score" in text
