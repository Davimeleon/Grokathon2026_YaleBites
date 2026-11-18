"""Authenticated mode: cookie scoping, session probe, expiry detection and the ``demo`` CLI.

``python -m app.auth demo`` checks the configured YALECONNECT_COOKIE in one request. The
valid-session path runs one full sync (``app.sync.demo_full_sync``) and prints anonymous vs
authenticated counts; tests can inject a different ``on_valid`` coroutine.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import TextIO

import httpx

from app.config import Secret, Settings, load_settings
from app.db.session import init_db, make_engine, make_session_factory
from app.fetch.client import FetchClient, cookie_allowed
from app.fetch.graphql import error_codes, probe_session, viewer_present
from app.fetch.redact import install_redaction
from app.fetch.signals import is_cas_redirect, looks_like_login_page
from app.schemas import AuthStatus
from app.sources import AUTH_NOT_AUTHENTICATED

__all__ = [
    "AuthManager",
    "AuthStatus",
    "count_collapsed",
    "is_cas_redirect",
    "looks_like_login_page",
    "main",
]

log = logging.getLogger(__name__)

COOKIE_ENV = "YALECONNECT_COOKIE"
EXIT_OK, EXIT_NOT_CONFIGURED, EXIT_EXPIRED = 0, 2, 3

HOW_TO_GET_COOKIE = (
    f"{COOKIE_ENV} is not set: authenticated mode is off and the app runs anonymously.\n"
    "To enable it:\n"
    "  1. Sign in to https://yaleconnect.yale.edu in your browser.\n"
    "  2. Open DevTools (F12) > Network, reload the page and click any request to yaleconnect.yale.edu.\n"
    "  3. Under Request Headers, copy the whole value of the 'Cookie' header.\n"
    f"  4. export {COOKIE_ENV}='<that value>' and run this command again.\n"
    "Optionally set YALECONNECT_ICS_FEEDS to your personal ICS feed URL (comma-separated for several)."
)
EXPIRED_GUIDANCE = (
    f"{COOKIE_ENV} was rejected (session expired or not accepted by the API). "
    "Copy a fresh Cookie header from DevTools after signing in again. "
    "Until then every sync runs anonymously."
)


def count_collapsed(auth_count: int, anon_count: int, previous_auth_count: int | None) -> bool:
    """Authenticated pass returned fewer events than anonymous, or under half of its previous run."""
    if auth_count < anon_count:
        return True
    return bool(previous_auth_count) and auth_count < 0.5 * previous_auth_count


class AuthManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.status = AuthStatus.NONE if not settings.yaleconnect_cookie else AuthStatus.VALID
        self._warned = False

    @property
    def has_cookie(self) -> bool:
        return bool(self.settings.yaleconnect_cookie)

    @property
    def ics_feeds(self) -> list[Secret]:
        return list(self.settings.yaleconnect_ics_feeds)

    def cookie_header_for(self, url: str) -> dict[str, str]:
        """``{"Cookie": ...}`` for https://yaleconnect.yale.edu URLs only, else ``{}``."""
        if not self.has_cookie or not cookie_allowed(httpx.URL(url)):
            return {}
        return {"Cookie": self.settings.yaleconnect_cookie.reveal()}

    def mark_expired(self, reason: str) -> AuthStatus:
        """Record expiry and log a single warning (per manager) naming the env var, never its value."""
        self.status = AuthStatus.EXPIRED
        if not self._warned:
            self._warned = True
            log.warning("%s looks expired (%s); this run falls back to anonymous mode.", COOKIE_ENV, reason)
        return self.status

    async def check(self, fc: FetchClient) -> AuthStatus:
        """Probe the session once. No retries beyond the fetch layer's transport backoff."""
        if not self.has_cookie:
            self.status = AuthStatus.NONE
            return self.status
        result = await probe_session(fc)
        if result.status == "auth_redirect":
            return self.mark_expired("redirected to sign-in")
        if result.status == "ok" and looks_like_login_page(result.body):
            return self.mark_expired("login page returned")
        if result.status == "ok" and viewer_present(result.body):
            self.status = AuthStatus.VALID
            self._warned = False  # a later expiry deserves a fresh warning
            return self.status
        if AUTH_NOT_AUTHENTICATED in error_codes(result.body):
            return self.mark_expired(AUTH_NOT_AUTHENTICATED)
        return self.mark_expired(f"probe {result.status}")


OnValid = Callable[[Settings, FetchClient, AuthManager], Awaitable[int]]


async def _demo(settings: Settings, out: TextIO, transport: httpx.AsyncBaseTransport | None,
                on_valid: OnValid | None) -> int:
    engine = make_engine(settings.db_path)
    init_db(engine)
    fc = FetchClient(settings, make_session_factory(engine), transport=transport)
    try:
        auth = AuthManager(settings)
        if not auth.has_cookie:
            print(HOW_TO_GET_COOKIE, file=out)
            print(f"{len(auth.ics_feeds)} personal ICS feed(s) configured; they work without the cookie.",
                  file=out)
            return await on_valid(settings, fc, auth) if on_valid else EXIT_OK
        status = await auth.check(fc)
        if status is AuthStatus.EXPIRED:
            print(EXPIRED_GUIDANCE, file=out)
            return EXIT_EXPIRED
        print(f"{COOKIE_ENV} is valid: the GraphQL API accepted the session (viewer probe answered).", file=out)
        return await on_valid(settings, fc, auth) if on_valid else EXIT_OK
    finally:
        await fc.aclose()
        engine.dispose()


def main(argv: list[str] | None = None, *, environ: dict[str, str] | None = None,
         transport: httpx.AsyncBaseTransport | None = None, on_valid: OnValid | None = None,
         out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth")
    parser.add_argument("command", choices=["demo"])
    parser.parse_args(argv)
    stream = out or sys.stdout
    settings = load_settings(environ)
    if not settings.auth_configured:
        print(HOW_TO_GET_COOKIE, file=stream)
        return EXIT_NOT_CONFIGURED
    install_redaction(settings)
    if on_valid is None:
        from app.sync import demo_full_sync  # sync imports auth, so resolve the default lazily

        async def on_valid(s: Settings, fc: FetchClient, auth: AuthManager) -> int:
            return await demo_full_sync(s, fc, auth, out=stream)

    return asyncio.run(_demo(settings, stream, transport, on_valid))


if __name__ == "__main__":
    raise SystemExit(main())
