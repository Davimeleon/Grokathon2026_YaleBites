"""AuthManager probe outcomes, expiry detectors, count collapse and the ``demo`` CLI."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from dataclasses import replace

import httpx
import pytest

from app.auth import (
    AuthManager,
    AuthStatus,
    count_collapsed,
    is_cas_redirect,
    looks_like_login_page,
    main,
)
from app.config import Secret
from app.fetch import redact
from app.fetch.client import FetchClient
from app.fetch.redact import SecretRedactor
from app.fetch.replay import ReplayTransport
from app.sources import GRAPHQL_URL
from tests.conftest import DUMMY_COOKIE, PERSONAL_FEED_URL, read_fixture_json, read_fixture_text


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    yield
    redact.clear_secrets()
    root = logging.getLogger()
    for flt in [f for f in root.filters if isinstance(f, SecretRedactor)]:
        root.removeFilter(flt)
    for logger in [root, *[lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger)]]:
        for handler in logger.handlers:
            for flt in [f for f in handler.filters if isinstance(f, SecretRedactor)]:
                handler.removeFilter(flt)


def _settings_with_cookie(auth_settings, cookie: str):
    return replace(auth_settings, yaleconnect_cookie=Secret(cookie))


async def _check(settings, session_factory, transport) -> tuple[AuthStatus, AuthManager]:
    fc = FetchClient(settings, session_factory, transport=transport, sleep=_noop_sleep)
    try:
        manager = AuthManager(settings)
        return await manager.check(fc), manager
    finally:
        await fc.aclose()


async def test_check_valid_with_fixture_cookie(auth_settings, session_factory):
    status, _ = await _check(auth_settings, session_factory, ReplayTransport())
    assert status is AuthStatus.VALID


async def test_check_none_without_cookie(tmp_settings, session_factory):
    replay = ReplayTransport()
    status, _ = await _check(tmp_settings, session_factory, replay)
    assert status is AuthStatus.NONE and replay.requests == []


async def test_check_expired_logs_one_warning_without_value(auth_settings, session_factory, caplog):
    settings = _settings_with_cookie(auth_settings, "CG.SessionID=STALE-SESSION-0000aaaa1111")
    fc = FetchClient(settings, session_factory, transport=ReplayTransport(), sleep=_noop_sleep)
    manager = AuthManager(settings)
    try:
        with caplog.at_level(logging.WARNING, logger="app.auth"):
            assert await manager.check(fc) is AuthStatus.EXPIRED
            assert await manager.check(fc) is AuthStatus.EXPIRED
    finally:
        await fc.aclose()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "app.auth"]
    assert len(warnings) == 1
    assert "YALECONNECT_COOKIE" in warnings[0].getMessage() and "anonymous" in warnings[0].getMessage()
    assert "STALE-SESSION" not in caplog.text


async def test_check_expired_on_cas_redirect(auth_settings, session_factory):
    cas = read_fixture_json("auth/cas_redirect.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(cas["status"], headers=cas["headers"])

    status, _ = await _check(auth_settings, session_factory, httpx.MockTransport(handler))
    assert status is AuthStatus.EXPIRED


async def test_check_expired_on_login_page_body(auth_settings, session_factory):
    page = read_fixture_text("auth/cas_login_page.html")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=page.encode())

    status, _ = await _check(auth_settings, session_factory, httpx.MockTransport(handler))
    assert status is AuthStatus.EXPIRED


async def test_check_expired_on_server_error(auth_settings, session_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404) if request.url.path == "/robots.txt" else httpx.Response(500)

    status, _ = await _check(auth_settings, session_factory, httpx.MockTransport(handler))
    assert status is AuthStatus.EXPIRED


def test_detectors_on_fixtures():
    cas = read_fixture_json("auth/cas_redirect.json")
    assert is_cas_redirect(cas["status"], cas["headers"])
    assert is_cas_redirect(302, {"Location": "https://yaleconnect.yale.edu/login?x=1"})
    assert not is_cas_redirect(302, {"location": "https://static-prod-us-east-1.campusgroups.com:443/ical/yale/x.ics"})
    assert not is_cas_redirect(200, cas["headers"])
    assert not is_cas_redirect(302, {})
    assert looks_like_login_page(read_fixture_text("auth/cas_login_page.html"))
    assert looks_like_login_page(read_fixture_text("auth/cas_login_page.html").encode())
    assert not looks_like_login_page(read_fixture_text("graphql/viewer_anonymous.json"))
    assert not looks_like_login_page(None)


def test_count_collapse():
    collapsed = len(read_fixture_json("auth/list_upcoming14d_auth_collapsed.json")["data"]["events"]["edges"])
    assert count_collapsed(collapsed, 371, None)
    assert count_collapsed(379, 371, 900)
    assert not count_collapsed(379, 371, 379)
    assert not count_collapsed(379, 371, None)
    assert not count_collapsed(371, 371, 700)


def test_cookie_header_scope(auth_settings, tmp_settings):
    manager = AuthManager(auth_settings)
    assert manager.has_cookie and len(manager.ics_feeds) == 1
    assert manager.ics_feeds[0].reveal() == PERSONAL_FEED_URL
    assert manager.cookie_header_for(GRAPHQL_URL) == {"Cookie": DUMMY_COOKIE}
    assert manager.cookie_header_for("http://yaleconnect.yale.edu/api/v2/graphql") == {}
    assert manager.cookie_header_for("https://static-prod-us-east-1.campusgroups.com/ical/yale/x.ics") == {}
    assert manager.cookie_header_for("https://secure.its.yale.edu/cas/login") == {}
    assert manager.cookie_header_for("https://yaleconnect.yale.edu.evil.example/") == {}
    assert AuthManager(tmp_settings).cookie_header_for(GRAPHQL_URL) == {}


def test_demo_without_env_exits_2_with_guidance(tmp_path):
    out = io.StringIO()
    code = main(["demo"], environ={"YALE_BITES_VAR_DIR": str(tmp_path)}, out=out)
    assert code == 2
    text = out.getvalue()
    assert "YALECONNECT_COOKIE is not set" in text and "DevTools" in text


def test_demo_valid_cookie_runs_seam(tmp_path):
    out = io.StringIO()
    calls = []

    async def on_valid(settings, fc, auth) -> int:
        calls.append(auth.status)
        return 0

    env = {"YALE_BITES_VAR_DIR": str(tmp_path), "YALECONNECT_COOKIE": DUMMY_COOKIE}
    code = main(["demo"], environ=env, transport=ReplayTransport(), on_valid=on_valid, out=out)
    assert code == 0 and calls == [AuthStatus.VALID]
    assert "is valid" in out.getvalue() and DUMMY_COOKIE not in out.getvalue()


def test_demo_expired_cookie_exits_3(tmp_path):
    out = io.StringIO()
    env = {"YALE_BITES_VAR_DIR": str(tmp_path), "YALECONNECT_COOKIE": "CG.SessionID=EXPIRED-0000aaaa1111"}
    code = main(["demo"], environ=env, transport=ReplayTransport(), out=out)
    assert code == 3
    assert "rejected" in out.getvalue() and "EXPIRED-0000aaaa1111" not in out.getvalue()


def test_demo_feeds_only_reports_and_runs_seam(tmp_path):
    out = io.StringIO()

    async def on_valid(settings, fc, auth) -> int:
        return 0 if auth.status is AuthStatus.NONE else 1

    env = {"YALE_BITES_VAR_DIR": str(tmp_path), "YALECONNECT_ICS_FEEDS": PERSONAL_FEED_URL}
    code = main(["demo"], environ=env, transport=ReplayTransport(), on_valid=on_valid, out=out)
    assert code == 0
    assert "1 personal ICS feed" in out.getvalue() and PERSONAL_FEED_URL not in out.getvalue()
