# freefood — Yale free-food & free-event finder

A small web app that shows, in near real time, which events on Yale's campus are happening now or in the
next few hours and offer **free food** (or are free to attend). Data comes from Yale Connect
(`yaleconnect.yale.edu`, a CampusGroups instance). Ingestion correctness and freshness are the core; the UI
is a thin map + list over a good dataset.

* `docs/sources.md`: what Yale Connect exposes, measured, with evidence and the source ranking.
* `docs/api.md`: the HTTP API.
* `tests/data/fixtures/`: recorded real responses (the interface contract every test runs against).

## Quick start

```bash
make install          # uv sync + npm ci (web)
make web              # build the frontend into web/dist
make seed             # load the recorded fixtures into var/freefood.sqlite3 (offline)
make test             # full test suite (offline, hermetic)
uv run app            # API + frontend on http://127.0.0.1:8000, live sync every 3 / 30 min
uv run app --no-sync  # serve the seeded database without touching the network
```

`make seed` replays the recorded fixtures through the real sync pipeline and shifts them by whole days so
the capture date lines up with today, so the default "free food in the next 2 hours" view has data. `/api/meta`
reports `dataset.kind = "fixture_replay"` until the first live sync replaces it.

## How it works

```
             every 30 min: full sync (next 14 days)        every 3 min: hot sync (next 6 hours)
                                  \                              /
  Yale Connect ──► app/fetch (one httpx client, ≤4 in flight, ≥250 ms/host, backoff+jitter, robots.txt,
                   ETag/Last-Modified + body-hash change detection, raw-response disk cache)
                ──► app/parse (GraphQL nodes / ICS VEVENTs → NormalizedEvent + EventExtras, content hash)
                ──► app/parse/store.plan_upsert (pin ids, merge withheld locations, diff hashes)
                ──► app/pipeline (only new/changed events): app/enrich/food.py + app/enrich/geo.py
                ──► SQLite (var/freefood.sqlite3) ──► app/api (FastAPI, 60 s memo) ──► web/ (Vite + MapLibre)
```

* **Primary source:** the CampusGroups GraphQL API (`/api/v2/graphql`, `events` connection with a server-side
  `dateRange`, 50 per page). It carries descriptions, the organizer's structured *food included* flag, location
  privacy, host group and registration, so there are no per-event detail fetches.
* **Secondary source:** the school-wide ICS feed, used only for the ICS `UID` (the event id) and `LAST-MODIFIED`
  (`updated_at_source`). It is conditional-GET'd, so it is almost always a free `304`.
* **Unchanged data is free:** a `304` or a byte-identical GraphQL page marks its events as seen without parsing.
  An unchanged `content_hash` only bumps `last_seen_at`. Enrichment runs only for new or changed events, or for
  everything once if `data/food_lexicon.yaml` or `data/buildings.yaml` changes.
* **Removals:** an event missing from two consecutive *complete* syncs of the *same auth mode* that covered its
  time becomes `canceled_or_removed`. Anonymous syncs never count absence for authenticated-only events, so an
  expired cookie cannot mass-cancel them.

## Authenticated mode (optional)

Anonymous Yale Connect withholds about 74% of event locations ("Sign in to download the location") and every
members-only event. To include them, supply your own browser session at runtime:

```bash
export YALECONNECT_COOKIE='<the whole Cookie header from a signed-in yaleconnect.yale.edu tab>'
export YALECONNECT_ICS_FEEDS='<your personal feed URL from /ics_helper>'   # optional, comma-separated
make demo-auth        # validates the session, runs one full sync, prints anonymous vs authenticated counts
uv run app
```

* Get the cookie from DevTools → Network → any request to `/api/v2/graphql` → Request Headers → `cookie`.
* Credentials are read from the environment only. They are never written to disk, logged, or returned by the
  API, and they are sent only to `yaleconnect.yale.edu`, never to CAS or the CDN. A test greps all log output
  from a full sync for them.
* An expired session (CAS redirect, login page, `AUTH_NOT_AUTHENTICATED`, or an event-count collapse) logs one
  warning naming `YALECONNECT_COOKIE`, and the run falls back to anonymous with `auth_status = expired`.
  `make demo-auth` exits non-zero with instructions so a dead cookie shows up before a demo.
