// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FetchLike } from '../src/api';
import { startApp, startPolling } from '../src/app';
import { requestPosition } from '../src/geolocate';
import { createStore, visibleEvents } from '../src/store';
import type { MapView } from '../src/views/map';
import { GENERATED_AT_MS, jsonResponse, memoryStore, sampleEvents, sampleMeta } from './helpers';

const flush = () => new Promise((r) => setTimeout(r, 0));

function fakeGeo(result: { lat: number; lng: number } | 'denied' | 'pending'): Geolocation {
  return {
    getCurrentPosition(success: PositionCallback, error?: PositionErrorCallback | null) {
      if (result === 'pending') return;
      if (result === 'denied') {
        error?.({ code: 1, PERMISSION_DENIED: 1, message: 'denied' } as GeolocationPositionError);
        return;
      }
      setTimeout(() => success({ coords: { latitude: result.lat, longitude: result.lng } } as GeolocationPosition), 0);
    },
  } as unknown as Geolocation;
}

function setup(opts: { fetch?: FetchLike; geo?: Geolocation; storage?: ReturnType<typeof memoryStore> } = {}) {
  document.body.innerHTML = '<div id="h"></div><div id="f"></div><div id="l"></div><div id="m"></div>';
  const urls: string[] = [];
  const fetchImpl: FetchLike =
    opts.fetch ??
    (async (url) => {
      urls.push(url);
      return jsonResponse(url.startsWith('/api/meta') ? sampleMeta() : sampleEvents());
    });
  const updates: { ids: string[]; selectedId: string | null; flyTo: boolean }[] = [];
  const routes: { id: string | null; from: { lat: number; lng: number } | null }[] = [];
  let mapSelect: (id: string) => void = () => undefined;
  let mapPosition: (p: { lat: number; lng: number }) => void = () => undefined;
  const createMap = (
    _root: HTMLElement,
    onSelect: (id: string) => void,
    onPosition: (p: { lat: number; lng: number }) => void,
  ): MapView => {
    mapSelect = onSelect;
    mapPosition = onPosition;
    return {
      update: (events, selectedId, flyTo) => void updates.push({ ids: events.map((e) => e.id), selectedId, flyTo }),
      showRoute: (event, from) => void routes.push({ id: event?.id ?? null, from }),
    };
  };
  const byId = (id: string) => document.getElementById(id)!;
  const app = startApp(
    { header: byId('h'), filters: byId('f'), list: byId('l'), map: byId('m') },
    {
      fetch: fetchImpl,
      storage: opts.storage ?? memoryStore(),
      geolocation: opts.geo ?? fakeGeo('pending'),
      now: () => GENERATED_AT_MS - 30_000,
      createMap,
    },
  );
  const titles = () => [...document.querySelectorAll('#l .title')].map((n) => n.textContent);
  return {
    app,
    urls,
    updates,
    routes,
    titles,
    mapSelect: (id: string) => mapSelect(id),
    mapPosition: (p: { lat: number; lng: number }) => mapPosition(p),
  };
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe('startApp', () => {
  it('loads the default query and renders list, map and header from one result set', async () => {
    const { urls, updates, titles } = setup();
    expect(document.querySelector('#l [role=status]')?.textContent).toBe('Loading events\u2026');
    await flush();
    expect(urls).toContain('/api/events?window=2h&food=yes%2Clikely&page_size=200');
    expect(urls).toContain('/api/meta');
    expect(titles()).toEqual(['Physics Colloquium', 'Queer Tables: Queer Italian Pasta', 'CSSA Hot Pot Night', 'Thesis Writing Group']);
    const last = updates.at(-1)!;
    expect(last.ids).toHaveLength(4);
    expect(document.getElementById('h')!.textContent).toContain('data as of 17:58');
    // relative time uses server time (generated_at), not the device clock that is 30 s behind
    expect(document.querySelector('#l .when')?.textContent).toBe('started 16 min ago');
  });

  it('syncs selection both ways', async () => {
    const { app, updates, mapSelect } = setup();
    await flush();
    (document.querySelectorAll('#l button.card-main')[1] as HTMLButtonElement).click();
    expect(updates.at(-1)).toMatchObject({ selectedId: 'ycal-2337199@campusgroups.com', flyTo: true });
    mapSelect('ycal-2340002@campusgroups.com');
    expect(updates.at(-1)).toMatchObject({ selectedId: 'ycal-2340002@campusgroups.com', flyTo: false });
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    expect(document.querySelector('#l li.selected')?.getAttribute('data-id')).toBe('ycal-2340002@campusgroups.com');
    expect(app.store.get().selectionSource).toBe('map');
  });

  it('refetches when a filter changes', async () => {
    const { urls } = setup();
    await flush();
    [...document.querySelectorAll('#f button')].find((b) => b.textContent === 'This week')!.dispatchEvent(new Event('click'));
    await flush();
    expect(urls.at(-1)).toBe('/api/events?window=week&food=yes%2Clikely&page_size=200');
  });

  it('re-sorts by walking distance when a position arrives', async () => {
    const { titles } = setup({ geo: fakeGeo({ lat: 41.3083, lng: -72.9289 }) });
    await flush();
    await flush();
    expect(titles()[0]).toBe('Thesis Writing Group');
    expect(document.querySelector('#l .walk')?.textContent).toMatch(/min walk/);
  });

  it('switches between free food and all events', async () => {
    const { urls } = setup();
    await flush();
    const click = (label: string) =>
      [...document.querySelectorAll('#f button')].find((b) => b.textContent === label)!.dispatchEvent(new Event('click'));
    click('All events');
    await flush();
    expect(urls.at(-1)).toBe('/api/events?window=2h&page_size=200');
    click('Free food');
    click('Confirmed');
    await flush();
    expect(urls.at(-1)).toBe('/api/events?window=2h&food=yes&page_size=200');
  });

  it('takes the position from the map locate control and draws the walking route for the selection', async () => {
    const { app, routes, mapPosition } = setup();
    await flush();
    app.store.set({ selectedId: 'ycal-2337199@campusgroups.com', selectionSource: 'list' });
    expect(routes.at(-1)).toEqual({ id: 'ycal-2337199@campusgroups.com', from: null });
    mapPosition({ lat: 41.3108, lng: -72.9282 });
    expect(app.store.get().position).toEqual({ lat: 41.3108, lng: -72.9282 });
    expect(routes.at(-1)).toEqual({ id: 'ycal-2337199@campusgroups.com', from: { lat: 41.3108, lng: -72.9282 } });
    app.store.set({ selectedId: null });
    expect(routes.at(-1)).toEqual({ id: null, from: { lat: 41.3108, lng: -72.9282 } });
  });

  it('shows cached data with a stale banner when the API is down, then recovers', async () => {
    const storage = memoryStore();
    let up = true;
    const flaky: FetchLike = async (url) => {
      if (!up) throw new TypeError('Failed to fetch');
      return jsonResponse(url.startsWith('/api/meta') ? sampleMeta() : sampleEvents());
    };
    const { app, titles } = setup({ fetch: flaky, storage });
    await flush();
    up = false;
    await app.refresh();
    expect(document.getElementById('h')!.textContent).toContain('Offline — showing data from 18:01');
    expect(titles()).toHaveLength(4);
    up = true;
    await app.refresh();
    expect(document.getElementById('h')!.textContent).not.toContain('Offline');
  });

  it('does not show the previous filter results when a new filter cannot load', async () => {
    let up = true;
    const flaky: FetchLike = async (url) => {
      if (!up) throw new TypeError('Failed to fetch');
      return jsonResponse(url.startsWith('/api/meta') ? sampleMeta() : sampleEvents());
    };
    const { titles } = setup({ fetch: flaky, storage: memoryStore() });
    await flush();
    expect(titles()).toHaveLength(4);
    up = false;
    [...document.querySelectorAll('#f button')].find((b) => b.textContent === 'This week')!.dispatchEvent(new Event('click'));
    await flush();
    expect(titles()).toHaveLength(0);
    expect(document.querySelector('#l')?.textContent).toBe('No data to show yet.');
  });

  it('keeps showing the current results when a poll fails without storage', async () => {
    let up = true;
    const flaky: FetchLike = async (url) => {
      if (!up) throw new TypeError('Failed to fetch');
      return jsonResponse(url.startsWith('/api/meta') ? sampleMeta() : sampleEvents());
    };
    const { app, titles } = setup({ fetch: flaky, storage: { getItem: () => null, setItem: () => undefined } as never });
    await flush();
    up = false;
    await app.refresh();
    expect(titles()).toHaveLength(4);
    expect(document.querySelector('#h [role=alert]')).not.toBeNull();
  });

  it('reports an error when the API is down and nothing is cached', async () => {
    const down: FetchLike = async () => jsonResponse({ detail: 'x' }, 503);
    setup({ fetch: down });
    await flush();
    expect(document.querySelector('#h [role=alert]')?.textContent).toContain('Cannot reach the server');
    expect(document.querySelector('#l')?.textContent).toBe('No data to show yet.');
  });
});

describe('startPolling', () => {
  it('polls only while visible and refreshes when the page becomes visible', () => {
    vi.useFakeTimers();
    const refresh = vi.fn(async () => undefined);
    let state: DocumentVisibilityState = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
    const stop = startPolling(document, refresh, 60_000);
    vi.advanceTimersByTime(60_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    state = 'hidden';
    vi.advanceTimersByTime(120_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    state = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    expect(refresh).toHaveBeenCalledTimes(2);
    stop();
    vi.advanceTimersByTime(600_000);
    expect(refresh).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});

describe('helpers', () => {
  it('requestPosition reports missing geolocation', () => {
    const onError = vi.fn();
    requestPosition(undefined, vi.fn(), onError);
    expect(onError).toHaveBeenCalledWith('Location is not available in this browser');
  });
  it('visibleEvents is empty before the first response', () => {
    expect(visibleEvents(createStore().get())).toEqual([]);
  });
});
