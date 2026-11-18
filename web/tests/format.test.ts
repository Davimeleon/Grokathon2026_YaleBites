import { describe, expect, it } from 'vitest';
import { clockOffset, evidenceQuote, formatClock, formatRelativeStart, formatWalk, locationLabel } from '../src/format';
import { eventById, GENERATED_AT_MS } from './helpers';

const NOW = Date.parse('2026-11-17T23:00:00Z'); // 6:00 PM in New Haven

describe('formatRelativeStart', () => {
  it('counts down for events starting soon', () => {
    expect(formatRelativeStart('2026-11-17T18:25:00-05:00', NOW)).toBe('in 25 min');
    expect(formatRelativeStart('2026-11-17T19:05:00-05:00', NOW)).toBe('in 1 h 5 min');
    expect(formatRelativeStart('2026-11-17T19:00:00-05:00', NOW)).toBe('in 1 h');
  });
  it('reports events already in progress', () => {
    expect(formatRelativeStart('2026-11-17T17:50:00-05:00', NOW)).toBe('started 10 min ago');
    expect(formatRelativeStart('2026-11-17T16:30:00-05:00', NOW)).toBe('started 1 h 30 min ago');
    expect(formatRelativeStart('2026-11-17T18:00:20-05:00', NOW)).toBe('starting now');
    // long-running listings show the start date instead of hundreds of hours
    expect(formatRelativeStart('2026-08-29T09:00:00-04:00', NOW)).toBe('started Aug 29');
  });
  it('uses campus-local day names for later events', () => {
    expect(formatRelativeStart('2026-11-17T21:15:00-05:00', NOW)).toBe('today 9:15 PM');
    expect(formatRelativeStart('2026-11-18T18:30:00-05:00', NOW)).toBe('tomorrow 6:30 PM');
    expect(formatRelativeStart('2026-11-20T12:00:00-05:00', NOW)).toBe('Fri 12:00 PM');
    expect(formatRelativeStart('2026-12-01T12:00:00-05:00', NOW)).toBe('Dec 1 12:00 PM');
  });
  it('treats late-evening UTC dates by New Haven calendar day', () => {
    // 04:30Z on the 18th is still 11:30 PM on the 17th in New Haven.
    expect(formatRelativeStart('2026-11-18T04:30:00Z', NOW)).toBe('today 11:30 PM');
  });
  it('labels all-day events by day only', () => {
    expect(formatRelativeStart('2026-11-18T00:00:00-05:00', NOW, true)).toBe('tomorrow (all day)');
  });
});

describe('small formatters', () => {
  it('formats the campus clock in 24 h', () => {
    expect(formatClock('2026-11-17T23:42:00Z')).toBe('18:42');
  });
  it('formats walking time', () => {
    expect(formatWalk(6)).toBe('~6 min walk');
  });
  it('computes the server clock offset', () => {
    expect(clockOffset('2026-11-17T23:01:12Z', GENERATED_AT_MS - 5_000)).toBe(5_000);
    expect(clockOffset('not a date', 0)).toBe(0);
  });
});

describe('locationLabel', () => {
  it('prefers the building name, then raw text', () => {
    expect(locationLabel(eventById('ycal-2337199@campusgroups.com'))).toBe('Yale LGBTQ Center (135 Prospect St)');
    expect(locationLabel(eventById('ycal-2340002@campusgroups.com'))).toBe('Bass Library L01');
  });
  it('explains withheld locations', () => {
    expect(locationLabel(eventById('ycal-2339100@campusgroups.com'))).toBe('Location requires Yale sign-in');
    const unknown = eventById('ycal-2339100@campusgroups.com');
    unknown.location.requires_sign_in = false;
    expect(locationLabel(unknown)).toBe('Location not listed');
  });
});

describe('evidenceQuote', () => {
  it('quotes the first evidence span', () => {
    expect(evidenceQuote(eventById('ycal-2337199@campusgroups.com'))).toBe('“Pizza will be served”');
  });
  it('returns null without evidence and clips long spans', () => {
    expect(evidenceQuote(eventById('ycal-2340002@campusgroups.com'))).toBeNull();
    const long = eventById('ycal-2337199@campusgroups.com');
    long.food.evidence[0].text = 'x'.repeat(120);
    const quote = evidenceQuote(long, 20)!;
    expect(quote).toHaveLength(22);
    expect(quote.endsWith('…”')).toBe(true);
  });
});
