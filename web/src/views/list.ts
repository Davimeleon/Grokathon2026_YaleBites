import { evidenceQuote, formatRelativeStart, formatWalk, locationLabel } from '../format';
import { eventWalkMinutes } from '../geo';
import type { EventOut, Position } from '../types';
import { el } from './dom';

export interface ListContext {
  nowMs: number;
  position: Position | null;
  selectedId: string | null;
  onSelect(id: string): void;
}

const BADGE_TEXT: Record<string, string> = { yes: 'Free food', likely: 'Likely food', no: 'No food listed' };

function card(event: EventOut, ctx: ListContext): HTMLLIElement {
  const item = el('li', 'card');
  item.dataset.id = event.id;
  const selected = event.id === ctx.selectedId;
  if (selected) item.classList.add('selected');

  const main = el('button', 'card-main');
  main.type = 'button';
  main.setAttribute('aria-pressed', String(selected));
  const top = el('div', 'card-top');
  top.append(el('span', `food-badge food-${event.food.label}`, BADGE_TEXT[event.food.label]));
  top.append(el('span', 'when', formatRelativeStart(event.starts_at, ctx.nowMs, event.all_day)));
  main.append(top, el('h3', 'title', event.title));
  if (event.host_group) main.append(el('p', 'host', event.host_group));
  const where = el('p', 'where', locationLabel(event));
  const walk = eventWalkMinutes(event, ctx.position);
  if (walk !== null) where.append(el('span', 'walk', ` · ${formatWalk(walk)}`));
  main.append(where);
  const quote = evidenceQuote(event);
  if (quote) main.append(el('p', 'evidence', quote));
  main.addEventListener('click', () => ctx.onSelect(event.id));

  const link = el('a', 'source-link', 'Open on Yale Connect');
  link.href = event.source_url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  item.append(main, link);
  return item;
}

/** Replace the list with a single status line (loading, empty, unavailable). */
export function renderMessage(root: HTMLElement, message: string): void {
  const p = el('p', 'empty', message);
  p.setAttribute('role', 'status');
  root.replaceChildren(p);
}

/** Render the card list. Returns the selected card element, if any, so callers can scroll to it. */
export function renderList(root: HTMLElement, events: readonly EventOut[], ctx: ListContext): HTMLElement | null {
  if (events.length === 0) {
    renderMessage(root, 'No matching events right now. Try a wider time window or "All events".');
    return null;
  }
  const list = el('ul', 'cards');
  let selected: HTMLElement | null = null;
  for (const event of events) {
    const node = card(event, ctx);
    if (event.id === ctx.selectedId) selected = node;
    list.append(node);
  }
  root.replaceChildren(list);
  return selected;
}
