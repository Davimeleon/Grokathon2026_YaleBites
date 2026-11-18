"""Source datetime parsing. Every value returned here is tz-aware, in America/New_York."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.clock import CAMPUS_TZ


def parse_iso_utc(value: str | None) -> datetime | None:
    """``2026-11-17T23:00:00Z`` (or any ISO-8601 with offset) -> campus time. Naive input is UTC."""
    if value is None or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(CAMPUS_TZ)


def local_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=CAMPUS_TZ)


def ics_value_to_campus(value: date | datetime, tzid: str | None = None) -> tuple[datetime, bool]:
    """An ICS DTSTART/DTEND value -> (campus datetime, is_date_only).

    ``VALUE=DATE`` becomes local midnight. A naive datetime is interpreted in its TZID (or campus
    time when there is none). Aware values are converted.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            zone = ZoneInfo(tzid) if tzid else CAMPUS_TZ
            value = value.replace(tzinfo=zone)
        return to_campus_normalized(value), False
    return local_midnight(value), True


def to_campus_normalized(dt: datetime) -> datetime:
    """Convert to campus time through UTC, which resolves DST gaps and folds to a real instant."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).astimezone(CAMPUS_TZ)


def is_local_midnight(dt: datetime) -> bool:
    local = dt.astimezone(CAMPUS_TZ)
    return local.hour == 0 and local.minute == 0 and local.second == 0


def detect_all_day(starts_at: datetime, ends_at: datetime | None, *, date_only: bool = False) -> bool:
    """All-day when the source said VALUE=DATE, or the event runs from local midnight to a later
    local midnight (whole days in campus time)."""
    if date_only:
        return True
    if ends_at is None:
        return False
    return is_local_midnight(starts_at) and is_local_midnight(ends_at) and ends_at > starts_at


def detect_multi_day(starts_at: datetime, ends_at: datetime | None, *, all_day: bool = False) -> bool:
    """True when the event ends on a later local calendar day (an all-day event ending at the next
    midnight is one day, not two)."""
    if ends_at is None:
        return False
    end = ends_at
    if all_day or is_local_midnight(ends_at):
        end = ends_at - timedelta(microseconds=1)
    return end.astimezone(CAMPUS_TZ).date() > starts_at.astimezone(CAMPUS_TZ).date()


def shift_local_days(dt: datetime, days: int) -> datetime:
    """Shift by whole days of campus wall-clock time (18:00 local stays 18:00 local across DST)."""
    local = dt.astimezone(CAMPUS_TZ)
    shifted = datetime.combine(local.date() + timedelta(days=days), local.timetz())
    return to_campus_normalized(shifted)
