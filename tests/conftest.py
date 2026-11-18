"""Shared test fixtures (owned by the integrator).

Tests are hermetic: pytest-socket blocks network access (see pyproject addopts), every test
starts with no YALECONNECT_* variables in the environment, and the clock is reset afterwards.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.config import FIXTURES_DIR, Settings, load_settings
from app.db.session import init_db, make_engine, make_session_factory

AUTH_FIXTURE_META = json.loads((FIXTURES_DIR / "auth" / "auth_fixture_meta.json").read_text())
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text())

# The instant the anonymous fixtures were recorded (UTC). Tests pin "now" relative to this.
CAPTURE_TIME = datetime.strptime(MANIFEST["captured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
DUMMY_COOKIE: str = AUTH_FIXTURE_META["dummy_cookie"]
DUMMY_ICS_TOKEN: str = AUTH_FIXTURE_META["dummy_ics_token"]
PERSONAL_FEED_URL: str = AUTH_FIXTURE_META["personal_feed_url"]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("YALECONNECT_COOKIE", "YALECONNECT_ICS_FEEDS", "FREEFOOD_DB", "FREEFOOD_VAR_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield
    clock.set_clock(None)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def capture_time() -> datetime:
    return CAPTURE_TIME


@pytest.fixture
def frozen_now() -> Iterator[datetime]:
    """Freeze app.clock five minutes after the fixture capture."""
    at = CAPTURE_TIME + timedelta(minutes=5)
    clock.freeze(at)
    yield at
    clock.set_clock(None)


def read_fixture_bytes(rel: str) -> bytes:
    path = FIXTURES_DIR / rel
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes())
    return path.read_bytes()


def read_fixture_text(rel: str) -> str:
    return read_fixture_bytes(rel).decode("utf-8")


def read_fixture_json(rel: str):
    return json.loads(read_fixture_bytes(rel))


def fixture_pages(prefix: str, auth: bool = False) -> list[dict]:
    """All GraphQL list pages for a recorded window, in page order.

    ``prefix`` is ``upcoming14d``, ``past14d`` or ``hot6h`` (anonymous) or ``upcoming14d`` with auth=True.
    """
    folder = FIXTURES_DIR / ("auth" if auth else "graphql")
    pattern = f"list_{prefix}_auth_p*.json" if auth else f"list_{prefix}_p*.json"
    return [json.loads(p.read_text()) for p in sorted(folder.glob(pattern))]


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    """Settings rooted in a temp dir, scheduler off, no online geocoding, no credentials."""
    env = {"FREEFOOD_VAR_DIR": str(tmp_path / "var"), "FREEFOOD_SCHEDULER": "0", "FREEFOOD_ONLINE_GEOCODING": "0"}
    return load_settings(env)


@pytest.fixture
def auth_settings(tmp_path: Path) -> Settings:
    """Like tmp_settings, but with the dummy fixture cookie and personal ICS feed configured."""
    env = {
        "FREEFOOD_VAR_DIR": str(tmp_path / "var"),
        "FREEFOOD_SCHEDULER": "0",
        "FREEFOOD_ONLINE_GEOCODING": "0",
        "YALECONNECT_COOKIE": DUMMY_COOKIE,
        "YALECONNECT_ICS_FEEDS": PERSONAL_FEED_URL,
    }
    return load_settings(env)


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = make_engine(":memory:")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Terminal summary: tests that compute headline numbers (classifier confusion matrix,
# gazetteer hit rate, coverage counts) register lines here so `make test` prints them
# even when output is captured.
# ---------------------------------------------------------------------------

_SUMMARY: list[tuple[str, list[str]]] = []


@pytest.fixture
def terminal_report():
    """Call ``terminal_report(title, lines)`` to print a block in the pytest terminal summary."""

    def _add(title: str, lines: list[str]) -> None:
        _SUMMARY.append((title, list(lines)))

    return _add


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    for title, lines in _SUMMARY:
        terminalreporter.write_sep("=", title)
        for line in lines:
            terminalreporter.write_line(line)
