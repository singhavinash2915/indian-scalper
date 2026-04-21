"""HMAC confirm-token registry — sign / verify / TTL / per-action binding."""

from __future__ import annotations

import pytest

from dashboard.confirm import ConfirmTokenRegistry


def test_issue_then_verify_roundtrip() -> None:
    reg = ConfirmTokenRegistry()
    token, exp = reg.issue("mode_change", "paper")
    assert reg.verify("mode_change", "paper", token) is True
    assert exp > 0


def test_tokens_bind_to_action_target() -> None:
    """A token minted for (mode_change, paper) must not verify against
    (mode_change, live) or (kill, paper)."""
    reg = ConfirmTokenRegistry()
    token, _ = reg.issue("mode_change", "paper")
    assert reg.verify("mode_change", "live", token) is False
    assert reg.verify("kill", "paper", token) is False


def test_expired_token_rejected() -> None:
    """A token whose embedded exp is in the past must not verify."""
    reg = ConfirmTokenRegistry(ttl_seconds=10)
    # Issue with a synthetic "now" 1000s in the past so exp < real now.
    token, _ = reg.issue("mode_change", "paper", now=0.0)
    assert reg.verify("mode_change", "paper", token) is False


def test_future_exp_verifies_even_if_secret_unchanged() -> None:
    reg = ConfirmTokenRegistry(ttl_seconds=30)
    token, exp = reg.issue("mode_change", "paper")
    # Verify at (exp - 1) — still inside the window.
    assert reg.verify("mode_change", "paper", token, now=float(exp - 1)) is True


def test_tampered_signature_rejected() -> None:
    reg = ConfirmTokenRegistry()
    token, _ = reg.issue("mode_change", "paper")
    exp, sig = token.split(".", 1)
    flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert reg.verify("mode_change", "paper", f"{exp}.{flipped}") is False


def test_malformed_token_rejected() -> None:
    reg = ConfirmTokenRegistry()
    assert reg.verify("mode_change", "paper", "") is False
    assert reg.verify("mode_change", "paper", "not-a-token") is False
    assert reg.verify("mode_change", "paper", "123.") is False
    assert reg.verify("mode_change", "paper", "abc.def") is False


def test_separate_registries_dont_accept_each_others_tokens() -> None:
    """Each ConfirmTokenRegistry has its own secret — tokens are not
    portable across them. This is why dashboard restart invalidates
    outstanding tokens."""
    a = ConfirmTokenRegistry()
    b = ConfirmTokenRegistry()
    token, _ = a.issue("mode_change", "paper")
    assert b.verify("mode_change", "paper", token) is False


def test_rejects_non_positive_ttl() -> None:
    reg = ConfirmTokenRegistry()
    with pytest.raises(ValueError):
        reg.issue("mode_change", "paper", ttl_seconds=0)
