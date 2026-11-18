import type { FetchLike } from './api';
import { DETOUR_FACTOR, haversineM, WALK_M_PER_MIN } from './geo';
import type { Position } from './types';

/** Public OSRM instance with a pedestrian profile (FOSSGIS, CORS-enabled). */
export const ROUTER_URL = 'https://routing.openstreetmap.de/routed-foot/route/v1/driving';

export interface WalkingRoute {
  /** [lng, lat] pairs, GeoJSON order. */
  coordinates: [number, number][];
  distanceM: number;
  durationS: number;
  /** False when the router was unreachable and this is a straight-line estimate. */
  routed: boolean;
}

interface OsrmResponse {
  code?: string;
  routes?: { distance: number; duration: number; geometry: { coordinates: [number, number][] } }[];
}

/** Straight line with the same walking estimate the list uses (detour factor, 80 m/min). */
export function straightLineRoute(from: Position, to: Position): WalkingRoute {
  const distanceM = haversineM(from, to) * DETOUR_FACTOR;
  return {
    coordinates: [
      [from.lng, from.lat],
      [to.lng, to.lat],
    ],
    distanceM,
    durationS: (distanceM / WALK_M_PER_MIN) * 60,
    routed: false,
  };
}

/** Walking route along real paths; falls back to a straight-line estimate if routing fails. */
export async function fetchWalkingRoute(
  fetchImpl: FetchLike,
  from: Position,
  to: Position,
  signal?: AbortSignal,
): Promise<WalkingRoute> {
  const url = `${ROUTER_URL}/${from.lng},${from.lat};${to.lng},${to.lat}?overview=full&geometries=geojson`;
  try {
    const res = await fetchImpl(url, { signal });
    if (!res.ok) return straightLineRoute(from, to);
    const body = (await res.json()) as OsrmResponse;
    const route = body.code === 'Ok' ? body.routes?.[0] : undefined;
    if (!route || route.geometry.coordinates.length < 2) return straightLineRoute(from, to);
    return { coordinates: route.geometry.coordinates, distanceM: route.distance, durationS: route.duration, routed: true };
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    return straightLineRoute(from, to);
  }
}

/** "11 min walk · 0.8 km" (or "· 450 m" under a kilometre). */
export function routeSummary(route: WalkingRoute): string {
  const minutes = Math.max(1, Math.round(route.durationS / 60));
  const distance = route.distanceM < 1000 ? `${Math.round(route.distanceM / 10) * 10} m` : `${(route.distanceM / 1000).toFixed(1)} km`;
  return `${minutes} min walk · ${distance}`;
}

/** Google Maps turn-by-turn walking directions (origin optional: Maps then uses the phone's location). */
export function googleMapsUrl(to: Position, from: Position | null): string {
  const params = new URLSearchParams({ api: '1', destination: `${to.lat},${to.lng}`, travelmode: 'walking' });
  if (from) params.set('origin', `${from.lat},${from.lng}`);
  return `https://www.google.com/maps/dir/?${params.toString()}`;
}
