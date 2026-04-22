"""Tests for the web-based Upstox re-auth flow."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from dashboard.auth_routes import (
    _load_dotenv,
    _token_expiry,
    _write_dotenv,
    apply_fresh_token,
    register_routes,
)


# --------------------------------------------------------------------- #
# Helpers                                                               #
# --------------------------------------------------------------------- #

def _make_jwt(exp_seconds_from_now: int = 3600) -> str:
    """Build a minimal valid-looking Upstox JWT (not signed; we never verify)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_dict = {"sub": "user", "exp": int(time.time()) + exp_seconds_from_now}
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _make_app(tmp_path: Path, env_path: Path | None = None, fetcher=None):
    app = FastAPI()
    templates_dir = Path(__file__).parent.parent / "src" / "dashboard" / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))

    # Minimal fake state
    broker = SimpleNamespace(
        fetcher=fetcher,
        store=SimpleNamespace(append_operator_audit=lambda **kwargs: None),
    )
    state = SimpleNamespace(broker=broker)

    register_routes(app, state, templates, env_path=env_path or tmp_path / ".env")
    return app, state


# --------------------------------------------------------------------- #
# .env utilities                                                        #
# --------------------------------------------------------------------- #

def test_load_and_write_dotenv_roundtrip(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# top comment\n"
        "UPSTOX_API_KEY=abc\n"
        "UPSTOX_API_SECRET=xyz\n"
        "UPSTOX_ACCESS_TOKEN=old\n"
    )
    loaded = _load_dotenv(env)
    assert loaded["UPSTOX_ACCESS_TOKEN"] == "old"
    _write_dotenv(env, {"UPSTOX_ACCESS_TOKEN": "new"})
    text = env.read_text()
    assert "# top comment" in text
    assert "UPSTOX_API_KEY=abc" in text
    assert "UPSTOX_ACCESS_TOKEN=new" in text
    assert "UPSTOX_ACCESS_TOKEN=old" not in text


def test_write_dotenv_appends_missing_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("UPSTOX_API_KEY=abc\n")
    _write_dotenv(env, {"UPSTOX_ACCESS_TOKEN": "new"})
    assert "UPSTOX_ACCESS_TOKEN=new" in env.read_text()


# --------------------------------------------------------------------- #
# JWT exp decode                                                        #
# --------------------------------------------------------------------- #

def test_token_expiry_returns_epoch():
    token = _make_jwt(exp_seconds_from_now=3600)
    exp = _token_expiry(token)
    assert exp is not None
    assert abs(exp - (int(time.time()) + 3600)) < 5


def test_token_expiry_handles_garbage():
    assert _token_expiry("") is None
    assert _token_expiry("not.a.jwt") is None
    assert _token_expiry("only.two") is None


# --------------------------------------------------------------------- #
# apply_fresh_token — in-memory hot-swap                                #
# --------------------------------------------------------------------- #

def test_apply_fresh_token_updates_running_fetcher():
    fetcher = SimpleNamespace(
        access_token="old",
        _ltp_cache={"RELIANCE": (time.monotonic(), 1350.0)},
    )
    state = SimpleNamespace(broker=SimpleNamespace(fetcher=fetcher))
    apply_fresh_token(state, "new_token")
    assert fetcher.access_token == "new_token"
    assert fetcher._ltp_cache == {}


def test_apply_fresh_token_noop_when_no_fetcher():
    state = SimpleNamespace(broker=SimpleNamespace(fetcher=None))
    apply_fresh_token(state, "ignored")   # must not raise


# --------------------------------------------------------------------- #
# GET /auth/upstox                                                      #
# --------------------------------------------------------------------- #

def test_start_page_shows_missing_creds_warning(tmp_path):
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.get("/auth/upstox")
    assert r.status_code == 200
    assert "Missing credentials" in r.text


def test_start_page_with_creds_shows_login_button(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "UPSTOX_API_KEY=abc\n"
        "UPSTOX_API_SECRET=xyz\n"
        f"UPSTOX_ACCESS_TOKEN={_make_jwt(3600)}\n"
    )
    app, _ = _make_app(tmp_path, env_path=env)
    client = TestClient(app)
    r = client.get("/auth/upstox")
    assert r.status_code == 200
    assert "Log in with Upstox" in r.text
    assert "api.upstox.com/v2/login/authorization/dialog" in r.text


def test_auth_status_endpoint(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "UPSTOX_API_KEY=abc\nUPSTOX_API_SECRET=xyz\n"
        f"UPSTOX_ACCESS_TOKEN={_make_jwt(7200)}\n"
    )
    app, _ = _make_app(tmp_path, env_path=env)
    client = TestClient(app)
    r = client.get("/api/auth/upstox/status")
    assert r.status_code == 200
    d = r.json()
    assert d["present"] is True
    assert d["expired"] is False
    assert d["hours_left"] in {1, 2}


def test_auth_status_endpoint_missing_token(tmp_path):
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.get("/api/auth/upstox/status")
    d = r.json()
    assert d["present"] is False
    assert d["expired"] is True


# --------------------------------------------------------------------- #
# GET /auth/upstox/callback                                             #
# --------------------------------------------------------------------- #

def test_callback_rejects_missing_code(tmp_path):
    app, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.get("/auth/upstox/callback")
    assert r.status_code == 400
    assert "Missing ?code=" in r.text


def test_callback_happy_path_persists_and_hotswaps(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "UPSTOX_API_KEY=abc\nUPSTOX_API_SECRET=xyz\nUPSTOX_ACCESS_TOKEN=old\n"
    )
    fetcher = SimpleNamespace(
        access_token="old", _ltp_cache={"X": (time.monotonic(), 1.0)},
    )
    app, state = _make_app(tmp_path, env_path=env, fetcher=fetcher)
    client = TestClient(app)

    new_token = _make_jwt(3600)
    fake_response = {
        "access_token": new_token,
        "user_name": "Test User",
    }
    with patch("dashboard.auth_routes.exchange_code_for_token", return_value=fake_response):
        r = client.get("/auth/upstox/callback?code=abc123")

    assert r.status_code == 200
    assert "Authenticated" in r.text
    assert "Test User" in r.text
    # Hot-swap on the live fetcher
    assert fetcher.access_token == new_token
    assert fetcher._ltp_cache == {}
    # Persisted to .env
    assert f"UPSTOX_ACCESS_TOKEN={new_token}" in env.read_text()
