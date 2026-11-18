from datetime import UTC, date, datetime, timedelta

from app.clock import CAMPUS_TZ
from app.parse.graphql import parse_event_nodes
from app.parse.ics import parse_ics_records
from app.parse.normalize import compute_content_hash, shift_days
from app.parse.times import ics_value_to_campus, parse_iso_utc
from app.sources import encode_event_global_id
from tests.conftest import fixture_pages

EDT = timedelta(hours=-4)
EST = timedelta(hours=-5)


def _node(**overrides) -> dict:
    node = {"id": encode_event_global_id(4242), "name": "Test event", "startDate": "2026-11-20T23:00:00Z",
            "endDate": "2026-11-21T01:00:00Z", "descriptionText": "x"}
    node.update(overrides)
    return node


def _one(**overrides):
    events, errors = parse_event_nodes([_node(**overrides)])
    assert errors == []
    return events[0]


def test_multi_day_fixture_spans_dst_change_with_correct_offsets():
    nodes = [e["node"] for page in fixture_pages("upcoming14d") for e in page["data"]["events"]["edges"]]
    raw = next(n for n in nodes if n["startDate"] == "2026-08-29T13:00:00Z" and n["endDate"] == "2026-12-19T17:00:00Z")
    parsed = parse_event_nodes([raw])[0][0]
    ev = parsed.event
    assert ev.starts_at.utcoffset() == EDT and (ev.starts_at.hour, ev.starts_at.minute) == (9, 0)
    assert ev.ends_at.utcoffset() == EST and ev.ends_at.hour == 12
    assert ev.starts_at.astimezone(UTC) == datetime(2026, 8, 29, 13, tzinfo=UTC)
    assert parsed.extras.is_multi_day is True
    assert parsed.extras.all_day is False


def test_graphql_all_day_midnight_to_midnight_local():
    parsed = _one(startDate="2026-11-20T05:00:00Z", endDate="2026-11-21T05:00:00Z", isMultiDay=False)
    assert parsed.extras.all_day is True
    assert parsed.extras.is_multi_day is False
    assert parsed.event.starts_at.hour == 0 and parsed.event.starts_at.utcoffset() == EST


def test_missing_end_date_gives_none():
    parsed = _one(endDate=None)
    assert parsed.event.ends_at is None
    assert parsed.extras.all_day is False and parsed.extras.is_multi_day is False


def test_fall_back_overlap_is_unambiguous_from_utc():
    first = _one(startDate="2026-11-01T05:30:00Z", endDate="2026-11-01T06:30:00Z").event
    assert (first.starts_at.hour, first.starts_at.minute) == (1, 30) and first.starts_at.utcoffset() == EDT
    assert (first.ends_at.hour, first.ends_at.minute) == (1, 30) and first.ends_at.utcoffset() == EST
    assert first.ends_at.astimezone(UTC) - first.starts_at.astimezone(UTC) == timedelta(hours=1)


def test_spring_forward_from_utc():
    ev = _one(startDate="2027-03-14T06:30:00Z", endDate="2027-03-14T07:30:00Z").event
    assert (ev.starts_at.hour, ev.starts_at.utcoffset()) == (1, EST)
    assert (ev.ends_at.hour, ev.ends_at.utcoffset()) == (3, EDT)


def test_ics_value_date_all_day_and_tzid_naive():
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:allday-1\r\n"
        "DTSTART;VALUE=DATE:20261120\r\nDTEND;VALUE=DATE:20261121\r\nSUMMARY:Fair\r\n"
        "URL:https://yaleconnect.yale.edu/rsvp?id=77\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:tz-1\r\n"
        "DTSTART;TZID=America/New_York:20261101T013000\r\nSUMMARY:Late\r\n"
        "URL:https://yaleconnect.yale.edu/rsvp?id=78\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    records, errors = parse_ics_records(ics)
    assert errors == []
    allday, naive = records
    assert allday.all_day is True
    assert allday.dtstart == datetime(2026, 11, 20, tzinfo=CAMPUS_TZ) and allday.dtstart.utcoffset() == EST
    assert naive.all_day is False and naive.dtend is None
    assert naive.dtstart.tzinfo is not None and naive.dtstart.hour == 1


def test_ics_value_helpers_never_naive():
    dt, date_only = ics_value_to_campus(date(2026, 7, 4))
    assert date_only and dt.utcoffset() == EDT
    assert parse_iso_utc("2026-11-17T23:00:00Z").utcoffset() == EST
    assert parse_iso_utc("2026-11-17T23:00:00").tzinfo is not None
    assert parse_iso_utc("") is None


def test_shift_days_keeps_local_wall_clock_across_dst():
    parsed = _one(startDate="2026-10-29T22:00:00Z", endDate="2026-10-30T00:00:00Z")
    assert parsed.event.starts_at.hour == 18 and parsed.event.starts_at.utcoffset() == EDT
    shifted = shift_days(parsed, 7)
    assert shifted.event.starts_at.hour == 18 and shifted.event.starts_at.utcoffset() == EST
    assert shifted.event.starts_at.date() == date(2026, 11, 5)
    assert shifted.event.ends_at.hour == 20
    assert shifted.event.content_hash == compute_content_hash(shifted.event, shifted.extras)
    assert shifted.event.content_hash != parsed.event.content_hash
    assert shift_days(shifted, -7).event.content_hash == parsed.event.content_hash
