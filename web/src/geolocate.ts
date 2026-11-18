import type { Position } from './types';

/**
 * Ask for the device position without blocking: resolves via callback whenever (and if) a fix
 * arrives. Denials and errors are silent; the UI keeps sorting by start time.
 */
export function requestPosition(
  geo: Geolocation | undefined,
  onPosition: (pos: Position) => void,
  onError: (message: string) => void = () => undefined,
): void {
  if (!geo) {
    onError('Location is not available in this browser');
    return;
  }
  geo.getCurrentPosition(
    (p) => onPosition({ lat: p.coords.latitude, lng: p.coords.longitude }),
    (err) => onError(err.code === err.PERMISSION_DENIED ? 'Location permission denied' : 'Could not get your location'),
    { enableHighAccuracy: false, maximumAge: 60_000, timeout: 15_000 },
  );
}
