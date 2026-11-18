import type { WindowName } from './types';

/** What the list shows: free food (the default) or every event. */
export type ShowMode = 'food' | 'all';

export interface Filters {
  window: WindowName;
  show: ShowMode;
  /** Food mode only: hide "maybe food" events, keep organizer-confirmed / explicit ones. */
  confirmedOnly: boolean;
  /** All-events mode only: hide events that mention a price. */
  freeEntryOnly: boolean;
  q: string;
}

export const DEFAULT_FILTERS: Filters = {
  window: '2h',
  show: 'food',
  confirmedOnly: false,
  freeEntryOnly: false,
  q: '',
};

export const PAGE_SIZE = 200;

/** Build the /api/events query string (no leading "?") from filter state. */
export function buildQuery(filters: Filters): string {
  const params = new URLSearchParams();
  params.set('window', filters.window);
  if (filters.show === 'food') params.set('food', filters.confirmedOnly ? 'yes' : 'yes,likely');
  if (filters.show === 'all' && filters.freeEntryOnly) params.set('free_only', 'true');
  const q = filters.q.trim();
  if (q) params.set('q', q);
  params.set('page_size', String(PAGE_SIZE));
  return params.toString();
}
