import { formatClock } from '../format';
import type { AppState } from '../store';
import { el } from './dom';

/** Header status line: data age, auth/fixture indicators, and the offline banner. */
export function renderHeader(root: HTMLElement, state: AppState): void {
  const status = el('div', 'status');
  const meta = state.meta;
  const asOf = meta?.data_as_of ?? state.response?.data_as_of ?? null;
  status.append(el('span', 'as-of', asOf ? `data as of ${formatClock(asOf)}` : 'no sync yet'));
  if (meta?.auth.data_includes_authenticated_events) {
    status.append(el('span', 'badge badge-auth', 'includes Yale-login-only events'));
  }
  if (meta?.dataset.kind === 'fixture_replay') {
    status.append(el('span', 'badge badge-fixture', 'fixture replay'));
  }
  root.replaceChildren(status);
  if (state.staleSince) {
    const banner = el('div', 'banner banner-stale', `Offline — showing data from ${formatClock(state.staleSince)}`);
    banner.setAttribute('role', 'status');
    root.append(banner);
  } else if (state.error) {
    const banner = el('div', 'banner banner-error', state.error);
    banner.setAttribute('role', 'alert');
    root.append(banner);
  }
}
