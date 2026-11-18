"""Single source of "now" for the whole app, so tests and fixture replays can pin time."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

CAMPUS_TZ = ZoneInfo("America/New_York")

_now_fn: Callable[[], datetime] | None = None


def now() -> datetime:
    """Current time, tz-aware UTC."""
    value = _now_fn() if _now_fn is not None else datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("clock override returned a naive datetime")
    return value.astimezone(UTC)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Override the clock (``None`` restores wall-clock time)."""
    global _now_fn
    _now_fn = fn


def freeze(at: datetime) -> None:
    if at.tzinfo is None:
        raise ValueError("freeze() needs a tz-aware datetime")
    set_clock(lambda: at)


def to_campus(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(CAMPUS_TZ)
