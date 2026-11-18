/** Mirrors the response models in app/schemas.py (EventsResponse, EventOut, MetaOut). */

export type FoodLabel = 'yes' | 'likely' | 'no';
export type WindowName = 'now' | '2h' | 'today' | 'week';

export interface EvidenceOut {
  text: string;
  field: string;
  kind: string;
  weight: number;
}

export interface FoodOut {
  score: number;
  label: FoodLabel;
  evidence: EvidenceOut[];
}

export interface LocationOut {
  raw: string | null;
  building_id: string | null;
  building_name: string | null;
  lat: number | null;
  lng: number | null;
  geocode_method: string;
  geocode_confidence: number;
  requires_sign_in: boolean;
}

export interface EventOut {
  id: string;
  source_url: string;
  title: string;
  host_group: string | null;
  starts_at: string;
  ends_at: string | null;
  all_day: boolean;
  starts_in_minutes: number;
  location: LocationOut;
  food: FoodOut;
  is_free_to_attend: boolean | null;
  free_evidence: string[];
  cost_raw: string | null;
  visibility: 'public' | 'authenticated';
  status: 'active' | 'canceled_or_removed';
  event_type: string | null;
  tags: string[];
  attendance_mode: string | null;
  distance_m: number | null;
  description_text: string | null;
  updated_at: string;
  last_seen_at: string;
}

export interface WindowOut {
  name: WindowName;
  start: string;
  end: string;
}

export interface EventsResponse {
  generated_at: string;
  data_as_of: string | null;
  data_age_seconds: number | null;
  window: WindowOut;
  total: number;
  page: number;
  page_size: number;
  next_page: number | null;
  items: EventOut[];
}

export interface AuthOut {
  mode: 'anonymous' | 'authenticated';
  status: 'none' | 'valid' | 'expired';
  cookie_configured: boolean;
  ics_feeds_configured: number;
  data_includes_authenticated_events: boolean;
}

export interface DatasetOut {
  kind: 'live' | 'fixture_replay';
  time_shift_days: number;
}

/** Only the parts of MetaOut the UI reads; the rest is passed through untyped. */
export interface MetaOut {
  now: string;
  data_as_of: string | null;
  data_age_seconds: number | null;
  auth: AuthOut;
  dataset: DatasetOut;
  [key: string]: unknown;
}

export interface Position {
  lat: number;
  lng: number;
}
