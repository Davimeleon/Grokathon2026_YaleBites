import maplibregl, { type GeoJSONSource, type MapLayerMouseEvent, type StyleSpecification } from 'maplibre-gl';
import { PIN_COLORS, toGeoJSON, unpinnedCount } from '../geojson';
import type { FetchLike } from '../api';
import { fetchWalkingRoute, googleMapsUrl, routeSummary, type WalkingRoute } from '../route';
import type { EventOut, Position } from '../types';
import { el } from './dom';

export const CAMPUS_CENTER: [number, number] = [-72.9267, 41.3105];
export const CAMPUS_ZOOM = 15;

const BASE_STYLE: StyleSpecification = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    osm: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
};

const SOURCE = 'events';
const ROUTE_SOURCE = 'route';
const ROUTE_COLOR = '#1a73e8';

function addEventLayers(map: maplibregl.Map): void {
  map.addSource(SOURCE, {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
    cluster: true,
    clusterRadius: 40,
    clusterMaxZoom: 16,
  });
  map.addLayer({
    id: 'clusters',
    type: 'circle',
    source: SOURCE,
    filter: ['has', 'point_count'],
    paint: { 'circle-color': '#1e3a8a', 'circle-radius': 18, 'circle-stroke-width': 2, 'circle-stroke-color': '#fff' },
  });
  map.addLayer({
    id: 'cluster-count',
    type: 'symbol',
    source: SOURCE,
    filter: ['has', 'point_count'],
    layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['Noto Sans Regular'], 'text-size': 13 },
    paint: { 'text-color': '#fff' },
  });
  map.addLayer({
    id: 'points-hit',
    type: 'circle',
    source: SOURCE,
    filter: ['!', ['has', 'point_count']],
    paint: { 'circle-radius': 22, 'circle-opacity': 0 },
  });
  map.addLayer({
    id: 'points',
    type: 'circle',
    source: SOURCE,
    filter: ['!', ['has', 'point_count']],
    paint: {
      'circle-color': ['match', ['get', 'pin'], 'yes', PIN_COLORS.yes, 'likely', PIN_COLORS.likely, PIN_COLORS.other],
      'circle-radius': 9,
      'circle-stroke-width': 2,
      'circle-stroke-color': '#fff',
    },
  });
  map.addLayer({
    id: 'points-selected',
    type: 'circle',
    source: SOURCE,
    filter: ['==', ['get', 'id'], ''],
    paint: { 'circle-radius': 14, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-width': 4, 'circle-stroke-color': '#111827' },
  });
}

export interface MapView {
  update(events: readonly EventOut[], selectedId: string | null, flyTo: boolean): void;
  /** Draw the walking route from `from` to the event (or clear it when `event` is null). */
  showRoute?(event: EventOut | null, from: Position | null): void;
}

export interface MapOptions {
  /** Called whenever the map's locate control gets a position fix. */
  onPosition?(position: Position): void;
  fetch?: FetchLike;
}

