"""Network-binding safety helpers.

Dashboard has no auth layer. Binding to 0.0.0.0 without a guard would
expose the kill switch to anyone on the same network. Two guards live
here:

    * ``SCALPER_TAILSCALE_ONLY=yes`` env var — caller asserts they
      want Tailscale-only binding. Still checked against
      ``tailscale status`` to confirm the interface is actually up.

    * ``resolve_bind_host()`` — the one place serve.py consults to
      decide what interface to bind. Returns 127.0.0.1 by default.
      Returns the Tailscale interface IP (100.x.x.x) when the env
      var is set AND Tailscale is running AND connected.

    * Never returns 0.0.0.0. If you need that (behind a proper auth
      proxy), set DASHBOARD_HOST=0.0.0.0 — that override is
      deliberately separate from the Tailscale gate so accidentally
      setting SCALPER_TAILSCALE_ONLY can't escalate to public
      binding.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from loguru import logger

TAILSCALE_ENV = "SCALPER_TAILSCALE_ONLY"
EXPLICIT_HOST_ENV = "DASHBOARD_HOST"


@dataclass(frozen=True)
class BindDecision:
    host: str
    reason: str
    tailscale_ip: str | None = None


def resolve_bind_host(default: str = "127.0.0.1") -> BindDecision:
    """Decide what interface serve.py should bind.

    Precedence:

    1. ``DASHBOARD_HOST`` env var (explicit override — bypasses every
       other check). Unit tests + Docker use this.
    2. ``SCALPER_TAILSCALE_ONLY=yes`` + Tailscale running + connected
       → bind to the detected Tailscale IPv4. If Tailscale isn't up,
       refuses (returns a decision that serve.py treats as a hard
       error via ``host == ""``).
    3. Default: ``127.0.0.1``.
    """
    explicit = os.environ.get(EXPLICIT_HOST_ENV, "").strip()
    if explicit:
        return BindDecision(
            host=explicit, reason=f"{EXPLICIT_HOST_ENV}={explicit}",
        )

    if os.environ.get(TAILSCALE_ENV, "").strip().lower() == "yes":
        ip = _detect_tailscale_ip()
        if ip is None:
            return BindDecision(
                host="",
                reason=(
                    f"{TAILSCALE_ENV}=yes but Tailscale is not up; refusing "
                    "to fall back to a public interface. Start Tailscale "
                    "(`tailscale up`) then re-launch, or unset the env var."
                ),
            )
        logger.info("Bind: Tailscale-only mode → {}", ip)
        return BindDecision(
            host=ip, reason=f"tailscale:{ip}", tailscale_ip=ip,
        )

    return BindDecision(host=default, reason="loopback (default)")


def _detect_tailscale_ip() -> str | None:
    """Return the node's first IPv4 Tailscale address, or None when
    Tailscale is not installed / not running / not connected.

    We use ``tailscale ip -4`` which exits 0 + prints one IP per line
    when connected, and exits non-zero otherwise. A missing binary
    throws FileNotFoundError we catch.
    """
    binary = shutil.which("tailscale")
    if binary is None:
        logger.warning("tailscale binary not found on PATH")
        return None
    try:
        result = subprocess.run(
            [binary, "ip", "-4"], capture_output=True, text=True, timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("tailscale probe failed: {}", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "tailscale ip -4 exited {} ({}); not connected?",
            result.returncode, result.stderr.strip(),
        )
        return None
    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not first_line:
        return None
    return first_line.strip()
