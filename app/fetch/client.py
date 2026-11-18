"""The one polite HTTP client: robots, spacing, concurrency cap, backoff, validators, disk cache.

Lock order per request: robots check (its own per-host lock) -> per-host spacing lock (held only
while waiting and stamping the time) -> global semaphore (held only around send + body read).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.config import Settings
from app.db.models import HttpValidator
from app.fetch.redact import redact_text, redact_url, register_secret, settings_secrets
from app.fetch.robots import RobotsCache
from app.fetch.signals import is_cas_redirect, looks_like_login_page
from app.sources import HOST

MAX_CONCURRENCY = 4
HOST_SPACING_S = 0.25
MAX_RETRIES = 4
BACKOFF_BASE_S = 0.5
RETRY_AFTER_CAP_S = 60.0
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class FetchResult:
    status: str
    http_status: int | None
    body: bytes | None
    headers: dict[str, str]
    request_key: str
    item_keys: list[str] = field(default_factory=list)
    error: str | None = None
    url_redacted: str = ""
    cache_path: str | None = None


@dataclass
class _Raw:
    status: int
    headers: dict[str, str]
    body: bytes | None


@dataclass
class _Pending:
    etag: str | None
    last_modified: str | None
    body_sha256: str | None


class FetchError(Exception):
    """Transport failure that survived every retry."""


def request_key(method: str, url_redacted: str, body: bytes | None, authenticated: bool,
                logical_key: str | None = None) -> str:
    """sha256 over redacted method+URL+body (or the logical key) and the auth mode."""
    mode = "auth" if authenticated else "anon"
    if logical_key:
        material = f"logical\n{mode}\n{redact_text(logical_key)}"
    else:
        body_digest = hashlib.sha256(body).hexdigest() if body else ""
        material = f"{method.upper()}\n{mode}\n{url_redacted}\n{body_digest}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_ext(content_type: str) -> str:
    ct = content_type.lower()
    for marker, ext in (("json", "json"), ("calendar", "ics"), ("html", "html"), ("text", "txt")):
        if marker in ct:
            return ext
    return "bin"


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - clock.now()).total_seconds())


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def cookie_allowed(url: httpx.URL) -> bool:
    """The session cookie may only ever travel to https://yaleconnect.yale.edu."""
    return url.scheme == "https" and url.host == HOST


