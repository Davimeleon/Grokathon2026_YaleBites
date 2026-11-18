"""Frozen interface contract: source response shapes, the normalized Event, enrichment
results, sync bookkeeping and API response bodies.

Owned by the integrator. Workstreams code against these types and request changes
instead of editing this file. Evidence for every source shape is in ``docs/sources.md``
and the recorded responses under ``tests/data/fixtures/``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 1. Source shapes: GraphQL /api/v2/graphql (primary source)
# ---------------------------------------------------------------------------


class _Gql(BaseModel):
    """Base for GraphQL payload models: camelCase aliases, unknown keys ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)


class GqlNamed(_Gql):
    id: str | None = None
    name: str | None = None


class GqlValue(_Gql):
    value: str | None = None


class GqlTag(_Gql):
    name: str | None = None


class GqlAddress(_Gql):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None


class GqlCoordinates(_Gql):
    latitude: float | None = None
    longitude: float | None = None


class GqlLocation(_Gql):
    kind: str | None = None  # ON_CAMPUS | OTHER | null
    name: str | None = None  # null whenever the location is withheld
    is_to_be_determined: bool | None = Field(default=None, alias="isToBeDetermined")
    # VISIBLE | SIGN_IN_REQUIRED | REGISTRATION_REQUIRED | null (online-only events)
    event_location_privacy: str | None = Field(default=None, alias="eventLocationPrivacy")
    address: GqlAddress | None = None
    coordinates: GqlCoordinates | None = None


class GqlPrice(_Gql):
    amount: float | None = None
    currency: str | None = None
    is_flexible: bool | None = Field(default=None, alias="isFlexible")


class GqlRegistrationOption(_Gql):
    name: str | None = None
    price: GqlPrice | None = None


class GqlRegistration(_Gql):
    capacity_mode: str | None = Field(default=None, alias="capacityMode")  # UNLIMITED | LIMITED | HIDDEN
    registration_url: str | None = Field(default=None, alias="registrationUrl")
    options: list[GqlRegistrationOption] = Field(default_factory=list)


class GqlExternalRegistration(_Gql):
    url: str | None = None


class GqlOnlineMeeting(_Gql):
    platform: str | None = None
    meeting_link_privacy: str | None = Field(default=None, alias="meetingLinkPrivacy")


class GqlEventNode(_Gql):
    """One ``Event`` as selected by ``app.sources.EVENT_FIELDS`` (list and detail alike)."""

    id: str  # Relay global id: base64("yale:Event:<numeric>")
    name: str | None = None
    start_date: str | None = Field(default=None, alias="startDate")  # ISO-8601 UTC, e.g. 2026-11-17T23:00:00Z
    end_date: str | None = Field(default=None, alias="endDate")
    timezone: str | None = None  # observed: always America/New_York
    is_multi_day: bool | None = Field(default=None, alias="isMultiDay")
    status: str | None = None  # observed: APPROVED
    custom_time_instruction: str | None = Field(default=None, alias="customTimeInstruction")
    attendance_mode: str | None = Field(default=None, alias="attendanceMode")  # IN_PERSON | ONLINE | HYBRID
    is_food_included: bool | None = Field(default=None, alias="isFoodIncluded")
    food_details: str | None = Field(default=None, alias="foodDetails")
    description_text: str | None = Field(default=None, alias="descriptionText")
    description_html: str | None = Field(default=None, alias="descriptionHtml")
    share_url: str | None = Field(default=None, alias="shareUrl")
    type: GqlValue | None = None
    tags: list[GqlTag] = Field(default_factory=list)
    host_group: GqlNamed | None = Field(default=None, alias="hostGroup")
    cohost_groups: list[GqlNamed] = Field(default_factory=list, alias="cohostGroups")
    parent: GqlNamed | None = None
    location: GqlLocation | None = None
    online_meeting: GqlOnlineMeeting | None = Field(default=None, alias="onlineMeeting")
    external_registration: GqlExternalRegistration | None = Field(default=None, alias="externalRegistration")
    registration: GqlRegistration | None = None


