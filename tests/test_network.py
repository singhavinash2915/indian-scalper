"""resolve_bind_host — precedence + Tailscale-up + Tailscale-down paths."""

from __future__ import annotations

from unittest.mock import patch

from network import EXPLICIT_HOST_ENV, TAILSCALE_ENV, resolve_bind_host


def test_default_is_loopback(monkeypatch) -> None:
    """No env vars set → 127.0.0.1."""
    monkeypatch.delenv(EXPLICIT_HOST_ENV, raising=False)
    monkeypatch.delenv(TAILSCALE_ENV, raising=False)
    decision = resolve_bind_host()
    assert decision.host == "127.0.0.1"
    assert "loopback" in decision.reason


def test_explicit_host_wins(monkeypatch) -> None:
    """DASHBOARD_HOST set → use it, bypassing every other check."""
    monkeypatch.setenv(EXPLICIT_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(TAILSCALE_ENV, "yes")  # should be IGNORED
    decision = resolve_bind_host()
    assert decision.host == "0.0.0.0"
    assert "DASHBOARD_HOST" in decision.reason


def test_tailscale_only_binds_to_tailnet_ip(monkeypatch) -> None:
    """SCALPER_TAILSCALE_ONLY=yes + tailscale up → bind to the
    tailnet IP."""
    monkeypatch.delenv(EXPLICIT_HOST_ENV, raising=False)
    monkeypatch.setenv(TAILSCALE_ENV, "yes")
    with patch("network._detect_tailscale_ip", return_value="100.101.102.103"):
        decision = resolve_bind_host()
    assert decision.host == "100.101.102.103"
    assert decision.tailscale_ip == "100.101.102.103"
    assert "tailscale" in decision.reason.lower()


def test_tailscale_only_refuses_when_tailnet_down(monkeypatch) -> None:
    """Env var asks for tailscale-only but tailscale is down → refuse
    to bind (host == ''). This is the safety guard that prevents
    accidental public exposure."""
    monkeypatch.delenv(EXPLICIT_HOST_ENV, raising=False)
    monkeypatch.setenv(TAILSCALE_ENV, "yes")
    with patch("network._detect_tailscale_ip", return_value=None):
        decision = resolve_bind_host()
    assert decision.host == ""
    assert "tailscale is not up" in decision.reason.lower()


def test_tailscale_env_var_case_insensitive(monkeypatch) -> None:
    monkeypatch.delenv(EXPLICIT_HOST_ENV, raising=False)
    monkeypatch.setenv(TAILSCALE_ENV, "YES")
    with patch("network._detect_tailscale_ip", return_value="100.1.2.3"):
        decision = resolve_bind_host()
    assert decision.host == "100.1.2.3"


def test_tailscale_env_var_anything_else_ignored(monkeypatch) -> None:
    """Only the literal 'yes' (case-insensitive) triggers Tailscale
    mode. 'true', '1', empty string, etc. → fall through to default."""
    monkeypatch.delenv(EXPLICIT_HOST_ENV, raising=False)
    for bad_value in ("true", "1", "", "on", "enabled"):
        monkeypatch.setenv(TAILSCALE_ENV, bad_value)
        decision = resolve_bind_host()
        assert decision.host == "127.0.0.1", f"got {decision.host!r} for {bad_value!r}"


def test_default_override_respected(monkeypatch) -> None:
    """The caller's default= parameter is used when nothing else wins."""
    monkeypatch.delenv(EXPLICIT_HOST_ENV, raising=False)
    monkeypatch.delenv(TAILSCALE_ENV, raising=False)
    decision = resolve_bind_host(default="192.168.1.5")
    assert decision.host == "192.168.1.5"
