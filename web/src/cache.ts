import type { EventsResponse } from './types';

/** Minimal Storage surface so tests can pass an in-memory map. */
export type KeyValueStore = Pick<Storage, 'getItem' | 'setItem'>;

export interface CacheEntry {
  savedAt: number; // device epoch ms when the payload was stored
  payload: EventsResponse;
}

const PREFIX = 'freefood:events:';
/** Older cached payloads are not shown at all: day-old "free pizza" is worse than nothing. */
export const MAX_CACHE_AGE_MS = 24 * 60 * 60 * 1000;

export const cacheKey = (query: string): string => PREFIX + query;

/** Store a successful payload. Storage failures (quota, private mode) are ignored. */
export function writeCache(store: KeyValueStore | null, query: string, payload: EventsResponse, nowMs: number): void {
  if (!store) return;
  const entry: CacheEntry = { savedAt: nowMs, payload };
  try {
    store.setItem(cacheKey(query), JSON.stringify(entry));
  } catch {
    // storage is a best-effort convenience
  }
}

function isEntry(value: unknown): value is CacheEntry {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Partial<CacheEntry>;
  return typeof v.savedAt === 'number' && typeof v.payload === 'object' && v.payload !== null && Array.isArray(v.payload.items);
}

/** Read the last payload for `query`, or null when missing, corrupt or older than `maxAgeMs`. */
export function readCache(
  store: KeyValueStore | null,
  query: string,
  nowMs: number,
  maxAgeMs: number = MAX_CACHE_AGE_MS,
): CacheEntry | null {
  if (!store) return null;
  let raw: string | null;
  try {
    raw = store.getItem(cacheKey(query));
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isEntry(parsed) || nowMs - parsed.savedAt > maxAgeMs) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** localStorage when the browser allows it, else null. */
export function browserStore(): KeyValueStore | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}
