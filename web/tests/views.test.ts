// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_FILTERS } from '../src/query';
import { initialState } from '../src/store';
import { createFilterBar } from '../src/views/filters';
import { renderHeader } from '../src/views/header';
import { renderList } from '../src/views/list';
import { GENERATED_AT_MS, sampleEvents, sampleMeta } from './helpers';

afterEach(() => vi.useRealTimers());

describe('renderList', () => {
  it('renders one card per event with time, place, quote, badge and source link', () => {
    const root = document.createElement('div');
    const onSelect = vi.fn();
    const items = sampleEvents().items;
    renderList(root, items, { nowMs: GENERATED_AT_MS, position: null, selectedId: null, onSelect });
    const cards = root.querySelectorAll('li.card');
    expect(cards).toHaveLength(4);
    const pasta = cards[0];
    expect(pasta.querySelector('.title')?.textContent).toBe('Queer Tables: Queer Italian Pasta');
    expect(pasta.querySelector('.host')?.textContent).toBe('Yale LGBTQ Center');
    expect(pasta.querySelector('.when')?.textContent).toBe('in 29 min');
    expect(pasta.querySelector('.evidence')?.textContent).toBe('“Pizza will be served”');
    expect(pasta.querySelector('.food-badge')?.className).toContain('food-yes');
    expect(pasta.querySelector('a')?.getAttribute('href')).toBe('https://yaleconnect.yale.edu/rsvp?id=2337199');
    expect(cards[2].querySelector('.where')?.textContent).toBe('Location requires Yale sign-in');
    (pasta.querySelector('button.card-main') as HTMLButtonElement).click();
    expect(onSelect).toHaveBeenCalledWith('ycal-2337199@campusgroups.com');
  });
  it('marks the selected card, shows walking time and returns the selected node', () => {
    const root = document.createElement('div');
    const selected = renderList(root, sampleEvents().items, {
      nowMs: GENERATED_AT_MS,
      position: { lat: 41.3083, lng: -72.9289 },
      selectedId: 'ycal-2338001@campusgroups.com',
      onSelect: vi.fn(),
    });
    expect(selected?.dataset.id).toBe('ycal-2338001@campusgroups.com');
    expect(selected?.querySelector('button')?.getAttribute('aria-pressed')).toBe('true');
    expect(selected?.querySelector('.walk')?.textContent).toMatch(/~\d+ min walk/);
  });
  it('shows an empty state', () => {
    const root = document.createElement('div');
    expect(renderList(root, [], { nowMs: 0, position: null, selectedId: null, onSelect: vi.fn() })).toBeNull();
    expect(root.textContent).toContain('No matching events');
  });
});

describe('renderHeader', () => {
  it('shows data age and the auth and fixture indicators', () => {
    const root = document.createElement('div');
    renderHeader(root, { ...initialState(), meta: sampleMeta() });
    expect(root.textContent).toContain('data as of 17:58');
    expect(root.textContent).toContain('includes Yale-login-only events');
    expect(root.textContent).toContain('fixture replay');
  });
  it('hides indicators for live anonymous data and shows the stale banner', () => {
    const meta = sampleMeta();
    meta.auth.data_includes_authenticated_events = false;
    meta.dataset.kind = 'live';
    const root = document.createElement('div');
    renderHeader(root, { ...initialState(), meta, staleSince: '2026-11-17T23:42:00Z' });
    expect(root.textContent).not.toContain('fixture replay');
    expect(root.textContent).not.toContain('Yale-login-only');
    expect(root.querySelector('[role=status]')?.textContent).toBe('Offline — showing data from 18:42');
  });
  it('shows an error when there is nothing to show', () => {
    const root = document.createElement('div');
    renderHeader(root, { ...initialState(), error: 'Cannot reach the server' });
    expect(root.textContent).toContain('no sync yet');
    expect(root.querySelector('[role=alert]')?.textContent).toBe('Cannot reach the server');
  });
});

describe('filter bar', () => {
  function setup() {
    const root = document.createElement('div');
    const onChange = vi.fn();
    const bar = createFilterBar(root, { onChange }, 300);
    bar.render(DEFAULT_FILTERS);
    const button = (label: string) =>
      [...root.querySelectorAll('button')].find((b) => b.textContent === label) as HTMLButtonElement;
    return { root, onChange, bar, button };
  }

  it('reflects state with aria-pressed and shows the option that fits the mode', () => {
    const { button, bar } = setup();
    expect(button('Next 2 hrs').getAttribute('aria-pressed')).toBe('true');
    expect(button('Now').getAttribute('aria-pressed')).toBe('false');
    expect(button('Free food').getAttribute('aria-pressed')).toBe('true');
    expect(button('All events').getAttribute('aria-pressed')).toBe('false');
    expect(button('Confirmed').hidden).toBe(false);
    expect(button('Free entry').hidden).toBe(true);
    bar.render({ ...DEFAULT_FILTERS, show: 'all' });
    expect(button('Confirmed').hidden).toBe(true);
    expect(button('Free entry').hidden).toBe(false);
  });
  it('emits patches for each control', () => {
    const { button, onChange } = setup();
    button('This week').click();
    button('All events').click();
    button('Free food').click();
    button('Confirmed').click();
    button('Free entry').click();
    expect(onChange.mock.calls.map((c) => c[0])).toEqual([
      { window: 'week' },
      { show: 'all' },
      { show: 'food' },
      { confirmedOnly: true },
      { freeEntryOnly: true },
    ]);
  });
  it('debounces the search box', () => {
    vi.useFakeTimers();
    const { bar, onChange } = setup();
    bar.search.value = 'piz';
    bar.search.dispatchEvent(new Event('input'));
    bar.search.value = 'pizza';
    bar.search.dispatchEvent(new Event('input'));
    vi.advanceTimersByTime(299);
    expect(onChange).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onChange.mock.calls).toEqual([[{ q: 'pizza' }]]);
  });
});
