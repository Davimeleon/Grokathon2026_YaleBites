"""ReplayTransport + GraphQL helpers: full pagination offline, auth pages, offsets, tolerant pageInfo."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx

from app.fetch.client import FetchClient
from app.fetch.graphql import error_codes, fetch_events_page, iso_z, page_info, viewer_present
from app.fetch.replay import ReplayTransport


async def _noop_sleep(_: float) -> None:
    return None


async def _paginate(fc: FetchClient, start, end, authenticated: bool) -> tuple[int, int]:
    pages = events = 0
    after = None
    while True:
        result = await fetch_events_page(fc, start=start, end=end, after=after, authenticated=authenticated,
                                         logical_key=None)
        assert result.status == "ok", result.error
        pages += 1
        events += len(json.loads(result.body)["data"]["events"]["edges"])
        info = page_info(result.body)
        if not info.has_next_page:
            return pages, events
        after = info.end_cursor


async def test_replay_paginates_anonymous_and_authenticated(auth_settings, session_factory, capture_time):
    replay = ReplayTransport()
    fc = FetchClient(auth_settings, session_factory, transport=replay, sleep=_noop_sleep)
    end = capture_time + timedelta(days=14)
    try:
        anon = await _paginate(fc, capture_time, end, authenticated=False)
        authed = await _paginate(fc, capture_time, end, authenticated=True)
        hot = await _paginate(fc, capture_time, capture_time + timedelta(hours=6), authenticated=False)
    finally:
        await fc.aclose()
    assert anon == (8, 371)
    assert authed == (8, 379)
    assert hot == (2, 72)
    assert replay.unknown == []


async def test_replay_time_offset_and_unknown_requests(tmp_settings, session_factory, capture_time):
    offset = timedelta(days=3)
    replay = ReplayTransport(time_offset=offset, personal_feed=False)
    fc = FetchClient(tmp_settings, session_factory, transport=replay, sleep=_noop_sleep)
    start = capture_time + offset
    try:
        shifted = await fetch_events_page(fc, start=start, end=start + timedelta(hours=6), after=None,
                                          authenticated=False, logical_key=None)
        unshifted = await fetch_events_page(fc, start=capture_time, end=capture_time + timedelta(hours=6),
                                            after=None, authenticated=False, logical_key=None)
        missing = await fc.request("https://yaleconnect.yale.edu/ical/yale/ical_personal_x.ics")
    finally:
        await fc.aclose()
    assert shifted.status == "ok"
    assert unshifted.status == "error" and unshifted.http_status == 404
    assert missing.status == "error"
    assert len(replay.unknown) == 2


async def test_replay_viewer_probe_by_cookie():
    replay = ReplayTransport()
    meta = replay.meta
    body = json.dumps({"operationName": "SessionProbe", "query": "q", "variables": {}}).encode()

    async def probe(cookie: str | None) -> bytes:
        headers = {"cookie": cookie} if cookie else {}
        request = httpx.Request("POST", "https://yaleconnect.yale.edu/api/v2/graphql", headers=headers, content=body)
        response = await replay.handle_async_request(request)
        return await response.aread()

    assert error_codes(await probe(None)) == ["AUTH_NOT_AUTHENTICATED"]
    assert viewer_present(await probe(meta["dummy_cookie"]))
    assert error_codes(await probe("CG.SessionID=other")) == ["AUTH_NOT_AUTHENTICATED"]


def test_page_info_tolerates_malformed_nodes_and_bodies():
    body = json.dumps({"data": {"events": {"pageInfo": {"endCursor": "c2", "hasNextPage": True},
                                           "edges": [{"node": {"id": 5, "startDate": None}}, None, "junk"]}}})
    assert page_info(body).end_cursor == "c2" and page_info(body).has_next_page
    assert page_info(b"not json") is None
    assert page_info(json.dumps({"data": None, "errors": [{"message": "x"}]})) is None
    assert page_info(json.dumps([1, 2])) is None


def test_iso_z_requires_aware_datetimes(capture_time):
    assert iso_z(capture_time) == "2026-11-17T22:53:00Z"
    try:
        iso_z(capture_time.replace(tzinfo=None))
    except ValueError:
        return
    raise AssertionError("naive datetime accepted")
