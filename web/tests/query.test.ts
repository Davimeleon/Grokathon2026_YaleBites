import { describe, expect, it } from 'vitest';
import { buildQuery, DEFAULT_FILTERS } from '../src/query';

describe('buildQuery', () => {
  it('defaults to free food (confirmed + likely) in the next two hours', () => {
    expect(buildQuery(DEFAULT_FILTERS)).toBe('window=2h&food=yes%2Clikely&page_size=200');
  });
  it('confirmed only asks for organizer/listing-confirmed food', () => {
    expect(new URLSearchParams(buildQuery({ ...DEFAULT_FILTERS, confirmedOnly: true })).get('food')).toBe('yes');
  });
  it('all events drops the food filter; free entry only applies there', () => {
    const q = buildQuery({ ...DEFAULT_FILTERS, window: 'week', show: 'all', freeEntryOnly: true, q: '  pizza night ' });
    expect(q).toBe('window=week&free_only=true&q=pizza+night&page_size=200');
    expect(buildQuery({ ...DEFAULT_FILTERS, freeEntryOnly: true })).not.toContain('free_only');
    expect(buildQuery({ ...DEFAULT_FILTERS, show: 'all', confirmedOnly: true })).not.toContain('food=');
  });
});