class GqlPageInfo(_Gql):
    end_cursor: str | None = Field(default=None, alias="endCursor")
    has_next_page: bool = Field(default=False, alias="hasNextPage")


class GqlEventEdge(_Gql):
    node: GqlEventNode


class GqlEventsConnection(_Gql):
    # null for open-ended ranges (timeRange UPCOMING) and for very large CUSTOM ranges; never rely on it.
    total_count: int | None = Field(default=None, alias="totalCount")
    page_info: GqlPageInfo = Field(alias="pageInfo")
    edges: list[GqlEventEdge] = Field(default_factory=list)


class GqlErrorExtensions(_Gql):
    code: str | None = None  # AUTH_NOT_AUTHENTICATED | EVENT_NOT_AVAILABLE | INVALID_ARGUMENT | ...


class GqlError(_Gql):
    message: str
    path: list[str | int] | None = None
    extensions: GqlErrorExtensions | None = None


class GqlEventsData(_Gql):
    events: GqlEventsConnection | None = None


class GqlEventsResponse(_Gql):
    data: GqlEventsData | None = None
    errors: list[GqlError] = Field(default_factory=list)


class GqlEventDetailData(_Gql):
    event: GqlEventNode | None = None


class GqlEventDetailResponse(_Gql):
    data: GqlEventDetailData | None = None
    errors: list[GqlError] = Field(default_factory=list)


