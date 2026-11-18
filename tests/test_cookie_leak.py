"""The session cookie and ICS feed token must never leak: logs, DB, disk cache, API responses."""

from __future__ import annotations

import io
import logging
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.api.app import create_app
from app.api.cache import clear_cache
from app.config import load_settings
from app.db.models import AppState, Event, HttpValidator, SyncRun
from app.fetch.redact import install_redaction
from app.fetch.replay import ReplayTransport
from tests.conftest import DUMMY_COOKIE, DUMMY_ICS_TOKEN, PERSONAL_FEED_URL
from tests.test_sync import full_plan, make_runner

EXPIRED_COOKIE = "CG.SessionID=EXPIRED-SESSION-5d1c0ffee; AWSALB=EXPIRED-ALB-77"
LOGGERS = ("", "httpx", "httpcore", "apscheduler", "app", "app.sync", "app.auth", "app.fetch", "uvicorn")


def _secrets() -> list[str]:
    parts = [DUMMY_COOKIE, EXPIRED_COOKIE, DUMMY_ICS_TOKEN, PERSONAL_FEED_URL]
    parts += [kv.split("=", 1)[1] for cookie in (DUMMY_COOKIE, EXPIRED_COOKIE) for kv in cookie.split("; ")]
    return parts


async def test_full_sync_never_leaks_credentials(auth_settings, session_factory, frozen_now, tmp_path):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    saved = {name: logging.getLogger(name).level for name in LOGGERS}
    logging.getLogger().addHandler(handler)
    try:
        install_redaction(auth_settings)
        for name in LOGGERS:  # after install_redaction, which quiets httpx: we want every trace here
            logging.getLogger(name).setLevel(logging.DEBUG)
        runner = make_runner(auth_settings, session_factory, ReplayTransport())
        good = await runner.run_plan(full_plan())
        assert good.mode.value == "authenticated" and good.authenticated_only_seen >= 8
        # Same credentials, but the session has expired: probe fails, warning names the env var.
        expired = load_settings({"FREEFOOD_VAR_DIR": str(tmp_path / "v2"), "FREEFOOD_ONLINE_GEOCODING": "0",
                                 "YALECONNECT_COOKIE": EXPIRED_COOKIE,
                                 "YALECONNECT_ICS_FEEDS": PERSONAL_FEED_URL})
        install_redaction(expired)
        for name in LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        clock.freeze(frozen_now + timedelta(minutes=30))
        bad = await make_runner(expired, session_factory, ReplayTransport()).run_plan(full_plan())
        assert bad.auth_status.value == "expired"
    finally:
        logging.getLogger().removeHandler(handler)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)

    logs = buf.getvalue()
    assert "sync full #1 authenticated/valid" in logs and "HTTP Request" in logs, "capture must see sync + httpx logs"
    haystacks = {"logs": logs}

    with session_factory() as s:
        haystacks["sync_runs"] = repr([(r.errors, r.source_stats) for r in s.scalars(select(SyncRun))])
        haystacks["validators"] = repr([(v.key, v.url_redacted, v.cache_path) for v in s.scalars(select(HttpValidator))])
        haystacks["app_state"] = repr([(a.key, a.value) for a in s.scalars(select(AppState))])
        haystacks["events"] = repr([(e.source_url, e.description_raw) for e in s.scalars(select(Event))])

    files = []
    for root in (auth_settings.var_dir, expired.var_dir):
        if root.exists():
            for path in root.rglob("*"):
                files.append(str(path))
                if path.is_file() and path.stat().st_size < 20_000_000:
                    haystacks[f"file:{path.name}"] = path.read_bytes().decode("utf-8", errors="replace")
    haystacks["filenames"] = "\n".join(files)

    clear_cache()
    client = TestClient(create_app(auth_settings, session_factory))
    for url in ("/api/meta", "/api/events?window=week", "/api/events?window=week&q=pizza"):
        resp = client.get(url)
        assert resp.status_code == 200
        haystacks[url] = resp.text + repr(dict(resp.headers))
    assert client.get("/api/meta").json()["auth"]["cookie_configured"] is True

    leaks = [(where, secret) for where, text in haystacks.items() for secret in _secrets() if secret in text]
    assert not leaks, f"credential leaked: {leaks[:5]}"
    assert "YALECONNECT_COOKIE" in logs  # the expiry warning names the variable
