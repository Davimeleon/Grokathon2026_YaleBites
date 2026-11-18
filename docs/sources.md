# Yale Connect data sources

Everything below was measured against `https://yaleconnect.yale.edu` (a CampusGroups instance) on
**2026-11-17 between 22:40 and 22:55 UTC** (17:40–17:55 EST), anonymously. The recorded responses are in
`tests/data/fixtures/` (see `tests/data/fixtures/manifest.json` for every URL, request body, status and
validator header). `scripts/record_fixtures.py` reproduces the recording; it is the only code that touches
the live site outside the running app.

## Summary and ranking

| rank | source | role in this app | why |
|---|---|---|---|
| 1 | GraphQL `POST /api/v2/graphql`, `events` connection | **primary**: every field of every event | live, server-side date filtering, structured food flag, location privacy markers, cost options, description; no N+1 |
| 2 | School-wide ICS `GET /ical/yale/ical_yale.ics` | **secondary**: ICS `UID` (event id) and `LAST-MODIFIED` (`updated_at_source`); fallback list if GraphQL fails | regenerated about once a day; 87% of its locations hidden; no cost or food fields |
| 3 | Per-user ICS feed (`YALECONNECT_ICS_FEEDS`) | authenticated mode only: favourites with locations | needs a per-user token |
| – | Legacy JSON `GET /mobile_ws/v17/mobile_events_list` | **not used** | `Disallow: /mobile_ws/v17/` in robots.txt |
| – | HTML (`/rsvp?id=`, `/events`) | **not used** | detail pages are an empty React shell; every field is in GraphQL |

The brief ranked "structured feed > JSON endpoint > HTML". The data overturns the first two: the ICS feed is
structured, but in the 14-day window it is a strict subset of GraphQL (344 vs 371 events). It is about 16 hours
stale despite advertising `X-PUBLISHED-TTL:PT1H`. It hides 4,696 of 5,412 locations, and it has no cost or
food fields. GraphQL is the structured source that is also fresh, so it is primary, and ICS contributes only
what GraphQL lacks.

## robots.txt

`https://yaleconnect.yale.edu/robots.txt` (fixture `robots/yaleconnect_robots.txt`):

```
User-agent: *
Disallow: /upload/
Disallow: /student_docs/
Disallow: /downloads/
Disallow: /mobile_ws/v17/
Disallow: /mobile_ws/v18/
Disallow: /*ajax_widget_new
```

* `/api/v2/graphql`, `/ical/...` and `/ics_helper` are allowed. `/upload/` (images, flyers) is disallowed, so no
  image is ever fetched.
* The ICS feed redirects to `static-prod-us-east-1.campusgroups.com`. That host answers `/robots.txt` with
  `403` (S3 AccessDenied). RFC 9309 §2.3.1.3 treats 4xx as "no restrictions". The fetch layer checks robots
  separately per host and keeps a separate politeness bucket for that host.

## 1. GraphQL API (primary)

* Endpoint: `POST https://yaleconnect.yale.edu/api/v2/graphql`, `Content-Type: application/json`, body
  `{"operationName", "query", "variables"}`. HotChocolate server (error codes `HC0046` etc.).
* Discovered in the React bundle served at `/w/assets/index-*.js` (`/api/v2/graphql` is the Apollo `uri`).
  The operation documents are compiled into `/w/assets/vendor-shared-*.js`. The SPA's own `Events` list
  query and `Event` detail query were decoded from there and are the basis of `app/sources.py`.
* Introspection is disabled (`"Introspection is not allowed for the current request."`). Field and filter
  names were confirmed from the bundle and from validation errors, which name unknown fields.
* Responses: `cache-control: no-store`, **no ETag/Last-Modified**. Change detection is by response-body
  SHA-256 (`http_validators.body_sha256`).

### `events` connection

