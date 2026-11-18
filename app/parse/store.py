"""Upsert of parsed events into ``events``: dedupe, id pinning, withheld-location merge, change detection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clock import to_campus
from app.db.models import Event
from app.parse.normalize import compute_content_hash
from app.schemas import (
    EnrichedEvent,
    EventExtras,
    EventStatus,
    NormalizedEvent,
    ParsedEvent,
    UpsertStats,
    Visibility,
)

_CHUNK = 500
_SOURCE_RANK = {"graphql": 2, "ics_personal": 1, "ics": 0}

_NORMALIZED_COLUMNS = ("title", "description_raw", "description_text", "host_group", "starts_at", "ends_at",
                       "location_raw", "cost_raw", "updated_at_source", "content_hash")
_EXTRAS_COLUMNS = ("cg_event_id", "graphql_id", "ics_uid", "source", "is_food_included", "food_details",
                   "location_privacy", "location_hidden", "location_kind", "location_address",
                   "attendance_mode", "all_day", "is_multi_day", "event_status", "event_type", "parent_title")


@dataclass
class UpsertPlan:
    to_enrich: list[ParsedEvent] = field(default_factory=list)  # new or changed, merged and re-hashed
    unchanged_source_urls: set[str] = field(default_factory=set)
    seen_source_urls: set[str] = field(default_factory=set)


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start:start + _CHUNK]


def _rows_by_url(session: Session, urls: Iterable[str]) -> dict[str, Event]:
    ordered = sorted(set(urls))
    rows: dict[str, Event] = {}
    for chunk in _chunks(ordered):
        for row in session.scalars(select(Event).where(Event.source_url.in_(chunk))):
            rows[row.source_url] = row
    return rows


def _preference(item: ParsedEvent) -> tuple[int, int]:
    return _SOURCE_RANK.get(item.extras.source, 0), 0 if item.extras.location_hidden else 1


def dedupe(parsed: Sequence[ParsedEvent]) -> list[ParsedEvent]:
    """One item per source_url: GraphQL beats ICS, a visible location beats a hidden one, else later wins."""
    chosen: dict[str, ParsedEvent] = {}
    for item in parsed:
        url = item.event.source_url
        current = chosen.get(url)
        if current is None or _preference(item) >= _preference(current):
            chosen[url] = item
    return list(chosen.values())


def _merge(item: ParsedEvent, row: Event | None) -> ParsedEvent:
    """Apply id pinning, the withheld-location rule and ICS metadata carry-over, then re-hash."""
    event_updates: dict[str, object] = {}
    extras_updates: dict[str, object] = {}
    ev, ex = item.event, item.extras
    if row is not None:
        event_updates["id"] = row.id
        if ex.location_hidden and ev.location_raw is None and row.location_raw is not None:
            event_updates["location_raw"] = row.location_raw
            extras_updates["location_address"] = row.location_address
            extras_updates["location_hidden"] = False
        if ev.updated_at_source is None and row.updated_at_source is not None:
            event_updates["updated_at_source"] = to_campus(row.updated_at_source)
        if ex.ics_uid is None and row.ics_uid is not None:
            extras_updates["ics_uid"] = row.ics_uid
    extras = ex.model_copy(update=extras_updates)
    event = ev.model_copy(update=event_updates)
    event = event.model_copy(update={"content_hash": compute_content_hash(event, extras)})
    return ParsedEvent(event=event, extras=extras)


def plan_upsert(session: Session, parsed: Sequence[ParsedEvent]) -> UpsertPlan:
    """Decide which events need enrichment (new or content-changed) and which are merely seen again."""
    items = dedupe(parsed)
    rows = _rows_by_url(session, (p.event.source_url for p in items))
    plan = UpsertPlan()
    for item in items:
        url = item.event.source_url
        row = rows.get(url)
        merged = _merge(item, row)
        plan.seen_source_urls.add(url)
        if row is not None and row.content_hash == merged.event.content_hash:
            plan.unchanged_source_urls.add(url)
        else:
            plan.to_enrich.append(merged)
    return plan


def _content_values(parsed: ParsedEvent) -> dict[str, object]:
    ev, ex = parsed.event, parsed.extras
    values: dict[str, object] = {name: getattr(ev, name) for name in _NORMALIZED_COLUMNS}
    values.update({name: getattr(ex, name) for name in _EXTRAS_COLUMNS})
    values["visibility"] = Visibility(ev.visibility).value
    values["tags"] = list(ex.tags)
    values["cohost_groups"] = list(ex.cohost_groups)
    return values


def _enrichment_values(enriched: EnrichedEvent) -> dict[str, object]:
    food, free, geo = enriched.food, enriched.free, enriched.geo
    return {
        "food_score": food.score,
        "food_label": food.label.value,
        "food_evidence": [span.model_dump(mode="json") for span in food.evidence],
        "is_free_to_attend": free.is_free_to_attend,
        "free_evidence": list(free.evidence),
        "lat": geo.lat,
        "lng": geo.lng,
        "building_id": geo.building_id,
        "building_name": geo.display_name,
        "geocode_confidence": geo.confidence,
        "geocode_method": geo.method.value,
        "geocode_score": geo.score,
    }


def _mark_seen(row: Event, *, now: datetime, sync_run_id: int | None) -> None:
    row.last_seen_at = now
    row.last_seen_sync_id = sync_run_id
    row.missing_count = 0
    row.missing_mode = None
    if row.status != EventStatus.ACTIVE.value:
        row.status = EventStatus.ACTIVE.value
        row.removed_at = None


def _assign(row: Event, values: Mapping[str, object]) -> None:
    for name, value in values.items():
        setattr(row, name, value)


def apply_upsert(
    session: Session,
    plan: UpsertPlan,
    enriched: Mapping[str, EnrichedEvent],
    *,
    now: datetime,
    sync_run_id: int | None,
) -> UpsertStats:
    """Write the plan: create/overwrite changed rows, bookkeeping only for unchanged ones. Flushes only."""
    stats = UpsertStats(seen=len(plan.seen_source_urls))
    urls = [p.event.source_url for p in plan.to_enrich] + sorted(plan.unchanged_source_urls)
    rows = _rows_by_url(session, urls)
    for parsed in plan.to_enrich:
        url = parsed.event.source_url
        values = _content_values(parsed) | _enrichment_values(enriched[url])
        row = rows.get(url)
        if row is None:
            row = Event(id=parsed.event.id, source_url=url, first_seen_at=now, status=EventStatus.ACTIVE.value)
            session.add(row)
            stats.created += 1
        else:
            stats.updated += 1
        _assign(row, values)
        row.updated_at = now
        _mark_seen(row, now=now, sync_run_id=sync_run_id)
    for url in plan.unchanged_source_urls:
        row = rows.get(url)
        if row is not None:
            _mark_seen(row, now=now, sync_run_id=sync_run_id)
            stats.unchanged += 1
    session.flush()
    return stats


def touch_seen(session: Session, source_urls: Iterable[str], *, now: datetime, sync_run_id: int | None) -> int:
    """Seen-again bookkeeping for events of an unchanged response body. Returns rows touched."""
    rows = _rows_by_url(session, source_urls)
    for row in rows.values():
        _mark_seen(row, now=now, sync_run_id=sync_run_id)
    session.flush()
    return len(rows)


def row_to_parsed(row: Event) -> ParsedEvent:
    """Rebuild the ParsedEvent a row was written from."""
    event = NormalizedEvent(
        id=row.id,
        source_url=row.source_url,
        title=row.title,
        description_raw=row.description_raw,
        description_text=row.description_text,
        host_group=row.host_group,
        starts_at=to_campus(row.starts_at),
        ends_at=to_campus(row.ends_at) if row.ends_at is not None else None,
        location_raw=row.location_raw,
        cost_raw=row.cost_raw,
        visibility=Visibility(row.visibility),
        updated_at_source=to_campus(row.updated_at_source) if row.updated_at_source is not None else None,
        content_hash=row.content_hash,
    )
    extras = EventExtras(
        **{name: getattr(row, name) for name in _EXTRAS_COLUMNS},
        tags=tuple(row.tags or ()),
        cohost_groups=tuple(row.cohost_groups or ()),
    )
    return ParsedEvent(event=event, extras=extras)


def load_parsed(session: Session, *, active_only: bool = True) -> list[ParsedEvent]:
    """Every (active) row as a ParsedEvent, for re-enrichment after a lexicon or gazetteer change."""
    query = select(Event).order_by(Event.starts_at, Event.source_url)
    if active_only:
        query = query.where(Event.status == EventStatus.ACTIVE.value)
    return [row_to_parsed(row) for row in session.scalars(query)]


def apply_enrichment(session: Session, enriched: Mapping[str, EnrichedEvent]) -> int:
    """Overwrite only the enrichment columns of existing rows (content and updated_at untouched)."""
    touched = 0
    for url, result in enriched.items():
        outcome = session.execute(
            update(Event).where(Event.source_url == url).values(**_enrichment_values(result))
        )
        touched += outcome.rowcount or 0
    session.flush()
    return touched
