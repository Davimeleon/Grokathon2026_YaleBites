"""FetchClient: politeness, backoff, robots, validators, unchanged detection, disk cache, cookie scope."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.config import load_settings
from app.db.models import HttpValidator
from app.fetch import redact
from app.fetch.client import FetchClient
from app.fetch.graphql import fetch_events_page, page_info, page_info_for, probe_session
from app.fetch.ics import fetch_ics
from app.fetch.replay import ReplayTransport
from app.sources import GRAPHQL_URL, ICS_SCHOOL_URL, LEGACY_LIST_ENDPOINT_DISALLOWED
from tests.conftest import DUMMY_COOKIE, DUMMY_ICS_TOKEN, PERSONAL_FEED_URL, read_fixture_json


class FakeTime:
    """Monotonic clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def monotonic(self) -> float:
        return self.t


def make_client(settings, session_factory, transport, fake: FakeTime | None = None, rng=lambda: 0.5):
    fake = fake or FakeTime()
    return FetchClient(settings, session_factory, transport=transport, sleep=fake.sleep, rng=rng,
                       monotonic=fake.monotonic)


def robots_404(request: httpx.Request) -> httpx.Response | None:
    return httpx.Response(404) if request.url.path == "/robots.txt" else None


async def test_concurrency_never_exceeds_four(tmp_settings, session_factory):
    state = {"in_flight": 0, "max": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if (r := robots_404(request)) is not None:
            return r
        state["in_flight"] += 1
        state["max"] = max(state["max"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["in_flight"] -= 1
        return httpx.Response(200, content=b"ok")

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler))
    try:
        results = await asyncio.gather(*(fc.request(f"https://h{i}.example/x") for i in range(12)))
    finally:
        await fc.aclose()
    assert all(r.status == "ok" for r in results)
    assert state["max"] == 4


async def test_per_host_spacing_at_least_250ms(tmp_settings, session_factory):
    fake = FakeTime()
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(fake.t)
        return robots_404(request) or httpx.Response(200, content=b"ok")

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler), fake)
    try:
        await asyncio.gather(*(fc.request(f"https://one.example/p{i}") for i in range(4)))
    finally:
        await fc.aclose()
    assert len(seen) == 5  # robots + 4 pages
    gaps = [b - a for a, b in zip(seen, seen[1:], strict=False)]
    assert all(gap >= 0.25 - 1e-9 for gap in gaps), gaps


async def test_backoff_honors_retry_after_then_succeeds(tmp_settings, session_factory):
    fake = FakeTime()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := robots_404(request)) is not None:
            return r
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if calls["n"] == 2:
            return httpx.Response(503, headers={"Retry-After": "120"})
        return httpx.Response(200, content=b"fine")

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler), fake)
    try:
        result = await fc.request("https://one.example/x")
    finally:
        await fc.aclose()
    assert result.status == "ok" and result.body == b"fine"
    assert 7 in fake.sleeps and 60.0 in fake.sleeps  # Retry-After honored, capped at 60 s
    assert calls["n"] == 3


async def test_backoff_attempts_bounded_with_jitter(tmp_settings, session_factory):
    fake = FakeTime()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := robots_404(request)) is not None:
            return r
        calls["n"] += 1
        return httpx.Response(503)

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler), fake, rng=lambda: 1.0)
    try:
        result = await fc.request("https://one.example/x")
    finally:
        await fc.aclose()
    assert result.status == "error" and result.http_status == 503
    assert calls["n"] == 5  # 1 + 4 retries
    assert fake.sleeps[-4:] == [0.75, 1.5, 3.0, 6.0]  # 0.5 * 2^i * (0.5 + 1.0)


async def test_transport_errors_retried_then_reported(tmp_settings, session_factory):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := robots_404(request)) is not None:
            return r
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler))
    try:
        result = await fc.request("https://one.example/x")
    finally:
        await fc.aclose()
    assert result.status == "error" and "ConnectError" in (result.error or "")
    assert calls["n"] == 5