```graphql
query Events($first: Int, $after: String, $filter: EventFilterInput) {
  events(first: $first, after: $after, filter: $filter) {
    totalCount
    pageInfo { endCursor hasNextPage }
    edges { node { ...EVENT_FIELDS (app/sources.py) } }
  }
}
```

* **Pagination**: Relay cursors. `first` must be 1–50 (`'first' must be between 1 and 50.`). `after` is the
  previous `pageInfo.endCursor`. Stop when `hasNextPage` is false.
* **Date range filtering is server-side**: `filter: {timeRange: CUSTOM, dateRange: {from: DateTime, to:
  DateTime}}`. `timeRange: CUSTOM` without `dateRange` fails with `timeRange CUSTOM requires
  'filter.dateRange'.`. Dates must be full DateTimes (`2026-11-18` alone is rejected). The filter is an
  **overlap** test: a volunteer program posted as 2026-08-29 → 2026-12-19 is returned for a 3-hour window
  next week.
* Other `timeRange` values (bundle enum): `ALL, CREATED_RECENTLY, CUSTOM, LIVE, LIVE_OR_UPCOMING, PAST,
  UPCOMING`. Other valid filter fields: `hubId, parentId, search, groupIds, groupTypeIds,
  groupCategoryIds, typeIds, tagIds, locationKinds, attendanceModes`. There is no server-side food filter
  (`isFoodIncluded` is not a filter field).
* `totalCount` is `null` for `UPCOMING` and for a 28-day `CUSTOM` range, but populated for 14-day ranges (371).
  The sync never relies on it.
* Measured: the 14-day window `[2026-11-17T22:53Z, 2026-12-01T22:53Z)` took 8 pages and 371 events (`totalCount`
  371, no duplicates). The 6-hour hot window took 2 pages and 72 events. The previous 14 days took 19 pages and
  920 events. The upcoming window spans Thanksgiving recess, so it is light; the previous fortnight is typical.

### Event fields and what they contain (1,291 events across the two 14-day windows)

| field | upcoming 371 | past 920 | notes |
|---|---|---|---|
| `descriptionText` non-empty | 371 | 918 | plain text with `\r\n`; always present |
| `descriptionHtml` non-empty | 47 | 93 | usually `""`; when present it is the HTML original |
| `isFoodIncluded` true | 36 | 106 | organizer checkbox ("food included"); **structured food signal** |
| `foodDetails` non-empty | 19 | 49 | free text attached to the flag ("Pizza!!", "Catered dinner", "Pizza & Salad, Cash Bar") |
| `location.eventLocationPrivacy` = `SIGN_IN_REQUIRED` | 273 | 733 | `location.name`/`address` are `null` anonymously |
| `… = REGISTRATION_REQUIRED` | 15 | 30 | hidden until you register, so a login alone does not reveal it |
| `… = VISIBLE` | 47 | 98 | `location.name` present (46 / 95) |
| `… = null` | 36 | 59 | online-only events (`attendanceMode` ONLINE) |
| `location.address` present | 28 | 54 | street/city/state/zip, zip often `""` |
| `location.coordinates` present | **0** | **0** | never populated, so all pins come from our gazetteer |
| `registration.options[].price` | all `$0 USD` | all `$0 USD` | 1,058 options, every one free; cost information lives in descriptions |
| `externalRegistration.url` | 47 | 72 | outside sign-up (Zoom, Google Forms, Eventbrite...) |
| `isMultiDay` | 26 | 43 | |
| `endDate` null | 0 | 0 | handled anyway |
| `status` | APPROVED (all) | APPROVED (all) | |
| `timezone` | America/New_York (all) | same | `startDate`/`endDate` are UTC `...Z` |

There is no `updatedAt`, `createdAt`, `visibility`, `privacy` or `isCanceled` field (validation errors
`The field 'updatedAt' does not exist on the type 'Event'`, etc.).

### Detail query (`event(id:)`): not needed

