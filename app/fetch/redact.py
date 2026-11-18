"""Redaction of credentials (session cookie, ICS feed tokens) from URLs, text and log records.

Secrets are registered process-wide (``register_secret``) so that every redaction path, the
request keys and cache filenames built from URLs included, masks them. Each mask carries a
short digest so two different feeds never collapse onto the same request key.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.config import Settings

_SECRETS: set[str] = set()

# A run of 16+ token characters that mixes letters and digits ("FIXTURE-ICS-TOKEN-4e8b1a6c9d2f").
_TOKEN_RUN = re.compile(r"(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{16,}")
_PERSONAL_SEGMENT = re.compile(r"(personal[_-]?)([^/.]+)", re.IGNORECASE)
_SENSITIVE_QUERY_NAMES = {
    "token", "key", "apikey", "api_key", "auth", "session", "sessionid", "sig", "signature",
    "secret", "password", "code", "access_token", "id_token", "ticket", "hash", "uid", "u",
}


def _mask_for(secret: str) -> str:
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]
    return f"[REDACTED-{digest}]"


def register_secret(value: str) -> None:
    """Mask ``value`` wherever redaction runs from now on (ignored when blank or very short)."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def clear_secrets() -> None:
    """Forget every registered secret (tests use this to isolate the process-wide registry)."""
    _SECRETS.clear()


def cookie_secret_parts(cookie_header: str) -> list[str]:
    """The full Cookie header value, each ``name=value`` pair and each value inside it."""
    parts = [cookie_header.strip()]
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        parts.append(pair)
        if "=" in pair:
            parts.append(pair.split("=", 1)[1].strip())
    return [p for p in parts if p]


def feed_secret_parts(feed_url: str) -> list[str]:
    """The full feed URL plus the token-looking parts of its path and query."""
    parts = [feed_url.strip()]
    split = urlsplit(feed_url.strip())
    for segment in split.path.split("/"):
        parts += _TOKEN_RUN.findall(segment)
        match = _PERSONAL_SEGMENT.search(segment)
        if match:
            parts.append(match.group(2))
    last = split.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if last:
        parts.append(last)
    parts += [value for _, value in parse_qsl(split.query) if len(value) >= 8]
    return [p for p in parts if p]


def _mask_all(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and secret in text:
            text = text.replace(secret, _mask_for(secret))
    return text


def redact_text(text: str, extra: Iterable[str] = ()) -> str:
    """Replace every registered (and ``extra``) secret in ``text``, longest first."""
    if not text:
        return text
    return _mask_all(text, _SECRETS.union(extra))


def _redact_path(path: str) -> str:
    segments = []
    for segment in path.split("/"):
        segment = _PERSONAL_SEGMENT.sub(lambda m: m.group(1) + _mask_for(m.group(2)), segment)
        segment = _TOKEN_RUN.sub(lambda m: _mask_for(m.group(0)), segment)
        segments.append(segment)
    return "/".join(segments)


def _looks_like_token(value: str) -> bool:
    return bool(_TOKEN_RUN.fullmatch(value)) or len(value) >= 32


def _redact_query(query: str) -> str:
    if not query:
        return query
    pairs = []
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name.lower() in _SENSITIVE_QUERY_NAMES or _looks_like_token(value):
            value = _mask_for(value) if value else value
        pairs.append((name, value))
    return urlencode(pairs, safe="[]-")


def redact_url(url: str) -> str:
    """URL with ICS tokens, personal-feed path parts and token-like query values masked.

    Patterns run first so the host survives; registered secret parts are masked next, and a
    registered whole URL that is still intact is masked entirely as the last resort.
    """
    split = urlsplit(url)
    netloc = split.hostname or ""
    if split.port:
        netloc = f"{netloc}:{split.port}"
    redacted = urlunsplit((split.scheme, netloc, _redact_path(split.path), _redact_query(split.query), ""))
    redacted = _mask_all(redacted, [s for s in _SECRETS if "://" not in s])
    return redact_text(redacted)


class SecretRedactor(logging.Filter):
    """Logging filter that rewrites msg, args, exception and stack text with secrets masked."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets: set[str] = {s for s in secrets if s}

    def add(self, secrets: Iterable[str]) -> None:
        self.secrets.update(s for s in secrets if s)

    def _clean(self, text: str) -> str:
        return redact_text(text, self.secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = f"{record.msg} {record.args}"
        record.msg = self._clean(message)
        record.args = None
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_text = self._clean(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        if record.stack_info:
            record.stack_info = self._clean(record.stack_info)
        return True


def settings_secrets(settings: Settings) -> list[str]:
    """Every secret string derived from the configured credentials."""
    secrets: list[str] = []
    if settings.yaleconnect_cookie:
        secrets += cookie_secret_parts(settings.yaleconnect_cookie.reveal())
    for feed in settings.yaleconnect_ics_feeds:
        secrets += feed_secret_parts(feed.reveal())
    return secrets


def _all_handlers() -> list[logging.Handler]:
    handlers = list(logging.getLogger().handlers)
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            handlers += logger.handlers
    return handlers


def install_redaction(settings: Settings) -> SecretRedactor:
    """Register the configured secrets and attach one redactor to the root logger and all handlers."""
    secrets = settings_secrets(settings)
    for secret in secrets:
        register_secret(secret)
    root = logging.getLogger()
    redactor = next((f for f in root.filters if isinstance(f, SecretRedactor)), None)
    if redactor is None:
        redactor = SecretRedactor(secrets)
        root.addFilter(redactor)
    else:
        redactor.add(secrets)
    for handler in _all_handlers():
        if redactor not in handler.filters:
            handler.addFilter(redactor)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return redactor
