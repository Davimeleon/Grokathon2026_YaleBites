from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import Event, SyncRun
from app.parse.graphql import parse_events_response
from app.parse.normalize import apply_ics_metadata, make_event_id, rehash, with_visibility
from app.parse.store import apply_enrichment, apply_upsert, dedupe, load_parsed, plan_upsert, touch_seen
from app.schemas import (
    EnrichedEvent,
    EvidenceSpan,
    FoodLabel,
    FoodResult,
    FreeResult,
    GeocodeMethod,
    GeoResult,
    IcsEvent,
    ParsedEvent,
    Visibility,
)
from tests.conftest import fixture_pages


def _enrich(parsed: ParsedEvent, label: FoodLabel = FoodLabel.YES) -> EnrichedEvent:
    span = EvidenceSpan(text="pizza", field="title", kind="positive", weight=0.6, rule="pizza")
    return EnrichedEvent(
        parsed=parsed,
        food=FoodResult(score=0.9, label=label, evidence=(span,)),
        free=FreeResult(is_free_to_attend=True, evidence=("RSVP: free",)),
        geo=GeoResult(lat=41.31, lng=-72.92, building_id="luce", display_name="Luce Hall", confidence=0.9,
                      method=GeocodeMethod.EXACT),
    )


def _upsert(session, parsed, now, run_id=None):
    plan = plan_upsert(session, parsed)
    stats = apply_upsert(session, plan, {p.event.source_url: _enrich(p) for p in plan.to_enrich},
                         now=now, sync_run_id=run_id)
    return plan, stats


def _row(session, url) -> Event:
    return session.scalars(select(Event).where(Event.source_url == url)).one()


@pytest.fixture
def upcoming() -> list[ParsedEvent]:
    return [e for page in fixture_pages("upcoming14d") for e in parse_events_response(page)[0]]


@pytest.fixture
def sync_run(session, frozen_now) -> int:
    run = SyncRun(kind="full", mode="anonymous", auth_status="none", started_at=frozen_now,
                  window_start=frozen_now, window_end=frozen_now + timedelta(days=14))
    session.add(run)
    session.flush()
    return run.id


def test_create_then_idempotent(session, upcoming, frozen_now, sync_run):
    plan, stats = _upsert(session, upcoming, frozen_now, sync_run)
    assert (stats.seen, stats.created, stats.updated, stats.unchanged) == (371, 371, 0, 0)
    row = _row(session, upcoming[0].event.source_url)
    assert row.first_seen_at == row.last_seen_at == row.updated_at == frozen_now
    assert row.food_label == "yes" and row.food_evidence[0]["rule"] == "pizza"
    assert row.free_evidence == ["RSVP: free"] and row.geocode_method == "exact"
    assert row.building_name == "Luce Hall" and row.last_seen_sync_id == sync_run

    later = frozen_now + timedelta(minutes=30)
    plan2, stats2 = _upsert(session, upcoming, later)
    assert plan2.to_enrich == [] and len(plan2.unchanged_source_urls) == 371
    assert (stats2.created, stats2.updated, stats2.unchanged) == (0, 0, 371)
    session.refresh(row)
    assert row.updated_at == frozen_now and row.last_seen_at == later
    assert session.scalar(select(func.count()).select_from(Event)) == 371


def test_content_change_updates_updated_at(session, upcoming, frozen_now):
    _upsert(session, upcoming[:5], frozen_now)
    changed = rehash(upcoming[0], title=upcoming[0].event.title + " (with pizza)")
    later = frozen_now + timedelta(hours=1)
    plan, stats = _upsert(session, [changed, *upcoming[1:5]], later)
    assert [p.event.source_url for p in plan.to_enrich] == [changed.event.source_url]
    assert (stats.updated, stats.unchanged) == (1, 4)
    row = _row(session, changed.event.source_url)
    assert row.title.endswith("(with pizza)") and row.updated_at == later
    assert row.content_hash == changed.event.content_hash


def test_id_pinning_no_duplicate(session, upcoming, frozen_now):
    first = upcoming[0]
    _upsert(session, [first], frozen_now)
    sha_id = make_event_id(first.event.source_url, None)
    assert _row(session, first.event.source_url).id == sha_id
    rec = IcsEvent(uid="late-uid@campusgroups.com", dtstart=first.event.starts_at,
                   last_modified=first.event.starts_at, event_id=first.extras.cg_event_id)
    with_uid = apply_ics_metadata(first, rec)
    plan, stats = _upsert(session, [with_uid], frozen_now + timedelta(minutes=3))
    assert stats.unchanged == 1 and stats.created == 0
    assert session.scalar(select(func.count()).select_from(Event)) == 1
    assert _row(session, first.event.source_url).id == sha_id