class FetchClient:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sessions = session_factory
        self._sleep = sleep
        self._rng = rng
        self._monotonic = monotonic
        for secret in settings_secrets(settings):
            register_secret(secret)
        # A jar that accepts nothing: server Set-Cookie must never leak onto anonymous requests.
        no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        self._client = httpx.AsyncClient(transport=transport, timeout=TIMEOUT, follow_redirects=False,
                                         cookies=no_cookies)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._pending: dict[str, _Pending] = {}
        self.robots = RobotsCache(settings.user_agent)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- politeness -------------------------------------------------------------------------

    async def _wait_turn(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_request.get(host)
            if last is not None:
                wait = HOST_SPACING_S - (self._monotonic() - last)
                if wait > 0:
                    await self._sleep(wait)
            self._last_request[host] = self._monotonic()

    async def _send_once(self, request: httpx.Request) -> _Raw:
        async with self._semaphore:  # take a slot first, so time spent queueing never eats the spacing
            await self._wait_turn(request.url.host)
            response = await self._client.send(request, stream=True)
            try:
                body = await response.aread() if 200 <= response.status_code < 300 else None
            finally:
                await response.aclose()
        return _Raw(response.status_code, dict(response.headers), body)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        honored = _retry_after_seconds(retry_after)
        if honored is not None:
            return min(honored, RETRY_AFTER_CAP_S)
        return BACKOFF_BASE_S * (2**attempt) * (0.5 + self._rng())

    async def _send(self, request: httpx.Request) -> _Raw:
        """Send with exponential backoff and jitter on 429/5xx and transport errors."""
        for attempt in range(MAX_RETRIES + 1):
            last_try = attempt == MAX_RETRIES
            try:
                raw = await self._send_once(request)
            except httpx.TransportError as exc:
                if last_try:
                    raise FetchError(f"{type(exc).__name__}: {redact_text(str(exc))}") from None
                await self._sleep(self._backoff(attempt, None))
                continue
            if _retryable(raw.status) and not last_try:
                await self._sleep(self._backoff(attempt, raw.headers.get("retry-after")))
                continue
            return raw
        raise FetchError("retries exhausted")

    async def _fetch_robots(self, robots_url: str) -> tuple[int | None, str]:
        request = self._client.build_request("GET", robots_url, headers=self._base_headers())
        try:
            raw = await self._send(request)
        except FetchError:
            return None, ""
        return raw.status, (raw.body or b"").decode("utf-8", "replace")

    def _base_headers(self) -> dict[str, str]:
        return {"User-Agent": self.settings.user_agent, "Accept": "*/*"}

    # ---- validators -------------------------------------------------------------------------

    def _load_validator(self, key: str) -> HttpValidator | None:
        with self._sessions() as session:
            return session.get(HttpValidator, key)

    def _save_validator(self, key: str, url_redacted: str, authenticated: bool, status: int,
                        cache_path: str | None) -> None:
        with self._sessions() as session:
            row = session.get(HttpValidator, key)
            if row is None:
                row = HttpValidator(key=key, item_keys=[])
                session.add(row)
            row.url_redacted = url_redacted
            row.authenticated = authenticated
            row.status_code = status
            row.fetched_at = clock.now()
            if cache_path is not None:
                row.cache_path = cache_path
            session.commit()

    def record_items(self, request_key: str, item_keys: list[str]) -> None:
        """Store the item keys of a successfully parsed body and arm its validators / body hash.

        Validators are only armed here, so a body that failed to parse is fetched and parsed
        again next time instead of answering 304/unchanged with an empty item list.
        """
        with self._sessions() as session:
            row = session.get(HttpValidator, request_key)
            if row is None:
                return
            row.item_keys = list(item_keys)
            pending = self._pending.pop(request_key, None)
            if pending is not None:
                row.etag = pending.etag
                row.last_modified = pending.last_modified
                row.body_sha256 = pending.body_sha256
            session.commit()

    # ---- disk cache -------------------------------------------------------------------------

    def _write_cache(self, key: str, authenticated: bool, content_type: str, body: bytes) -> str:
        root: Path = self.settings.auth_cache_dir if authenticated else self.settings.public_cache_dir
        folder = root / clock.now().strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{key}.{_cache_ext(content_type)}"
        path.write_bytes(body)
        return str(path)

    # ---- request ----------------------------------------------------------------------------

    def _build(self, method: str, url: str, content: bytes | None, authenticated: bool,
               validator: HttpValidator | None, json_request: bool) -> httpx.Request:
        headers = self._base_headers()
        if json_request:
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
        if validator is not None and validator.etag:
            headers["If-None-Match"] = validator.etag
        if validator is not None and validator.last_modified:
            headers["If-Modified-Since"] = validator.last_modified
        parsed = httpx.URL(url)
        if authenticated and cookie_allowed(parsed) and self.settings.yaleconnect_cookie:
            headers["Cookie"] = self.settings.yaleconnect_cookie.reveal()
        return self._client.build_request(method, url, headers=headers, content=content)

    async def request(
        self,
        url: str,
        *,
        method: str = "GET",
        json_body: dict | None = None,
        authenticated: bool = False,
        logical_key: str | None = None,
    ) -> FetchResult:
        """One logical fetch. Redirects are followed manually (never to CAS), one hop at a time."""
        content = json.dumps(json_body, separators=(",", ":")).encode("utf-8") if json_body is not None else None
        hop_url = url
        for hop in range(MAX_REDIRECTS + 1):
            result_or_next = await self._hop(hop_url, method, content, authenticated,
                                             logical_key if hop == 0 else None)
            if isinstance(result_or_next, FetchResult):
                return result_or_next
            hop_url = result_or_next
        url_r = redact_url(hop_url)
        return FetchResult("error", None, None, {}, request_key(method, url_r, content, authenticated),
                           error="too many redirects", url_redacted=url_r)

    async def _hop(self, url: str, method: str, content: bytes | None, authenticated: bool,
                   logical_key: str | None) -> FetchResult | str:
        url_r = redact_url(url)
        key = request_key(method, url_r, content, authenticated, logical_key)

        def result(status: str, http_status: int | None = None, **kw: object) -> FetchResult:
            return FetchResult(status, http_status, kw.pop("body", None), kw.pop("headers", {}), key,
                               url_redacted=url_r, **kw)  # type: ignore[arg-type]

        if not await self.robots.allowed(url, self._fetch_robots):
            return result("robots_disallowed", error=f"robots.txt disallows {url_r}")
        validator = self._load_validator(key)
        request = self._build(method, url, content, authenticated, validator, content is not None)
        try:
            raw = await self._send(request)
        except FetchError as exc:
            return result("error", error=redact_text(str(exc)))
        stored_items = list(validator.item_keys) if validator is not None else []
        cache_path = validator.cache_path if validator is not None else None
        if 300 <= raw.status < 400 and raw.status != 304:
            return self._redirect(url, raw, result)
        if raw.status == 304:
            return result("not_modified", 304, headers=raw.headers, item_keys=stored_items, cache_path=cache_path)
        if raw.status >= 400 or raw.body is None:
            return result("error", raw.status, headers=raw.headers, error=f"HTTP {raw.status} for {url_r}")
        if authenticated and looks_like_login_page(raw.body):
            return result("auth_redirect", raw.status, headers=raw.headers, error="session answered with a login page")
        return self._store_body(key, url_r, method, authenticated, raw, validator, result)

    def _redirect(self, url: str, raw: _Raw, result: Callable[..., FetchResult]) -> FetchResult | str:
        location = raw.headers.get("location", "")
        if is_cas_redirect(raw.status, raw.headers):
            return result("auth_redirect", raw.status, headers={}, error="redirected to sign-in")
        if not location:
            return result("error", raw.status, error="redirect without Location")
        return str(httpx.URL(url).join(location))

    def _store_body(self, key: str, url_r: str, method: str, authenticated: bool, raw: _Raw,
                    validator: HttpValidator | None, result: Callable[..., FetchResult]) -> FetchResult:
        body = raw.body or b""
        sha = hashlib.sha256(body).hexdigest()
        if (method.upper() == "POST" and validator is not None and validator.body_sha256 == sha
                and validator.cache_path and Path(validator.cache_path).exists()):
            self._save_validator(key, url_r, authenticated, raw.status, None)
            return result("unchanged", raw.status, headers=raw.headers, item_keys=list(validator.item_keys),
                          cache_path=validator.cache_path)
        path = self._write_cache(key, authenticated, raw.headers.get("content-type", ""), body)
        self._save_validator(key, url_r, authenticated, raw.status, path)
        self._pending[key] = _Pending(raw.headers.get("etag"), raw.headers.get("last-modified"),
                                      sha if method.upper() == "POST" else None)
        return result("ok", raw.status, body=body, headers=raw.headers, cache_path=path)
