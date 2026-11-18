"""``make demo-auth`` (= ``python -m app.auth demo``): fails loudly without a usable session."""

from __future__ import annotations

import io
import subprocess
import sys

from app import clock
from app.auth import main
from app.config import REPO_ROOT
from app.fetch.replay import ReplayTransport
from app.sync import floor_to
from tests.conftest import CAPTURE_TIME, DUMMY_COOKIE


def _env(tmp_path, cookie: str | None) -> dict[str, str]:
    env = {"FREEFOOD_VAR_DIR": str(tmp_path / "var"), "FREEFOOD_ONLINE_GEOCODING": "0"}
    if cookie:
        env["YALECONNECT_COOKIE"] = cookie
    return env


def test_demo_auth_without_cookie_fails_cleanly(tmp_path):
    out = io.StringIO()
    code = main(["demo"], environ=_env(tmp_path, None), out=out)
    assert code == 2
    assert "YALECONNECT_COOKIE is not set" in out.getvalue() and "DevTools" in out.getvalue()


def test_demo_auth_cli_process_exit_code():
    """The real entry point, as `make demo-auth` runs it, with no credentials in the environment."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(REPO_ROOT)}
    proc = subprocess.run([sys.executable, "-m", "app.auth", "demo"], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "YALECONNECT_COOKIE is not set" in proc.stdout


def test_demo_auth_valid_session_runs_full_sync_and_prints_counts(tmp_path):
    clock.freeze(CAPTURE_TIME)
    transport = ReplayTransport(time_offset=floor_to(CAPTURE_TIME, 5) - CAPTURE_TIME)
    out = io.StringIO()
    code = main(["demo"], environ=_env(tmp_path, DUMMY_COOKIE), transport=transport, out=out)
    text = out.getvalue()
    assert code == 0, text
    assert "anonymous events: 371, authenticated events: 379 (8 authenticated-only" in text
    assert DUMMY_COOKIE not in text


def test_demo_auth_expired_session_exits_non_zero(tmp_path):
    clock.freeze(CAPTURE_TIME)
    out = io.StringIO()
    code = main(["demo"], environ=_env(tmp_path, "CG.SessionID=stale"), transport=ReplayTransport(), out=out)
    assert code == 3
    assert "YALECONNECT_COOKIE was rejected" in out.getvalue()
    assert "stale" not in out.getvalue()
