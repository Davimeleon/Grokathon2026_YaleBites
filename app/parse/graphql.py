"""GraphQL ``events`` / ``event(id:)`` responses -> ParsedEvent, one node at a time."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any, Literal

from app.parse.normalize import build_cost_raw, build_location_raw, compute_content_hash, make_event_id
from app.parse.text import collapse_ws, html_to_text
from app.parse.times import detect_all_day, detect_multi_day, parse_iso_utc
from app.schemas import EventExtras, GqlEventNode, NormalizedEvent, ParsedEvent, ParseError, Visibility
from app.sources import canonical_source_url, decode_event_global_id

log = logging.getLogger(__name__)

_HIDDEN_PRIVACY = frozenset({"SIGN_IN_REQUIRED", "REGISTRATION_REQUIRED"})
SourceName = Literal["graphql", "ics", "ics_personal"]


class NodeError(ValueError):
    """A node that validates structurally but cannot be normalized."""


def _node_ref(node: Any, index: int) -> str:
    if isinstance(node, dict) and isinstance(node.get("id"), str):
        return node["id"]
    return f"#{index}"


def _description(node: GqlEventNode) -> tuple[str | None, str | None]:
    html_value = node.description_html if node.description_html and node.description_html.strip() else None
    raw = html_value if html_value is not None else (node.description_text or None)
    return raw, html_to_text(raw)


def _extras(node: GqlEventNode, cg_id: int, source: SourceName, *, all_day: bool, multi_day: bool,
            location_hidden: bool, address: str | None) -> EventExtras:
    loc = node.location
    coords = loc.coordinates if loc is not None else None
    return EventExtras(
        cg_event_id=cg_id,
        graphql_id=node.id,
        source=source,
        is_food_included=node.is_food_included,
        food_details=collapse_ws(node.food_details),
        location_privacy=loc.event_location_privacy if loc is not None else None,
        location_hidden=location_hidden,
        location_kind=loc.kind if loc is not None else None,
        location_address=address,
        source_lat=coords.latitude if coords is not None else None,
        source_lng=coords.longitude if coords is not None else None,
        attendance_mode=node.attendance_mode,
        all_day=all_day,
        is_multi_day=multi_day,
        event_status=node.status,
        event_type=collapse_ws(node.type.value) if node.type is not None else None,
        tags=tuple(t for t in (collapse_ws(tag.name) for tag in node.tags) if t),
        cohost_groups=tuple(g for g in (collapse_ws(c.name) for c in node.cohost_groups) if g),
        parent_title=collapse_ws(node.parent.name) if node.parent is not None else None,
    )


def node_to_parsed(raw: dict, *, visibility: Visibility, source: SourceName) -> ParsedEvent:
    """Validate and normalize one GraphQL node. Raises on any malformed input."""
    node = GqlEventNode.model_validate(raw)
    cg_id = decode_event_global_id(node.id)
    if cg_id is None:
        raise NodeError(f"id {node.id!r} does not decode to a numeric event id")
    title = collapse_ws(node.name)
    if title is None:
        raise NodeError("empty title")
    starts_at = parse_iso_utc(node.start_date)
    if starts_at is None:
        raise NodeError("missing startDate")
    ends_at = parse_iso_utc(node.end_date)
    all_day = detect_all_day(starts_at, ends_at)
    multi_day = bool(node.is_multi_day) or detect_multi_day(starts_at, ends_at, all_day=all_day)
    location_raw, address = build_location_raw(node.location)
    privacy = node.location.event_location_privacy if node.location is not None else None
    location_hidden = privacy in _HIDDEN_PRIVACY and collapse_ws(node.location.name) is None
    description_raw, description_text = _description(node)
    source_url = canonical_source_url(cg_id)
    extras = _extras(node, cg_id, source, all_day=all_day, multi_day=multi_day,
                     location_hidden=location_hidden, address=address)
    draft = NormalizedEvent(
        id=make_event_id(source_url, None),
        source_url=source_url,
        title=title,
        description_raw=description_raw,
        description_text=description_text,
        host_group=collapse_ws(node.host_group.name) if node.host_group is not None else None,
        starts_at=starts_at,
        ends_at=ends_at,
        location_raw=location_raw,
        cost_raw=build_cost_raw(node.registration, node.external_registration),
        visibility=visibility,
        content_hash="",
    )
    event = draft.model_copy(update={"content_hash": compute_content_hash(draft, extras)})
    return ParsedEvent(event=event, extras=extras)


def parse_event_nodes(
    nodes: Iterable[dict], *, visibility: Visibility = Visibility.PUBLIC, source: str = "graphql"
) -> tuple[list[ParsedEvent], list[ParseError]]:
    """Normalize nodes one by one; a malformed node becomes a logged ParseError and is skipped."""
    events: list[ParsedEvent] = []
    errors: list[ParseError] = []
    for index, raw in enumerate(nodes):
        ref = _node_ref(raw, index)
        try:
            if not isinstance(raw, dict):
                raise NodeError(f"node is {type(raw).__name__}, not an object")
            events.append(node_to_parsed(raw, visibility=visibility, source=source))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - one bad node must never abort a page
            message = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
            log.warning("skipping malformed GraphQL event node %s: %s", ref, message)
            errors.append(ParseError(source=source, ref=ref, message=message))
    return events, errors


def _load_payload(payload: dict | str | bytes) -> Any:
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _graphql_errors(body: dict, source: str) -> list[ParseError]:
    errors = body.get("errors") or []
    out: list[ParseError] = []
    for err in errors if isinstance(errors, list) else [errors]:
        code = None
        message = str(err)
        if isinstance(err, dict):
            message = str(err.get("message", err))
            code = (err.get("extensions") or {}).get("code") if isinstance(err.get("extensions"), dict) else None
        out.append(ParseError(source=source, ref=code, message=f"GraphQL error: {message}"))
    return out


def _payload_or_error(payload: dict | str | bytes, source: str) -> tuple[dict | None, list[ParseError]]:
    try:
        body = _load_payload(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        return None, [ParseError(source=source, ref=None, message=f"invalid JSON: {exc}")]
    if not isinstance(body, dict):
        return None, [ParseError(source=source, ref=None, message="response is not a JSON object")]
    return body, _graphql_errors(body, source)


def _extract_edges(body: dict) -> tuple[list[Any], dict | None]:
    data = body.get("data")
    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, dict):
        return [], None
    edges = events.get("edges") or []
    nodes = [edge.get("node") if isinstance(edge, dict) else edge for edge in edges]
    page = events.get("pageInfo")
    page_info = None
    if isinstance(page, dict):
        page_info = {"endCursor": page.get("endCursor"), "hasNextPage": bool(page.get("hasNextPage"))}
    return nodes, page_info


def parse_events_response(
    payload: dict | str | bytes, *, visibility: Visibility = Visibility.PUBLIC
) -> tuple[list[ParsedEvent], list[ParseError], dict | None]:
    """One ``events`` connection page -> (events, errors, {"endCursor", "hasNextPage"} or None)."""
    body, errors = _payload_or_error(payload, "graphql")
    if body is None:
        return [], errors, None
    nodes, page_info = _extract_edges(body)
    if page_info is None and not errors:
        errors.append(ParseError(source="graphql", ref=None, message="response has no data.events"))
    events, node_errors = parse_event_nodes(nodes, visibility=visibility)
    return events, errors + node_errors, page_info


def parse_event_detail(
    payload: dict | str | bytes, *, visibility: Visibility = Visibility.PUBLIC
) -> tuple[ParsedEvent | None, list[ParseError]]:
    """An ``event(id:)`` response -> (event or None, errors). EVENT_NOT_AVAILABLE gives None + error."""
    body, errors = _payload_or_error(payload, "graphql")
    if body is None:
        return None, errors
    data = body.get("data")
    node = data.get("event") if isinstance(data, dict) else None
    if node is None:
        if not errors:
            errors.append(ParseError(source="graphql", ref=None, message="response has no data.event"))
        return None, errors
    events, node_errors = parse_event_nodes([node], visibility=visibility)
    return (events[0] if events else None), errors + node_errors
