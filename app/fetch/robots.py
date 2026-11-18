"""Per-host robots.txt cache (RFC 9309 semantics for fetch failures)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.robotparser import RobotFileParser

import httpx

from app import clock

# Fetches a robots.txt URL; returns (HTTP status or None when unreachable, body text).
RobotsFetcher = Callable[[str], Awaitable[tuple[int | None, str]]]

ROBOTS_TTL = timedelta(hours=24)
# A 5xx or unreachable robots.txt disallows the host only briefly ("for that run").
FAILURE_TTL = timedelta(minutes=10)


@dataclass
class _Entry:
    parser: RobotFileParser
    expires_at: datetime


def _parser_for(status: int | None, text: str) -> RobotFileParser:
    parser = RobotFileParser()
    if status is not None and 200 <= status < 300:
        parser.parse(text.splitlines())
    elif status is not None and 400 <= status < 500:
        parser.allow_all = True
    else:
        parser.disallow_all = True
    return parser


class RobotsCache:
    """Fetches ``/robots.txt`` once per host per 24 h and answers ``can_fetch`` from it."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def robots_url(url: str) -> str:
        parsed = httpx.URL(url)
        return str(parsed.copy_with(path="/robots.txt", query=None, fragment=None))

    async def _entry(self, url: str, fetch: RobotsFetcher) -> _Entry:
        host = httpx.URL(url).host
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            entry = self._entries.get(host)
            if entry is not None and entry.expires_at > clock.now():
                return entry
            status, text = await fetch(self.robots_url(url))
            ok = status is not None and status < 500
            ttl = ROBOTS_TTL if ok else FAILURE_TTL
            entry = _Entry(parser=_parser_for(status, text), expires_at=clock.now() + ttl)
            self._entries[host] = entry
            return entry

    async def allowed(self, url: str, fetch: RobotsFetcher) -> bool:
        entry = await self._entry(url, fetch)
        return entry.parser.can_fetch(self.user_agent, url)