async def test_robots_disallows_legacy_endpoint_and_never_requests_it(tmp_settings, session_factory):
    replay = ReplayTransport()
    fc = make_client(tmp_settings, session_factory, replay)
    try:
        result = await fc.request(LEGACY_LIST_ENDPOINT_DISALLOWED + "?range=0&limit=40")
    finally:
        await fc.aclose()
    assert result.status == "robots_disallowed"
    assert [r.url.path for r in replay.requests] == ["/robots.txt"]


async def test_robots_403_means_allow_and_5xx_means_disallow(tmp_settings, session_factory, frozen_now):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(403 if request.url.host == "allow.example" else 503)
        return httpx.Response(200, content=b"x")

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler))
    try:
        allowed = await fc.request("https://allow.example/a")
        denied = await fc.request("https://down.example/a")
        again = await fc.request("https://allow.example/b")
    finally:
        await fc.aclose()
    assert allowed.status == "ok" and again.status == "ok"
    assert denied.status == "robots_disallowed"


async def test_robots_fetched_once_per_host(tmp_settings, session_factory, frozen_now):
    replay = ReplayTransport()
    fc = make_client(tmp_settings, session_factory, replay)
    try:
        await probe_session(fc)
        await probe_session(fc)
    finally:
        await fc.aclose()
    assert [r.url.path for r in replay.requests].count("/robots.txt") == 1


class SpyStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.iterated = False

    async def __aiter__(self):
        self.iterated = True
        yield b"this body must never be read"


async def test_conditional_get_304_returns_stored_items_without_reading_body(
        tmp_settings, session_factory, frozen_now):
    spy = SpyStream()
    sent: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (r := robots_404(request)) is not None:
            return r
        sent.append(request.headers)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'}, stream=spy)
        return httpx.Response(200, headers={"ETag": '"v1"', "Last-Modified": "Tue, 17 Nov 2026 06:05:24 GMT",
                                            "Content-Type": "text/calendar"}, content=b"BEGIN:VCALENDAR")

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler))
    try:
        first = await fc.request("https://feed.example/cal.ics")
        unparsed = await fc.request("https://feed.example/cal.ics")
        fc.record_items(unparsed.request_key, ["u1", "u2"])
        second = await fc.request("https://feed.example/cal.ics")
    finally:
        await fc.aclose()
    assert first.status == "ok" and unparsed.status == "ok"
    assert "if-none-match" not in sent[1]  # validators are armed only after record_items
    assert sent[2]["if-none-match"] == '"v1"'
    assert sent[2]["if-modified-since"] == "Tue, 17 Nov 2026 06:05:24 GMT"
    assert second.status == "not_modified" and second.http_status == 304
    assert second.item_keys == ["u1", "u2"] and second.body is None
    assert spy.iterated is False


async def test_identical_graphql_body_is_unchanged(tmp_settings, session_factory, frozen_now, capture_time):
    fc = make_client(tmp_settings, session_factory, ReplayTransport())
    window = dict(start=capture_time, end=capture_time + timedelta(days=14), after=None,
                  authenticated=False, logical_key=None)
    try:
        first = await fetch_events_page(fc, **window)
        fc.record_items(first.request_key, ["https://yaleconnect.yale.edu/rsvp?id=1"])
        second = await fetch_events_page(fc, **window)
    finally:
        await fc.aclose()
    assert first.status == "ok" and first.body
    assert second.status == "unchanged" and second.body is None
    assert second.item_keys == ["https://yaleconnect.yale.edu/rsvp?id=1"]
    assert page_info_for(second) == page_info(first.body)
    assert page_info_for(second).has_next_page is True


async def test_logical_key_detects_unchanged_page_across_sliding_windows(tmp_settings, session_factory, frozen_now,
                                                                        capture_time):
    page = json.dumps(read_fixture_json("graphql/list_hot6h_p02.json")).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return robots_404(request) or httpx.Response(200, headers={"content-type": "application/json"}, content=page)

    fc = make_client(tmp_settings, session_factory, httpx.MockTransport(handler))
    try:
        results = []
        for minutes, key in ((0, "graphql:hot:anonymous:p1"), (3, "graphql:hot:anonymous:p1"), (6, None)):
            start = capture_time + timedelta(minutes=minutes)
            result = await fetch_events_page(fc, start=start, end=start + timedelta(hours=6), after=None,
                                             authenticated=False, logical_key=key)
            if result.status == "ok":
                fc.record_items(result.request_key, ["k"])
            results.append(result)
    finally:
        await fc.aclose()
    assert [r.status for r in results] == ["ok", "unchanged", "ok"]
    assert results[0].request_key == results[1].request_key != results[2].request_key


