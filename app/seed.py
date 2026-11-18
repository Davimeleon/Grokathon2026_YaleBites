"""``make seed``: rebuild the database from the recorded fixtures through the real sync pipeline (offline).

The recorded windows (tests/data/fixtures/manifest.json: the 14 days before and after the capture) are
replayed through FetchClient over ReplayTransport, parsed, enriched (no online geocoding) and upserted
exactly like a live sync. Every event is then shifted by a whole number of days in America/New_York
wall-clock time, so the capture date lines up with today and "free food in the next 2 hours" has data
whenever the app is demoed. /api/meta reports dataset.kind = "fixture_replay" and the shift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, select

from app import clock
from app.config import FIXTURES_DIR, Settings, load_settings
from app.db.models import Event
from app.db.session import init_db, make_engine, make_session_factory
from app.fetch.replay import ReplayTransport
from app.schemas import SyncKind
from app.sync import RunPlan, build_runtime, set_state

SEED_WINDOWS = ("past14d", "upcoming14d")


def capture_time() -> datetime:
    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text())
    return datetime.strptime(manifest["captured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def shift_for_today(now: datetime | None = None) -> int:
    """Whole days between the capture date and today, both in America/New_York."""
    today = clock.to_campus(now or clock.now()).date()
    return (today - clock.to_campus(capture_time()).date()).days


def _reset_db(settings: Settings) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = settings.db_path.with_name(settings.db_path.name + suffix)
        if path.exists():
            path.unlink()


async def seed(settings: Settings) -> dict:
    settings = replace(settings, online_geocoding=False, scheduler_enabled=False)
    _reset_db(settings)
    engine = make_engine(settings.db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text())
    days = shift_for_today()

    runner = await build_runtime(settings, factory, transport=ReplayTransport(), online=False)
    results = []
    try:
        for name in SEED_WINDOWS:
            window = manifest["windows"][name]
            plan = RunPlan(
                kind=SyncKind.SEED,
                start=datetime.fromisoformat(window["from"].replace("Z", "+00:00")),
                end=datetime.fromisoformat(window["to"].replace("Z", "+00:00")),
                use_ics=True,
                allow_auth=False,
                shift_days=days,
                count_absence=False,
                key_scope=f"seed:{name}",
            )
            results.append(await runner.run_plan(plan))
    finally:
        await runner.aclose()

    with factory() as session:
        set_state(session, "dataset", {"kind": "fixture_replay", "time_shift_days": days})
        session.commit()
        total = session.scalar(select(func.count()).select_from(Event))
        labels = dict(session.execute(select(Event.food_label, func.count()).group_by(Event.food_label)).all())
        pinned = session.scalar(select(func.count()).select_from(Event).where(Event.lat.is_not(None)))
    engine.dispose()
    return {
        "db": str(settings.db_path),
        "events": total,
        "by_food_label": labels,
        "geocoded": pinned,
        "time_shift_days": days,
        "runs": [{"kind": r.kind.value, "complete": r.complete, "seen": r.events_seen, "created": r.events_created,
                  "parse_errors": r.parse_errors, "errors": r.errors} for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    summary = asyncio.run(seed(load_settings()))
    print(f"seeded {summary['events']} events into {summary['db']}")
    print(f"  food labels: {summary['by_food_label']}")
    print(f"  geocoded (pinned): {summary['geocoded']}")
    print(f"  time shift: {summary['time_shift_days']} days (fixture capture date -> today, America/New_York)")
    for run in summary["runs"]:
        print(f"  run: {run}")
    ok = all(r["complete"] for r in summary["runs"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
