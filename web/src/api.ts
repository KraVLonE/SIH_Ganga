export type VarKey = "do" | "bod" | "fc" | "no3" | "nh4" | "tp" | "flow" | "p_unfit";

export interface VarInfo {
  label: string;
  unit: string;
  limit?: number;
  bad?: "above" | "below";
  confidence?: string;
}

export interface Meta {
  run_id: number;
  issued_on: string;
  first_day: string;
  last_day: string;
  length_km: number;
  variables: Record<VarKey, VarInfo>;
  run: { members?: number; calibrated?: string; created_at: string };
  note: string;
}

export interface Place {
  slug: string;
  name: string;
  kind: "node" | "confluence";
  chainage_km: number;
  lon: number;
  lat: number;
}

export interface Station {
  code: string;
  name: string;
  chainage_km: number;
  lon: number;
  lat: number;
  observations: number;
}

export interface Point {
  date: string;
  p05: number | null;
  p50: number;
  p95: number | null;
  forecast: boolean;
}

export interface Forecast {
  place: string;
  chainage_km: number;
  issued_on: string;
  series: Partial<Record<VarKey, Point[]>>;
  rain: { date: string; mm: number; forecast: boolean }[];
}

export interface Profile {
  variable: VarKey;
  day: string;
  forecast: boolean;
  chainage_km: number[];
  p50: number[];
}

export interface Observation {
  station: string;
  name: string;
  chainage_km: number;
  date: string;
  resolution: string;
  value: number;
  source: string;
  source_file: string;
  page: number | null;
}

export interface Alert {
  id: number;
  created_at: string;
  kind: string;
  severity: "severe" | "warning" | "info";
  chainage_km: number;
  valid_on: string | null;
  message: string;
  details: Record<string, unknown>;
}

export interface Metrics {
  n: number;
  rmse?: number;
  bias?: number;
  nse?: number;
  kge?: number;
}

export interface Skill {
  created: string;
  period: [string, string];
  split_date: string;
  counts: { total: number; train: number; stations: number; unseen_stations: string[] };
  model: Record<string, Record<string, Metrics>>;
  climatology_baseline: Record<string, Record<string, Metrics>>;
  coverage_90: Record<string, Record<string, number>>;
  residual_sigma: Record<string, number>;
}

export interface History {
  place: string;
  chainage_km: number;
  months: string[];
  model: Record<string, number[]>;
}

export interface Town {
  town: string;
  state: string;
  chainage_km: string;
  sewage_mld: string;
  stp_capacity_mld: string;
  stp_utilised_mld: string;
  untreated_mld: string;
  confidence: string;
  lon: string;
  lat: string;
}

export interface ScenarioRequest {
  kind: "plant_offline" | "load_change" | "rain" | "low_flow" | "spill";
  town?: string;
  percent?: number;
  rain_mm?: number;
  day_offset?: number;
  tonnes_bod?: number;
  chainage_km?: number;
}

export interface ScenarioResult {
  description: string;
  days: string[];
  chainage_km: number[];
  baseline: Record<string, number[][]>;
  scenario: Record<string, number[][]>;
  places: { name: string; chainage_km: number; arrival_days: number | null }[];
  summary: string[];
}

export interface Investment {
  note: string;
  clusters: {
    name: string;
    station: string;
    station_name: string;
    observed: Record<string, { before: number | null; after: number | null; n_before: number; n_after: number }>;
    modelled: Record<string, { with_investment: number; without_investment: number }>;
  }[];
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export const api = {
  meta: () => get<Meta>("/api/meta"),
  nodes: () => get<Place[]>("/api/nodes"),
  stations: () => get<Station[]>("/api/stations"),
  river: () => get<GeoJSON.FeatureCollection>("/api/river"),
  loads: () => get<Town[]>("/api/loads"),
  forecast: (chainage: number) =>
    get<Forecast>(`/api/forecast?chainage=${chainage}&variables=do,bod,fc,no3,flow,p_unfit,p_do,p_bod,p_fc`),
  profile: (variable: VarKey, day: string) => get<Profile>(`/api/profile?variable=${variable}&day=${day}`),
  observations: (chainage: number, variable: string) =>
    get<Observation[]>(`/api/observations?chainage=${chainage}&variable=${variable}&within_km=12`),
  history: (chainage: number) => get<History>(`/api/history?chainage=${chainage}`),
  alerts: () => get<Alert[]>("/api/alerts?days=7"),
  skill: () => get<Skill>("/api/skill"),
  investment: () => get<Investment>("/api/investment"),
  scenario: async (body: ScenarioRequest) => {
    const r = await fetch("/api/scenario", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as ScenarioResult;
  },
  ask: async (question: string) => {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!r.ok) throw new Error(await r.text());
    return (await r.json()) as { answer: string; used: string[] };
  },
};
