// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { sampleEvents } from './helpers';

type Handler = (e: unknown) => unknown;

const created: FakeMap[] = [];
const webgl = { broken: false };

class FakeMap {
  handlers = new Map<string, Handler>();
  layers: { id: string; type: string }[] = [];
  source = {
    data: null as unknown,
    setData: (d: unknown) => void (this.source.data = d),
    getClusterExpansionZoom: vi.fn(async () => 17),
  };
  sourceOptions: Record<string, unknown> | null = null;
  filters = new Map<string, unknown>();
  flyTo = vi.fn();
  easeTo = vi.fn();
  options: Record<string, unknown>;
  constructor(options: Record<string, unknown>) {
    if (webgl.broken) throw new Error('Failed to initialize WebGL');
    this.options = options;
    created.push(this);
  }
  controls: unknown[] = [];
  fitBounds = vi.fn();
  addControl(control: unknown) {
    this.controls.push(control);
  }
  on(event: string, layerOrHandler: string | Handler, handler?: Handler) {
    this.handlers.set(handler ? `${event}:${layerOrHandler as string}` : event, handler ?? (layerOrHandler as Handler));
  }
  sources = new Map<string, { data: unknown; setData: (d: unknown) => void }>();
  addSource(id: string, options: Record<string, unknown>) {
    if (id === 'events') this.sourceOptions = options;
    else this.sources.set(id, { data: options.data, setData: (d) => void (this.sources.get(id)!.data = d) });
  }
  addLayer(layer: { id: string; type: string }) {
    this.layers.push(layer);
  }
  getSource(id: string) {
    return id === 'events' ? this.source : this.sources.get(id);
  }
  setFilter(id: string, filter: unknown) {
    this.filters.set(id, filter);
  }
  getZoom() {
    return 15;
  }
}

class FakeGeolocate {
  handlers = new Map<string, Handler>();
  trigger = vi.fn();
  constructor(readonly options: Record<string, unknown>) {}
  on(event: string, handler: Handler) {
    this.handlers.set(event, handler);
  }
}

class FakeBounds {
  points: [number, number][] = [];
  constructor(a: [number, number], b: [number, number]) {
    this.points.push(a, b);
  }
  extend(p: [number, number]) {
    this.points.push(p);
    return this;
  }
}

vi.mock('maplibre-gl', () => ({
  default: { Map: FakeMap, NavigationControl: class {}, GeolocateControl: FakeGeolocate, LngLatBounds: FakeBounds },
}));

const { createMapView, CAMPUS_CENTER, CAMPUS_ZOOM } = await import('../src/views/map');

