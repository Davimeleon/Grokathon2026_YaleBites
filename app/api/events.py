"""Event listing: window maths, SQL filters, Python-side filters, sorting, paging and EventOut mapping."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.db.models import Event
from app.schemas import (
    EventOut,
    EventStatus,
    EvidenceOut,
    FoodLabel,
    FoodOut,
    GeocodeMethod,
    LocationOut,
    Visibility,
    WindowName,
    WindowOut,
)

DEFAULT_DURATION = timedelta(hours=1)
LONG_RUNNING = timedelta(hours=24)
# An all-day event on a DST-change day spans 23 or 25 hours; it is still a single day, not long-running.
ALL_DAY_MAX = timedelta(hours=25)
LIST_DESCRIPTION_CHARS = 600
EVIDENCE_TEXT_CHARS = 120
EARTH_RADIUS_M = 6_371_008.8
PRIVACY_GATED = frozenset({"SIGN_IN_REQUIRED", "REGISTRATION_REQUIRED"})
REMOVED = EventStatus.CANCELED_OR_REMOVED.value


class SortKey(StrEnum):
    START = "start"
    DISTANCE = "distance"


@dataclass(frozen=True)
class EventQuery:
    """Validated /api/events parameters."""

    window: WindowName
    food: frozenset[str] | None = None
    free_only: bool = False
    near: tuple[float, float] | None = None
    radius_m: int | None = None
    q: str | None = None
    sort: SortKey = SortKey.START
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True)
class EventPage:
    window: WindowOut
    total: int
    items: list[EventOut]
    next_page: int | None


def window_bounds(name: WindowName, now: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` of a named window; ``today`` ends at the next local midnight on campus."""
    if name is WindowName.NOW:
        return now, now + timedelta(minutes=30)
    if name is WindowName.TWO_HOURS:
        return now, now + timedelta(hours=2)
    if name is WindowName.WEEK:
        return now, now + timedelta(days=7)
    local_date = clock.to_campus(now).date()
    midnight = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=clock.CAMPUS_TZ)
    return now, midnight.astimezone(now.tzinfo)


def effective_end(event: Event) -> datetime:
    return event.ends_at if event.ends_at is not None else event.starts_at + DEFAULT_DURATION


def is_long_running(event: Event) -> bool:
    span = effective_end(event) - event.starts_at
    if event.all_day and span <= ALL_DAY_MAX:
        return False
    return span > LONG_RUNNING


def matches_window(event: Event, name: WindowName, start: datetime, end: datetime) -> bool:
    """Overlap test, except long-running events must *start* inside every window but ``week``."""
    if not (event.starts_at < end and effective_end(event) > start):
        return False
    if name is not WindowName.WEEK and is_long_running(event):
        return start <= event.starts_at < end
    return True


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sql_filters(params: EventQuery, start: datetime, end: datetime) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [
        Event.status != REMOVED,
        Event.starts_at < end,
        or_(
            Event.ends_at > start,
            and_(Event.ends_at.is_(None), Event.starts_at > start - DEFAULT_DURATION),
        ),
    ]
    if params.food:
        filters.append(Event.food_label.in_(sorted(params.food)))
    if params.free_only:
        filters.append(Event.is_free_to_attend.is_(True))
    if params.q:
        pattern = f"%{escape_like(params.q)}%"
        columns = (Event.title, Event.host_group, Event.description_text, Event.location_raw)
        filters.append(or_(*(col.ilike(pattern, escape="\\") for col in columns)))
    return filters


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def event_distance(event: Event, near: tuple[float, float] | None) -> float | None:
    if near is None or event.lat is None or event.lng is None:
        return None
    return round(haversine_m(near, (event.lat, event.lng)), 1)


def _start_key(event: Event) -> tuple:
    return (event.starts_at, -event.food_score, event.title, event.id)


