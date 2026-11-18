import { DEFAULT_FILTERS, type Filters, type ShowMode } from '../query';
import type { WindowName } from '../types';
import { el } from './dom';

const WHEN: [WindowName, string][] = [
  ['now', 'Now'],
  ['2h', 'Next 2 hrs'],
  ['today', 'Today'],
  ['week', 'This week'],
];
const SHOW: [ShowMode, string][] = [
  ['food', 'Free food'],
  ['all', 'All events'],
];

export interface FilterBarHandlers {
  onChange(patch: Partial<Filters>): void;
}

interface Control {
  button: HTMLButtonElement;
  pressed(filters: Filters): boolean;
  visible?(filters: Filters): boolean;
}

/**
 * Filter bar: a search box, a "When" segmented control, and a "Show" segmented control with one
 * context option (confirmed food only / free entry only). The sort lives in the list header.
 * Controls are built once so focus survives re-renders; `render` syncs pressed state.
 */
export function createFilterBar(root: HTMLElement, handlers: FilterBarHandlers, debounceMs = 300) {
  let current: Filters = DEFAULT_FILTERS;
  const controls: Control[] = [];

  function button(label: string, className: string, onClick: () => Partial<Filters>, control: Omit<Control, 'button'>) {
    const b = el('button', className, label);
    b.type = 'button';
    b.addEventListener('click', () => handlers.onChange(onClick()));
    controls.push({ button: b, ...control });
    return b;
  }

  function segmented<T extends string>(label: string, options: [T, string][], key: 'window' | 'show') {
    const group = el('div', `segmented segmented-${key}`);
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', label);
    for (const [value, text] of options) {
      group.append(
        button(text, 'seg', () => ({ [key]: value }) as Partial<Filters>, {
          pressed: (f) => f[key] === value,
        }),
      );
    }
    return group;
  }

  const search = el('input', 'search');
  search.type = 'search';
  search.placeholder = 'Search pizza, clubs, buildings…';
  search.setAttribute('aria-label', 'Search events');
  search.enterKeyHint = 'search';
  let timer: ReturnType<typeof setTimeout> | undefined;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => handlers.onChange({ q: search.value }), debounceMs);
  });

  const confirmed = button('Confirmed', 'chip', () => ({ confirmedOnly: !current.confirmedOnly }), {
    pressed: (f) => f.confirmedOnly,
    visible: (f) => f.show === 'food',
  });
  confirmed.title = 'Only events where the organizer or listing confirms food';
  confirmed.setAttribute('aria-label', 'Confirmed food only');
  const freeEntry = button('Free entry', 'chip', () => ({ freeEntryOnly: !current.freeEntryOnly }), {
    pressed: (f) => f.freeEntryOnly,
    visible: (f) => f.show === 'all',
  });
  freeEntry.setAttribute('aria-label', 'Free entry only');

  const when = segmented('When', WHEN, 'window');
  const show = segmented('Show', SHOW, 'show');

  const rowWhen = el('div', 'filter-row');
  rowWhen.append(when);
  const rowShow = el('div', 'filter-row filter-row-scroll');
  rowShow.append(show, confirmed, freeEntry);
  root.replaceChildren(search, rowWhen, rowShow);

  function render(filters: Filters): void {
    current = filters;
    if (document.activeElement !== search && search.value !== filters.q) search.value = filters.q;
    for (const c of controls) {
      c.button.setAttribute('aria-pressed', String(c.pressed(filters)));
      c.button.hidden = c.visible ? !c.visible(filters) : false;
    }
  }

  render(current);
  return { render, search };
}