* We do **not** script the CAS login, store passwords, or spoof the browser-only OIDC endpoint (see
  `docs/sources.md` §4).

## Using the map

* The page asks for your location when it loads (the map's ◎ button asks again or recenters). With a position,
  the list shows walking times and sorts by distance.
* Tap an event (card or pin) to draw the **walking route** to it, Google-Maps style, with time and distance;
  "Google Maps" opens turn-by-turn directions. Routes come from the public OpenStreetMap pedestrian router at
  `routing.openstreetmap.de`, so your position and the destination are sent there when you select an event. If it
  is unreachable, a dashed straight line with an estimate is shown instead.

## Configuration

| variable | default | meaning |
|---|---|---|
| `YALECONNECT_COOKIE` | – | session cookie header (authenticated mode) |
| `YALECONNECT_ICS_FEEDS` | – | comma-separated per-user ICS feed URLs |
| `FREEFOOD_CONTACT` | `operator@example.invalid` | contact string in the User-Agent |
| `FREEFOOD_VAR_DIR` | `./var` | DB, raw-response caches, Nominatim cache |
| `FREEFOOD_DB` | `$FREEFOOD_VAR_DIR/freefood.sqlite3` | database file |
| `FREEFOOD_SCHEDULER` | `1` | `0` serves existing data without syncing |
| `FREEFOOD_ONLINE_GEOCODING` | `1` | Nominatim fallback for unknown locations (1 req/s, cached permanently) |
| `FREEFOOD_HOST` / `FREEFOOD_PORT` | `127.0.0.1` / `8000` | bind address |

## Repository layout

| path | contents |
|---|---|
| `app/sources.py` | endpoints, GraphQL documents, id helpers (contract) |
| `app/schemas.py`, `app/db/models.py` | frozen data contracts |
| `app/fetch/`, `app/auth.py`, `app/sync.py`, `app/seed.py` | fetch layer, session handling, scheduler, fixture replay |
| `app/parse/` | GraphQL/ICS parsing, normalization, content hash, upsert |
| `app/enrich/food.py`, `data/food_lexicon.yaml` | free-food scoring classifier and its editable lexicon |
| `app/enrich/geo.py`, `data/buildings.yaml` | Yale gazetteer, fuzzy matching, Nominatim fallback |
| `app/api/` | FastAPI app |
| `web/` | Vite + TypeScript + MapLibre frontend |
| `scripts/record_fixtures.py` | operator-only: re-record live responses |
| `scripts/build_auth_fixtures.py` | builds the hand-constructed authenticated fixtures |

## Measured quality (fixture set, `make test` prints these)

* Free-food classifier, 250 hand-labeled real events. Gold labels came from two blind annotators plus an adjudicator; 2 of 250 disagreements.
  "yes" precision **0.992** (130/131), recall 0.977; "yes+likely" precision 0.965. A separate review of the
  1,007 unlabeled real events found 79 of 81 predicted "yes" correct. Both misses were fixed with general rules and
  regression tests (`tests/test_food_review_fixes.py`).
* Gazetteer: **93.4%** of the 376 distinct `location_raw` values in the fixtures resolve (347 pinned, 4 online/TBD).
  The 25 unresolved are listed by the test; most are out of town (Berlin, Tokyo, NYC, DC).

## Known limitations

* Anonymous Yale Connect withholds the location of about 74% of events, so most cards say "Location requires Yale
  sign-in" and are not on the map until authenticated mode is configured.
* Whether `/api/v2/graphql` accepts the browser session cookie alone (rather than the SPA's OIDC bearer token) could
  not be verified without a real session. `make demo-auth` tells you in one request. Personal ICS feeds work either way.
* Building coordinates in `data/buildings.yaml` were placed by hand. Nominatim and Overpass refused requests from the
  build machine. Spot checks put most within about 100 m.
* The Nominatim fallback (`FREEFOOD_ONLINE_GEOCODING=1`) needs an egress IP that Nominatim accepts. A refusal just
  means no pin; it is never cached as a miss.
