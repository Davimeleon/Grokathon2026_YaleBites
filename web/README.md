# freefood web

Vanilla TypeScript + Vite + MapLibre GL (OpenStreetMap raster tiles). One page: a filter bar driving a
synchronized map and card list of events with free food on Yale's campus.

## Dev

```sh
uv run app            # backend on http://127.0.0.1:8000 (from the repo root; `make seed` for fixture data)
cd web && npm install
npm run dev           # http://localhost:5173, proxies /api -> http://127.0.0.1:8000
```

## Build and test

```sh
npm run build         # tsc --noEmit && vite build -> web/dist (base "/"), served by FastAPI at "/"
npm test              # vitest (jsdom for DOM tests; MapLibre is mocked, no network)
```

## How it talks to the API

* Relative URLs only (`/api/events`, `/api/meta`), so the same build works behind the dev proxy and when
  FastAPI serves `web/dist` from the same origin. Contract: `docs/api.md`.
* Default query: `window=2h&food=yes,likely&page_size=200`. Filters map to `window`, `food`, `free_only`,
  `q` (debounced 300 ms) and, with "Near me" on and a position known, `near=lat,lng&sort=distance`.
* Relative times ("in 25 min") are computed against server time: the offset between `generated_at` and
  the device clock is measured on every fresh response.
* Walking time is haversine x 1.3 at 80 m/min. With a position the list and map are sorted by it,
  otherwise by start time; the page never waits for the geolocation prompt.
* Each successful `/api/events` payload is cached in `localStorage` keyed by its query string. When the
  API is down or errors, the cached payload (at most 24 h old) is shown under an "Offline — showing data
  from HH:MM" banner; the next successful fetch clears it. `/api/events` and `/api/meta` are polled every
  60 s while the page is visible, and immediately when it becomes visible again.

## Layout

| path | role |
|---|---|
| `src/api.ts` | fetch client, `loadEvents` (fetch, cache, offline fallback) |
| `src/cache.ts` | localStorage cache read/write and staleness |
| `src/store.ts` | app state, subscribe, `visibleEvents` (the one result set both views render) |
| `src/query.ts` | filter state and `/api/events` query building |
| `src/format.ts`, `src/geo.ts`, `src/geojson.ts` | formatting, walking distance/sorting, pins |
| `src/views/*` | header, filter bar, list, map |
| `src/app.ts`, `src/main.ts` | wiring, polling, browser entry |
