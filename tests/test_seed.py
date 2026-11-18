"""``make seed``: the real pipeline over the recorded fixtures, shifted to today."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app import clock
from app.db.models import AppState, Event, SyncRun
from app.db.session import make_engine, make_session_factory
from app.seed import main, seed, shift_for_today
from tests.conftest import CAPTURE_TIME


async def test_seed_replays_fixtures_shifted_to_today(tmp_settings):
    clock.freeze(CAPTURE_TIME + timedelta(days=3, hours=-2))  # three days later, early evening
    summary = await seed(tmp_settings)
    assert summary["time_shift_days"] == 3 == shift_for_today()
    assert all(run["complete"] and run["parse_errors"] == 0 for run in summary["runs"])
    engine = make_engine(tmp_settings.db_path)
    with make_session_factory(engine)() as s:
        total = s.scalar(select(func.count()).select_from(Event))
        assert total == summary["events"] and total >= 1250  # union of both recorded windows
        assert s.get(AppState, "dataset").value == {"kind": "fixture_replay", "time_shift_days": 3}
        assert {r.kind for r in s.scalars(select(SyncRun))} == {"seed"}
        now = clock.now()
        soon = s.scalar(select(func.count()).select_from(Event).where(
            Event.starts_at >= now, Event.starts_at < now + timedelta(hours=2)))
        assert soon > 0
        assert s.scalar(select(func.count()).select_from(Event).where(Event.food_label == "yes")) > 0
    engine.dispose()


def test_seed_cli_is_idempotent(tmp_settings, monkeypatch, capsys):
    monkeypatch.setenv("YALE_BITES_VAR_DIR", str(tmp_settings.var_dir))
    monkeypatch.setenv("YALE_BITES_ONLINE_GEOCODING", "0")
    clock.freeze(CAPTURE_TIME)
    assert main([]) == 0
    first = capsys.readouterr().out
    assert main([]) == 0
    second = capsys.readouterr().out
    assert "seeded" in first and first.splitlines()[0] == second.splitlines()[0]