async def test_anonymous_and_authenticated_never_share_keys_or_cache(auth_settings, session_factory, frozen_now,
                                                                    capture_time):
    fc = make_client(auth_settings, session_factory, ReplayTransport())
    window = dict(start=capture_time, end=capture_time + timedelta(hours=6), after=None, logical_key=None)
    try:
        anon = await fetch_events_page(fc, authenticated=False, **window)
        authed = await fetch_events_page(fc, authenticated=True, **window)
    finally:
        await fc.aclose()
    assert anon.status == authed.status == "ok"
    assert anon.request_key != authed.request_key
    day = frozen_now.strftime("%Y-%m-%d")
    assert Path(anon.cache_path) == auth_settings.public_cache_dir / day / f"{anon.request_key}.json"
    assert Path(authed.cache_path) == auth_settings.auth_cache_dir / day / f"{authed.request_key}.json"
    with session_factory() as s:
        flags = {v.key: v.authenticated for v in s.scalars(select(HttpValidator))}
    assert flags[anon.request_key] is False and flags[authed.request_key] is True


async def test_personal_feed_token_never_in_filenames_or_validators(auth_settings, session_factory, frozen_now):
    replay = ReplayTransport()
    fc = make_client(auth_settings, session_factory, replay)
    try:
        result = await fetch_ics(fc, PERSONAL_FEED_URL, authenticated=True)
    finally:
        await fc.aclose()
    assert result.status == "ok" and b"BEGIN:VCALENDAR" in result.body
    assert DUMMY_ICS_TOKEN not in result.url_redacted
    files = [str(p) for p in auth_settings.cache_dir.rglob("*")]
    assert files and not any(DUMMY_ICS_TOKEN in f for f in files)
    assert not list(auth_settings.public_cache_dir.rglob("*.ics"))
    with session_factory() as s:
        rows = list(s.scalars(select(HttpValidator)))
    assert rows and all(DUMMY_ICS_TOKEN not in (r.url_redacted + (r.cache_path or "")) for r in rows)


async def test_cookie_only_sent_to_yaleconnect_and_ics_redirect_followed(auth_settings, session_factory, frozen_now):
    replay = ReplayTransport()
    fc = make_client(auth_settings, session_factory, replay)
    try:
        result = await fetch_ics(fc, ICS_SCHOOL_URL, authenticated=True)
        anon = await fc.request(GRAPHQL_URL, method="POST", json_body={"operationName": "SessionProbe",
                                                                       "query": "q", "variables": {}})
    finally:
        await fc.aclose()
    assert result.status == "ok" and result.body.count(b"BEGIN:VEVENT") == 5412
    assert "static-prod-us-east-1.campusgroups.com" in result.url_redacted
    with_cookie = [(r.url.host, r.url.path) for r in replay.requests if "cookie" in r.headers]
    assert with_cookie == [("yaleconnect.yale.edu", "/ical/yale/ical_yale.ics")]
    assert all(r.headers.get("cookie") in (None, DUMMY_COOKIE) for r in replay.requests)
    assert anon.status == "ok" and b"AUTH_NOT_AUTHENTICATED" in anon.body
    assert all(r.headers.get("user-agent") == auth_settings.user_agent for r in replay.requests)


async def test_ics_304_on_second_fetch_with_validators_for_final_url(tmp_settings, session_factory, frozen_now):
    replay = ReplayTransport()
    fc = make_client(tmp_settings, session_factory, replay)
    try:
        first = await fetch_ics(fc, ICS_SCHOOL_URL, authenticated=False)
        fc.record_items(first.request_key, ["https://yaleconnect.yale.edu/rsvp?id=7"])
        second = await fetch_ics(fc, ICS_SCHOOL_URL, authenticated=False)
    finally:
        await fc.aclose()
    assert second.status == "not_modified" and second.item_keys == ["https://yaleconnect.yale.edu/rsvp?id=7"]
    assert second.request_key == first.request_key
    with session_factory() as s:
        row = s.get(HttpValidator, first.request_key)
    assert row.etag == '"1ad58a33f0de6cd70bd0a12ba687bf2b"'
    assert row.url_redacted.startswith("https://static-prod-us-east-1.campusgroups.com")


