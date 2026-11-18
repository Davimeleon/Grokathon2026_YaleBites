"""CampusGroups ICS feeds -> IcsEvent records -> (fallback) ParsedEvent."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any, Literal

import icalendar

from app.parse.normalize import compute_content_hash, make_event_id
from app.parse.text import collapse_ws, html_to_text
from app.parse.times import detect_all_day, detect_multi_day, ics_value_to_campus, to_campus_normalized
from app.schemas import EventExtras, IcsEvent, NormalizedEvent, ParsedEvent, ParseError, Visibility
from app.sources import ICS_HIDDEN_LOCATION_TEXT, canonical_source_url, event_id_from_url

log = logging.getLogger(__name__)

_DETAILS_TAIL_RE = re.compile(r"\n?---\nEvent Details: \S*\s*$")
_VEVENT_BLOCK_RE = re.compile(r"BEGIN:VEVENT\r?\n.*?END:VEVENT", re.DOTALL)
_SPLIT_CATEGORIES = frozenset({"event_tags"})


class IcsRecordError(ValueError):
    """A VEVENT that cannot be turned into an IcsEvent."""


def _text(component: Any, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0]
    return str(value).replace("\r\n", "\n")


def _clean_description(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = _DETAILS_TAIL_RE.sub("", raw).strip()
    return stripped or None


def _clean_location(raw: str | None) -> str | None:
    """None for empty values and for the sign-in placeholder."""
    location = collapse_ws(raw)
    if location is None or location == ICS_HIDDEN_LOCATION_TEXT:
        return None
    return location


def _categories(component: Any) -> dict[str, list[str]]:
    """X-CG-CATEGORY parameter -> values. Only event_tags is a comma list; other kinds are one value
    that may itself contain commas (event_type "Lecture, Talk, or Panel")."""
    props = component.get("CATEGORIES")
    if props is None:
        return {}
    out: dict[str, list[str]] = {}
    for prop in props if isinstance(props, list) else [props]:
        kind = prop.params.get("X-CG-CATEGORY", "categories")
        values = [str(v) for v in getattr(prop, "cats", [])]
        if kind in _SPLIT_CATEGORIES:
            cleaned = [c for c in (collapse_ws(v) for v in values) if c]
        else:
            joined = collapse_ws(",".join(values))
            cleaned = [joined] if joined else []
        out.setdefault(kind, []).extend(cleaned)
    return out


def _datetime_prop(component: Any, name: str) -> tuple[Any, str | None] | None:
    prop = component.get(name)
    if prop is None:
        return None
    return prop.dt, prop.params.get("TZID")


def vevent_to_record(component: Any) -> IcsEvent:
    """One icalendar VEVENT -> IcsEvent. Raises on missing UID/DTSTART or unparseable values."""
    uid = collapse_ws(_text(component, "UID"))
    if uid is None:
        raise IcsRecordError("VEVENT without UID")
    start = _datetime_prop(component, "DTSTART")
    if start is None:
        raise IcsRecordError("VEVENT without DTSTART")
    dtstart, all_day = ics_value_to_campus(*start)
    end = _datetime_prop(component, "DTEND")
    dtend = ics_value_to_campus(*end)[0] if end is not None else None
    url = collapse_ws(_text(component, "URL"))
    event_id = event_id_from_url(url or "") or event_id_from_url(_text(component, "CONTACT") or "")
    organizer = component.get("ORGANIZER")
    last_modified = component.get("LAST-MODIFIED")
    return IcsEvent(
        uid=uid,
        url=url,
        event_id=event_id,
        summary=collapse_ws(_text(component, "SUMMARY")),
        description=_clean_description(_text(component, "DESCRIPTION")),
        location=_clean_location(_text(component, "LOCATION")),
        organizer=collapse_ws(organizer.params.get("CN")) if organizer is not None else None,
        dtstart=dtstart,
        dtend=dtend,
        all_day=all_day,
        last_modified=to_campus_normalized(last_modified.dt) if last_modified is not None else None,
        categories=_categories(component),
    )


def _vevents(text: str) -> list[Any]:
    """Every VEVENT component. Falls back to parsing block by block if the calendar as a whole fails."""
    try:
        return list(icalendar.Calendar.from_ical(text).walk("VEVENT"))
    except Exception as exc:  # noqa: BLE001 - a broken feed degrades to per-block parsing
        log.warning("ICS calendar did not parse as a whole (%s); parsing VEVENT blocks individually", exc)
    components: list[Any] = []
    for index, block in enumerate(_VEVENT_BLOCK_RE.findall(text)):
        try:
            components.extend(icalendar.Event.from_ical(block, multiple=True))
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping unparseable VEVENT block #%d: %s", index, exc)
    return components


def parse_ics_records(text: str) -> tuple[list[IcsEvent], list[ParseError]]:
    """Parse a feed into IcsEvent records; a bad VEVENT is logged, reported and skipped."""
    records: list[IcsEvent] = []
    errors: list[ParseError] = []
    for index, component in enumerate(_vevents(text)):
        ref = collapse_ws(_text(component, "UID")) or f"#{index}"
        try:
            for err in getattr(component, "errors", []) or []:
                raise IcsRecordError(f"{err[0]}: {err[1]}")
            records.append(vevent_to_record(component))
        except Exception as exc:  # noqa: BLE001 - one bad VEVENT must never abort a feed
            message = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            log.warning("skipping malformed VEVENT %s: %s", ref, message)
            errors.append(ParseError(source="ics", ref=ref, message=message))
    return records, errors


def ics_uid_index(records: Iterable[IcsEvent]) -> dict[int, IcsEvent]:
    """Numeric CampusGroups event id -> record (records without an id are skipped)."""
    return {rec.event_id: rec for rec in records if rec.event_id is not None}


def _first(categories: dict[str, list[str]], kind: str) -> str | None:
    values = categories.get(kind) or []
    return values[0] if values else None


def record_to_parsed(rec: IcsEvent, *, visibility: Visibility, source: Literal["ics", "ics_personal"]) -> ParsedEvent:
    if rec.event_id is None:
        raise IcsRecordError("no numeric event id in URL or CONTACT")
    title = rec.summary
    if title is None:
        raise IcsRecordError("empty SUMMARY")
    source_url = canonical_source_url(rec.event_id)
    all_day = detect_all_day(rec.dtstart, rec.dtend, date_only=rec.all_day)
    extras = EventExtras(
        cg_event_id=rec.event_id,
        ics_uid=rec.uid,
        source=source,
        # IcsEvent has no placeholder flag; every recorded VEVENT carries a non-empty LOCATION, so a
        # None location means the sign-in placeholder (unknown, not empty).
        location_hidden=rec.location is None,
        all_day=all_day,
        is_multi_day=detect_multi_day(rec.dtstart, rec.dtend, all_day=all_day),
        event_type=_first(rec.categories, "event_type"),
        tags=tuple(rec.categories.get("event_tags", [])),
    )
    draft = NormalizedEvent(
        id=make_event_id(source_url, rec.uid),
        source_url=source_url,
        title=title,
        description_raw=rec.description,
        description_text=html_to_text(rec.description),
        host_group=rec.organizer,
        starts_at=rec.dtstart,
        ends_at=rec.dtend,
        location_raw=rec.location,
        cost_raw=None,
        visibility=visibility,
        updated_at_source=rec.last_modified,
        content_hash="",
    )
    event = draft.model_copy(update={"content_hash": compute_content_hash(draft, extras)})
    return ParsedEvent(event=event, extras=extras)


def ics_events_to_parsed(
    records: Iterable[IcsEvent], *, visibility: Visibility, source: str = "ics"
) -> tuple[list[ParsedEvent], list[ParseError]]:
    """ICS-only normalization (fallback when GraphQL fails, and personal feeds)."""
    events: list[ParsedEvent] = []
    errors: list[ParseError] = []
    for rec in records:
        try:
            events.append(record_to_parsed(rec, visibility=visibility, source=source))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - one bad record must never abort a feed
            message = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            log.warning("skipping ICS record %s: %s", rec.uid, message)
            errors.append(ParseError(source=source, ref=rec.uid, message=message))
    return events, errors
