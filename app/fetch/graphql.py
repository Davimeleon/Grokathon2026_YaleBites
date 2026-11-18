"""GraphQL fetches (events pages, session probe) and tolerant pageInfo readers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.fetch.client import FetchClient, FetchResult
from app.sources import EVENTS_QUERY, GRAPHQL_URL, VIEWER_PROBE_QUERY, events_variables, graphql_body


@dataclass(frozen=True)
class PageInfo:
    end_cursor: str | None
    has_next_page: bool


def iso_z(dt: datetime) -> str:
    """``2026-11-17T22:53:00Z``: the DateTime format the recorded requests use."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch_events_page(
    fc: FetchClient,
    *,
    start: datetime,
    end: datetime,
    after: str | None,
    authenticated: bool,
    logical_key: str | None,
) -> FetchResult:
    body = graphql_body("Events", EVENTS_QUERY, events_variables(iso_z(start), iso_z(end), after))
    return await fc.request(GRAPHQL_URL, method="POST", json_body=body,
                            authenticated=authenticated, logical_key=logical_key)


async def probe_session(fc: FetchClient) -> FetchResult:
    body = graphql_body("SessionProbe", VIEWER_PROBE_QUERY, {})
    return await fc.request(GRAPHQL_URL, method="POST", json_body=body, authenticated=True)


def _load(body: bytes | str | None) -> dict | None:
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def page_info(body: bytes | str | None) -> PageInfo | None:
    """pageInfo of an events response by plain dict access; nodes are never validated."""
    data = _load(body)
    events = ((data or {}).get("data") or {}).get("events")
    if not isinstance(events, dict) or not isinstance(events.get("pageInfo"), dict):
        return None
    info = events["pageInfo"]
    cursor = info.get("endCursor")
    return PageInfo(cursor if isinstance(cursor, str) else None, bool(info.get("hasNextPage")))


def page_info_for(result: FetchResult) -> PageInfo | None:
    """pageInfo from the result body, or from its cached file for unchanged / not-modified pages."""
    if result.body is not None:
        return page_info(result.body)
    if result.cache_path and Path(result.cache_path).exists():
        return page_info(Path(result.cache_path).read_bytes())
    return None


def error_codes(body: bytes | str | None) -> list[str]:
    """``errors[].extensions.code`` values of a GraphQL response."""
    data = _load(body) or {}
    codes = []
    for err in data.get("errors") or []:
        code = ((err or {}).get("extensions") or {}).get("code") if isinstance(err, dict) else None
        if isinstance(code, str):
            codes.append(code)
    return codes


def viewer_present(body: bytes | str | None) -> bool:
    """True when the probe answered ``data.viewer`` as an object."""
    data = _load(body) or {}
    return isinstance((data.get("data") or {}).get("viewer"), dict)
