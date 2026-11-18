import 'maplibre-gl/dist/maplibre-gl.css';
import './style.css';
import { startApp, startPolling } from './app';
import { browserStore } from './cache';
import { createMapView } from './views/map';

function root(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id}`);
  return node;
}

const { refresh } = startApp(
  { header: root('header-status'), filters: root('filters'), list: root('list'), map: root('map') },
  {
    fetch: (input, init) => fetch(input, init),
    storage: browserStore(),
    geolocation: navigator.geolocation,
    now: () => Date.now(),
    createMap: (mapRoot, onSelect, onPosition) => createMapView(mapRoot, onSelect, { onPosition }),
  },
);
startPolling(document, refresh);
