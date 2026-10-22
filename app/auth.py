"""Single-password access gate.

Not a user system — one shared passphrase, a signed session cookie, and a throttle
on repeated failures. The network boundary (VPN/Tailscale) is still the primary
control; this is defence in depth.
"""

import hmac
import logging
import math
import os
import secrets
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from . import config
from .db import DB_PATH

log = logging.getLogger("networth.auth")

# Reachable without a session.
PUBLIC_PATHS = {"/login", "/healthz"}
PUBLIC_PREFIXES = ("/static/",)

# Eight free attempts, then one every COOLDOWN_SECONDS. A cooldown rather than a
# long lockout because every client may share one address (Docker's gateway, a
# userspace VPN): a lockout would shut the owner out along with whoever is
# guessing, while this caps a guesser at a similar rate and delays the owner by
# two minutes at most. The count resets after WINDOW_SECONDS with no failures.
MAX_ATTEMPTS = 8
COOLDOWN_SECONDS = 120
WINDOW_SECONDS = 900

_failures = {}  # ip -> (count, time of last failure)


def _client_ip(request):
    # The real connection address only. Nothing proxies this app, so a
    # X-Forwarded-For header is client-supplied and would let anyone dodge the
    # throttle by inventing a new address per attempt.
    return request.client.host if request.client else "unknown"


def is_throttled(ip):
    """Seconds until this address may try again; 0 when it may try now.

    A throttled attempt is refused without checking the password and doesn't
    count, so it can't extend the wait for anyone sharing the address.
    """
    rec = _failures.get(ip)
    if not rec:
        return 0
    count, last = rec
    since = time.time() - last
    if since > WINDOW_SECONDS:
        _failures.pop(ip, None)
        return 0
    if count < MAX_ATTEMPTS:
        return 0
    return max(0, math.ceil(COOLDOWN_SECONDS - since))


def record_failure(ip):
    count, last = _failures.get(ip, (0, 0.0))
    now = time.time()
    if now - last > WINDOW_SECONDS:
        count = 0
    _failures[ip] = (count + 1, now)
    log.warning("failed login from %s (%s in window)", ip, count + 1)


def clear_failures(ip):
    _failures.pop(ip, None)


def check_password(candidate: str) -> bool:
    if not config.APP_PASSWORD:
        return False
    # Constant-time compare so response timing doesn't leak the password.
    return hmac.compare_digest(candidate or "", config.APP_PASSWORD)


def session_secret() -> str:
    """Stable across restarts, so a redeploy doesn't sign everyone out."""
    if config.SECRET_KEY:
        return config.SECRET_KEY
    path = os.path.join(os.path.dirname(DB_PATH) or ".", ".session_secret")
    try:
        if os.path.exists(path):
            existing = open(path).read().strip()
            if existing:
                return existing
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            fh.write(key)
        os.chmod(path, 0o600)
    except OSError as exc:
        # Name the path and the reason — this is almost always the data volume being
        # owned by a different uid than the one `user:` runs as.
        log.warning(
            "could not persist session secret to %s (%s); running as uid %s:%s. "
            "Sessions will reset on every restart until the folder is writable.",
            path, exc, os.getuid(), os.getgid(),
        )
    return key


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not config.AUTH_ENABLED:
            return await call_next(request)

        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if request.session.get("auth"):
            return await call_next(request)

        # Inline edits are fetch() calls; a redirect would be swallowed silently
        # and look like the save just did nothing.
        accepts = request.headers.get("accept", "")
        if request.method != "GET" or "application/json" in accepts:
            return JSONResponse(
                {"ok": False, "error": "Session expired — reload the page and sign in again."},
                status_code=401,
            )

        nxt = request.url.path
        if request.url.query:
            nxt += "?" + request.url.query
        return RedirectResponse(f"/login?next={nxt}", status_code=303)