`query Event($id: ID!) { event(id: $id) { ...same fields } }`. For all 16 recorded detail responses
(`graphql/detail_*.json`) the detail node is **byte-for-byte equal** to the node in the list response. The
list already carries description, food, location, host group and registration, so the sync makes **no
per-event detail fetches**. An unknown id returns `errors[0].extensions.code = "EVENT_NOT_AVAILABLE"`
(`graphql/detail_not_found.json`).

### Identifiers

* `Event.id` is a Relay global id: `base64("yale:Event:<numeric>")`, e.g. `eWFsZTpFdmVudDoyMzI2NTE1` →
  `2326515`.
* The numeric id is the join key with ICS, whose `URL:` is `https://yaleconnect.yale.edu/rsvp?id=<numeric>`.
* **Canonical `source_url`** = `https://yaleconnect.yale.edu/rsvp?id=<numeric>` for every source.

### Fields deliberately not requested

`organizerContact` (host names/e-mails), `speakers`, `attendees`/`attendeeCount`, registration `questions`,
`settledTickets`, `ongoingCarts`: personal data or other users' data. Volatile fields `isLive`,
`isSoldOut`, `slotsRemaining`: they would change the response body every few minutes and defeat
unchanged-body detection.

## 2. School-wide ICS feed (secondary)

* `GET https://yaleconnect.yale.edu/ical/yale/ical_yale.ics` → `302` →
  `https://static-prod-us-east-1.campusgroups.com:443/ical/yale/ical_yale.ics` (S3/CloudFront).
* `200`, `text/calendar`, **6.08 MB**, 5,412 VEVENTs, events from 2026-08-19 to 2029-01-01 (it includes past
  events). It has `ETag` and `Last-Modified`, and **conditional requests work**: `If-None-Match` → `304`
  (`ics/ical_yale_conditional.txt`).
* Freshness: `Last-Modified: Tue, 17 Nov 2026 06:05:24 GMT`, about 16.6 hours old when fetched, although the
  calendar says `X-PUBLISHED-TTL:PT1H` and `/ics_helper` claims feeds update "every time an event is added or
  updated". Max `LAST-MODIFIED` inside is `20261117T055949Z`. A per-group feed was 4 days old.
* VEVENT properties: `DTSTAMP, LAST-MODIFIED, CREATED, SEQUENCE, ORGANIZER;CN="<group>"`,
  `CONTACT:https://yaleconnect.yale.edu/<group>/rsvp_boot?id=<n>#event_host`,
  `CATEGORIES;X-CG-CATEGORY=club_acronym|event_type|event_tags:<values>`, `DTSTART`/`DTEND` (always UTC
  `…Z`, no `VALUE=DATE` seen), `UID` (unique over all 5,412, e.g. `4204b9…872026_1:54:19_PM@campusgroups.com`),
  `SUMMARY;ENCODING=QUOTED-PRINTABLE`, `LOCATION`, `URL`, and `DESCRIPTION`, which ends with
  `\n---\nEvent Details: <url>`.
* **`LOCATION:Sign in to download the location` on 4,696 / 5,412 events (87%)**. In the 14-day window 291 / 344
  are hidden.
* No price, registration or food properties.

### Reconciliation with GraphQL (same 14-day window)

* ICS in window (overlap): **344**. GraphQL: **371**. ICS ∖ GraphQL = **0**. GraphQL ∖ ICS = **27**.
* Of the 27, most have numeric ids above the highest id in the ICS (`2339268`), so they were created after the
  daily regeneration. The rest are older events edited or published since (e.g. `2325120`).
* Consequence for ids: an event can be seen first through GraphQL, before it has an ICS UID. The rule
  "id = ICS UID if present, else sha256(source_url)" is therefore applied **once, at first insert**. Rows are
  looked up by `source_url` and the stored `id` never changes afterwards (see "Normalization rules").

### Per-group feeds and `/ics_helper`

`GET /ics_helper` (anonymous, 3.4 MB HTML; fixture `html/ics_helper.html.gz`) lists:

