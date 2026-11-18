import { describe, expect, it } from 'vitest';
import { eventWalkMinutes, haversineM, sortEvents, walkMinutes } from '../src/geo';
import { sampleEvents } from './helpers';

const OLD_CAMPUS = { lat: 41.3083, lng: -72.9289 };

describe('haversine and walking time', () => {
  it('measures great-circle distance', () => {
    // 0.01 deg of latitude is ~1112 m everywhere.
    expect(haversineM({ lat: 41.3, lng: -72.9 }, { lat: 41.31, lng: -72.9 })).toBeCloseTo(1111.95, 0);
    expect(haversineM(OLD_CAMPUS, OLD_CAMPUS)).toBe(0);
  });
  it('applies the 1.3 detour factor at 80 m/min', () => {
    // 1112 m * 1.3 / 80 = 18.07 -> 18 min
    expect(walkMinutes({ lat: 41.3, lng: -72.9 }, { lat: 41.31, lng: -72.9 })).toBe(18);
    expect(walkMinutes(OLD_CAMPUS, OLD_CAMPUS)).toBe(1);
  });
  it('is null for events without a pin or without an origin', () => {
    const [pasta, , hotpot] = sampleEvents().items;
    expect(eventWalkMinutes(pasta, OLD_CAMPUS)).toBeGreaterThan(0);
    expect(eventWalkMinutes(hotpot, OLD_CAMPUS)).toBeNull();
    expect(eventWalkMinutes(pasta, null)).toBeNull();
  });
});

describe('sortEvents', () => {
  it('sorts by start time without a position', () => {
    const titles = sortEvents(sampleEvents().items, null).map((e) => e.title);
    expect(titles).toEqual(['Physics Colloquium', 'Queer Tables: Queer Italian Pasta', 'CSSA Hot Pot Night', 'Thesis Writing Group']);
  });
  it('sorts by walking distance with a position, unpinned last', () => {
    const titles = sortEvents(sampleEvents().items, OLD_CAMPUS).map((e) => e.title);
    expect(titles).toEqual(['Thesis Writing Group', 'Queer Tables: Queer Italian Pasta', 'Physics Colloquium', 'CSSA Hot Pot Night']);
  });
  it('does not mutate its input', () => {
    const items = sampleEvents().items;
    const before = items.map((e) => e.id);
    sortEvents(items, OLD_CAMPUS);
    expect(items.map((e) => e.id)).toEqual(before);
  });
});
