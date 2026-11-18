"""Redaction of the session cookie and ICS feed tokens from URLs and log records."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from app.fetch import redact
from app.fetch.redact import SecretRedactor, install_redaction, redact_text, redact_url
from tests.conftest import DUMMY_COOKIE, DUMMY_ICS_TOKEN, PERSONAL_FEED_URL

COOKIE_VALUES = ("FIXTURE-SESSION-COOKIE-7f3a9c2e5b1d", "FIXTURE-ALB-STICKY")


@pytest.fixture
def clean_logging() -> Iterator[None]:
    yield
    redact.clear_secrets()
    for logger in [logging.getLogger(), *[lg for lg in logging.Logger.manager.loggerDict.values()
                                         if isinstance(lg, logging.Logger)]]:
        for flt in [f for f in logger.filters if isinstance(f, SecretRedactor)]:
            logger.removeFilter(flt)
        for handler in logger.handlers:
            for flt in [f for f in handler.filters if isinstance(f, SecretRedactor)]:
                handler.removeFilter(flt)


def test_redact_url_masks_personal_feed_and_token_queries():
    out = redact_url(PERSONAL_FEED_URL)
    assert DUMMY_ICS_TOKEN not in out and out.startswith("https://yaleconnect.yale.edu/ical/yale/ical_personal_")
    q = redact_url("https://example.org/feed.ics?token=abc123secretvalue&id=2326515&x=" + "a1b2" * 10)
    assert "abc123secretvalue" not in q and "a1b2a1b2" not in q and "id=2326515" in q
    assert redact_url("https://yaleconnect.yale.edu/api/v2/graphql") == "https://yaleconnect.yale.edu/api/v2/graphql"
    assert redact_url("https://static-prod-us-east-1.campusgroups.com:443/ical/yale/ical_yale.ics").endswith(
        ":443/ical/yale/ical_yale.ics")


def test_two_feeds_keep_distinct_redacted_urls():
    a = redact_url("https://yaleconnect.yale.edu/ical/yale/ical_personal_TOKENaaaa1111bbbb2222.ics")
    b = redact_url("https://yaleconnect.yale.edu/ical/yale/ical_personal_TOKENcccc3333dddd4444.ics")
    assert a != b


def test_filter_masks_message_args_and_exception(clean_logging):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretRedactor([DUMMY_COOKIE, *COOKIE_VALUES, PERSONAL_FEED_URL, DUMMY_ICS_TOKEN]))
    logger = logging.getLogger("test.redact.filter")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("cookie=%s feed=%s", DUMMY_COOKIE, PERSONAL_FEED_URL)
        logger.warning("inline " + COOKIE_VALUES[0])
        try:
            raise RuntimeError(f"failed GET {PERSONAL_FEED_URL} with {DUMMY_COOKIE}")
        except RuntimeError:
            logger.exception("fetch failed for %s", DUMMY_ICS_TOKEN)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    text = stream.getvalue()
    for secret in (DUMMY_COOKIE, *COOKIE_VALUES, DUMMY_ICS_TOKEN):
        assert secret not in text
    assert "RuntimeError" in text and "[REDACTED-" in text


def test_install_redaction_attaches_everywhere(auth_settings, clean_logging):
    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(logging.Formatter("%(message)s"))
    other = logging.getLogger("test.redact.install")
    other.addHandler(handler)
    try:
        redactor = install_redaction(auth_settings)
        assert install_redaction(auth_settings) is redactor
        assert redactor in logging.getLogger().filters and redactor in handler.filters
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        other.error("token %s and %s", DUMMY_ICS_TOKEN, COOKIE_VALUES[1])
        assert DUMMY_ICS_TOKEN not in handler.stream.getvalue()
        assert COOKIE_VALUES[1] not in handler.stream.getvalue()
        assert DUMMY_ICS_TOKEN not in redact_text(f"see {PERSONAL_FEED_URL}")
        after_install = redact_url(PERSONAL_FEED_URL)
        assert after_install.startswith("https://yaleconnect.yale.edu/ical/") and DUMMY_ICS_TOKEN not in after_install
    finally:
        other.removeHandler(handler)


def test_format_errors_do_not_drop_records(clean_logging):
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d items " + DUMMY_COOKIE, ("nope",), None)
    assert SecretRedactor([DUMMY_COOKIE]).filter(record)
    assert DUMMY_COOKIE not in record.getMessage()
