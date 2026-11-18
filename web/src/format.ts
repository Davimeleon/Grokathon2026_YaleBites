import type { EventOut } from './types';

export const CAMPUS_TZ = 'America/New_York';
const MINUTE_MS = 60_000;

const timeFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: CAMPUS_TZ,
  hour: 'numeric',
  minute: '2-digit',
});
const clockFmt = new Intl.DateTimeFormat('en-GB', {
  timeZone: CAMPUS_TZ,
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});
const dayKeyFmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: CAMPUS_TZ,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});
const weekdayFmt = new Intl.DateTimeFormat('en-US', { timeZone: CAMPUS_TZ, weekday: 'short' });
const dateFmt = new Intl.DateTimeFormat('en-US', { timeZone: CAMPUS_TZ, month: 'short', day: 'numeric' });

/** Whole days between the campus-local calendar dates of `a` and `b` (b - a). */
function campusDayDiff(a: Date, b: Date): number {
  const toUtcMidnight = (d: Date): number => Date.parse(`${dayKeyFmt.format(d)}T00:00:00Z`);
  return Math.round((toUtcMidnight(b) - toUtcMidnight(a)) / (24 * 60 * MINUTE_MS));
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

function dayLabel(start: Date, now: Date): string {
  const days = campusDayDiff(now, start);
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  if (days > 1 && days < 7) return weekdayFmt.format(start);
  return dateFmt.format(start);
}

/**
 * Human start time relative to `nowMs` (server-corrected): "in 25 min", "started 10 min ago",
 * "today 6:30 PM", "tomorrow 6:30 PM". Events within two hours get a countdown.
 */
export function formatRelativeStart(startsAt: string, nowMs: number, allDay = false): string {
  const start = new Date(startsAt);
  const now = new Date(nowMs);
  if (allDay) return `${dayLabel(start, now)} (all day)`;
  const minutes = Math.round((start.getTime() - nowMs) / MINUTE_MS);
  if (minutes <= -24 * 60) return `started ${dateFmt.format(start)}`;
  if (minutes < 0) return `started ${formatDuration(-minutes)} ago`;
  if (minutes === 0) return 'starting now';
  if (minutes < 120) return `in ${formatDuration(minutes)}`;
  return `${dayLabel(start, now)} ${timeFmt.format(start)}`;
}

/** "HH:MM" in campus time (24 h), used for "data as of" and the offline banner. */
export function formatClock(iso: string): string {
  return clockFmt.format(new Date(iso));
}

export function formatWalk(minutes: number): string {
  return `~${minutes} min walk`;
}

/** Location line for a card, following the precedence in the UI spec. */
export function locationLabel(event: EventOut): string {
  const loc = event.location;
  if (loc.building_name) return loc.building_name;
  if (loc.raw) return loc.raw;
  if (loc.requires_sign_in) return 'Location requires Yale sign-in';
  return 'Location not listed';
}

/** The first food evidence span as a short curly-quoted string, or null. */
export function evidenceQuote(event: EventOut, maxLength = 90): string | null {
  const text = event.food.evidence[0]?.text.trim();
  if (!text) return null;
  const clipped = text.length > maxLength ? `${text.slice(0, maxLength - 1).trimEnd()}…` : text;
  return `“${clipped}”`;
}

/** Offset (ms) to add to the device clock to get server time. */
export function clockOffset(serverIso: string, deviceNowMs: number): number {
  const server = Date.parse(serverIso);
  return Number.isNaN(server) ? 0 : server - deviceNowMs;
}
