import { DEFAULT_FILTERS, type Filters } from './query';
import { sortEvents } from './geo';
import type { EventOut, EventsResponse, MetaOut, Position } from './types';

export type SelectionSource = 'list' | 'map';

export interface AppState {
  filters: Filters;
  position: Position | null;
  response: EventsResponse | null;
  /** The query string `response` answers; used to avoid showing another filter's results. */
  responseQuery: string | null;
  meta: MetaOut | null;
  staleSince: string | null;
  error: string | null;
  loading: boolean;
  offsetMs: number;
  selectedId: string | null;
  selectionSource: SelectionSource | null;
}

export type Listener = (state: AppState, prev: AppState) => void;

export interface Store {
  get(): AppState;
  set(patch: Partial<AppState>): void;
  subscribe(listener: Listener): () => void;
}

export function initialState(): AppState {
  return {
    filters: { ...DEFAULT_FILTERS },
    position: null,
    response: null,
    responseQuery: null,
    meta: null,
    staleSince: null,
    error: null,
    loading: true,
    offsetMs: 0,
    selectedId: null,
    selectionSource: null,
  };
}

/** A tiny observable store: views subscribe and re-render from the whole state. */
export function createStore(state: AppState = initialState()): Store {
  const listeners = new Set<Listener>();
  return {
    get: () => state,
    set(patch) {
      const prev = state;
      state = { ...state, ...patch };
      listeners.forEach((l) => l(state, prev));
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The one result set both views render, in display order. */
export function visibleEvents(state: AppState): EventOut[] {
  return state.response ? sortEvents(state.response.items, state.position) : [];
}
