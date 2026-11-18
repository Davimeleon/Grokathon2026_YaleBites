# HTTP API

Base URL: `http://127.0.0.1:8000` (`uv run app`). All responses are JSON (`application/json; charset=utf-8`).
Timestamps are ISO-8601. Event times carry the `America/New_York` offset (e.g. `2026-11-17T18:00:00-05:00`);
bookkeeping times (`generated_at`, `data_as_of`, `last_seen_at`, sync times) are UTC (`...Z` / `+00:00`).
Response models are defined in `app/schemas.py` (`EventsResponse`, `EventOut`, `MetaOut`).

The built frontend (`web/dist`) is served at `/` by the same process when it exists.

## Caching and data age

* Every `/api/*` response carries `Cache-Control: public, max-age=60` and `X-Data-Age: <seconds>` (age of
  the newest successful sync; omitted when there has never been one).
* The server memoises each distinct response for 60 seconds, keyed by path and normalized query string.
  A completed sync clears the memo, so new data is visible right after a sync.
* Bodies carry `data_as_of` (finish time of the newest successful sync) and `data_age_seconds`.

## `GET /api/events`

| param | type | default | meaning |
|---|---|---|---|
| `window` | `now` \| `2h` \| `today` \| `week` | `today` | time window, see below |
| `food` | comma list of `yes`,`likely`,`no` | (all) | keep events whose `food.label` is in the list |
| `free_only` | bool (`true`/`false`/`1`/`0`) | `false` | keep only `is_free_to_attend == true` |
| `near` | `lat,lng` | – | origin for distance; adds `distance_m` |
| `radius_m` | int, 1–20000 | `1500` when `near` is given | keep events with a pin within this radius of `near` |
| `q` | string | – | case-insensitive substring match on title, host group, description, location |
| `sort` | `start` \| `distance` | `start` | `distance` requires `near` |
| `page` | int ≥ 1 | `1` | |
| `page_size` | int 1–200 | `50` | |

Invalid values return `422` with FastAPI's standard `{"detail": [...]}` body.

### Window semantics

`now` is the request time (UTC). A missing `ends_at` is treated as `starts_at + 1 hour` for filtering only.
An event matches a window when its span overlaps it:

| window | span |
|---|---|
| `now` | `[now, now + 30 min)`: in progress now or starting within 30 minutes |
| `2h` | `[now, now + 2 h)` |
| `today` | `[now, next local midnight in America/New_York)` |
| `week` | `[now, now + 7 days)` |

**Long-running events.** CampusGroups filters by overlap, so a listing that runs for weeks (for example a
volunteer program posted as Aug 29 – Dec 19) would sit in every window. An event whose span is longer than
24 hours matches `now`, `2h` and `today` only if it *starts* inside that window; in `week` it matches by
overlap like everything else. All-day events (local midnight to midnight, exactly 24 h) are not long-running.

Events with `status == "canceled_or_removed"` are never returned by `/api/events`.

### Sort order

`sort=start` (default): `starts_at` ascending, then `food.score` descending, then `title`.
`sort=distance`: `distance_m` ascending, then `starts_at`, then `food.score` descending. `near` always applies the
radius filter, so only pinned events are returned when `near` is given. The frontend's default walking-distance
ordering is done client-side and keeps unpinned events, which it lists after the pinned ones.

### Response

```json
{
  "generated_at": "2026-11-17T23:01:12Z",
  "data_as_of": "2026-11-17T22:58:40Z",
  "data_age_seconds": 152,
  "window": {"name": "2h", "start": "2026-11-17T23:01:12Z", "end": "2026-11-18T01:01:12Z"},
  "total": 14,
  "page": 1,
  "page_size": 50,
  "next_page": null,
  "items": [
    {
      "id": "e0a7c3...@campusgroups.com",
      "source_url": "https://yaleconnect.yale.edu/rsvp?id=2337199",
      "title": "Queer Tables: Queer Italian Pasta",
      "host_group": "Yale LGBTQ Center",
      "starts_at": "2026-11-17T18:30:00-05:00",
      "ends_at": "2026-11-17T20:00:00-05:00",
      "all_day": false,
      "starts_in_minutes": 29,
      "location": {
        "raw": "LGBTQ Center, 135 Prospect St",
        "building_id": "lgbtq-center",
        "building_name": "Yale LGBTQ Center (135 Prospect St)",
        "lat": 41.3133,
        "lng": -72.9254,
        "geocode_method": "alias",
        "geocode_confidence": 0.95,
        "requires_sign_in": false
      },
      "food": {
        "score": 0.97,
        "label": "yes",
        "evidence": [
          {"text": "Food included (organizer flag)", "field": "food_flag", "kind": "flag", "weight": 2.0},
          {"text": "pasta dinner provided", "field": "description", "kind": "positive", "weight": 1.6}
        ]
      },
      "is_free_to_attend": true,
      "free_evidence": ["RSVP: $0.00"],
      "cost_raw": "RSVP: free ($0.00 USD); capacity: unlimited",
      "visibility": "public",
      "status": "active",
      "event_type": "Social",
      "tags": ["LGBTQ+"],
      "attendance_mode": "IN_PERSON",
      "distance_m": null,
      "description_text": "Join us for ... (truncated to 600 characters)",
      "updated_at": "2026-11-17T22:58:40Z",
      "last_seen_at": "2026-11-17T22:58:40Z"
    }
  ]
}
```

