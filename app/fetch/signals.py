"""Pure detectors for an expired Yale Connect session (shared by the fetch client and AuthManager)."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

_LOGIN_MARKERS = ("Central Authentication Service", 'id="fm1"', 'name="username"')


def is_cas_redirect(status: int | None, headers: Mapping[str, str]) -> bool:
    """A 3xx whose Location points at CAS (``/cas/``) or a ``/login`` page."""
    if status is None or not 300 <= status < 400:
        return False
    location = next((v for k, v in headers.items() if k.lower() == "location"), "")
    if not location:
        return False
    path = httpx.URL(location).path.lower()
    return "/cas/" in path or path.startswith("/cas") or "/login" in path


def looks_like_login_page(body: bytes | str | None) -> bool:
    """True when a body is the CAS login form (two of the known markers)."""
    if not body:
        return False
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    head = text[:20000]
    return sum(marker in head for marker in _LOGIN_MARKERS) >= 2
