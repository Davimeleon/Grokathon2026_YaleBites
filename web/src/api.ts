import { clockOffset } from './format';
import { readCache, writeCache, type KeyValueStore } from './cache';
import type { EventsResponse, MetaOut } from './types';

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function getJson<T>(fetchImpl: FetchLike, url: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetchImpl(url, { headers: { Accept: 'application/json' }, signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError('network error', null);
  }
  if (!res.ok) throw new ApiError(`HTTP ${res.status}`, res.status);
  return (await res.json()) as T;
}

/** Upper bound on pages followed per query (200 events each), so a runaway result set cannot hang the UI. */
export const MAX_PAGES = 10;

/** Fetch every page of a query (following next_page) and merge the items into one response. */
export async function fetchEvents(fetchImpl: FetchLike, query: string, signal?: AbortSignal): Promise<EventsResponse> {
  const first = await getJson<EventsResponse>(fetchImpl, `/api/events?${query}`, signal);
  let items = first.items;
  let next = first.next_page;
  for (let pages = 1; next !== null && pages < MAX_PAGES; pages += 1) {
    const page = await getJson<EventsResponse>(fetchImpl, `/api/events?${query}&page=${next}`, signal);
    items = items.concat(page.items);
    next = page.next_page;
  }
  return { ...first, items, next_page: next };
}

export const fetchMeta = (fetchImpl: FetchLike, signal?: AbortSignal): Promise<MetaOut> =>
  getJson<MetaOut>(fetchImpl, '/api/meta', signal);

export interface EventsResult {
  payload: EventsResponse;
  /** Null for fresh data; otherwise the cached payload's generated_at (server time). */
  staleSince: string | null;
  /** Device-to-server clock offset in ms, measured on fresh responses. */
  offsetMs: number | null;
}

/**
 * Fetch events; on success cache them, on failure fall back to the last cached payload for the
 * same query. Rethrows when there is nothing cached (or the request was aborted).
 */
export async function loadEvents(
  fetchImpl: FetchLike,
  store: KeyValueStore | null,
  query: string,
  deviceNow: () => number,
  signal?: AbortSignal,
): Promise<EventsResult> {
  try {
    const payload = await fetchEvents(fetchImpl, query, signal);
    const now = deviceNow();
    writeCache(store, query, payload, now);
    return { payload, staleSince: null, offsetMs: clockOffset(payload.generated_at, now) };
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    const cached = readCache(store, query, deviceNow());
    if (!cached) throw err;
    return { payload: cached.payload, staleSince: cached.payload.generated_at, offsetMs: null };
  }
}