def _sorted(rows: list[tuple[Event, float | None]], sort: SortKey) -> list[tuple[Event, float | None]]:
    if sort == SortKey.DISTANCE:
        return sorted(rows, key=lambda r: (r[1] is None, r[1] or 0.0, *_start_key(r[0])))
    return sorted(rows, key=lambda r: _start_key(r[0]))


def list_events(session: Session, params: EventQuery, now: datetime) -> EventPage:
    start, end = window_bounds(params.window, now)
    candidates = session.scalars(select(Event).where(*_sql_filters(params, start, end))).all()
    rows: list[tuple[Event, float | None]] = []
    for event in candidates:
        if not matches_window(event, params.window, start, end):
            continue
        distance = event_distance(event, params.near)
        if params.near is not None and (distance is None or distance > (params.radius_m or 0)):
            continue
        rows.append((event, distance))
    ordered = _sorted(rows, params.sort)
    offset = (params.page - 1) * params.page_size
    page_rows = ordered[offset : offset + params.page_size]
    items = [to_event_out(event, now, distance=dist, truncate=True) for event, dist in page_rows]
    has_more = offset + params.page_size < len(ordered)
    return EventPage(
        window=WindowOut(name=params.window, start=start, end=end),
        total=len(ordered),
        items=items,
        next_page=params.page + 1 if has_more else None,
    )


def _evidence(raw: Sequence) -> list[EvidenceOut]:
    """Map stored EvidenceSpan dicts, skipping malformed entries instead of failing the response."""
    out: list[EvidenceOut] = []
    for item in raw or ():
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        out.append(
            EvidenceOut(
                text=item["text"][:EVIDENCE_TEXT_CHARS],
                field=str(item.get("field", "")),
                kind=str(item.get("kind", "")),
                weight=float(item.get("weight") or 0.0),
            )
        )
    return out


def _geocode_method(value: str | None) -> GeocodeMethod:
    try:
        return GeocodeMethod(value)
    except ValueError:
        return GeocodeMethod.NONE


def _location(event: Event) -> LocationOut:
    return LocationOut(
        raw=event.location_raw,
        building_id=event.building_id,
        building_name=event.building_name,
        lat=event.lat,
        lng=event.lng,
        geocode_method=_geocode_method(event.geocode_method),
        geocode_confidence=event.geocode_confidence or 0.0,
        requires_sign_in=event.location_privacy in PRIVACY_GATED,
    )


def _description(text: str | None, truncate: bool) -> str | None:
    if text is None or not truncate:
        return text
    return text[:LIST_DESCRIPTION_CHARS]


def to_event_out(event: Event, now: datetime, *, distance: float | None, truncate: bool) -> EventOut:
    return EventOut(
        id=event.id,
        source_url=event.source_url,
        title=event.title,
        host_group=event.host_group,
        starts_at=clock.to_campus(event.starts_at),
        ends_at=clock.to_campus(event.ends_at) if event.ends_at is not None else None,
        all_day=event.all_day,
        starts_in_minutes=(event.starts_at - now) // timedelta(minutes=1),
        location=_location(event),
        food=FoodOut(
            score=event.food_score,
            label=FoodLabel(event.food_label),
            evidence=_evidence(event.food_evidence),
        ),
        is_free_to_attend=event.is_free_to_attend,
        free_evidence=[str(x) for x in (event.free_evidence or ())],
        cost_raw=event.cost_raw,
        visibility=Visibility(event.visibility),
        status=EventStatus(event.status),
        event_type=event.event_type,
        tags=[str(t) for t in (event.tags or ())],
        attendance_mode=event.attendance_mode,
        distance_m=distance,
        description_text=_description(event.description_text, truncate),
        updated_at=event.updated_at,
        last_seen_at=event.last_seen_at,
    )


def get_event(session: Session, event_id: str, now: datetime) -> EventOut | None:
    event = session.get(Event, event_id)
    if event is None:
        return None
    return to_event_out(event, now, distance=None, truncate=False)
