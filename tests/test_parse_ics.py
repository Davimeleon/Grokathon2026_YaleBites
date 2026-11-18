import pytest

from app.parse.graphql import parse_events_response
from app.parse.ics import ics_events_to_parsed, ics_uid_index, parse_ics_records
from app.parse.normalize import apply_ics_metadata, make_event_id
from app.schemas import Visibility
from tests.conftest import fixture_pages, read_fixture_text

GOOD = (
    "BEGIN:VEVENT\r\nUID:good-{n}\r\nDTSTART:20261120T230000Z\r\nDTEND:20261121T010000Z\r\n"
    "SUMMARY:Event {n}\r\nLOCATION:Sign in to download the location\r\n"
    "URL:https://yaleconnect.yale.edu/rsvp?id={n}\r\nEND:VEVENT\r\n"
)


@pytest.fixture(scope="module")
def school():
    return parse_ics_records(read_fixture_text("ics/ical_yale.ics.gz"))


def test_school_feed_parses(school):
    records, errors = school
    assert errors == []
    assert len(records) == 5412
    assert len({r.uid for r in records}) == 5412
    assert sum(1 for r in records if r.location is None) == 4696
    assert all(r.dtstart.tzinfo is not None and r.event_id is not None for r in records)
    first = records[0]
    assert first.event_id == 2326136 and first.organizer == "Planetary Health and Global Engagement"
    assert first.categories["event_type"] == ["Lecture, Talk, or Panel"]
    assert first.categories["event_tags"][0] == "AI (Artificial Intelligence)"
    assert "Event Details:" not in first.description and first.description.endswith("#/registration")
    assert first.last_modified is not None


def test_graphql_upcoming_joins_ics(school):
    index = ics_uid_index(school[0])
    events = [e for page in fixture_pages("upcoming14d") for e in parse_events_response(page)[0]]
    joined = [e for e in events if e.extras.cg_event_id in index]
    assert len(joined) == 344
    same_title = sum(1 for e in joined if index[e.extras.cg_event_id].summary == e.event.title)
    assert same_title >= 343  # one event was retitled after the daily ICS regeneration
    enriched = apply_ics_metadata(joined[0], index[joined[0].extras.cg_event_id])
    assert enriched.event.id == index[joined[0].extras.cg_event_id].uid


def test_ics_only_normalization(school):
    parsed, errors = ics_events_to_parsed(school[0], visibility=Visibility.PUBLIC)
    assert len(parsed) + len(errors) == 5412
    assert len(errors) <= 1  # one VEVENT has an empty SUMMARY
    hidden = [p for p in parsed if p.extras.location_hidden]
    assert all(p.event.location_raw is None for p in hidden) and len(hidden) >= 4690
    sample = parsed[0]
    assert sample.extras.source == "ics" and sample.event.cost_raw is None
    assert sample.event.id == make_event_id(sample.event.source_url, sample.extras.ics_uid)
    assert sample.event.updated_at_source is not None


def test_personal_feed_has_locations():
    records, errors = parse_ics_records(read_fixture_text("auth/personal_feed.ics"))
    assert errors == [] and len(records) == 8
    parsed, perrors = ics_events_to_parsed(records, visibility=Visibility.AUTHENTICATED, source="ics_personal")
    assert perrors == []
    assert all(p.extras.source == "ics_personal" and not p.extras.location_hidden for p in parsed)
    assert "weeding, mulching" in parsed[0].event.description_raw


def test_malformed_vevent_is_one_error():
    bad = "BEGIN:VEVENT\r\nUID:bad-1\r\nDTSTART:notadate\r\nSUMMARY:Broken\r\nEND:VEVENT\r\n"
    text = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + GOOD.format(n=1) + bad + GOOD.format(n=2) + "END:VCALENDAR\r\n"
    records, errors = parse_ics_records(text)
    assert [r.uid for r in records] == ["good-1", "good-2"]
    assert len(errors) == 1 and errors[0].ref == "bad-1"


def test_contact_fallback_and_missing_uid():
    contact = (
        "BEGIN:VEVENT\r\nUID:c-1\r\nDTSTART:20261120T230000Z\r\nSUMMARY:X\r\n"
        "CONTACT:https://yaleconnect.yale.edu/grp/rsvp_boot?id=99#event_host\r\nLOCATION:\r\nEND:VEVENT\r\n"
    )
    no_uid = "BEGIN:VEVENT\r\nDTSTART:20261120T230000Z\r\nSUMMARY:Y\r\nEND:VEVENT\r\n"
    records, errors = parse_ics_records("BEGIN:VCALENDAR\r\n" + contact + no_uid + "END:VCALENDAR\r\n")
    assert records[0].event_id == 99 and records[0].location is None
    assert len(errors) == 1
    assert ics_uid_index(records) == {99: records[0]}


def test_record_without_event_id_is_error():
    records, _ = parse_ics_records("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:n\r\nDTSTART:20261120T230000Z\r\n"
                                   "SUMMARY:Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    parsed, errors = ics_events_to_parsed(records, visibility=Visibility.PUBLIC)
    assert parsed == [] and errors[0].ref == "n"


def test_truncated_feed_falls_back_to_blocks():
    text = "BEGIN:VCALENDAR\r\n" + GOOD.format(n=1) + GOOD.format(n=2) + "BEGIN:VEVENT\r\nUID:cut\r\nDTST"
    records, _ = parse_ics_records(text)
    assert [r.uid for r in records] == ["good-1", "good-2"]