class GqlViewerProbeResponse(_Gql):
    """Response to ``app.sources.VIEWER_PROBE_QUERY``. Authenticated iff ``data.viewer`` is an object."""

    data: dict | None = None
    errors: list[GqlError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. Source shapes: ICS (school-wide feed; per-user feeds in authenticated mode)
# ---------------------------------------------------------------------------


class IcsEvent(BaseModel):
    """One VEVENT from a CampusGroups ICS feed after unfolding and unescaping.

    Observed properties: DTSTAMP, LAST-MODIFIED, CREATED, SEQUENCE, ORGANIZER;CN=<group>,
    CONTACT, CATEGORIES;X-CG-CATEGORY=club_acronym|event_type|event_tags, DTSTART, DTEND
    (always UTC ``...Z``), UID, SUMMARY, LOCATION, URL (``.../rsvp?id=<numeric>``), DESCRIPTION
    (ends with ``\\n---\\nEvent Details: <url>``).
    """

    model_config = ConfigDict(frozen=True)

    uid: str
    url: str | None = None
    event_id: int | None = None  # numeric id parsed from URL / CONTACT
    summary: str | None = None
    description: str | None = None
    location: str | None = None  # literally "Sign in to download the location" when withheld
    organizer: str | None = None  # ORGANIZER CN = host group name
    dtstart: AwareDatetime
    dtend: AwareDatetime | None = None
    all_day: bool = False  # DTSTART;VALUE=DATE
    last_modified: AwareDatetime | None = None
    categories: dict[str, list[str]] = Field(default_factory=dict)  # X-CG-CATEGORY -> values


# ---------------------------------------------------------------------------
# 3. Normalized event (exactly the specified schema) + side channel
# ---------------------------------------------------------------------------


class Visibility(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"


class NormalizedEvent(BaseModel):
    """The normalized event, field for field as specified. Nothing else goes here."""

    model_config = ConfigDict(frozen=True)

    id: str  # ICS UID if known when first seen, else sha256(source_url); pinned forever after first insert
    source_url: str  # canonical: https://yaleconnect.yale.edu/rsvp?id=<numeric>
    title: str
    description_raw: str | None = None
    description_text: str | None = None  # HTML stripped, entities decoded, whitespace collapsed
    host_group: str | None = None
    starts_at: AwareDatetime  # tz-aware, America/New_York
    ends_at: AwareDatetime | None = None  # tz-aware, America/New_York
    location_raw: str | None = None
    cost_raw: str | None = None  # whatever the source says about price/registration
    visibility: Visibility = Visibility.PUBLIC
    updated_at_source: AwareDatetime | None = None  # ICS LAST-MODIFIED when known; GraphQL has none
    content_hash: str  # see app.parse content-hash rules in docs/sources.md


class EventExtras(BaseModel):
    """Structured source facts that the specified Event model has no slot for.

    Enrichment (food, free-to-attend, geo) and the API read these. They never change
    ``NormalizedEvent``'s shape.
    """

    model_config = ConfigDict(frozen=True)

    cg_event_id: int  # numeric CampusGroups event id (join key between GraphQL and ICS)
    graphql_id: str | None = None
    ics_uid: str | None = None
    source: Literal["graphql", "ics", "ics_personal"] = "graphql"
    is_food_included: bool | None = None  # organizer-set structured flag (GraphQL only)
    food_details: str | None = None  # organizer free text attached to the flag
    location_privacy: str | None = None  # VISIBLE | SIGN_IN_REQUIRED | REGISTRATION_REQUIRED | None
    location_hidden: bool = False  # True when the source withheld the location (unknown, not empty)
    location_kind: str | None = None  # ON_CAMPUS | OTHER
    location_address: str | None = None  # "street, city, state zip" when the source supplies one
    source_lat: float | None = None
    source_lng: float | None = None
    attendance_mode: str | None = None  # IN_PERSON | ONLINE | HYBRID
    all_day: bool = False
    is_multi_day: bool = False
    event_status: str | None = None  # source status, e.g. APPROVED
    event_type: str | None = None
    tags: tuple[str, ...] = ()
    cohost_groups: tuple[str, ...] = ()
    parent_title: str | None = None


class ParsedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: NormalizedEvent
    extras: EventExtras


class ParseError(BaseModel):
    """A per-event parse failure. Logged and skipped; never aborts a sync."""

    source: str
    ref: str | None = None  # global id / UID / index of the offending record
    message: str


# ---------------------------------------------------------------------------
# 4. Enrichment results
# ---------------------------------------------------------------------------


class FoodLabel(StrEnum):
    YES = "yes"
    LIKELY = "likely"
    NO = "no"


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str  # the matched span, quoted to the user verbatim (trimmed to <= 120 chars)
    field: Literal["title", "description", "food_details", "food_flag", "cost"]
    kind: Literal["positive", "negative", "veto", "flag"]
    weight: float
    rule: str  # lexicon rule id that fired


class FoodResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    label: FoodLabel
    evidence: tuple[EvidenceSpan, ...] = ()


class FreeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_free_to_attend: bool | None  # None = the listing does not say either way
    evidence: tuple[str, ...] = ()


class GeocodeMethod(StrEnum):
    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    ADDRESS = "address"  # matched a known street address
    DESCRIPTION = "description"  # location withheld; inferred from an explicit cue in the description
    NOMINATIM = "nominatim"
    NON_PHYSICAL = "non_physical"  # online / TBD / placeholder: resolved, deliberately no pin
    NONE = "none"


class GeoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    lat: float | None = None
    lng: float | None = None
    building_id: str | None = None
    display_name: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: GeocodeMethod = GeocodeMethod.NONE
    score: float | None = None  # fuzzy score (0-100) when method == fuzzy
    matched: str | None = None  # the alias / text that matched


class EnrichedEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    parsed: ParsedEvent
    food: FoodResult
    free: FreeResult
    geo: GeoResult


# ---------------------------------------------------------------------------
# 5. Sync bookkeeping
# ---------------------------------------------------------------------------


class SyncKind(StrEnum):
    FULL = "full"  # next 14 days, every 30 min
    HOT = "hot"  # next 6 hours, every 3 min
    SEED = "seed"  # fixture replay


class AuthMode(StrEnum):
    ANONYMOUS = "anonymous"
    AUTHENTICATED = "authenticated"


class AuthStatus(StrEnum):
    NONE = "none"  # no credential configured
    VALID = "valid"
    EXPIRED = "expired"  # credential configured but rejected / redirected to CAS / count collapse


class UpsertStats(BaseModel):
    seen: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0


class SyncResult(BaseModel):
    run_id: int | None = None
    kind: SyncKind
    mode: AuthMode
    auth_status: AuthStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    complete: bool  # every page of the primary source fetched; only complete runs count toward removal
    events_seen: int = 0
    events_created: int = 0
    events_updated: int = 0
    events_unchanged: int = 0
    events_removed: int = 0
    authenticated_only_seen: int = 0
    parse_errors: int = 0
    errors: list[str] = Field(default_factory=list)
    skipped_unchanged_responses: int = 0  # 304s and identical GraphQL bodies (zero parse work)


# ---------------------------------------------------------------------------
# 6. API response bodies (see docs/api.md)
# ---------------------------------------------------------------------------


class WindowName(StrEnum):
    NOW = "now"
    TWO_HOURS = "2h"
    TODAY = "today"
    WEEK = "week"


class EventStatus(StrEnum):
    ACTIVE = "active"
    CANCELED_OR_REMOVED = "canceled_or_removed"


class EvidenceOut(BaseModel):
    text: str
    field: str
    kind: str
    weight: float


class FoodOut(BaseModel):
    score: float
    label: FoodLabel
    evidence: list[EvidenceOut]


class LocationOut(BaseModel):
    raw: str | None
    building_id: str | None
    building_name: str | None
    lat: float | None
    lng: float | None
    geocode_method: GeocodeMethod
    geocode_confidence: float
    requires_sign_in: bool  # the source withheld the location from anonymous users


class EventOut(BaseModel):
    id: str
    source_url: str
    title: str
    host_group: str | None
    starts_at: datetime  # ISO-8601 with America/New_York offset
    ends_at: datetime | None
    all_day: bool
    starts_in_minutes: int  # negative once started
    location: LocationOut
    food: FoodOut
    is_free_to_attend: bool | None
    free_evidence: list[str]
    cost_raw: str | None
    visibility: Visibility
    status: EventStatus
    event_type: str | None
    tags: list[str]
    attendance_mode: str | None
    distance_m: float | None  # only when `near` was given and the event has a pin
    description_text: str | None  # truncated to 600 chars in lists, full in /api/events/{id}
    updated_at: datetime
    last_seen_at: datetime


class WindowOut(BaseModel):
    name: WindowName
    start: datetime
    end: datetime


class EventsResponse(BaseModel):
    generated_at: datetime
    data_as_of: datetime | None  # finish time of the last successful sync
    data_age_seconds: int | None
    window: WindowOut
    total: int
    page: int
    page_size: int
    next_page: int | None
    items: list[EventOut]


class SyncRunOut(BaseModel):
    id: int
    kind: SyncKind
    mode: AuthMode
    auth_status: AuthStatus
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    complete: bool
    events_seen: int
    events_created: int
    events_updated: int
    events_removed: int
    authenticated_only_seen: int
    error_count: int


class AuthOut(BaseModel):
    mode: AuthMode  # mode of the most recent completed sync
    status: AuthStatus
    cookie_configured: bool  # presence only, never the value
    ics_feeds_configured: int  # count only, never the URLs
    data_includes_authenticated_events: bool


class CountsOut(BaseModel):
    active: int
    removed: int
    by_visibility: dict[str, int]
    by_food_label: dict[str, int]
    geocoded: int
    location_withheld: int


class CoverageOut(BaseModel):
    primary_source: str
    sources: dict[str, dict]  # per source: last fetch time, status, events, last_modified, ...
    geocode_rate: float | None


class DatasetOut(BaseModel):
    kind: Literal["live", "fixture_replay"]
    time_shift_days: int  # fixture replays shift event times by whole days so "now" has data


class MetaOut(BaseModel):
    now: datetime
    data_as_of: datetime | None
    data_age_seconds: int | None
    last_sync: SyncRunOut | None
    last_full_sync: SyncRunOut | None
    last_hot_sync: SyncRunOut | None
    auth: AuthOut
    counts: CountsOut
    coverage: CoverageOut
    dataset: DatasetOut
