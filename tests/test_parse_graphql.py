import copy
import json
import logging

import pytest

from app.parse.graphql import parse_event_detail, parse_event_nodes, parse_events_response
from app.parse.normalize import (
    apply_ics_metadata,
    build_cost_raw,
    build_location_raw,
    compute_content_hash,
    make_event_id,
    with_visibility,
)
from app.schemas import (
    GqlExternalRegistration,
    GqlLocation,
    GqlRegistration,
    IcsEvent,
    Visibility,
)
from app.sources import canonical_source_url, decode_event_global_id
from tests.conftest import FIXTURES_DIR, fixture_pages, read_fixture_json


def _parse_pages(pages, visibility=Visibility.PUBLIC):
    events, errors = [], []
    for page in pages:
        evs, errs, page_info = parse_events_response(page, visibility=visibility)
        assert page_info is not None and set(page_info) == {"endCursor", "hasNextPage"}
        events += evs
        errors += errs
    return events, errors


@pytest.mark.parametrize(("prefix", "auth", "expected"), [
    ("upcoming14d", False, 371), ("past14d", False, 920), ("hot6h", False, 72), ("upcoming14d", True, 379),
])
def test_every_list_page_parses_without_errors(prefix, auth, expected):
    pages = fixture_pages(prefix, auth=auth)
    events, errors = _parse_pages(pages)
    assert errors == []
    assert len(events) == expected
    assert len({e.event.source_url for e in events}) == expected
    assert pages[-1]["data"]["events"]["pageInfo"]["hasNextPage"] is False
    for parsed in events:
        ev = parsed.event
        assert ev.starts_at.tzinfo is not None
        assert ev.source_url == canonical_source_url(parsed.extras.cg_event_id)
        assert ev.id == make_event_id(ev.source_url, None)
        assert ev.content_hash == compute_content_hash(ev, parsed.extras)


def test_field_mapping_on_real_nodes():
    events, _ = _parse_pages(fixture_pages("upcoming14d"))
    hidden = [e for e in events if e.extras.location_privacy == "SIGN_IN_REQUIRED"]
    assert len(hidden) == 273 and all(e.extras.location_hidden and e.event.location_raw is None for e in hidden)
    assert sum(1 for e in events if e.extras.is_food_included) == 36
    assert sum(1 for e in events if e.extras.food_details) == 19
    green = next(e for e in events if e.event.title == "New Haven Greenspace Volunteering")
    assert green.event.host_group == "Urban Resources Initiative"
    assert green.event.cost_raw == "external registration: https://tinyurl.com/NHVgreenspace"
    assert green.event.location_raw == "various public locations in New Haven"
    assert green.extras.tags[:2] == ("Earth Day", "Climate Crisis")
    assert green.extras.event_type == "Commmunity Service & Volunteer"
    assert green.event.description_raw.startswith("Urban Resources Initiative")
    assert "< tinyurl.com/NHVgreenspace >" in green.event.description_text
    rsvp = [e for e in events if e.event.cost_raw and e.event.cost_raw.startswith("RSVP: free ($0.00 USD)")]
    assert rsvp
    with_html = [e for e in events if e.event.description_raw and "<" in e.event.description_raw]
    assert with_html and all("<p>" not in (e.event.description_text or "") for e in with_html)


def test_auth_pages_reveal_locations_and_members_only_events():
    auth_events, errors = _parse_pages(fixture_pages("upcoming14d", auth=True), Visibility.AUTHENTICATED)
    assert errors == []
    assert all(e.event.visibility == Visibility.AUTHENTICATED for e in auth_events)
    assert sum(1 for e in auth_events if e.extras.cg_event_id >= 2399001) == 8
    revealed = [e for e in auth_events if e.extras.location_privacy == "SIGN_IN_REQUIRED"]
    assert revealed and all(e.event.location_raw and not e.extras.location_hidden for e in revealed)


def test_detail_responses_equal_list_nodes():
    events, _ = _parse_pages(fixture_pages("upcoming14d") + fixture_pages("past14d") + fixture_pages("hot6h"))
    by_gid = {e.extras.graphql_id: e for e in events}
    details = sorted(p for p in (FIXTURES_DIR / "graphql").glob("detail_*.json") if "not_found" not in p.name)
    assert len(details) == 16
    for path in details:
        parsed, errors = parse_event_detail(path.read_bytes())
        assert errors == [] and parsed is not None
        assert parsed.event == by_gid[parsed.extras.graphql_id].event
        assert parsed.extras == by_gid[parsed.extras.graphql_id].extras


def test_detail_not_found():
    parsed, errors = parse_event_detail(read_fixture_json("graphql/detail_not_found.json"))
    assert parsed is None
    assert len(errors) == 1 and errors[0].ref == "EVENT_NOT_AVAILABLE"


