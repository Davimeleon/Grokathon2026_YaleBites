import eventsJson from './fixtures/events.json';
import metaJson from './fixtures/meta.json';
import type { KeyValueStore } from '../src/cache';
import type { EventOut, EventsResponse, MetaOut } from '../src/types';

export const GENERATED_AT_MS = Date.parse('2026-11-17T23:01:12Z');

/** A fresh deep copy of the sample /api/events response. */
export function sampleEvents(): EventsResponse {
  return structuredClone(eventsJson) as EventsResponse;
}

export function sampleMeta(): MetaOut {
  return structuredClone(metaJson) as unknown as MetaOut;
}

export function eventById(id: string): EventOut {
  const found = sampleEvents().items.find((e) => e.id === id);
  if (!found) throw new Error(`no fixture event ${id}`);
  return found;
}

export function memoryStore(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
