"""End-to-end sync tests: real fixtures through ReplayTransport -> parse -> enrich -> store."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app import clock
from app.config import Settings, load_settings
from app.db.models import Event, HttpValidator, SyncRun
from app.fetch.client import FetchClient
from app.fetch.replay import ReplayTransport
from app.pipeline import build_enricher
from app.schemas import AuthMode, AuthStatus, SyncKind
from app.sources import decode_event_global_id
from app.sync import RunPlan, SyncRunner, create_scheduler, floor_to, mark_absent
from tests.conftest import CAPTURE_TIME, MANIFEST, fixture_pages

UPCOMING = MANIFEST["windows"]["upcoming14d"]
HOT = MANIFEST["windows"]["hot6h"]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _no_sleep(_seconds: float) -> None:
    return None


def make_runner(settings: Settings, session_factory, transport: httpx.AsyncBaseTransport) -> SyncRunner:
    fc = FetchClient(settings, session_factory, transport=transport, sleep=_no_sleep)
    return SyncRunner(settings=settings, session_factory=session_factory, fetch_client=fc,
                      enricher=build_enricher(settings, None, online=False))


def full_plan(**kw) -> RunPlan:
    return RunPlan(kind=SyncKind.FULL, start=_dt(UPCOMING["from"]), end=_dt(UPCOMING["to"]), use_ics=True, **kw)


def hot_plan() -> RunPlan:
    return RunPlan(kind=SyncKind.HOT, start=_dt(HOT["from"]), end=_dt(HOT["to"]), use_ics=False)


class EditingTransport(ReplayTransport):
    """ReplayTransport that can drop or corrupt nodes, fail pages, or serve a collapsed auth list."""

    def __init__(self, *, drop: set[int] = frozenset(), corrupt: set[int] = frozenset(),
                 fail_after: str | None = None, collapse_auth: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.drop, self.corrupt, self.fail_after, self.collapse_auth = set(drop), set(corrupt), fail_after, collapse_auth

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/graphql":
            payload = json.loads(request.content)
            if payload.get("operationName") == "Events":
                after = payload["variables"].get("after")
                if self.fail_after is not None and after == self.fail_after:
                    self.requests.append(request)
                    return httpx.Response(500, content=b"boom")
                if self.collapse_auth and request.headers.get("cookie"):
                    self.requests.append(request)
                    body = (self.fixtures_dir / "auth" / "list_upcoming14d_auth_collapsed.json").read_bytes()
                    return httpx.Response(200, headers={"content-type": "application/json"}, content=body)
        response = await super().handle_async_request(request)
        if request.url.path != "/api/v2/graphql" or response.status_code != 200 or not (self.drop or self.corrupt):
            return response
        data = json.loads(response.content)
        edges = ((data.get("data") or {}).get("events") or {}).get("edges")
        if edges is None:
            return response
        kept = []
        for edge in edges:
            num = decode_event_global_id(edge["node"]["id"])
            if num in self.drop:
                continue
            if num in self.corrupt:
                edge["node"]["startDate"] = "not-a-date"
            kept.append(edge)
        data["data"]["events"]["edges"] = kept
        return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(data).encode())


def first_event_id() -> int:
    node = fixture_pages("upcoming14d")[2]["data"]["events"]["edges"][5]["node"]
    return decode_event_global_id(node["id"])


async def test_full_sync_anonymous_then_unchanged(tmp_settings, session_factory, frozen_now):
    runner = make_runner(tmp_settings, session_factory, ReplayTransport())
    first = await runner.run_plan(full_plan())
    assert first.complete and first.mode == AuthMode.ANONYMOUS and first.auth_status == AuthStatus.NONE
    assert first.events_seen == 371 and first.events_created == 371 and first.parse_errors == 0
    with session_factory() as s:
        row = s.get(SyncRun, first.run_id)
        assert row.finished_at is not None and row.duration_ms is not None and row.complete
        assert row.source_stats["graphql"]["pages"] == 8 and row.source_stats["ics"]["status"] == "ok"
        assert row.source_stats["ics"]["events_in_window"] == 344
        joined = s.scalar(select(func.count()).select_from(Event).where(Event.ics_uid.is_not(None)))
        assert joined == 344
        before = {e.source_url: (e.updated_at, e.last_seen_at) for e in s.scalars(select(Event))}

    clock.freeze(frozen_now + timedelta(minutes=30))
    second = await runner.run_plan(full_plan())
    assert second.complete and second.events_seen == 371
    assert second.events_created == 0 and second.events_updated == 0 and second.events_unchanged == 371
    assert second.skipped_unchanged_responses == 9  # 8 identical GraphQL pages + ICS 304
    with session_factory() as s:
        for e in s.scalars(select(Event)):
            assert e.updated_at == before[e.source_url][0]
            assert e.last_seen_at > before[e.source_url][1]
        assert s.get(SyncRun, second.run_id).source_stats["ics"]["status"] == "not_modified"
    await runner.aclose()


async def test_hot_sync_repeat_parses_nothing(tmp_settings, session_factory, frozen_now):
    runner = make_runner(tmp_settings, session_factory, ReplayTransport())
    first = await runner.run_plan(hot_plan())
    assert first.complete and first.events_seen == 72 and first.events_created == 72
    second = await runner.run_plan(hot_plan())
    assert second.skipped_unchanged_responses == 2 and second.events_created == 0
    assert second.events_updated == 0 and second.events_unchanged == 72
    await runner.aclose()


async def test_plan_for_quantizes_windows(tmp_settings, session_factory):
    clock.freeze(CAPTURE_TIME)  # 22:53 -> window starts at 22:50
    offset = floor_to(CAPTURE_TIME, 5) - CAPTURE_TIME
    runner = make_runner(tmp_settings, session_factory, ReplayTransport(time_offset=offset))
    plan = runner.plan_for(SyncKind.FULL)
    assert plan.start == datetime(2026, 11, 17, 22, 50, tzinfo=UTC) and plan.end - plan.start == timedelta(days=14)
    result = await runner.run(SyncKind.FULL)
    assert result.complete and result.events_seen == 371
    hot = runner.plan_for(SyncKind.HOT)
    assert hot.end - hot.start == timedelta(hours=6) and not hot.use_ics
    await runner.aclose()


async def test_absence_two_consecutive_same_mode_syncs(tmp_settings, session_factory, frozen_now):
    gone = first_event_id()
    await make_runner(tmp_settings, session_factory, ReplayTransport()).run_plan(full_plan())
    runner = make_runner(tmp_settings, session_factory, EditingTransport(drop={gone}))
    r1 = await runner.run_plan(full_plan())
    with session_factory() as s:
        row = s.scalars(select(Event).where(Event.cg_event_id == gone)).one()
        assert row.status == "active" and row.missing_count == 1 and row.missing_mode == "anonymous"
    r2 = await runner.run_plan(full_plan())
    assert r1.events_removed == 0 and r2.events_removed == 1
    with session_factory() as s:
        row = s.scalars(select(Event).where(Event.cg_event_id == gone)).one()
        assert row.status == "canceled_or_removed" and row.removed_at is not None
    # It comes back: reactivated.
    back = await make_runner(tmp_settings, session_factory, ReplayTransport()).run_plan(full_plan())
    assert back.complete
    with session_factory() as s:
        row = s.scalars(select(Event).where(Event.cg_event_id == gone)).one()
        assert row.status == "active" and row.missing_count == 0


async def test_incomplete_run_never_counts_absence(tmp_settings, session_factory, frozen_now):
    await make_runner(tmp_settings, session_factory, ReplayTransport()).run_plan(full_plan())
    cursor = MANIFEST["files"]["graphql/list_upcoming14d_p03.json"]["request_json"]["variables"]["after"]
    runner = make_runner(tmp_settings, session_factory, EditingTransport(fail_after=cursor))
    for _ in range(2):
        result = await runner.run_plan(full_plan())
        assert not result.complete and result.events_removed == 0 and result.errors
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Event).where(Event.missing_count > 0)) == 0


async def test_authenticated_mode_visibility_and_locations(auth_settings, session_factory, frozen_now):
    runner = make_runner(auth_settings, session_factory, ReplayTransport())
    result = await runner.run_plan(full_plan())
    assert result.complete and result.mode == AuthMode.AUTHENTICATED and result.auth_status == AuthStatus.VALID
    assert result.authenticated_only_seen == 8 and result.events_seen == 379
    with session_factory() as s:
        auth_rows = s.scalars(select(Event).where(Event.visibility == "authenticated")).all()
        assert sorted(r.cg_event_id for r in auth_rows) == list(range(2399001, 2399009))
        hidden = s.scalars(select(Event).where(Event.location_privacy == "SIGN_IN_REQUIRED",
                                               Event.visibility == "public")).all()
        assert hidden and all(r.location_raw for r in hidden)
        assert s.get(SyncRun, result.run_id).source_stats["graphql_auth"]["events"] == 379


async def test_expired_session_falls_back_once(tmp_path, session_factory, frozen_now, caplog):
    settings = load_settings({"YALE_BITES_VAR_DIR": str(tmp_path / "var"), "YALE_BITES_ONLINE_GEOCODING": "0",
                              "YALECONNECT_COOKIE": "CG.SessionID=stale-session-value"})
    transport = ReplayTransport()
    runner = make_runner(settings, session_factory, transport)
    with caplog.at_level(logging.WARNING):
        result = await runner.run_plan(full_plan())
    assert result.mode == AuthMode.ANONYMOUS and result.auth_status == AuthStatus.EXPIRED and result.complete
    warnings = [r for r in caplog.records if "YALECONNECT_COOKIE" in r.getMessage()]
    assert len(warnings) == 1
    probes = [r for r in transport.requests if b"SessionProbe" in (r.content or b"")]
    assert len(probes) == 1
    assert not any(r.headers.get("cookie") for r in transport.requests if b"Events" in (r.content or b""))


async def test_count_collapse_marks_expired(auth_settings, session_factory, frozen_now, caplog):
    runner = make_runner(auth_settings, session_factory, EditingTransport(collapse_auth=True))
    with caplog.at_level(logging.WARNING):
        result = await runner.run_plan(full_plan())
    assert result.auth_status == AuthStatus.EXPIRED and result.mode == AuthMode.ANONYMOUS
    # The personal ICS feed carries its own token, so its 2 members-only events still arrive.
    assert result.events_seen == 373 and result.authenticated_only_seen == 2
    assert sum("YALECONNECT_COOKIE" in r.getMessage() for r in caplog.records) == 1


async def test_expired_cookie_never_mass_cancels_authenticated_events(auth_settings, tmp_path, session_factory,
                                                                     frozen_now):
    await make_runner(auth_settings, session_factory, ReplayTransport()).run_plan(full_plan())
    anon_settings = load_settings({"YALE_BITES_VAR_DIR": str(tmp_path / "var2"), "YALE_BITES_ONLINE_GEOCODING": "0"})
    runner = make_runner(anon_settings, session_factory, ReplayTransport())
    for _ in range(3):
        result = await runner.run_plan(full_plan())
        assert result.complete and result.mode == AuthMode.ANONYMOUS
    with session_factory() as s:
        rows = s.scalars(select(Event).where(Event.visibility == "authenticated")).all()
        assert len(rows) == 8 and all(r.status == "active" and r.missing_count == 0 for r in rows)
        # The anonymous runs did not erase locations revealed by the authenticated run.
        revealed = s.scalar(select(func.count()).select_from(Event).where(
            Event.location_privacy == "SIGN_IN_REQUIRED", Event.location_raw.is_not(None)))
        assert revealed >= 273


async def test_mixed_mode_misses_do_not_cancel(auth_settings, tmp_path, session_factory, frozen_now):
    gone = first_event_id()
    await make_runner(auth_settings, session_factory, ReplayTransport()).run_plan(full_plan())
    anon_settings = load_settings({"YALE_BITES_VAR_DIR": str(tmp_path / "v"), "YALE_BITES_ONLINE_GEOCODING": "0"})
    await make_runner(anon_settings, session_factory, EditingTransport(drop={gone})).run_plan(full_plan())
    authed = await make_runner(auth_settings, session_factory, EditingTransport(drop={gone})).run_plan(full_plan())
    assert authed.mode == AuthMode.AUTHENTICATED
    with session_factory() as s:
        row = s.scalars(select(Event).where(Event.cg_event_id == gone)).one()
        assert row.status == "active" and row.missing_count == 1 and row.missing_mode == "authenticated"


async def test_malformed_node_is_skipped(tmp_settings, session_factory, frozen_now):
    bad = first_event_id()
    runner = make_runner(tmp_settings, session_factory, EditingTransport(corrupt={bad}))
    result = await runner.run_plan(full_plan())
    assert result.complete and result.parse_errors == 1 and result.events_seen == 370
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Event)) == 370
        # The page with the bad node was not armed, so it is parsed again next time.
        assert s.scalar(select(func.count()).select_from(HttpValidator).where(HttpValidator.body_sha256.is_not(None))) >= 7


async def test_on_complete_and_failure_recorded(tmp_settings, session_factory, frozen_now):
    seen = []
    runner = make_runner(tmp_settings, session_factory, httpx.MockTransport(lambda r: httpx.Response(503)))
    runner.on_complete = seen.append
    result = await runner.run_plan(full_plan())
    assert not result.complete and result.errors and seen == [result]
    with session_factory() as s:
        row = s.get(SyncRun, result.run_id)
        assert row.finished_at is not None and not row.complete and row.errors


def test_scheduler_jobs(tmp_settings, session_factory):
    clock.freeze(CAPTURE_TIME)
    runner = make_runner(tmp_settings, session_factory, ReplayTransport())
    scheduler = create_scheduler(runner, tmp_settings)
    jobs = {j.id: j for j in scheduler.get_jobs()}
    assert set(jobs) == {"full_sync", "hot_sync"}
    assert jobs["full_sync"].trigger.interval == timedelta(minutes=30)
    assert jobs["hot_sync"].trigger.interval == timedelta(minutes=3)
    assert jobs["full_sync"].max_instances == 1 and jobs["hot_sync"].coalesce



def _empty_page_transport() -> httpx.MockTransport:
    empty = {"data": {"events": {"totalCount": 0, "pageInfo": {"endCursor": None, "hasNextPage": False}, "edges": []}}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /mobile_ws/v17/\n")
        return httpx.Response(200, json=empty)

    return httpx.MockTransport(handler)


async def test_empty_hot_window_stays_complete_when_unchanged(tmp_settings, session_factory, frozen_now):
    """An empty window is parsed once; later identical empty pages are skipped but the run is complete."""
    runner = make_runner(tmp_settings, session_factory, _empty_page_transport())
    results = [await runner.run_plan(hot_plan()) for _ in range(3)]
    assert all(r.complete and not r.errors for r in results)
    assert [r.skipped_unchanged_responses for r in results] == [0, 1, 1]


async def test_expired_cookie_warns_once_across_runs(tmp_path, session_factory, frozen_now, caplog):
    settings = load_settings({"YALE_BITES_VAR_DIR": str(tmp_path / "var"), "YALE_BITES_ONLINE_GEOCODING": "0",
                              "YALECONNECT_COOKIE": "CG.SessionID=stale-session-value"})
    runner = make_runner(settings, session_factory, ReplayTransport())
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert (await runner.run_plan(full_plan())).auth_status == AuthStatus.EXPIRED
    assert sum("YALECONNECT_COOKIE" in r.getMessage() for r in caplog.records) == 1


async def test_personal_feed_only_events_need_a_successful_feed_fetch_to_count_absence(
        tmp_settings, session_factory, frozen_now):
    await make_runner(tmp_settings, session_factory, ReplayTransport()).run_plan(full_plan())
    gone = first_event_id()
    start, end = _dt(UPCOMING["from"]), _dt(UPCOMING["to"])
    with session_factory() as s:
        row = s.scalars(select(Event).where(Event.cg_event_id == gone)).one()
        row.source = "ics_personal"  # as if only the personal ICS feed knew this event
        s.commit()
        seen = {u for u in s.scalars(select(Event.source_url))} - {row.source_url}
        kw = {"window_start": start, "window_end": end, "seen": seen, "mode": AuthMode.AUTHENTICATED, "now": frozen_now}
        # Hot syncs never fetch personal feeds, so they cannot vouch for the event's absence.
        assert [mark_absent(s, **kw) for _ in range(3)] == [0, 0, 0]
        assert row.status == "active" and row.missing_count == 0
        # A run whose personal feeds were all fetched successfully does count it.
        assert [mark_absent(s, **kw, personal_feeds_ok=True) for _ in range(2)] == [0, 1]
        assert row.status == "canceled_or_removed"
