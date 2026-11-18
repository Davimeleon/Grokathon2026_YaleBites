"""/api/meta: sync bookkeeping, auth presence flags, counts, source coverage and dataset kind."""

from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import AppState, Event, SyncRun
from app.schemas import (
    AuthMode,
    AuthOut,
    AuthStatus,
    CountsOut,
    CoverageOut,
    DatasetOut,
    FoodLabel,
    MetaOut,
    SyncKind,
    SyncRunOut,
    Visibility,
)

ACTIVE = "active"
REMOVED = "canceled_or_removed"
PRIVACY_GATED = ("SIGN_IN_REQUIRED", "REGISTRATION_REQUIRED")
PHYSICAL_MODES = ("IN_PERSON", "HYBRID")
DEFAULT_DATASET = DatasetOut(kind="live", time_shift_days=0)


def latest_complete_run(session: Session) -> SyncRun | None:
    """Newest successful (complete, finished) sync run; its finish time is the data's as-of time."""
    stmt = (
        select(SyncRun)
        .where(SyncRun.complete.is_(True), SyncRun.finished_at.is_not(None))
        .order_by(SyncRun.finished_at.desc(), SyncRun.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def data_as_of(session: Session) -> datetime | None:
    run = latest_complete_run(session)
    return run.finished_at if run is not None else None


def age_seconds(as_of: datetime | None, now: datetime) -> int | None:
    if as_of is None:
        return None
    return max(0, int((now - as_of).total_seconds()))


def _newest_run(session: Session, kinds: tuple[str, ...], *, finished_only: bool = False) -> SyncRun | None:
    stmt = select(SyncRun).where(SyncRun.kind.in_(kinds))
    if finished_only:
        stmt = stmt.where(SyncRun.finished_at.is_not(None))
    return session.scalars(stmt.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)).first()


def sync_run_out(run: SyncRun | None) -> SyncRunOut | None:
    if run is None:
        return None
    return SyncRunOut(
        id=run.id,
        kind=SyncKind(run.kind),
        mode=AuthMode(run.mode),
        auth_status=AuthStatus(run.auth_status),
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        complete=run.complete,
        events_seen=run.events_seen,
        events_updated=run.events_updated,
        events_created=run.events_created,
        events_removed=run.events_removed,
        authenticated_only_seen=run.authenticated_only_seen,
        error_count=len(run.errors or ()),
    )


def _count(session: Session, *where: ColumnElement[bool]) -> int:
    return session.scalar(select(func.count()).select_from(Event).where(*where)) or 0


def _grouped(session: Session, column, keys: list[str]) -> dict[str, int]:
    counts = dict.fromkeys(keys, 0)
    rows = session.execute(select(column, func.count()).where(Event.status == ACTIVE).group_by(column)).all()
    for key, n in rows:
        counts[str(key)] = n
    return counts


def _pinned() -> ColumnElement[bool]:
    return and_(Event.lat.is_not(None), Event.lng.is_not(None))


def build_counts(session: Session) -> CountsOut:
    active = Event.status == ACTIVE
    withheld = and_(
        active,
        or_(Event.location_hidden.is_(True), Event.location_privacy.in_(PRIVACY_GATED)),
        or_(Event.location_raw.is_(None), Event.location_raw == ""),
    )
    return CountsOut(
        active=_count(session, active),
        removed=_count(session, Event.status == REMOVED),
        by_visibility=_grouped(session, Event.visibility, [v.value for v in Visibility]),
        by_food_label=_grouped(session, Event.food_label, [f.value for f in FoodLabel]),
        geocoded=_count(session, active, _pinned()),
        location_withheld=_count(session, withheld),
    )


def _dict_entries(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, dict)}


def merged_source_stats(session: Session) -> dict[str, dict]:
    """Newest finished full/seed run's per-source stats, overlaid key by key with the newest hot run's."""
    full = _newest_run(session, (SyncKind.FULL.value, SyncKind.SEED.value), finished_only=True)
    hot = _newest_run(session, (SyncKind.HOT.value,), finished_only=True)
    merged = _dict_entries(full.source_stats if full else None)
    for name, stats in _dict_entries(hot.source_stats if hot else None).items():
        merged[name] = {**merged.get(name, {}), **stats}
    return merged


def geocode_rate(session: Session) -> float | None:
    physical = and_(
        Event.status == ACTIVE,
        or_(Event.attendance_mode.in_(PHYSICAL_MODES), Event.attendance_mode.is_(None)),
    )
    total = _count(session, physical)
    if total == 0:
        return None
    return round(_count(session, physical, _pinned()) / total, 4)


def dataset_out(session: Session) -> DatasetOut:
    state = session.get(AppState, "dataset")
    if state is None or not isinstance(state.value, dict):
        return DEFAULT_DATASET
    try:
        return DatasetOut.model_validate(state.value)
    except ValidationError:
        return DEFAULT_DATASET


def auth_out(session: Session, settings: Settings) -> AuthOut:
    """Presence flags only: the cookie and feed URLs are reduced to bool/len and never read."""
    run = latest_complete_run(session)
    includes_auth = _count(session, Event.status == ACTIVE, Event.visibility == Visibility.AUTHENTICATED.value)
    return AuthOut(
        mode=AuthMode(run.mode) if run else AuthMode.ANONYMOUS,
        status=AuthStatus(run.auth_status) if run else AuthStatus.NONE,
        cookie_configured=bool(settings.yaleconnect_cookie),
        ics_feeds_configured=len(settings.yaleconnect_ics_feeds),
        data_includes_authenticated_events=includes_auth > 0,
    )


def build_meta(session: Session, settings: Settings, now: datetime) -> MetaOut:
    as_of = data_as_of(session)
    return MetaOut(
        now=now,
        data_as_of=as_of,
        data_age_seconds=age_seconds(as_of, now),
        last_sync=sync_run_out(_newest_run(session, tuple(k.value for k in SyncKind))),
        last_full_sync=sync_run_out(_newest_run(session, (SyncKind.FULL.value, SyncKind.SEED.value))),
        last_hot_sync=sync_run_out(_newest_run(session, (SyncKind.HOT.value,))),
        auth=auth_out(session, settings),
        counts=build_counts(session),
        coverage=CoverageOut(
            primary_source="graphql",
            sources=merged_source_stats(session),
            geocode_rate=geocode_rate(session),
        ),
        dataset=dataset_out(session),
    )
