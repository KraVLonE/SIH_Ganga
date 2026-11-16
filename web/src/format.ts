import type { VarInfo, VarKey } from "./api";

export function fmt(v: number | null | undefined, key: VarKey | string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  if (key === "fc" || key === "tc") return v >= 10000 ? `${(v / 1000).toFixed(0)}k` : Math.round(v).toLocaleString("en-IN");
  if (key === "flow") return Math.round(v).toLocaleString("en-IN");
  if (key.startsWith("p_")) return `${Math.round(v * 100)}%`;
  return v.toFixed(v < 10 ? 1 : 0);
}

export function dayLabel(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function monthLabel(iso: string): string {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { month: "short", year: "2-digit" });
}

/** 0 = comfortably fine, 1 = at the limit, >1 = failing. Used to colour the river. */
export function severity(v: number, key: VarKey, info: VarInfo): number {
  if (key === "p_unfit") return v * 2;
  if (info.limit === undefined) return 0;
  if (info.bad === "below") return v <= 0 ? 3 : (info.limit / v) ** 2;
  if (key === "fc") return Math.max(0, 1 + Math.log10(Math.max(v, 1) / info.limit));
  return v / info.limit;
}

// Colour-blind-safe ramp: teal (fine) -> yellow (near the limit) -> orange -> dark red.
const STOPS: [number, [number, number, number]][] = [
  [0.0, [33, 145, 140]],
  [0.6, [94, 201, 98]],
  [1.0, [253, 201, 38]],
  [1.5, [240, 120, 40]],
  [2.2, [170, 30, 50]],
];

export function colour(sev: number): string {
  const s = Math.max(0, Math.min(sev, 2.2));
  for (let i = 1; i < STOPS.length; i++) {
    if (s <= STOPS[i][0]) {
      const [a, ca] = STOPS[i - 1];
      const [b, cb] = STOPS[i];
      const t = (s - a) / (b - a);
      const c = ca.map((x, k) => Math.round(x + (cb[k] - x) * t));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    }
  }
  return "rgb(170,30,50)";
}

export function verdict(p: number): { text: string; tone: "good" | "warn" | "bad" } {
  if (p < 0.25) return { text: "Likely meets bathing limits", tone: "good" };
  if (p < 0.6) return { text: "May fail bathing limits", tone: "warn" };
  return { text: "Likely fails bathing limits", tone: "bad" };
}
