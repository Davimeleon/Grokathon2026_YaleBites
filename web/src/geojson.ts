import type { EventOut, FoodLabel } from './types';

export type PinClass = 'yes' | 'likely' | 'other';

export interface PinProperties {
  id: string;
  title: string;
  pin: PinClass;
}

export interface PinFeature {
  type: 'Feature';
  id: number;
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: PinProperties;
}

export interface PinCollection {
  type: 'FeatureCollection';
  features: PinFeature[];
}

export const PIN_COLORS: Record<PinClass, string> = {
  yes: '#15803d',
  likely: '#d97706',
  other: '#6b7280',
};

export function pinClass(label: FoodLabel): PinClass {
  return label === 'yes' || label === 'likely' ? label : 'other';
}

/** One point per event with a pin; events without lat/lng are list-only. */
export function toGeoJSON(events: readonly EventOut[]): PinCollection {
  const features: PinFeature[] = [];
  events.forEach((event, index) => {
    const { lat, lng } = event.location;
    if (lat === null || lng === null) return;
    features.push({
      type: 'Feature',
      id: index,
      geometry: { type: 'Point', coordinates: [lng, lat] },
      properties: { id: event.id, title: event.title, pin: pinClass(event.food.label) },
    });
  });
  return { type: 'FeatureCollection', features };
}

export function unpinnedCount(events: readonly EventOut[]): number {
  return events.filter((e) => e.location.lat === null || e.location.lng === null).length;
}
