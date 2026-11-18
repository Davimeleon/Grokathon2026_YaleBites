"""Source endpoints and request documents for Yale Connect (CampusGroups).

This module is part of the frozen interface contract. Every URL, query document and
identifier convention here was determined empirically; see ``docs/sources.md`` for the
evidence. Fetch code (``app/fetch``) and parse code (``app/parse``) both import from here
so that the request shape and the parser always agree.
"""

from __future__ import annotations

import base64
import binascii
import re

BASE_URL = "https://yaleconnect.yale.edu"
HOST = "yaleconnect.yale.edu"

# GraphQL API used by the CampusGroups React front-end (/w/...). Allowed by robots.txt.
GRAPHQL_URL = f"{BASE_URL}/api/v2/graphql"

# School-wide public ICS feed. Answers 302 to a static CDN host that supports ETag/304.
ICS_SCHOOL_URL = f"{BASE_URL}/ical/yale/ical_yale.ics"
ICS_STATIC_HOST = "static-prod-us-east-1.campusgroups.com"

# Page that lists the school-wide feed and the ~1,450 per-group feeds.
ICS_HELPER_URL = f"{BASE_URL}/ics_helper"

ROBOTS_URL = f"{BASE_URL}/robots.txt"

# The legacy JSON list endpoint the old /events page calls. robots.txt disallows
# /mobile_ws/v17/ and /mobile_ws/v18/, so it is documented but never requested.
LEGACY_LIST_ENDPOINT_DISALLOWED = f"{BASE_URL}/mobile_ws/v17/mobile_events_list"

# Server-enforced maximum for the GraphQL `first` argument ("'first' must be between 1 and 50").
GRAPHQL_PAGE_SIZE = 50

# Fields selected for every event, list and detail alike. Deliberately excluded:
#   organizerContact, speakers, attendees, attendeeCount, registration.settledTickets,
#   registration questions (personal data / other users' data), and volatile fields such
#   as isLive, isSoldOut, slotsRemaining that would churn the body hash every hot sync.
EVENT_FIELDS = """
  id
  name
  startDate
  endDate
  timezone
  isMultiDay
  status
  customTimeInstruction
  attendanceMode
  isFoodIncluded
  foodDetails
  descriptionText
  descriptionHtml
  shareUrl
  type { value }
  tags { name }
  hostGroup { id name }
  cohostGroups { id name }
  parent { id name }
  location {
    kind
    name
    isToBeDetermined
    eventLocationPrivacy
    address { street city state zipcode }
    coordinates { latitude longitude }
  }
  onlineMeeting { platform meetingLinkPrivacy }
  externalRegistration { url }
  registration {
    capacityMode
    registrationUrl
    options { name price { amount currency isFlexible } }
  }
"""

EVENTS_QUERY = (
    "query Events($first: Int, $after: String, $filter: EventFilterInput) {\n"
    "  events(first: $first, after: $after, filter: $filter) {\n"
    "    totalCount\n"
    "    pageInfo { endCursor hasNextPage }\n"
    "    edges { node {" + EVENT_FIELDS + "} }\n"
    "  }\n"
    "}\n"
)

EVENT_DETAIL_QUERY = (
    "query Event($id: ID!) {\n  event(id: $id) {" + EVENT_FIELDS + "}\n}\n"
)

# Session probe: selects nothing identifying. Anonymous or expired sessions get
# errors[].extensions.code == "AUTH_NOT_AUTHENTICATED" and data == null.
VIEWER_PROBE_QUERY = "query SessionProbe { viewer { latestHubId } }\n"
AUTH_NOT_AUTHENTICATED = "AUTH_NOT_AUTHENTICATED"

# Location privacy values observed in eventLocationPrivacy.
LOCATION_PRIVACY_VISIBLE = "VISIBLE"
LOCATION_PRIVACY_SIGN_IN_REQUIRED = "SIGN_IN_REQUIRED"

# Text the school-wide ICS feed puts in LOCATION when the location needs sign-in.
ICS_HIDDEN_LOCATION_TEXT = "Sign in to download the location"


def events_variables(start_iso: str, end_iso: str, after: str | None = None) -> dict:
    """GraphQL variables for one page of events overlapping [start_iso, end_iso).

    ``timeRange: CUSTOM`` requires ``dateRange`` (``DateRangeInput{from,to}``, DateTime
    scalars). Filtering is server-side and by overlap: an event is returned when its
    span intersects the range, so multi-week "events" appear in every window.
    """
    variables: dict = {
        "first": GRAPHQL_PAGE_SIZE,
        "filter": {"timeRange": "CUSTOM", "dateRange": {"from": start_iso, "to": end_iso}},
    }
    if after:
        variables["after"] = after
    return variables


def graphql_body(operation_name: str, query: str, variables: dict) -> dict:
    return {"operationName": operation_name, "query": query, "variables": variables}


_GLOBAL_ID_RE = re.compile(r"^yale:Event:(\d+)$")
_RSVP_ID_RE = re.compile(r"[?&]id=(\d+)")


def decode_event_global_id(global_id: str) -> int | None:
    """``eWFsZTpFdmVudDoyMzI2NTE1`` -> 2326515 (base64 of ``yale:Event:2326515``)."""
    try:
        padded = global_id + "=" * (-len(global_id) % 4)
        decoded = base64.b64decode(padded, validate=False).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    match = _GLOBAL_ID_RE.match(decoded)
    return int(match.group(1)) if match else None


def encode_event_global_id(event_id: int) -> str:
    return base64.b64encode(f"yale:Event:{event_id}".encode("ascii")).decode("ascii")


def event_id_from_url(url: str) -> int | None:
    """Numeric event id from an ICS URL/CONTACT value such as ``.../rsvp?id=2326136``."""
    match = _RSVP_ID_RE.search(url or "")
    return int(match.group(1)) if match else None


def canonical_source_url(event_id: int) -> str:
    """One canonical URL per event, identical whichever source the event came from.

    It is exactly the ``URL:`` value the ICS feed carries, so the ICS and GraphQL
    records of the same event share ``source_url``.
    """
    return f"{BASE_URL}/rsvp?id={event_id}"