function addRouteLayers(map: maplibregl.Map): void {
  map.addSource(ROUTE_SOURCE, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer(
    {
      id: 'route-casing',
      type: 'line',
      source: ROUTE_SOURCE,
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#ffffff', 'line-width': 9 },
    },
    'clusters',
  );
  map.addLayer(
    {
      id: 'route-line',
      type: 'line',
      source: ROUTE_SOURCE,
      filter: ['==', ['get', 'routed'], true],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': ROUTE_COLOR, 'line-width': 5 },
    },
    'clusters',
  );
  // Straight-line estimate when the router is unreachable: dashed, so it doesn't pass for a real path.
  map.addLayer(
    {
      id: 'route-estimate',
      type: 'line',
      source: ROUTE_SOURCE,
      filter: ['==', ['get', 'routed'], false],
      layout: { 'line-join': 'round' },
      paint: { 'line-color': ROUTE_COLOR, 'line-width': 4, 'line-dasharray': [2, 2] },
    },
    'clusters',
  );
}

function routeFeature(route: WalkingRoute) {
  return {
    type: 'FeatureCollection' as const,
    features: [
      {
        type: 'Feature' as const,
        properties: { routed: route.routed },
        geometry: { type: 'LineString' as const, coordinates: route.coordinates },
      },
    ],
  };
}

/** Keeps the "N not on map" note current. */
function updateNote(note: HTMLElement, events: readonly EventOut[]): void {
  const missing = unpinnedCount(events);
  note.textContent = missing > 0 ? `${missing} not on map` : '';
  note.hidden = missing === 0;
}

/**
 * MapLibre view: clustered, colour-coded pins; tapping a pin calls `onSelect`. Without WebGL the
 * map is replaced by a notice and the list keeps working.
 */
export function createMapView(root: HTMLElement, onSelect: (id: string) => void, options: MapOptions = {}): MapView {
  const canvas = el('div', 'map-canvas');
  const note = el('div', 'map-note');
  note.setAttribute('aria-live', 'polite');
  const panel = el('div', 'route-panel');
  panel.setAttribute('aria-live', 'polite');
  panel.hidden = true;
  root.replaceChildren(canvas, note, panel);
  const fetchImpl: FetchLike = options.fetch ?? ((input, init) => fetch(input, init));

  let map: maplibregl.Map;
  try {
    map = new maplibregl.Map({ container: canvas, style: BASE_STYLE, center: CAMPUS_CENTER, zoom: CAMPUS_ZOOM });
  } catch {
    canvas.replaceChildren(el('p', 'map-unavailable', 'The map is unavailable on this device. All events are in the list.'));
    return { update: (events) => updateNote(note, events) };
  }
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  const locate = new maplibregl.GeolocateControl({
    positionOptions: { enableHighAccuracy: true, timeout: 15_000, maximumAge: 30_000 },
    trackUserLocation: true,
    showUserLocation: true,
    showAccuracyCircle: false,
    fitBoundsOptions: { maxZoom: 16 },
  });
  map.addControl(locate, 'top-right');
  locate.on('geolocate', (e: GeolocationPosition) =>
    options.onPosition?.({ lat: e.coords.latitude, lng: e.coords.longitude }),
  );

  let pending: { events: readonly EventOut[]; selectedId: string | null; flyTo: boolean } | null = null;
  let ready = false;

  function apply(events: readonly EventOut[], selectedId: string | null, flyTo: boolean): void {
    (map.getSource(SOURCE) as GeoJSONSource).setData(toGeoJSON(events));
    map.setFilter('points-selected', ['==', ['get', 'id'], selectedId ?? '']);
    const target = events.find((e) => e.id === selectedId);
    if (flyTo && target && target.location.lat !== null && target.location.lng !== null) {
      map.flyTo({ center: [target.location.lng, target.location.lat], zoom: Math.max(map.getZoom(), 17) });
    }
  }

  let routeRequest: AbortController | null = null;
  let pendingRoute: { event: EventOut | null; from: Position | null } | null = null;

  function clearRoute(): void {
    routeRequest?.abort();
    routeRequest = null;
    if (ready) (map.getSource(ROUTE_SOURCE) as GeoJSONSource).setData({ type: 'FeatureCollection', features: [] });
    panel.hidden = true;
    panel.replaceChildren();
  }

  function renderPanel(event: EventOut, to: Position, text: string, from: Position | null): void {
    const info = el('div', 'route-info');
    info.append(el('strong', 'route-summary', text), el('span', 'route-dest', event.location.building_name ?? event.title));
    const gmaps = el('a', 'route-gmaps', 'Google Maps');
    gmaps.setAttribute('aria-label', 'Open walking directions in Google Maps');
    gmaps.href = googleMapsUrl(to, from);
    gmaps.target = '_blank';
    gmaps.rel = 'noopener noreferrer';
    const close = el('button', 'route-close', '\u00d7');
    close.type = 'button';
    close.setAttribute('aria-label', 'Hide route');
    close.addEventListener('click', clearRoute);
    panel.replaceChildren(info, gmaps, close);
    panel.hidden = false;
  }

  async function drawRoute(event: EventOut | null, from: Position | null): Promise<void> {
    clearRoute();
    const { lat, lng } = event?.location ?? { lat: null, lng: null };
    if (!event || lat === null || lng === null) return;
    const to = { lat, lng };
    if (!from) {
      renderPanel(event, to, 'Tap ◎ to show your walking route', null);
      return;
    }
    renderPanel(event, to, 'Finding walking route…', from);
    const controller = new AbortController();
    routeRequest = controller;
    let route: WalkingRoute;
    try {
      route = await fetchWalkingRoute(fetchImpl, from, to, controller.signal);
    } catch {
      return; // superseded by a newer selection
    }
    if (routeRequest !== controller) return;
    (map.getSource(ROUTE_SOURCE) as GeoJSONSource).setData(routeFeature(route));
    const bounds = route.coordinates.reduce(
      (b, c) => b.extend(c),
      new maplibregl.LngLatBounds(route.coordinates[0], route.coordinates[0]),
    );
    map.fitBounds(bounds, { padding: { top: 50, bottom: 90, left: 40, right: 60 }, maxZoom: 17 });
    renderPanel(event, to, routeSummary(route) + (route.routed ? '' : ' (estimate)'), from);
  }

  map.on('load', () => {
    addEventLayers(map);
    addRouteLayers(map);
    ready = true;
    if (pending) apply(pending.events, pending.selectedId, pending.flyTo);
    if (pendingRoute) void drawRoute(pendingRoute.event, pendingRoute.from);
    locate.trigger(); // ask for the student's location right away (the browser shows its permission prompt)
  });
  map.on('click', 'points-hit', (e: MapLayerMouseEvent) => {
    const id = e.features?.[0]?.properties?.id;
    if (typeof id === 'string') onSelect(id);
  });
  map.on('click', 'clusters', async (e: MapLayerMouseEvent) => {
    const feature = e.features?.[0];
    if (!feature || feature.geometry.type !== 'Point') return;
    const zoom = await (map.getSource(SOURCE) as GeoJSONSource).getClusterExpansionZoom(feature.properties.cluster_id);
    map.easeTo({ center: feature.geometry.coordinates as [number, number], zoom });
  });

  return {
    update(events, selectedId, flyTo) {
      updateNote(note, events);
      if (ready) apply(events, selectedId, flyTo);
      else pending = { events, selectedId, flyTo };
    },
    showRoute(event, from) {
      if (ready) void drawRoute(event, from);
      else pendingRoute = { event, from };
    },
  };
}
