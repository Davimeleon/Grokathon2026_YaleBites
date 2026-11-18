"""HTTP API tests (docs/api.md) against an in-memory DB filled through app.db.models."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.api.app import create_app
from app.api.cache import clear_cache
from app.config import Settings
from app.db.models import AppState, Event, SyncRun
from tests.conftest import DUMMY_COOKIE, DUMMY_ICS_TOKEN, PERSONAL_FEED_URL

# 17:00 in New Haven (EST); "today" therefore ends 7 hours later at 05:00Z.
NOW = datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
YALE = (41.3083, -72.9279)
_ids = itertools.count(1000)


def make_event(**overrides) -> Event:
    """An active, public, food=no event starting in an hour; override any column."""
    n = next(_ids)
    values: dict = {
        "id": f"ev{n}@campusgroups.com",
        "source_url": f"https://yaleconnect.yale.edu/rsvp?id={n}",
        "title": f"Event {n}",
        "starts_at": NOW + timedelta(hours=1),
        "ends_at": NOW + timedelta(hours=2),
        "content_hash": f"hash{n}",
        "cg_event_id": n,
        "first_seen_at": NOW - timedelta(days=1),
        "last_seen_at": NOW - timedelta(minutes=5),
        "updated_at": NOW - timedelta(hours=3),
    }
    values.update(overrides)
    return Event(**values)


def make_run(**overrides) -> SyncRun:
    values: dict = {
        "kind": "full",
        "mode": "anonymous",
        "auth_status": "none",
        "started_at": NOW - timedelta(minutes=10),
        "finished_at": NOW - timedelta(minutes=9),
        "duration_ms": 60000,
        "window_start": NOW - timedelta(minutes=10),
        "window_end": NOW + timedelta(days=14),
        "complete": True,
    }
    values.update(overrides)
    return SyncRun(**values)


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    clear_cache()
    clock.freeze(NOW)
    yield
    clear_cache()


@pytest.fixture
def add(session_factory: sessionmaker[Session]) -> Callable[..., None]:
    def _add(*rows) -> None:
        with session_factory() as s:
            s.add_all(rows)
            s.commit()

    return _add


@pytest.fixture
def settings(tmp_settings: Settings, tmp_path: Path) -> Settings:
    return replace(tmp_settings, web_dist_dir=tmp_path / "no-dist")


@pytest.fixture
def client(settings: Settings, session_factory: sessionmaker[Session]) -> TestClient:
    return TestClient(create_app(settings, session_factory))


def titles(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()["items"]]


# --------------------------------------------------------------------------- windows


@pytest.fixture
def window_events(add) -> None:
    h, m = timedelta(hours=1), timedelta(minutes=1)
    local_midnight = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)
    add(
        make_event(title="soon", starts_at=NOW + 10 * m, ends_at=NOW + 70 * m),
        make_event(title="in-an-hour", starts_at=NOW + h, ends_at=NOW + 2 * h),
        make_event(title="in-progress", starts_at=NOW - 30 * m, ends_at=NOW + 30 * m),
        make_event(title="ended", starts_at=NOW - 2 * h, ends_at=NOW - 10 * m),
        make_event(title="tonight-no-end", starts_at=NOW + 3 * h, ends_at=None),
        make_event(title="no-end-still-on", starts_at=NOW - 50 * m, ends_at=None),
        make_event(title="no-end-over", starts_at=NOW - 70 * m, ends_at=None),
        make_event(title="tomorrow", starts_at=NOW + 8 * h, ends_at=NOW + 9 * h),
        make_event(title="semester-long", starts_at=NOW - timedelta(days=30), ends_at=NOW + timedelta(days=30)),
        make_event(title="multi-day-starting", starts_at=NOW + 20 * m, ends_at=NOW + timedelta(days=3)),
        make_event(title="all-day", starts_at=local_midnight, ends_at=local_midnight + 24 * h, all_day=True),
        make_event(title="next-month", starts_at=NOW + timedelta(days=30)),
    )


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("now", {"soon", "in-progress", "no-end-still-on", "multi-day-starting", "all-day"}),
        ("2h", {"soon", "in-an-hour", "in-progress", "no-end-still-on", "multi-day-starting", "all-day"}),
        (
            "today",
            {"soon", "in-an-hour", "in-progress", "no-end-still-on", "multi-day-starting", "all-day",
             "tonight-no-end"},
        ),
        (
            "week",
            {"soon", "in-an-hour", "in-progress", "no-end-still-on", "multi-day-starting", "all-day",
             "tonight-no-end", "tomorrow", "semester-long"},
        ),
    ],
)
def test_windows(client: TestClient, window_events, window: str, expected: set[str]) -> None:
    resp = client.get("/api/events", params={"window": window})
    assert set(titles(resp)) == expected
    assert resp.json()["window"]["name"] == window


def test_today_is_default_and_ends_at_local_midnight(client: TestClient, window_events) -> None:
    body = client.get("/api/events").json()
    assert body["window"] == {"name": "today", "start": "2026-11-17T22:00:00Z", "end": "2026-11-18T05:00:00Z"}
    assert body["generated_at"] == "2026-11-17T22:00:00Z"
    assert body["data_as_of"] is None and body["data_age_seconds"] is None


def test_invalid_window_is_422(client: TestClient) -> None:
    resp = client.get("/api/events", params={"window": "month"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


# --------------------------------------------------------------------------- filters


def test_food_filter(client: TestClient, add) -> None:
    add(
        make_event(title="pizza", food_label="yes", food_score=0.9),
        make_event(title="maybe", food_label="likely", food_score=0.6),
        make_event(title="talk", food_label="no", food_score=0.1),
    )
    assert set(titles(client.get("/api/events?food=yes,likely"))) == {"pizza", "maybe"}
    assert titles(client.get("/api/events?food=YES")) == ["pizza"]
    assert len(titles(client.get("/api/events?food="))) == 3
    bad = client.get("/api/events?food=yes,maybe")
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["loc"] == ["query", "food"]


def test_free_only(client: TestClient, add) -> None:
    add(
        make_event(title="free", is_free_to_attend=True),
        make_event(title="paid", is_free_to_attend=False),
        make_event(title="unknown", is_free_to_attend=None),
    )
    assert titles(client.get("/api/events?free_only=true")) == ["free"]
    assert titles(client.get("/api/events?free_only=1")) == ["free"]
    assert len(titles(client.get("/api/events?free_only=0"))) == 3
    assert client.get("/api/events?free_only=maybe").status_code == 422


def test_near_radius_and_distance_sort(client: TestClient, add) -> None:
    add(
        make_event(title="close", lat=41.3092, lng=-72.9279, starts_at=NOW + timedelta(hours=2)),
        make_event(title="far", lat=41.3533, lng=-72.9279, starts_at=NOW + timedelta(minutes=30)),
        make_event(title="unpinned", starts_at=NOW + timedelta(minutes=40)),
    )
    near = f"{YALE[0]},{YALE[1]}"
    default_radius = client.get("/api/events", params={"near": near}).json()
    assert [i["title"] for i in default_radius["items"]] == ["close"]
    assert 90 < default_radius["items"][0]["distance_m"] < 110

    wide = client.get("/api/events", params={"near": near, "radius_m": 10000})
    assert titles(wide) == ["far", "close"]
    by_distance = client.get("/api/events", params={"near": near, "radius_m": 10000, "sort": "distance"})
    assert titles(by_distance) == ["close", "far"]
    assert 4900 < by_distance.json()["items"][1]["distance_m"] < 5100

    plain = client.get("/api/events").json()["items"]
    assert len(plain) == 3 and all(i["distance_m"] is None for i in plain)


@pytest.mark.parametrize(
    "query",
    [
        "near=abc",
        "near=91,0",
        "near=0,181",
        "near=nan,0",
        "near=1,2,3",
        "near=41.3,-72.9&radius_m=0",
        "near=41.3,-72.9&radius_m=20001",
        "sort=distance",
        "sort=nearest",
        "page=0",
        "page_size=0",
        "page_size=201",
    ],
)
def test_invalid_params_are_422(client: TestClient, query: str) -> None:
    resp = client.get(f"/api/events?{query}")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert resp.headers["content-type"] == "application/json; charset=utf-8"


def test_q_matches_fields_case_insensitively_and_escapes_wildcards(client: TestClient, add) -> None:
    add(
        make_event(title="Pasta Night"),
        make_event(title="Lecture", host_group="Italian Club"),
        make_event(title="Mixer", description_text="Bring a friend; snacks"),
        make_event(title="Study hall", location_raw="Sterling Memorial Library"),
        make_event(title="100% free pizza"),
        make_event(title="1000 free things"),
        make_event(title="snake_case meetup"),
    )
    assert titles(client.get("/api/events", params={"q": "PASTA"})) == ["Pasta Night"]
    assert titles(client.get("/api/events", params={"q": "italian"})) == ["Lecture"]
    assert titles(client.get("/api/events", params={"q": "snacks"})) == ["Mixer"]
    assert titles(client.get("/api/events", params={"q": "sterling"})) == ["Study hall"]
    assert titles(client.get("/api/events", params={"q": "100%"})) == ["100% free pizza"]
    assert titles(client.get("/api/events", params={"q": "_"})) == ["snake_case meetup"]


# --------------------------------------------------------------------------- sorting and paging


def test_start_sort_ties_break_on_food_score_then_title(client: TestClient, add) -> None:
    same = NOW + timedelta(hours=1)
    add(
        make_event(title="B low", starts_at=same, food_score=0.2),
        make_event(title="A low", starts_at=same, food_score=0.2),
        make_event(title="Z high", starts_at=same, food_score=0.9),
        make_event(title="earliest", starts_at=NOW + timedelta(minutes=30), food_score=0.0),
    )
    assert titles(client.get("/api/events")) == ["earliest", "Z high", "A low", "B low"]


def test_distance_sort_ties_break_on_start_then_food_score(client: TestClient, add) -> None:
    pin = {"lat": 41.3092, "lng": -72.9279}
    add(
        make_event(title="late", starts_at=NOW + timedelta(hours=2), **pin),
        make_event(title="early low", starts_at=NOW + timedelta(hours=1), food_score=0.1, **pin),
        make_event(title="early high", starts_at=NOW + timedelta(hours=1), food_score=0.8, **pin),
    )
    resp = client.get("/api/events", params={"near": f"{YALE[0]},{YALE[1]}", "sort": "distance"})
    assert titles(resp) == ["early high", "early low", "late"]


def test_pagination(client: TestClient, add) -> None:
    add(*(make_event(title=f"e{i}", starts_at=NOW + timedelta(minutes=10 * (i + 1))) for i in range(5)))
    first = client.get("/api/events?page_size=2").json()
    assert (first["total"], first["page"], first["page_size"], first["next_page"]) == (5, 1, 2, 2)
    assert [i["title"] for i in first["items"]] == ["e0", "e1"]
    last = client.get("/api/events?page_size=2&page=3").json()
    assert [i["title"] for i in last["items"]] == ["e4"] and last["next_page"] is None
    beyond = client.get("/api/events?page_size=2&page=9").json()
    assert beyond["items"] == [] and beyond["total"] == 5 and beyond["next_page"] is None


# --------------------------------------------------------------------------- mapping and detail


def test_event_out_mapping(client: TestClient, add) -> None:
    add(
        make_event(
            id="abc:def@campusgroups.com",
            title="Queer Tables",
            host_group="LGBTQ Center",
            starts_at=NOW + timedelta(minutes=29, seconds=30),
            ends_at=NOW + timedelta(hours=2),
            location_raw="LGBTQ Center, 135 Prospect St",
            location_privacy="SIGN_IN_REQUIRED",
            building_id="lgbtq-center",
            building_name="Yale LGBTQ Center",
            lat=41.3133,
            lng=-72.9254,
            geocode_method="alias",
            geocode_confidence=0.95,
            food_score=0.97,
            food_label="yes",
            food_evidence=[
                {"text": "Food included", "field": "food_flag", "kind": "flag", "weight": 2.0, "rule": "flag"},
                {"text": "x" * 200, "field": "description", "kind": "positive", "weight": 1.6, "rule": "r"},
                "not-a-dict",
            ],
            is_free_to_attend=True,
            free_evidence=["RSVP: $0.00"],
            cost_raw="RSVP: free",
            visibility="authenticated",
            event_type="Social",
            tags=["LGBTQ+"],
            attendance_mode="IN_PERSON",
            description_text="d" * 700,
        )
    )
    item = client.get("/api/events").json()["items"][0]
    assert item["starts_at"] == "2026-11-17T17:29:30-05:00"
    assert item["ends_at"] == "2026-11-17T19:00:00-05:00"
    assert item["starts_in_minutes"] == 29
    assert item["location"] == {
        "raw": "LGBTQ Center, 135 Prospect St",
        "building_id": "lgbtq-center",
        "building_name": "Yale LGBTQ Center",
        "lat": 41.3133,
        "lng": -72.9254,
        "geocode_method": "alias",
        "geocode_confidence": 0.95,
        "requires_sign_in": True,
    }
    assert item["food"]["label"] == "yes" and item["food"]["score"] == 0.97
    assert item["food"]["evidence"][0] == {"text": "Food included", "field": "food_flag", "kind": "flag",
                                           "weight": 2.0}
    assert len(item["food"]["evidence"]) == 2 and len(item["food"]["evidence"][1]["text"]) == 120
    assert item["free_evidence"] == ["RSVP: $0.00"] and item["is_free_to_attend"] is True
    assert (item["visibility"], item["status"], item["tags"]) == ("authenticated", "active", ["LGBTQ+"])
    assert len(item["description_text"]) == 600
    assert item["updated_at"] == "2026-11-17T19:00:00Z"

    detail = client.get(f"/api/events/{quote(item['id'], safe='')}")
    assert detail.status_code == 200
    assert detail.json()["description_text"] == "d" * 700


def test_started_event_has_negative_floored_minutes(client: TestClient, add) -> None:
    add(make_event(starts_at=NOW - timedelta(seconds=90), ends_at=NOW + timedelta(hours=1), all_day=False))
    assert client.get("/api/events?window=now").json()["items"][0]["starts_in_minutes"] == -2


def test_removed_events_hidden_from_lists_but_resolvable(client: TestClient, add) -> None:
    add(
        make_event(id="gone@campusgroups.com", title="gone", status="canceled_or_removed"),
        make_event(title="here"),
    )
    assert titles(client.get("/api/events?window=week")) == ["here"]
    resp = client.get("/api/events/gone@campusgroups.com")
    assert resp.status_code == 200 and resp.json()["status"] == "canceled_or_removed"


@pytest.mark.parametrize("event_id", ["e0a7c3:2337199@campusgroups.com", "sha256/abc:def@x", "plain"])
def test_detail_ids_with_reserved_characters(client: TestClient, add, event_id: str) -> None:
    add(make_event(id=event_id, title="target"))
    encoded = client.get(f"/api/events/{quote(event_id, safe='')}")
    assert encoded.status_code == 200 and encoded.json()["id"] == event_id
    raw = client.get(f"/api/events/{event_id}")
    assert raw.status_code == 200 and raw.json()["id"] == event_id


def test_unknown_id_is_404(client: TestClient) -> None:
    resp = client.get("/api/events/nope@campusgroups.com")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "event not found"}
    assert resp.headers["cache-control"] == "public, max-age=60"


# --------------------------------------------------------------------------- headers and memo


def test_cache_headers_and_data_age(client: TestClient, add) -> None:
    first = client.get("/api/events")
    assert first.headers["cache-control"] == "public, max-age=60"
    assert first.headers["content-type"] == "application/json; charset=utf-8"
    assert "x-data-age" not in first.headers
    add(
        make_run(finished_at=NOW - timedelta(seconds=120)),
        make_run(kind="hot", started_at=NOW - timedelta(seconds=30), finished_at=NOW - timedelta(seconds=20),
                 complete=False),
    )
    clear_cache()
    resp = client.get("/api/events")
    assert resp.headers["x-data-age"] == "120"
    assert resp.json()["data_as_of"] == "2026-11-17T21:58:00Z"
    assert resp.json()["data_age_seconds"] == 120
    meta = client.get("/api/meta")
    assert meta.headers["cache-control"] == "public, max-age=60" and meta.headers["x-data-age"] == "120"
    assert "cache-control" not in client.get("/healthz").headers


def test_memo_hits_within_60s_and_misses_after(client: TestClient, add) -> None:
    add(make_event(title="first"))
    assert titles(client.get("/api/events?window=today&food=no")) == ["first"]
    add(make_event(title="second"))
    assert titles(client.get("/api/events?food=no&window=today")) == ["first"]

    clock.freeze(NOW + timedelta(seconds=59))
    assert titles(client.get("/api/events?window=today&food=no")) == ["first"]
    clock.freeze(NOW + timedelta(seconds=61))
    assert titles(client.get("/api/events?window=today&food=no")) == ["first", "second"]

    add(make_event(title="third"))
    assert len(titles(client.get("/api/events?window=today&food=no"))) == 2
    clear_cache()
    assert len(titles(client.get("/api/events?window=today&food=no"))) == 3


# --------------------------------------------------------------------------- meta


def test_meta_empty_db(client: TestClient) -> None:
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert body["now"] == "2026-11-17T22:00:00Z"
    assert body["last_sync"] is None and body["last_full_sync"] is None and body["last_hot_sync"] is None
    assert body["data_as_of"] is None and body["data_age_seconds"] is None
    assert body["auth"] == {
        "mode": "anonymous",
        "status": "none",
        "cookie_configured": False,
        "ics_feeds_configured": 0,
        "data_includes_authenticated_events": False,
    }
    assert body["counts"] == {
        "active": 0,
        "removed": 0,
        "by_visibility": {"public": 0, "authenticated": 0},
        "by_food_label": {"yes": 0, "likely": 0, "no": 0},
        "geocoded": 0,
        "location_withheld": 0,
    }
    assert body["coverage"] == {"primary_source": "graphql", "sources": {}, "geocode_rate": None}
    assert body["dataset"] == {"kind": "live", "time_shift_days": 0}
    assert "x-data-age" not in resp.headers


def test_meta_with_runs_counts_and_dataset(client: TestClient, add) -> None:
    m = timedelta(minutes=1)
    add(
        make_run(kind="seed", started_at=NOW - 120 * m, finished_at=NOW - 119 * m,
                 source_stats={"graphql": {"pages": 9}}),
        make_run(kind="full", mode="authenticated", auth_status="valid", started_at=NOW - 30 * m,
                 finished_at=NOW - 29 * m, errors=["boom"],
                 source_stats={"graphql": {"pages": 3, "events": 300, "status": "ok", "unchanged_pages": 1},
                               "ics": {"status": "not_modified", "last_modified": "Tue, 17 Nov 2026 06:05:24 GMT",
                                       "events_in_window": 344},
                               "junk": 5}),
        make_run(kind="hot", started_at=NOW - 3 * m, finished_at=NOW - 2 * m,
                 source_stats={"graphql": {"pages": 1, "events": 72, "last_fetch_at": "2026-11-17T21:58:00Z"}}),
        make_run(kind="hot", started_at=NOW - 1 * m, finished_at=None, complete=False, source_stats={}),
        AppState(key="dataset", value={"kind": "fixture_replay", "time_shift_days": 3}, updated_at=NOW),
        make_event(food_label="yes", lat=41.3, lng=-72.9, attendance_mode="IN_PERSON"),
        make_event(food_label="likely", visibility="authenticated", attendance_mode="HYBRID"),
        make_event(food_label="no", location_hidden=True, location_raw=None),
        make_event(food_label="no", location_privacy="REGISTRATION_REQUIRED", location_raw="Revealed Hall"),
        make_event(food_label="no", attendance_mode="ONLINE"),
        make_event(status="canceled_or_removed", food_label="yes"),
    )
    body = client.get("/api/meta").json()
    assert body["last_sync"]["kind"] == "hot" and body["last_sync"]["complete"] is False
    assert body["last_sync"]["finished_at"] is None
    assert body["last_full_sync"]["kind"] == "full" and body["last_full_sync"]["error_count"] == 1
    assert body["last_hot_sync"]["id"] == body["last_sync"]["id"]
    assert body["data_as_of"] == "2026-11-17T21:58:00Z" and body["data_age_seconds"] == 120
    assert body["auth"]["mode"] == "anonymous" and body["auth"]["status"] == "none"
    assert body["auth"]["data_includes_authenticated_events"] is True
    assert body["counts"] == {
        "active": 5,
        "removed": 1,
        "by_visibility": {"public": 4, "authenticated": 1},
        "by_food_label": {"yes": 1, "likely": 1, "no": 3},
        "geocoded": 1,
        "location_withheld": 1,
    }
    assert body["coverage"]["sources"] == {
        "graphql": {"pages": 1, "events": 72, "status": "ok", "unchanged_pages": 1,
                    "last_fetch_at": "2026-11-17T21:58:00Z"},
        "ics": {"status": "not_modified", "last_modified": "Tue, 17 Nov 2026 06:05:24 GMT",
                "events_in_window": 344},
    }
    assert body["coverage"]["geocode_rate"] == 0.25
    assert body["dataset"] == {"kind": "fixture_replay", "time_shift_days": 3}


def test_meta_seed_counts_as_full_and_sets_auth(client: TestClient, add) -> None:
    add(
        make_run(kind="seed", mode="authenticated", auth_status="expired", source_stats={"graphql": {"pages": 9}}),
        AppState(key="dataset", value={"kind": "bogus"}, updated_at=NOW),
    )
    body = client.get("/api/meta").json()
    assert body["last_full_sync"]["kind"] == "seed" and body["last_hot_sync"] is None
    assert body["auth"]["mode"] == "authenticated" and body["auth"]["status"] == "expired"
    assert body["coverage"]["sources"] == {"graphql": {"pages": 9}}
    assert body["dataset"] == {"kind": "live", "time_shift_days": 0}


def test_secrets_never_leak(auth_settings: Settings, session_factory, add, tmp_path: Path) -> None:
    settings = replace(auth_settings, web_dist_dir=tmp_path / "no-dist")
    add(make_event(id="leak-check@campusgroups.com"), make_run(mode="authenticated", auth_status="valid"))
    client = TestClient(create_app(settings, session_factory))
    meta = client.get("/api/meta").json()
    assert meta["auth"]["cookie_configured"] is True and meta["auth"]["ics_feeds_configured"] == 1
    paths = ["/api/meta", "/api/events", "/api/events/leak-check@campusgroups.com", "/api/events/missing",
             "/api/events?window=bad", "/", "/healthz"]
    for path in paths:
        resp = client.get(path)
        blob = resp.text + str(dict(resp.headers))
        for secret in (DUMMY_COOKIE, DUMMY_ICS_TOKEN, PERSONAL_FEED_URL):
            assert secret not in blob, path


# --------------------------------------------------------------------------- app plumbing


def test_healthz_and_root_hint(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}
    root = client.get("/")
    assert root.status_code == 200 and root.json()["events"] == "/api/events"


def test_serves_built_frontend(tmp_settings: Settings, session_factory, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Yale Bites</title>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    client = TestClient(create_app(replace(tmp_settings, web_dist_dir=dist), session_factory))
    root = client.get("/")
    assert root.status_code == 200 and "<title>Yale Bites</title>" in root.text
    assert client.get("/assets/app.js").text == "console.log('hi')"
    assert client.get("/api/events").json()["items"] == []
    assert client.get("/healthz").json() == {"ok": True}


def test_default_session_factory_from_settings(settings: Settings) -> None:
    client = TestClient(create_app(settings))
    assert client.get("/api/events").json()["total"] == 0
    assert client.get("/api/meta").status_code == 200
    assert settings.db_path.exists()


def test_long_running_boundaries(client: TestClient, add) -> None:
    """Exactly 24 h is not long-running; a 25 h all-day DST day is not either; 24 h + 1 s is."""
    clock.freeze(datetime(2026, 11, 1, 16, 0, tzinfo=UTC))
    dst_midnight = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    add(
        make_event(title="exactly-24h", starts_at=dst_midnight, ends_at=dst_midnight + timedelta(hours=24)),
        make_event(title="dst-all-day", starts_at=dst_midnight, ends_at=dst_midnight + timedelta(hours=25),
                   all_day=True),
        make_event(title="just-over", starts_at=dst_midnight, ends_at=dst_midnight + timedelta(hours=24, seconds=1)),
    )
    assert set(titles(client.get("/api/events?window=now"))) == {"exactly-24h", "dst-all-day"}
    assert len(titles(client.get("/api/events?window=week"))) == 3
