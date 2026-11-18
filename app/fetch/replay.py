"""Offline transport that answers from the recorded fixtures (seeding and tests)."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path

import httpx

from app.config import FIXTURES_DIR
from app.sources import HOST, ICS_STATIC_HOST

_STATIC_403 = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Error><Code>AccessDenied</Code></Error>"
_AUTH_WINDOW = "upcoming14d"
_AUTH_CURSOR_PREFIX = "FIXTURE-AUTH-CURSOR-"
_JSON_HEADERS = {"cache-control": "no-store", "content-type": "application/json; charset=utf-8"}


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (AttributeError, ValueError):
        return None


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves the recorded responses; anything unknown gets a 404 and is kept in ``unknown``.

    ``time_offset`` is how far the requested windows are shifted from the recorded ones
    (request window minus offset must equal a recorded window). ``personal_feed`` toggles
    whether the personal ICS feed URL from ``auth_fixture_meta.json`` answers.
    """

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR, *, time_offset: timedelta = timedelta(0),
                 personal_feed: bool = True) -> None:
        self.fixtures_dir = fixtures_dir
        self.time_offset = time_offset
        self.personal_feed = personal_feed
        self.requests: list[httpx.Request] = []
        self.unknown: list[httpx.Request] = []
        self.manifest = json.loads((fixtures_dir / "manifest.json").read_text())
        self.meta = json.loads((fixtures_dir / "auth" / "auth_fixture_meta.json").read_text())

    @cached_property
    def _ics_body(self) -> bytes:
        return gzip.decompress((self.fixtures_dir / "ics" / "ical_yale.ics.gz").read_bytes())

    def _file(self, rel: str, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, content=(self.fixtures_dir / rel).read_bytes())

    def _recorded_headers(self, rel: str) -> dict[str, str]:
        headers = dict(self.manifest["files"][rel].get("headers") or {})
        headers.pop("content-length", None)
        return headers

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._route(request)
        if response is None:
            self.unknown.append(request)
            return httpx.Response(404, content=b"not recorded")
        return response

    def _route(self, request: httpx.Request) -> httpx.Response | None:
        host, path = request.url.host, request.url.path
        if request.method == "POST" and host == HOST and path == "/api/v2/graphql":
            return self._graphql(request)
        if request.method != "GET":
            return None
        if path == "/robots.txt":
            if host == HOST:
                return self._file("robots/yaleconnect_robots.txt", headers={"content-type": "text/plain"})
            if host == ICS_STATIC_HOST:
                return httpx.Response(403, headers={"content-type": "application/xml"}, content=_STATIC_403)
            return None
        if host == HOST and str(request.url) == self.meta["personal_feed_url"]:
            if not self.personal_feed:
                return None
            return self._file("auth/personal_feed.ics", headers={"content-type": "text/calendar; charset=utf-8"})
        if host == HOST and path.startswith("/ical/yale/"):
            target = f"https://{ICS_STATIC_HOST}:443{path}"
            return httpx.Response(302, headers={"location": target, "content-type": "text/html"})
        if host == ICS_STATIC_HOST:
            return self._static_ics(request, path)
        return None

    def _static_ics(self, request: httpx.Request, path: str) -> httpx.Response | None:
        name = path.rsplit("/", 1)[-1]
        if name == "ical_yale.ics":
            headers = self._recorded_headers("ics/ical_yale.ics.gz")
            if request.headers.get("if-none-match") == headers.get("etag"):
                return httpx.Response(304, headers={k: headers[k] for k in ("etag", "last-modified")})
            return httpx.Response(200, headers=headers, content=self._ics_body)
        rel = f"ics/{name}"
        if rel in self.manifest["files"]:
            headers = self._recorded_headers(rel)
            if request.headers.get("if-none-match") == headers.get("etag"):
                return httpx.Response(304, headers={"etag": headers.get("etag", "")})
            return self._file(rel, headers=headers)
        return None

    def _graphql(self, request: httpx.Request) -> httpx.Response | None:
        try:
            payload = json.loads(request.content)
        except ValueError:
            return None
        cookie = request.headers.get("cookie")
        if payload.get("operationName") == "SessionProbe":
            if not cookie:
                rel = "graphql/viewer_anonymous.json"
            elif cookie == self.meta["dummy_cookie"]:
                rel = "auth/viewer_authenticated.json"
            else:
                rel = "auth/viewer_expired.json"
            return self._file(rel, headers=_JSON_HEADERS)
        if payload.get("operationName") != "Events":
            return None
        variables = payload.get("variables") or {}
        authed = cookie == self.meta["dummy_cookie"]
        rel = self._events_page(variables, authed)
        return self._file(rel, headers=_JSON_HEADERS) if rel else None

    def _window_name(self, variables: dict) -> str | None:
        date_range = (variables.get("filter") or {}).get("dateRange") or {}
        start, end = _parse_dt(date_range.get("from", "")), _parse_dt(date_range.get("to", ""))
        if start is None or end is None:
            return None
        for name, window in self.manifest["windows"].items():
            if (_parse_dt(window["from"]) == start - self.time_offset
                    and _parse_dt(window["to"]) == end - self.time_offset):
                return name
        return None

    def _events_page(self, variables: dict, authed: bool) -> str | None:
        window = self._window_name(variables)
        if window is None:
            return None
        after = variables.get("after")
        if authed and window == _AUTH_WINDOW:
            return self._auth_page(after)
        for rel, entry in self.manifest["files"].items():
            if not rel.startswith(f"graphql/list_{window}_"):
                continue
            recorded = (entry.get("request_json") or {}).get("variables") or {}
            if recorded.get("after") == after:
                return rel
        return None

    def _auth_page(self, after: str | None) -> str | None:
        if after is None:
            return "auth/list_upcoming14d_auth_p01.json"
        if not after.startswith(_AUTH_CURSOR_PREFIX):
            return None
        rel = f"auth/list_upcoming14d_auth_p{int(after.removeprefix(_AUTH_CURSOR_PREFIX)) + 1:02d}.json"
        return rel if (self.fixtures_dir / rel).exists() else None