* `location.requires_sign_in` is `true` when Yale Connect withheld the location from anonymous users
  (`eventLocationPrivacy` `SIGN_IN_REQUIRED`/`REGISTRATION_REQUIRED`). If an authenticated sync revealed it,
  `raw` is filled and the flag stays `true`.
* `location.lat`/`lng` are `null` when the location could not be resolved; such events are still listed.
* `food.evidence[].text` is the matched span, verbatim, at most 120 characters.
* `distance_m` is great-circle distance in metres from `near`; `null` without `near` or without a pin.
* `next_page` is `page + 1` when more results exist, else `null`.

## `GET /api/events/{id}`

`id` is the event's `id` exactly as returned in lists (URL-encode it: UIDs contain `@` and `:`).
Returns one `EventOut` with the full `description_text` (not truncated), plus `food.evidence` in full.
Removed events are returned too (with `"status": "canceled_or_removed"`) so old links resolve.

* `404` `{"detail": "event not found"}` for unknown ids.

## `GET /api/meta`

```json
{
  "now": "2026-11-17T23:01:12Z",
  "data_as_of": "2026-11-17T22:58:40Z",
  "data_age_seconds": 152,
  "last_sync": {
    "id": 42, "kind": "hot", "mode": "anonymous", "auth_status": "none",
    "started_at": "2026-11-17T22:58:38Z", "finished_at": "2026-11-17T22:58:40Z", "duration_ms": 1830,
    "complete": true, "events_seen": 72, "events_created": 0, "events_updated": 1, "events_removed": 0,
    "authenticated_only_seen": 0, "error_count": 0
  },
  "last_full_sync": {"...": "same shape as last_sync"},
  "last_hot_sync": {"...": "same shape as last_sync"},
  "auth": {
    "mode": "anonymous",
    "status": "none",
    "cookie_configured": false,
    "ics_feeds_configured": 0,
    "data_includes_authenticated_events": false
  },
  "counts": {
    "active": 371,
    "removed": 2,
    "by_visibility": {"public": 371, "authenticated": 0},
    "by_food_label": {"yes": 31, "likely": 9, "no": 331},
    "geocoded": 64,
    "location_withheld": 288
  },
  "coverage": {
    "primary_source": "graphql",
    "sources": {
      "graphql": {"last_fetch_at": "2026-11-17T22:58:40Z", "pages": 2, "events": 72, "status": "ok"},
      "ics": {"last_fetch_at": "2026-11-17T22:31:02Z", "status": "not_modified",
              "last_modified": "Tue, 17 Nov 2026 06:05:24 GMT", "events_in_window": 344}
    },
    "geocode_rate": 0.17
  },
  "dataset": {"kind": "live", "time_shift_days": 0}
}
```

* `last_sync`, `last_full_sync`, `last_hot_sync` are `null` before the first sync.
* `auth.mode` is the mode of the newest completed sync. `auth.status` is `none` (no credential configured),
  `valid`, or `expired` (configured, but rejected, redirected to CAS, or the event count collapsed).
  `cookie_configured` and `ics_feeds_configured` report presence and count only, never values.
* `auth.data_includes_authenticated_events` is `true` when any active event has `visibility == "authenticated"`.
* `dataset.kind` is `fixture_replay` after `make seed`. Seeding shifts every event by a whole number of days
  (`time_shift_days`) so the recorded data lines up with today. A live sync sets it back to `live`.
* `coverage.geocode_rate` is the share of active, in-person events with a pin.
