import type { EventOut, Position } from './types';

const EARTH_RADIUS_M = 6_371_000;
export const DETOUR_FACTOR = 1.3;
export const WALK_M_PER_MIN = 80;

const toRad = (deg: number): number => (deg * Math.PI) / 180;

/** Great-circle distance in metres. */
export function haversineM(a: Position, b: Position): number {
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Estimated walking minutes: straight-line distance x detour factor at 80 m/min, at least 1. */
export function walkMinutes(from: Position, to: Position): number {
  return Math.max(1, Math.round((haversineM(from, to) * DETOUR_FACTOR) / WALK_M_PER_MIN));
}

export function eventPosition(event: EventOut): Position | null {
  const { lat, lng } = event.location;
  return lat === null || lng === null ? null : { lat, lng };
}

/** Walking minutes from `origin` to the event's pin, or null when either is unknown. */
export function eventWalkMinutes(event: EventOut, origin: Position | null): number | null {
  const pos = eventPosition(event);
  return origin && pos ? walkMinutes(origin, pos) : null;
}

function byStart(a: EventOut, b: EventOut): number {
  return Date.parse(a.starts_at) - Date.parse(b.starts_at) || b.food.score - a.food.score || a.title.localeCompare(b.title);
}

/**
 * Sort for display: by walking distance when `origin` is known (unpinned events last, ties by
 * start), otherwise by start time. Returns a new array.
 */
export function sortEvents(events: readonly EventOut[], origin: Position | null): EventOut[] {
  if (!origin) return [...events].sort(byStart);
  const dist = new Map(events.map((e) => [e.id, eventPosition(e) ? haversineM(origin, eventPosition(e)!) : null]));
  return [...events].sort((a, b) => {
    const da = dist.get(a.id) ?? null;
    const db = dist.get(b.id) ?? null;
    if (da === null && db === null) return byStart(a, b);
    if (da === null) return 1;
    if (db === null) return -1;
    return da - db || byStart(a, b);
  });
}
