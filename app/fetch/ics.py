"""ICS feed fetch: a conditional GET whose 302 to the static host is followed by FetchClient."""

from __future__ import annotations

from app.fetch.client import FetchClient, FetchResult


async def fetch_ics(fc: FetchClient, url: str, *, authenticated: bool) -> FetchResult:
    """Fetch an ICS feed. Validators (ETag/Last-Modified) are stored for the final, redirected URL."""
    return await fc.request(url, method="GET", authenticated=authenticated)