* the school-wide feed above;
* **1,446 per-group feeds** `https://yaleconnect.yale.edu/ical/yale/ical_club_<group_id>.ics` (same 302 →
  static host pattern);
* "Your Personal Feed of Events — Login Required": a per-user favourites feed whose URL embeds a user token.
  It is only shown when signed in, so its exact URL shape was not observable. `YALECONNECT_ICS_FEEDS` accepts
  any `https://` URL.

Per-group feeds add almost nothing. Group 38473's feed has 74 events, of which 1 is not in the school-wide feed
(an August event), 70 have hidden locations, and the file is 4 days old. Group 33342: 203 events, none
missing from the school feed. Polling 1,446 feeds would cost 1,446 requests for that. They are not polled.

## 3. HTML

* `GET /rsvp?id=2326515` → `302 /frocos27/rsvp?id=…` → `302 /frocos27/rsvp_boot?id=…` →
  `302 /w/_/_/events/eWFsZTpFdmVudDoyMzI2NTE1` → `200`: a 432-byte React shell with `<div id="root">` and no
  content (`html/rsvp_spa_shell.html`). Every field it renders comes from GraphQL. **No HTML is parsed.**
* The legacy `/events` page (276 KB) populates its list from
  `/mobile_ws/v17/mobile_events_list?range=<offset>&limit=40&filter1..9=…&order=&search_word=`, a
  positional `p0…pN` JSON array whose last row carries `counter`. robots.txt disallows `/mobile_ws/v17/`.
  The endpoint was identified from page JavaScript only and **never requested**.

## 4. Authentication and coverage

### What the anonymous responses reveal is withheld

Over the 14-day window `[2026-11-17T22:53Z, 2026-12-01T22:53Z)`:

| | anonymous | authenticated session would add |
|---|---|---|
| events listed (GraphQL) | **371** | events whose audience is restricted to signed-in users or members. They are absent from anonymous responses, not merely redacted, so their number cannot be measured anonymously. |
| events listed (ICS) | 344 | nothing for the static school-wide file, which is identical for everyone |
| locations | 47 visible + 36 online | **273 `SIGN_IN_REQUIRED` locations** (74% of events) |
| locations after registration | – | 15 `REGISTRATION_REQUIRED` stay hidden: they need a registration, not just a login |
| cost / food / description | complete | nothing: these fields are not privacy-gated |

So the measurable gain from signing in is location: anonymously only about 22% of in-person events have a
location. The unmeasurable gain is members-only events. The app measures it at runtime: in authenticated mode
each sync runs the anonymous pass and the authenticated pass over the same window, and an event seen only in
the authenticated pass gets `visibility = authenticated`. There is no visibility field in the schema, so this
diff is the only way to tell.

### How the front-end authenticates

* The React app uses OIDC (authorization code + PKCE) against the same origin: `client_id=cg-web-spa`,
  `scope="openid profile email offline_access campusgroups"`, endpoints from
  `/.well-known/openid-configuration` (`/auth/authorize`, `/auth/token`). Apollo sends
  `Authorization: Bearer <access_token>`, and fetches are same-origin, so the browser also sends cookies.
* `/auth/authorize` refuses non-browser clients: `403 {"error":"fetch_metadata_required","error_description":
  "This endpoint requires a browser that emits Sec-Fetch-* headers…"}`. This is a deliberate anti-automation
  control. **We do not spoof Sec-Fetch headers and do not mint tokens.** We do not script CAS either.
* The app sends the operator-supplied `YALECONNECT_COOKIE` as the `Cookie` header on requests to
  `yaleconnect.yale.edu` only, exactly as the browser does for same-origin fetches.
* Session probe: `query SessionProbe { viewer { latestHubId } }`. Anonymously it returns
  `errors[0].extensions.code = "AUTH_NOT_AUTHENTICATED"`, `data: null` (`graphql/viewer_anonymous.json`), and
  so does a bogus `CG.SessionID` cookie. A session the API accepts returns `data.viewer` as an object. The
  probe selects nothing identifying.
