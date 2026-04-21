"""Tests for scalper-upstox-auth — covers .env round-trip + token exchange."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from upstox_auth import _load_dotenv, _write_dotenv, exchange_code_for_token


def test_load_dotenv_parses_and_ignores_comments(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# Comment line\n"
        "UPSTOX_API_KEY=abc123\n"
        "UPSTOX_API_SECRET=def456\n"
        "\n"
        "  UPSTOX_ACCESS_TOKEN=xyz789\n"
        "NOT_A_KV_LINE\n"
    )
    out = _load_dotenv(env)
    assert out == {
        "UPSTOX_API_KEY": "abc123",
        "UPSTOX_API_SECRET": "def456",
        "UPSTOX_ACCESS_TOKEN": "xyz789",
    }


def test_write_dotenv_preserves_order_and_updates_token(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# Upstox creds\n"
        "UPSTOX_API_KEY=abc123\n"
        "UPSTOX_API_SECRET=def456\n"
        "UPSTOX_ACCESS_TOKEN=old_token\n"
        "LIVE_TRADING_ACKNOWLEDGED=no\n"
    )
    _write_dotenv(env, {"UPSTOX_ACCESS_TOKEN": "new_token"})
    text = env.read_text()
    assert "# Upstox creds" in text   # comment preserved
    assert "UPSTOX_API_KEY=abc123" in text  # untouched
    assert "UPSTOX_ACCESS_TOKEN=new_token" in text
    assert "UPSTOX_ACCESS_TOKEN=old_token" not in text


def test_write_dotenv_appends_new_key_if_missing(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("UPSTOX_API_KEY=abc123\n")
    _write_dotenv(env, {"UPSTOX_ACCESS_TOKEN": "new_token"})
    text = env.read_text()
    assert "UPSTOX_API_KEY=abc123" in text
    assert "UPSTOX_ACCESS_TOKEN=new_token" in text


def test_exchange_code_for_token_posts_expected_payload():
    """Mock httpx.post and verify we send the right OAuth fields."""
    import httpx

    captured: dict = {}

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"access_token": "tok_abc", "user_name": "Test User"}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["data"] = kwargs.get("data")
        return FakeResp()

    with patch.object(httpx, "post", side_effect=fake_post):
        result = exchange_code_for_token(
            code="the_code", api_key="key", api_secret="secret",
            redirect_uri="http://x/cb",
        )
    assert result["access_token"] == "tok_abc"
    assert captured["data"]["code"] == "the_code"
    assert captured["data"]["client_id"] == "key"
    assert captured["data"]["client_secret"] == "secret"
    assert captured["data"]["grant_type"] == "authorization_code"
