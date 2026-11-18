import { describe, expect, it } from 'vitest';
import { ApiError, fetchEvents, fetchMeta, loadEvents, type FetchLike } from '../src/api';
import { browserStore, cacheKey, MAX_CACHE_AGE_MS, readCache, writeCache } from '../src/cache';
import { GENERATED_AT_MS, jsonResponse, memoryStore, sampleEvents, sampleMeta } from './helpers';

const QUERY = 'window=2h&food=yes%2Clikely&page_size=200';

describe('offline cache', () => {
  it('round-trips a payload keyed by query', () => {
    const store = memoryStore();
    writeCache(store, QUERY, sampleEvents(), 1_000);
    expect([...store.data.keys()]).toEqual([cacheKey(QUERY)]);
    expect(readCache(store, QUERY, 2_000)?.payload.total).toBe(4);
    expect(readCache(store, 'window=week', 2_000)).toBeNull();
  });
  it('drops entries older than the max age and corrupt entries', () => {
    const store = memoryStore();
    writeCache(store, QUERY, sampleEvents(), 0);
    expect(readCache(store, QUERY, MAX_CACHE_AGE_MS)).not.toBeNull();
    expect(readCache(store, QUERY, MAX_CACHE_AGE_MS + 1)).toBeNull();
    store.data.set(cacheKey(QUERY), '{not json');
    expect(readCache(store, QUERY, 0)).toBeNull();
    store.data.set(cacheKey(QUERY), JSON.stringify({ savedAt: 0, payload: { items: 'nope' } }));
    expect(readCache(store, QUERY, 0)).toBeNull();
  });
  it('tolerates missing or throwing storage', () => {
    const broken = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('quota');
      },
    };
    expect(() => writeCache(broken, QUERY, sampleEvents(), 0)).not.toThrow();
    expect(readCache(broken, QUERY, 0)).toBeNull();
    expect(readCache(null, QUERY, 0)).toBeNull();
    expect(browserStore()).toBeNull(); // node test environment has no localStorage
  });
});

describe('loadEvents', () => {
  const ok: FetchLike = async () => jsonResponse(sampleEvents());
  const down: FetchLike = async () => {
    throw new TypeError('Failed to fetch');
  };
  const failing: FetchLike = async () => jsonResponse({ detail: 'boom' }, 500);

  it('returns fresh data, caches it and measures the clock offset', async () => {
    const store = memoryStore();
    const result = await loadEvents(ok, store, QUERY, () => GENERATED_AT_MS - 90_000);
    expect(result.staleSince).toBeNull();
    expect(result.offsetMs).toBe(90_000);
    expect(readCache(store, QUERY, GENERATED_AT_MS)?.payload.items).toHaveLength(4);
  });
  it('falls back to the cached payload when the API is down or errors', async () => {
    const store = memoryStore();
    await loadEvents(ok, store, QUERY, () => GENERATED_AT_MS);
    for (const broken of [down, failing]) {
      const result = await loadEvents(broken, store, QUERY, () => GENERATED_AT_MS + 60_000);
      expect(result.staleSince).toBe('2026-11-17T23:01:12Z');
      expect(result.offsetMs).toBeNull();
      expect(result.payload.items).toHaveLength(4);
    }
  });
  it('recovers on the next successful fetch', async () => {
    const store = memoryStore();
    await loadEvents(ok, store, QUERY, () => GENERATED_AT_MS);
    expect((await loadEvents(down, store, QUERY, () => GENERATED_AT_MS)).staleSince).not.toBeNull();
    expect((await loadEvents(ok, store, QUERY, () => GENERATED_AT_MS)).staleSince).toBeNull();
  });
  it('rethrows when nothing is cached', async () => {
    await expect(loadEvents(failing, memoryStore(), QUERY, () => 0)).rejects.toMatchObject({ status: 500 });
    await expect(loadEvents(down, memoryStore(), QUERY, () => 0)).rejects.toBeInstanceOf(ApiError);
  });
  it('propagates aborts instead of serving stale data', async () => {
    const store = memoryStore();
    await loadEvents(ok, store, QUERY, () => GENERATED_AT_MS);
    const aborted: FetchLike = async () => {
      throw new DOMException('aborted', 'AbortError');
    };
    await expect(loadEvents(aborted, store, QUERY, () => GENERATED_AT_MS)).rejects.toMatchObject({ name: 'AbortError' });
  });
});

describe('api client', () => {
  it('requests relative /api URLs', async () => {
    const urls: string[] = [];
    const recorder: FetchLike = async (url) => {
      urls.push(url);
      return jsonResponse(url.startsWith('/api/meta') ? sampleMeta() : sampleEvents());
    };
    await fetchEvents(recorder, QUERY);
    const meta = await fetchMeta(recorder);
    expect(urls).toEqual([`/api/events?${QUERY}`, '/api/meta']);
    expect(meta.dataset.kind).toBe('fixture_replay');
  });
});

describe('fetchEvents pagination', () => {
  it('follows next_page and merges every page', async () => {
    const base = sampleEvents();
    const one = base.items[0];
    const urls: string[] = [];
    const paged: FetchLike = async (url) => {
      urls.push(url);
      const page = Number(new URL(url, 'http://x').searchParams.get('page') ?? '1');
      const item = { ...one, id: `e${page}` };
      return jsonResponse({ ...base, page, total: 3, items: [item], next_page: page < 3 ? page + 1 : null });
    };
    const merged = await fetchEvents(paged, 'window=week');
    expect(merged.items.map((e) => e.id)).toEqual(['e1', 'e2', 'e3']);
    expect(merged.next_page).toBeNull();
    expect(urls).toEqual(['/api/events?window=week', '/api/events?window=week&page=2', '/api/events?window=week&page=3']);
  });
});