* **Open question that only a real session can answer**: whether `/api/v2/graphql` accepts the legacy session
  cookie without a bearer token. `make demo-auth` answers it in one request and exits non-zero with
  instructions if the cookie is rejected. Per-user ICS feeds (`YALECONNECT_ICS_FEEDS`) work from a token in the
  URL and do not depend on that answer.

### Session-expiry signals handled

1. the probe returns `AUTH_NOT_AUTHENTICATED`;
2. a request answered with a redirect to `secure.its.yale.edu/cas/...` or `/login`. Such redirects are never
   followed. Other redirects are followed one hop at a time by the fetch layer, and the cookie is attached only
   when the target is exactly `https://yaleconnect.yale.edu`, so it never reaches CAS or the static ICS host;
3. a CAS login page in a body (`Central Authentication Service`, `name="username"` / `id="fm1"`);
4. a count collapse: the authenticated pass returns fewer events than the anonymous pass of the same run, or
   under half of the previous authenticated run of the same kind.

Any of these sets `auth_status = expired` for the run, logs one warning naming `YALECONNECT_COOKIE`, and
completes the run anonymously. There are no retry loops.

### Authenticated fixtures

No authenticated response was captured. `tests/data/fixtures/auth/` holds hand-constructed, sanitized
responses (`scripts/build_auth_fixtures.py`) derived from the real anonymous ones: the 273
`SIGN_IN_REQUIRED` locations are filled from real public ICS `LOCATION` strings, 8 members-only events are
added, and the set includes probe outcomes, a CAS redirect, a CAS login page, a count-collapse page and a
personal ICS feed. Dummy credentials are listed in `tests/data/fixtures/auth/README.md`.

## 5. Normalization rules forced by the data

* `title` = `name` (whitespace-collapsed). `host_group` = `hostGroup.name`.
* `description_raw` = `descriptionHtml` when non-empty, else `descriptionText`. `description_text` = HTML
  stripped, entities decoded, whitespace collapsed.
* `starts_at`/`ends_at` = `startDate`/`endDate` (UTC) converted to America/New_York.
* `location_raw` = `location.name`, followed by `", street, city, state zip"` when an address is present and its
  street is not already in the name. ICS: `LOCATION` unescaped, `null` for the "Sign in to download the
  location" placeholder. A withheld location is **unknown, not empty**. An anonymous run never erases a
  location that an authenticated run revealed, and that does not count as a content change.
* `cost_raw` = registration options rendered as text (`"RSVP: free ($0.00 USD)"`), capacity mode, and the
  external registration URL. `null` when the source says nothing.
* `id` = ICS `UID` when the ICS index knows the numeric id at first insert, else `sha256(source_url)`; pinned
  forever by `source_url`.
* `updated_at_source` = ICS `LAST-MODIFIED` when known (GraphQL has no timestamp). It is excluded from
  `content_hash` so the daily ICS regeneration does not churn rows.
* `content_hash` = sha256 over the semantic fields (`title, description_raw, description_text, host_group,
  starts_at, ends_at, location_raw, cost_raw, visibility`) **plus the food inputs** `is_food_included` and
  `food_details`. Those feed the classifier, so a change to them must re-run enrichment.
* `descriptionText` vs `descriptionHtml`: for 81 of the 1,291 recorded events the text of the HTML differs from
  `descriptionText`. Most differences are formatting, but some are wholesale mismatches. For example, "Lost in New
  Haven: Museum Tour & Happy Hour" carries a vermicomposting workshop's text in `descriptionText`, while its HTML
  matches the title. The HTML is the up-to-date one, so it wins. 19 of the 250 labeled events are affected; their
  gold labels were made on the plain text, so `tests/data/labeled.jsonl` keeps that text for them
  (`description_source`), and `tests/test_labeled_consistency.py` pins every other input to the parser's output.
