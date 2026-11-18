"""60-second response memo for /api/*, keyed by path and normalized query string.

Time comes from ``app.clock`` so tests can advance it; the sync calls ``clear_cache()`` after every run.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode

from app import clock

TTL = timedelta(seconds=60)

_memo: dict[tuple[str, str], tuple[datetime, bytes]] = {}


def cache_key(path: str, query: str) -> tuple[str, str]:
    """Key a request by path plus its query parameters sorted, so parameter order does not matter."""
    return path, urlencode(sorted(parse_qsl(query, keep_blank_values=True)))


def get_cached(key: tuple[str, str]) -> bytes | None:
    entry = _memo.get(key)
    if entry is None:
        return None
    stored_at, body = entry
    if clock.now() - stored_at >= TTL:
        _memo.pop(key, None)
        return None
    return body


def put_cached(key: tuple[str, str], body: bytes) -> None:
    _memo[key] = (clock.now(), body)


def clear_cache() -> None:
    _memo.clear()