describe('createMapView', () => {
  it('starts on central campus with OSM attribution and clustered pins', () => {
    const root = document.createElement('div');
    const view = createMapView(root, vi.fn());
    const map = created.at(-1)!;
    expect(map.options.center).toEqual(CAMPUS_CENTER);
    expect(map.options.zoom).toBe(CAMPUS_ZOOM);
    const style = map.options.style as { glyphs: string; sources: { osm: { attribution: string; tiles: string[] } } };
    expect(style.sources.osm.tiles[0]).toBe('https://tile.openstreetmap.org/{z}/{x}/{y}.png');
    expect(style.sources.osm.attribution).toContain('OpenStreetMap');
    expect(style.glyphs).toContain('{fontstack}');

    view.update(sampleEvents().items, null, false);
    expect(map.source.data).toBeNull(); // queued until the style loads
    expect(root.querySelector('.map-note')?.textContent).toBe('1 not on map');
    map.handlers.get('load')!({});
    expect(map.sourceOptions).toMatchObject({ type: 'geojson', cluster: true });
    expect(map.layers.map((l) => l.id)).toEqual([
      'clusters',
      'cluster-count',
      'points-hit',
      'points',
      'points-selected',
      'route-casing',
      'route-line',
      'route-estimate',
    ]);
    const locate = map.controls.find((c) => c instanceof FakeGeolocate) as FakeGeolocate;
    expect(locate.trigger).toHaveBeenCalledTimes(1); // asks for the location as soon as the map loads
    expect((map.source.data as { features: unknown[] }).features).toHaveLength(3);
  });

  it('highlights and flies to a selected pin, and reports pin taps', async () => {
    const onSelect = vi.fn();
    const root = document.createElement('div');
    const view = createMapView(root, onSelect);
    const map = created.at(-1)!;
    map.handlers.get('load')!({});
    view.update(sampleEvents().items, 'ycal-2337199@campusgroups.com', true);
    expect(map.filters.get('points-selected')).toEqual(['==', ['get', 'id'], 'ycal-2337199@campusgroups.com']);
    expect(map.flyTo).toHaveBeenCalledWith({ center: [-72.9254, 41.3133], zoom: 17 });

    view.update(sampleEvents().items, 'ycal-2339100@campusgroups.com', true); // no pin: no fly
    expect(map.flyTo).toHaveBeenCalledTimes(1);

    map.handlers.get('click:points-hit')!({ features: [{ properties: { id: 'ycal-2338001@campusgroups.com' } }] });
    expect(onSelect).toHaveBeenCalledWith('ycal-2338001@campusgroups.com');

    await map.handlers.get('click:clusters')!({
      features: [{ geometry: { type: 'Point', coordinates: [-72.92, 41.31] }, properties: { cluster_id: 7 } }],
    });
    expect(map.source.getClusterExpansionZoom).toHaveBeenCalledWith(7);
    expect(map.easeTo).toHaveBeenCalledWith({ center: [-72.92, 41.31], zoom: 17 });

    view.update([], null, false);
    expect(root.querySelector<HTMLElement>('.map-note')!.hidden).toBe(true);
  });

  it('falls back to a notice when WebGL is unavailable, keeping the unpinned note', () => {
    webgl.broken = true;
    const root = document.createElement('div');
    const view = createMapView(root, vi.fn());
    webgl.broken = false;
    expect(root.textContent).toContain('The map is unavailable on this device');
    view.update(sampleEvents().items, null, true);
    expect(root.querySelector('.map-note')?.textContent).toBe('1 not on map');
  });

  it('requests the location, reports fixes, and draws the walking route to a selected event', async () => {
    const onPosition = vi.fn();
    const fetchRoute = vi.fn(async (_url: string) =>
      new Response(
        JSON.stringify({
          code: 'Ok',
          routes: [{ distance: 810, duration: 640, geometry: { coordinates: [[-72.9282, 41.3108], [-72.927, 41.312], [-72.9254, 41.3133]] } }],
        }),
      ),
    );
    const root = document.createElement('div');
    const view = createMapView(root, vi.fn(), { onPosition, fetch: fetchRoute });
    const map = created.at(-1)!;
    map.handlers.get('load')!({});
    const locate = map.controls.find((c) => c instanceof FakeGeolocate) as FakeGeolocate;
    locate.handlers.get('geolocate')!({ coords: { latitude: 41.3108, longitude: -72.9282 } });
    expect(onPosition).toHaveBeenCalledWith({ lat: 41.3108, lng: -72.9282 });

    const pasta = sampleEvents().items.find((e) => e.id === 'ycal-2337199@campusgroups.com')!;
    view.showRoute!(pasta, { lat: 41.3108, lng: -72.9282 });
    await new Promise((r) => setTimeout(r, 0));
    expect(String(fetchRoute.mock.calls[0][0])).toContain('/routed-foot/route/v1/driving/-72.9282,41.3108;-72.9254,41.3133');
    const route = map.sources.get('route')!.data as { features: { geometry: { coordinates: unknown[] } }[] };
    expect(route.features[0].geometry.coordinates).toHaveLength(3);
    expect(map.fitBounds).toHaveBeenCalled();
    const panel = root.querySelector<HTMLElement>('.route-panel')!;
    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain('11 min walk · 810 m');
    expect(panel.querySelector('a')!.getAttribute('href')).toContain('travelmode=walking');

    (panel.querySelector('button') as HTMLButtonElement).click();
    expect(panel.hidden).toBe(true);
    expect((map.sources.get('route')!.data as { features: unknown[] }).features).toHaveLength(0);

    view.showRoute!(pasta, null); // no location yet: prompt instead of a route
    expect(panel.textContent).toContain('show your walking route');
  });
});