@pytest.mark.parametrize("location", [
    "https://secure.its.yale.edu/cas/login?service=https%3A%2F%2Fyaleconnect.yale.edu%2Flogin_only",
    "https://yaleconnect.yale.edu/login?redirect=%2Fevents",
])
async def test_authenticated_redirect_to_sign_in_not_followed(auth_settings, session_factory, location):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return robots_404(request) or httpx.Response(302, headers={"location": location})

    fc = make_client(auth_settings, session_factory, httpx.MockTransport(handler))
    try:
        result = await fc.request("https://yaleconnect.yale.edu/ics_helper", authenticated=True)
    finally:
        await fc.aclose()
    assert result.status == "auth_redirect"
    assert all(r.url.host == "yaleconnect.yale.edu" for r in seen)
    assert len([r for r in seen if r.url.path != "/robots.txt"]) == 1


async def test_authenticated_login_page_body_is_auth_redirect(auth_settings, session_factory):
    page = (Path(__file__).parent / "data/fixtures/auth/cas_login_page.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return robots_404(request) or httpx.Response(200, headers={"content-type": "text/html"}, content=page)

    fc = make_client(auth_settings, session_factory, httpx.MockTransport(handler))
    try:
        result = await fc.request("https://yaleconnect.yale.edu/ics_helper", authenticated=True)
    finally:
        await fc.aclose()
    assert result.status == "auth_redirect"


async def test_server_set_cookie_never_leaks_to_anonymous_requests(auth_settings, session_factory):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if (r := robots_404(request)) is not None:
            return r
        return httpx.Response(200, headers={"Set-Cookie": "CG.SessionID=server-set; Path=/"}, content=b"{}")

    fc = make_client(auth_settings, session_factory, httpx.MockTransport(handler))
    try:
        await fc.request("https://yaleconnect.yale.edu/ics_helper", authenticated=True)
        await fc.request("https://yaleconnect.yale.edu/ics_helper")
    finally:
        await fc.aclose()
    pages = [r for r in seen if r.url.path != "/robots.txt"]
    assert pages[0].headers["cookie"] == DUMMY_COOKIE
    assert "cookie" not in pages[1].headers


async def test_pattern_proof_feed_token_redacted_via_registered_secret(tmp_path, session_factory):
    feed = "https://yaleconnect.yale.edu/ical/yale/feed_Ab12xY.ics"
    settings = load_settings({"FREEFOOD_VAR_DIR": str(tmp_path / "var"), "YALECONNECT_ICS_FEEDS": feed})

    def handler(request: httpx.Request) -> httpx.Response:
        return robots_404(request) or httpx.Response(500)

    fc = make_client(settings, session_factory, httpx.MockTransport(handler))
    try:
        result = await fetch_ics(fc, feed, authenticated=True)
    finally:
        await fc.aclose()
        redact.clear_secrets()
    assert result.status == "error"
    assert result.url_redacted.startswith("https://yaleconnect.yale.edu/")
    assert "Ab12xY" not in result.url_redacted and "Ab12xY" not in (result.error or "")


async def test_spacing_holds_when_requests_queue_for_a_slot(tmp_settings, session_factory):
    """With more requests than concurrency slots, queued requests must still be >= 250 ms apart."""
    import asyncio as _asyncio
    import time as _time

    from app.fetch.client import FetchClient as _FetchClient

    sent: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        sent.append(_time.monotonic())
        await _asyncio.sleep(0.05 if len(sent) % 2 else 0.6)  # uneven latency fills the slots
        return httpx.Response(200, text="ok")

    fc = _FetchClient(tmp_settings, session_factory, transport=httpx.MockTransport(handler))
    await _asyncio.gather(*(fc.request(f"https://example.org/item/{i}") for i in range(8)))
    await fc.aclose()
    gaps = [b - a for a, b in zip(sent, sent[1:], strict=False)]
    assert len(sent) == 8 and min(gaps) >= 0.24, gaps
