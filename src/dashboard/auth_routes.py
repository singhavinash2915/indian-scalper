"""Web-based Upstox re-authentication flow.

Tailnet-friendly replacement for ``scalper-upstox-auth``. The CLI
requires port 8080 to be free — impossible when the bot is already
running. This module exposes two routes on the same bot process:

    GET /auth/upstox            — start the OAuth dance (button + setup notice)
    GET /auth/upstox/callback   — receive the code, exchange, persist, continue

When the callback fires, we:
  1. POST to Upstox's token endpoint with the code + client_id/secret from .env
  2. Write the new access_token back to .env
  3. Mutate the running fetcher's ``access_token`` attribute in-memory so the
     next candle/LTP fetch uses the fresh token — **no process restart**
  4. Clear the fetcher's LTP cache so the dashboard's next 5-sec poll shows
     fresh prices
  5. Append an operator_audit row so the activity trail is complete
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

if TYPE_CHECKING:
    from dashboard.app import DashboardState


UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


# --------------------------------------------------------------------- #
# .env read/write helpers — small, dependency-free, idempotent          #
# --------------------------------------------------------------------- #

def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _write_dotenv(path: Path, updates: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                lines.append(raw)
                continue
            k = line.split("=", 1)[0].strip()
            if k in updates:
                lines.append(f"{k}={updates[k]}")
                seen.add(k)
            else:
                lines.append(raw)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


def _token_expiry(token: str) -> int | None:
    """Return the epoch-seconds ``exp`` claim from an Upstox JWT, or None
    if the token can't be parsed. Upstox tokens are standard JWTs —
    we only need to decode the payload (middle segment), no signature check."""
    if not token:
        return None
    try:
        import base64
        import json
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)   # pad base64
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


# --------------------------------------------------------------------- #
# Token exchange — mockable via direct import in tests                  #
# --------------------------------------------------------------------- #

def exchange_code_for_token(
    code: str, api_key: str, api_secret: str, redirect_uri: str,
) -> dict:
    import httpx
    r = httpx.post(
        UPSTOX_TOKEN_URL,
        headers={
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "code": code,
            "client_id": api_key,
            "client_secret": api_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def apply_fresh_token(state: "DashboardState", access_token: str) -> None:
    """Push the new token into the running bot without a restart.

    - Updates ``UpstoxFetcher.access_token`` on the live broker.
    - Clears the 1-sec LTP cache so the next dashboard poll hits fresh.
    """
    fetcher = getattr(state.broker, "fetcher", None)
    if fetcher is None:
        return
    if hasattr(fetcher, "access_token"):
        fetcher.access_token = access_token
    # Flush any cached LTPs so next poll uses new token.
    if hasattr(fetcher, "_ltp_cache"):
        try:
            fetcher._ltp_cache.clear()
        except Exception:
            pass


# --------------------------------------------------------------------- #
# Route registration                                                    #
# --------------------------------------------------------------------- #

def register_routes(
    app: FastAPI,
    state: "DashboardState",
    templates: Jinja2Templates,
    env_path: Path = Path(".env"),
) -> None:
    """Attach /auth/upstox + /auth/upstox/callback to the FastAPI app."""

    @app.get("/auth/upstox", response_class=HTMLResponse, name="auth_upstox_start")
    def start(request: Request) -> HTMLResponse:
        env = _load_dotenv(env_path)
        api_key = env.get("UPSTOX_API_KEY") or os.environ.get("UPSTOX_API_KEY", "")
        secret_present = bool(env.get("UPSTOX_API_SECRET") or os.environ.get("UPSTOX_API_SECRET"))

        # Compute the callback URL using whatever host the user hit.
        # That way tailnet (http://scalper:8080) and localhost both work
        # from the same deployed binary.
        callback_url = str(request.url_for("auth_upstox_callback"))

        current_token = env.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        exp_ts = _token_expiry(current_token)
        now = int(time.time())
        token_age = {
            "present": bool(current_token),
            "expires_in_hours": None if exp_ts is None else max(0, (exp_ts - now)) // 3600,
            "expired": (exp_ts is None or exp_ts < now) if current_token else True,
        }

        login_url = ""
        if api_key and secret_present:
            from urllib.parse import urlencode
            login_url = f"{UPSTOX_AUTH_URL}?" + urlencode({
                "client_id": api_key,
                "redirect_uri": callback_url,
                "response_type": "code",
            })

        return templates.TemplateResponse(
            request,
            "auth_upstox.html",
            {
                "banner": "UPSTOX AUTHENTICATION // WEB FLOW",
                "api_key_present": bool(api_key),
                "secret_present": secret_present,
                "callback_url": callback_url,
                "login_url": login_url,
                "token_age": token_age,
            },
        )

    @app.get("/auth/upstox/callback", response_class=HTMLResponse, name="auth_upstox_callback")
    def callback(request: Request, code: str | None = None, error: str | None = None) -> HTMLResponse:
        if error:
            raise HTTPException(400, f"Upstox OAuth error: {error}")
        if not code:
            raise HTTPException(400, "Missing ?code= in callback URL")

        env = _load_dotenv(env_path)
        api_key = env.get("UPSTOX_API_KEY") or os.environ.get("UPSTOX_API_KEY")
        api_secret = env.get("UPSTOX_API_SECRET") or os.environ.get("UPSTOX_API_SECRET")
        if not api_key or not api_secret:
            raise HTTPException(500, "UPSTOX_API_KEY / UPSTOX_API_SECRET missing in .env")

        # Rebuild redirect_uri exactly as Upstox saw it — required match.
        callback_url = str(request.url_for("auth_upstox_callback"))

        try:
            payload = exchange_code_for_token(code, api_key, api_secret, callback_url)
        except Exception as exc:
            raise HTTPException(502, f"token exchange failed: {exc}") from exc

        new_token = payload.get("access_token")
        if not new_token:
            raise HTTPException(502, f"no access_token in Upstox response: {payload}")

        # Persist + hot-swap on the running broker.
        _write_dotenv(env_path, {"UPSTOX_ACCESS_TOKEN": new_token})
        os.environ["UPSTOX_ACCESS_TOKEN"] = new_token   # picked up by fresh constructors
        apply_fresh_token(state, new_token)

        # Audit trail.
        try:
            state.broker.store.append_operator_audit(
                action="upstox_reauth",
                actor="web",
                detail=f"token refreshed · user={payload.get('user_name', 'unknown')}",
            )
        except Exception:
            pass

        exp_ts = _token_expiry(new_token)
        return templates.TemplateResponse(
            request,
            "auth_upstox_success.html",
            {
                "banner": "UPSTOX AUTHENTICATION // SUCCESS",
                "user_name": payload.get("user_name") or payload.get("email") or "unknown",
                "expires_in_hours": None if exp_ts is None else max(0, (exp_ts - int(time.time()))) // 3600,
            },
        )

    @app.get("/api/auth/upstox/status")
    def auth_status() -> dict:
        """Small JSON endpoint the dashboard polls to show token age."""
        env = _load_dotenv(env_path)
        token = env.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN", "")
        exp_ts = _token_expiry(token)
        now = int(time.time())
        if not token:
            return {"present": False, "expired": True, "seconds_left": 0}
        seconds_left = max(0, (exp_ts - now)) if exp_ts else 0
        return {
            "present": True,
            "expired": exp_ts is None or exp_ts < now,
            "seconds_left": seconds_left,
            "hours_left": seconds_left // 3600,
        }
