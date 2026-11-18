import { describe, expect, it } from 'vitest';
import { fetchWalkingRoute, googleMapsUrl, ROUTER_URL, routeSummary, straightLineRoute } from '../src/route';

const from = { lat: 41.3108, lng: -72.9282 };
const to = { lat: 41.3133, lng: -72.9254 };

describe('fetchWalkingRoute', () => {
  it('uses the pedestrian router and returns its path', async () => {
    const urls: string[] = [];
    const route = await fetchWalkingRoute(async (url) => {
      urls.push(url);
      return new Response(JSON.stringify({ code: 'Ok', routes: [{ distance: 810, duration: 640, geometry: { coordinates: [[1, 2], [3, 4]] } }] }));
    }, from, to);
    expect(urls[0]).toBe(`${ROUTER_URL}/-72.9282,41.3108;-72.9254,41.3133?overview=full&geometries=geojson`);
    expect(route).toEqual({ coordinates: [[1, 2], [3, 4]], distanceM: 810, durationS: 640, routed: true });
  });
  it('falls back to a straight-line estimate when the router fails', async () => {
    const down = await fetchWalkingRoute(async () => { throw new TypeError('offline'); }, from, to);
    expect(down.routed).toBe(false);
    expect(down.coordinates).toEqual([[from.lng, from.lat], [to.lng, to.lat]]);
    const noRoute = await fetchWalkingRoute(async () => new Response(JSON.stringify({ code: 'NoRoute' })), from, to);
    expect(noRoute).toEqual(straightLineRoute(from, to));
  });
});

describe('route helpers', () => {
  it('summarises minutes and distance', () => {
    expect(routeSummary({ coordinates: [], distanceM: 812, durationS: 640, routed: true })).toBe('11 min walk · 810 m');
    expect(routeSummary({ coordinates: [], distanceM: 1530, durationS: 1200, routed: true })).toBe('20 min walk · 1.5 km');
  });
  it('builds Google Maps walking directions, with or without an origin', () => {
    expect(googleMapsUrl(to, from)).toBe(
      'https://www.google.com/maps/dir/?api=1&destination=41.3133%2C-72.9254&travelmode=walking&origin=41.3108%2C-72.9282',
    );
    expect(googleMapsUrl(to, null)).not.toContain('origin');
  });
});
