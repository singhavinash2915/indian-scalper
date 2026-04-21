"""Short-TTL HMAC confirm tokens for state-changing dashboard endpoints.

The two-step pattern: the client first POSTs ``/api/.../prepare`` with
the intended target; the server returns a token and whatever context
the UI needs to show in a confirm modal (current state, warnings,
open-position count, etc.). The client then POSTs ``/api/.../apply``
with ``{target, token}``; the server verifies the token and executes.

Why not just use a CSRF cookie? We want a *per-action* token that:
  * expires quickly (30s default) so a user can't confirm a stale action,
  * binds to ``(action, target)`` so a token minted for "switch to paper"
    can't be replayed to "switch to live".

The secret is per-process (new `secrets.token_bytes(32)` on every
``ConfirmTokenRegistry()``). Dashboard restart invalidates outstanding
tokens — fine, they expire in 30s anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

DEFAULT_TTL_SECONDS = 30
_SIG_LEN = 16  # truncated hex — plenty for a single-session in-memory secret


class ConfirmTokenRegistry:
    def __init__(self, secret: bytes | None = None, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._secret = secret or secrets.token_bytes(32)
        self._ttl = ttl_seconds

    def issue(
        self,
        action: str,
        target: str,
        *,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> tuple[str, int]:
        """Mint a ``{exp}.{sig}`` token for a single ``(action, target)`` pair.
        Returns (token, expires_at_unix_seconds)."""
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        now_s = int(now if now is not None else time.time())
        exp = now_s + ttl
        sig = self._sign(action, target, exp)
        return f"{exp}.{sig}", exp

    def verify(
        self,
        action: str,
        target: str,
        token: str,
        *,
        now: float | None = None,
    ) -> bool:
        if not token:
            return False
        try:
            exp_str, sig = token.split(".", 1)
            exp = int(exp_str)
        except (ValueError, AttributeError):
            return False
        now_s = int(now if now is not None else time.time())
        if exp < now_s:
            return False
        expected = self._sign(action, target, exp)
        return hmac.compare_digest(sig, expected)

    def _sign(self, action: str, target: str, exp: int) -> str:
        msg = f"{action}:{target}:{exp}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:_SIG_LEN]