def test_new_row_uses_ics_uid(session, upcoming, frozen_now):
    rec = IcsEvent(uid="first-uid@campusgroups.com", dtstart=upcoming[1].event.starts_at,
                   event_id=upcoming[1].extras.cg_event_id)
    _upsert(session, [apply_ics_metadata(upcoming[1], rec)], frozen_now)
    row = _row(session, upcoming[1].event.source_url)
    assert row.id == "first-uid@campusgroups.com" and row.ics_uid == row.id


def test_hidden_location_is_not_erased(session, upcoming, frozen_now):
    auth_events = [e for page in fixture_pages("upcoming14d", auth=True)
                   for e in parse_events_response(page, visibility=Visibility.PUBLIC)[0]]
    anon = next(e for e in upcoming if e.extras.location_privacy == "SIGN_IN_REQUIRED")
    revealed = next(e for e in auth_events if e.event.source_url == anon.event.source_url)
    assert revealed.event.location_raw and anon.event.location_raw is None
    _upsert(session, [revealed], frozen_now)
    plan, stats = _upsert(session, [anon], frozen_now + timedelta(minutes=3))
    assert stats.unchanged == 1 and plan.to_enrich == []
    assert _row(session, anon.event.source_url).location_raw == revealed.event.location_raw


def test_hidden_location_carried_into_changed_row(session, upcoming, frozen_now):
    auth_events = [e for page in fixture_pages("upcoming14d", auth=True) for e in parse_events_response(page)[0]]
    anon = next(e for e in upcoming if e.extras.location_privacy == "SIGN_IN_REQUIRED")
    revealed = next(e for e in auth_events if e.event.source_url == anon.event.source_url)
    _upsert(session, [revealed], frozen_now)
    edited = rehash(anon, title="Edited")
    plan, _ = _upsert(session, [edited], frozen_now + timedelta(minutes=3))
    merged = plan.to_enrich[0]
    assert merged.event.location_raw == revealed.event.location_raw
    assert merged.extras.location_hidden is False
    assert _row(session, anon.event.source_url).location_raw == revealed.event.location_raw


def test_removed_event_reactivated(session, upcoming, frozen_now):
    _upsert(session, upcoming[:2], frozen_now)
    row = _row(session, upcoming[0].event.source_url)
    row.status, row.removed_at, row.missing_count, row.missing_mode = (
        "canceled_or_removed", frozen_now, 2, "anonymous")
    session.flush()
    _upsert(session, upcoming[:2], frozen_now + timedelta(hours=1))
    session.refresh(row)
    assert (row.status, row.removed_at, row.missing_count, row.missing_mode) == ("active", None, 0, None)
    assert row.updated_at == frozen_now


def test_touch_seen(session, upcoming, frozen_now, sync_run):
    _upsert(session, upcoming[:3], frozen_now)
    later = frozen_now + timedelta(minutes=3)
    urls = [e.event.source_url for e in upcoming[:3]] + ["https://yaleconnect.yale.edu/rsvp?id=1"]
    assert touch_seen(session, urls, now=later, sync_run_id=sync_run) == 3
    row = _row(session, upcoming[0].event.source_url)
    assert row.last_seen_at == later and row.updated_at == frozen_now and row.last_seen_sync_id == sync_run


def test_load_parsed_round_trip(session, upcoming, frozen_now):
    _upsert(session, upcoming, frozen_now)
    loaded = {p.event.source_url: p for p in load_parsed(session)}
    assert len(loaded) == 371
    for original in upcoming:
        back = loaded[original.event.source_url]
        assert back.event == original.event
        assert back.extras.model_copy(update={"source_lat": None, "source_lng": None}) == original.extras
        assert rehash(back).event.content_hash == original.event.content_hash
    row = _row(session, upcoming[0].event.source_url)
    row.status = "canceled_or_removed"
    session.flush()
    assert len(load_parsed(session)) == 370 and len(load_parsed(session, active_only=False)) == 371


def test_apply_enrichment_only_touches_enrichment(session, upcoming, frozen_now):
    _upsert(session, upcoming[:2], frozen_now)
    target = upcoming[0]
    n = apply_enrichment(session, {target.event.source_url: _enrich(target, FoodLabel.NO)})
    assert n == 1
    row = _row(session, target.event.source_url)
    session.refresh(row)
    assert row.food_label == "no" and row.updated_at == frozen_now and row.title == target.event.title


def test_dedupe_preferences(upcoming):
    gql = upcoming[0]
    ics = ParsedEvent(event=gql.event, extras=gql.extras.model_copy(update={"source": "ics"}))
    assert dedupe([gql, ics]) == [gql]
    hidden = ParsedEvent(event=gql.event, extras=gql.extras.model_copy(update={"location_hidden": True}))
    assert dedupe([gql, hidden]) == [gql]
    later = with_visibility(gql, Visibility.AUTHENTICATED)
    assert dedupe([gql, later]) == [later]
