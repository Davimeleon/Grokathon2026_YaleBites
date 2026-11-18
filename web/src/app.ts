import { fetchMeta, loadEvents, type FetchLike } from './api';
import type { KeyValueStore } from './cache';
import { requestPosition } from './geolocate';
import { buildQuery, type Filters } from './query';
import { createStore, visibleEvents, type Store } from './store';
import { createFilterBar } from './views/filters';
import { renderHeader } from './views/header';
import { renderList, renderMessage } from './views/list';
import type { Position } from './types';
import type { MapView } from './views/map';

export const POLL_MS = 60_000;

export interface AppDeps {
  fetch: FetchLike;
  storage: KeyValueStore | null;
  geolocation: Geolocation | undefined;
  now: () => number;
  createMap: (root: HTMLElement, onSelect: (id: string) => void, onPosition: (position: Position) => void) => MapView;
}

export interface AppRoots {
  header: HTMLElement;
  filters: HTMLElement;
  list: HTMLElement;
  map: HTMLElement;
}

/**
 * Wire the store, data loading and the views together. Returns the store and a `refresh`
 * function (also used by the 60 s poll) so tests can drive it.
 */
export function startApp(roots: AppRoots, deps: AppDeps): { store: Store; refresh: () => Promise<void> } {
  const store = createStore();
  let inflight: AbortController | null = null;

  async function refreshEvents(): Promise<void> {
    const { filters } = store.get();
    inflight?.abort();
    const controller = new AbortController();
    inflight = controller;
    const query = buildQuery(filters);
    store.set({ loading: true });
    try {
      const result = await loadEvents(deps.fetch, deps.storage, query, deps.now, controller.signal);
      store.set({
        response: result.payload,
        responseQuery: query,
        staleSince: result.staleSince,
        offsetMs: result.offsetMs ?? store.get().offsetMs,
        error: null,
        loading: false,
      });
    } catch {
      if (controller.signal.aborted) return;
      const keep = store.get().responseQuery === query;
      store.set({
        error: 'Cannot reach the server and no saved data for these filters.',
        loading: false,
        ...(keep ? {} : { response: null, responseQuery: null, staleSince: null }),
      });
    }
  }

  async function refreshMeta(): Promise<void> {
    try {
      store.set({ meta: await fetchMeta(deps.fetch) });
    } catch {
      // the events banner already reports connectivity problems
    }
  }

  const refresh = async (): Promise<void> => {
    await Promise.all([refreshEvents(), refreshMeta()]);
  };

  const select = (source: 'list' | 'map') => (id: string) =>
    store.set({ selectedId: store.get().selectedId === id && source === 'list' ? null : id, selectionSource: source });

  const mapView = deps.createMap(roots.map, select('map'), (position) => {
    const prev = store.get().position;
    if (!prev || prev.lat !== position.lat || prev.lng !== position.lng) store.set({ position });
  });
  const filterBar = createFilterBar(roots.filters, {
    onChange(patch: Partial<Filters>) {
      const filters = { ...store.get().filters, ...patch };
      store.set({ filters, selectedId: null, selectionSource: null });
      void refreshEvents();
    },
  });

  store.subscribe((state, prev) => {
    renderHeader(roots.header, state);
    filterBar.render(state.filters);
    const events = visibleEvents(state);
    const selected = state.response
      ? renderList(roots.list, events, {
          nowMs: deps.now() + state.offsetMs,
          position: state.position,
          selectedId: state.selectedId,
          onSelect: select('list'),
        })
      : null;
    if (!state.response) renderMessage(roots.list, state.loading ? 'Loading events\u2026' : 'No data to show yet.');
    const selectionChanged = state.selectedId !== prev.selectedId;
    if (selected && selectionChanged && state.selectionSource === 'map') {
      selected.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    mapView.update(events, state.selectedId, selectionChanged && state.selectionSource === 'list');
    if (selectionChanged || (state.selectedId && state.position !== prev.position)) {
      mapView.showRoute?.(events.find((e) => e.id === state.selectedId) ?? null, state.position);
    }
  });

  store.set({});
  requestPosition(deps.geolocation, (position) => store.set({ position }));
  void refresh();
  return { store, refresh };
}

/** Poll every `intervalMs` while the page is visible; refresh immediately when it becomes visible. */
export function startPolling(doc: Document, refresh: () => Promise<void>, intervalMs = POLL_MS): () => void {
  const timer = setInterval(() => {
    if (doc.visibilityState === 'visible') void refresh();
  }, intervalMs);
  const onVisible = () => {
    if (doc.visibilityState === 'visible') void refresh();
  };
  doc.addEventListener('visibilitychange', onVisible);
  return () => {
    clearInterval(timer);
    doc.removeEventListener('visibilitychange', onVisible);
  };
}
