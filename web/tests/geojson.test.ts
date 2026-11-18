import { describe, expect, it } from 'vitest';
import { pinClass, toGeoJSON, unpinnedCount } from '../src/geojson';
import { sampleEvents } from './helpers';

describe('toGeoJSON', () => {
  it('emits pins only for events with lat/lng, as [lng, lat]', () => {
    const fc = toGeoJSON(sampleEvents().items);
    expect(fc.type).toBe('FeatureCollection');
    expect(fc.features.map((f) => f.properties.id)).toEqual([
      'ycal-2337199@campusgroups.com',
      'ycal-2338001@campusgroups.com',
      'ycal-2340002@campusgroups.com',
    ]);
    expect(fc.features[0].geometry.coordinates).toEqual([-72.9254, 41.3133]);
  });
  it('colour-classes pins by food label', () => {
    const pins = toGeoJSON(sampleEvents().items).features.map((f) => f.properties.pin);
    expect(pins).toEqual(['yes', 'likely', 'other']);
    expect(pinClass('no')).toBe('other');
  });
  it('counts list-only events', () => {
    expect(unpinnedCount(sampleEvents().items)).toBe(1);
    expect(toGeoJSON([]).features).toEqual([]);
  });
});
