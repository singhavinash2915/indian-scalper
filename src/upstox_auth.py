"""scalper-upstox-auth — refresh the Upstox daily access token.

Upstox OAuth tokens expire every day at 03:30 IST. This helper runs the
full OAuth dance so you can start the bot cold each morning:

    uv run scalper-upstox-auth
    # → opens browser, listens on 127.0.0.1:8080/upstox/callback, captures
    #   the code, exchanges for an access_token, rewrites .env in place.

Requires UPSTOX_API_KEY + UPSTOX_API_SECRET in .env (never changes — only
the access_token rotates).
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx


REDIRECT_URI = "http://127.0.0.1:8080/upstox/callback"
UPSTOX_AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"


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


def _write_dotenv(path: Path, env: dict[str, str]) -> None:
    """Rewrite .env preserving comments + ordering; update only changed keys."""
    lines: list[str] = []
    seen_keys: set[str] = set()
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                lines.append(raw)
                continue
            k = line.split("=", 1)[0].strip()
            if k in env:
                lines.append(f"{k}={env[k]}")
                seen_keys.add(k)
            else:
                lines.append(raw)
    for k, v in env.items():
        if k not in seen_keys:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    """One-shot handler that records the ``code=`` query param then exits."""

    captured_code: str | None = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [None])[0]
        if code:
            type(self).captured_code = code
            body = b"<h2>Upstox login captured.</h2><p>You can close this tab.</p>"
        else:
            body = b"<h2>No code in callback.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence default stderr spam
        return


def _run_capture_server(port: int = 8080, timeout: float = 120.0) -> str:
    """Spin up a one-shot HTTP server that captures the OAuth redirect.

    Blocks until a ``code=...`` arrives or ``timeout`` elapses.
    """
    srv = http.server.HTTPServer(("127.0.0.1", port), _CaptureHandler)
    srv.socket.settimeout(0.5)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    import time
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if _CaptureHandler.captured_code:
                return _CaptureHandler.captured_code
            time.sleep(0.25)
        raise TimeoutError(f"OAuth callback not received within {timeout}s")
    finally:
        srv.shutdown()


def exchange_code_for_token(
    code: str, api_key: str, api_secret: str, redirect_uri: str = REDIRECT_URI,
) -> dict:
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scalper-upstox-auth", description=__doc__)
    ap.add_argument("--env", type=Path, default=Path(".env"),
                    help="path to .env (default: ./.env)")
    ap.add_argument("--no-browser", action="store_true",
                    help="print the login URL instead of auto-opening")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for OAuth callback (default 120)")
    args = ap.parse_args(argv)

    env = _load_dotenv(args.env)
    api_key = env.get("UPSTOX_API_KEY") or os.environ.get("UPSTOX_API_KEY")
    api_secret = env.get("UPSTOX_API_SECRET") or os.environ.get("UPSTOX_API_SECRET")
    if not api_key or not api_secret:
        print("error: UPSTOX_API_KEY + UPSTOX_API_SECRET missing (put them in .env)",
              file=sys.stderr)
        return 1

    params = {
        "client_id": api_key,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
    }
    login_url = f"{UPSTOX_AUTH_URL}?{urllib.parse.urlencode(params)}"

    if args.no_browser:
        print(f"Open this URL in your browser, log in, then come back:\n\n  {login_url}\n")
    else:
        print(f"Opening browser for Upstox login…\n  {login_url}\n")
        try:
            webbrowser.open(login_url)
        except Exception:
            print(f"Browser open failed — paste manually:\n  {login_url}")

    print(f"Listening on {REDIRECT_URI} for the callback (up to {args.timeout:.0f}s)…")
    code = _run_capture_server(timeout=args.timeout)
    print(f"Got code. Exchanging for access_token…")

    payload = exchange_code_for_token(code, api_key, api_secret)
    token = payload.get("access_token")
    if not token:
        print(f"error: token exchange failed — response: {json.dumps(payload)[:400]}",
              file=sys.stderr)
        return 1

    env["UPSTOX_ACCESS_TOKEN"] = token
    _write_dotenv(args.env, env)
    user = payload.get("user_name") or payload.get("email") or "unknown"
    print(f"✓ Access token written to {args.env} (user: {user})")
    print(f"  Expires at Upstox's 03:30 IST daily rollover — re-run before market open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