def test_top_level_errors_without_data():
    events, errors, page_info = parse_events_response(json.dumps(read_fixture_json("graphql/viewer_anonymous.json")))
    assert events == [] and page_info is None
    assert len(errors) == 1 and errors[0].ref == "AUTH_NOT_AUTHENTICATED"
    assert parse_events_response(b"not json")[1][0].message.startswith("invalid JSON")


@pytest.mark.parametrize("breakage", ["missing_id", "bad_date", "wrong_type", "bad_global_id", "empty_title"])
def test_malformed_node_is_one_error_rest_parse(breakage, caplog):
    page = copy.deepcopy(fixture_pages("upcoming14d")[0])
    edges = page["data"]["events"]["edges"]
    node = edges[3]["node"]
    if breakage == "missing_id":
        del node["id"]
    elif breakage == "bad_date":
        node["startDate"] = "next Tuesday"
    elif breakage == "wrong_type":
        node["tags"] = "food"
    elif breakage == "bad_global_id":
        node["id"] = "bm90LWFuLWV2ZW50"
    else:
        node["name"] = "  \r\n "
    with caplog.at_level(logging.WARNING, logger="app.parse.graphql"):
        events, errors, _ = parse_events_response(page)
    assert len(errors) == 1 and len(events) == len(edges) - 1
    assert errors[0].source == "graphql"
    assert any("skipping malformed" in r.message for r in caplog.records)


def test_non_dict_node_never_raises():
    events, errors = parse_event_nodes([None, 7, "x"])
    assert events == [] and [e.ref for e in errors] == ["#0", "#1", "#2"]


def test_cost_raw_rendering():
    reg = GqlRegistration.model_validate({"capacityMode": "LIMITED", "options": [
        {"name": "General", "price": {"amount": 12.5, "currency": "USD", "isFlexible": False}},
        {"name": "Donation", "price": {"amount": 0, "currency": "USD", "isFlexible": True}},
        {"name": "RSVP", "price": {"amount": 0, "currency": "USD", "isFlexible": False}},
    ]})
    ext = GqlExternalRegistration(url="https://example.org/form")
    assert build_cost_raw(reg, ext) == ("General: $12.50 USD; Donation: pay what you want; RSVP: free ($0.00 USD); "
                                        "capacity: limited; external registration: https://example.org/form")
    assert build_cost_raw(None, None) is None
    assert build_cost_raw(GqlRegistration(), GqlExternalRegistration()) is None


def test_location_raw_rendering():
    loc = GqlLocation.model_validate({"name": "Henry R. Luce Hall,  202", "address": {
        "street": "34 Hillhouse Avenue", "city": "New Haven", "state": "CT", "zipcode": ""}})
    assert build_location_raw(loc) == ("Henry R. Luce Hall, 202, 34 Hillhouse Avenue, New Haven, CT",
                                       "34 Hillhouse Avenue, New Haven, CT")
    same = GqlLocation.model_validate({"name": "135 Prospect Street", "address": {
        "street": "135 Prospect Street", "city": "New Haven", "state": "", "zipcode": "06511"}})
    assert build_location_raw(same) == ("135 Prospect Street", "135 Prospect Street, New Haven, 06511")
    only_city = GqlLocation.model_validate({"name": None, "address": {"street": "", "city": "Antalya"}})
    assert build_location_raw(only_city) == ("Antalya", "Antalya")
    assert build_location_raw(None) == (None, None)
    assert build_location_raw(GqlLocation(eventLocationPrivacy="SIGN_IN_REQUIRED")) == (None, None)


def test_ics_metadata_and_visibility_rehash():
    parsed = parse_event_nodes([fixture_pages("hot6h")[0]["data"]["events"]["edges"][0]["node"]])[0][0]
    rec = IcsEvent(uid="uid-1@campusgroups.com", dtstart=parsed.event.starts_at,
                   last_modified=parsed.event.starts_at, event_id=parsed.extras.cg_event_id)
    with_ics = apply_ics_metadata(parsed, rec)
    assert with_ics.event.id == "uid-1@campusgroups.com" and with_ics.extras.ics_uid == rec.uid
    assert with_ics.event.updated_at_source == rec.last_modified
    assert with_ics.event.content_hash == parsed.event.content_hash  # id/updated_at_source are not hashed
    authed = with_visibility(parsed, Visibility.AUTHENTICATED)
    assert authed.event.content_hash != parsed.event.content_hash
    assert authed.event.content_hash == compute_content_hash(authed.event, authed.extras)
    assert decode_event_global_id(parsed.extras.graphql_id) == parsed.extras.cg_event_id
