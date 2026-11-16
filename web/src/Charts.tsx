import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Observation, Point, VarInfo, VarKey } from "./api";
import { dayLabel, fmt, monthLabel } from "./format";

const INK = "var(--ink-2)";
const GRID = "var(--line)";

interface SeriesProps {
  title: string;
  varKey: VarKey;
  info: VarInfo;
  points: Point[];
  today: string;
  log?: boolean;
}

/** Past ten days and forecast for one variable, with the 5–95% band and the bathing limit. */
export function ForecastChart({ title, varKey, info, points, today, log }: SeriesProps) {
  const data = points.map((p) => ({
    date: p.date,
    p50: p.p50,
    band: p.p05 !== null && p.p95 !== null ? [p.p05, p.p95] : undefined,
    past: p.forecast ? undefined : p.p50,
    ahead: p.forecast || p.date === today ? p.p50 : undefined,
  }));
  const last = points[points.length - 1];
  const now = points.find((p) => p.date === today);
  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">{title}</span>
        <span className="chart-now">
          today <b>{fmt(now?.p50, varKey)}</b> → {dayLabel(last?.date ?? today)} <b>{fmt(last?.p50, varKey)}</b>{" "}
          <span className="unit">{info.unit}</span>
        </span>
      </figcaption>
      <ResponsiveContainer width="100%" height={150}>
        <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="date" tickFormatter={dayLabel} tick={{ fontSize: 11, fill: INK }} interval={2} tickLine={false} axisLine={{ stroke: GRID }} />
          <YAxis
            scale={log ? "log" : "auto"}
            domain={log ? ["auto", "auto"] : [(min: number) => Math.max(0, Math.floor(min * 0.9)), "auto"]}
            tickFormatter={(v: number) => fmt(v, varKey)}
            tick={{ fontSize: 11, fill: INK }}
            width={44}
            tickLine={false}
            axisLine={false}
            allowDataOverflow
          />
          <Tooltip
            formatter={(value: number | number[], name: string) =>
              Array.isArray(value)
                ? [`${fmt(value[0], varKey)} – ${fmt(value[1], varKey)}`, "90% range"]
                : [`${fmt(value, varKey)} ${info.unit}`, name === "past" ? "estimate" : "forecast"]
            }
            labelFormatter={dayLabel}
            contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid var(--line)", background: "var(--card)" }}
          />
          <Area dataKey="band" stroke="none" fill="var(--accent)" fillOpacity={0.16} isAnimationActive={false} />
          {info.limit !== undefined && (
            <ReferenceLine y={info.limit} stroke="var(--bad)" strokeDasharray="4 3" label={{ value: `limit ${fmt(info.limit, varKey)}`, fontSize: 10, fill: "var(--bad)", position: "insideTopRight" }} />
          )}
          <ReferenceLine x={today} stroke={INK} strokeDasharray="2 3" />
          <Line dataKey="past" stroke="var(--accent)" strokeWidth={2} dot={false} isAnimationActive={false} />
          <Line dataKey="ahead" stroke="var(--accent)" strokeWidth={2} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </figure>
  );
}

export function RainChart({ rain, today }: { rain: { date: string; mm: number }[]; today: string }) {
  const total = rain.filter((r) => r.date > today).reduce((a, r) => a + r.mm, 0);
  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">Rainfall</span>
        <span className="chart-now">
          next 5 days <b>{total.toFixed(0)}</b> <span className="unit">mm</span>
        </span>
      </figcaption>
      <ResponsiveContainer width="100%" height={90}>
        <ComposedChart data={rain} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="date" tickFormatter={dayLabel} tick={{ fontSize: 11, fill: INK }} interval={2} tickLine={false} axisLine={{ stroke: GRID }} />
          <YAxis tick={{ fontSize: 11, fill: INK }} width={44} tickLine={false} axisLine={false} domain={[0, (max: number) => Math.max(5, Math.ceil(max))]} />
          <Tooltip formatter={(v: number) => [`${v.toFixed(1)} mm`, "rain"]} labelFormatter={dayLabel} contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid var(--line)", background: "var(--card)" }} />
          <ReferenceLine x={today} stroke={INK} strokeDasharray="2 3" />
          <Bar dataKey="mm" fill="var(--rain)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </figure>
  );
}

interface HistoryProps {
  title: string;
  varKey: VarKey;
  info: VarInfo;
  months: string[];
  model: number[];
  observed: Observation[];
  log?: boolean;
}

/** Monthly modelled history against measured values at nearby monitoring stations. */
export function HistoryChart({ title, varKey, info, months, model, observed, log }: HistoryProps) {
  const at = (iso: string) => new Date(iso + "T00:00:00").getTime();
  const line = months.map((m, i) => ({ t: at(m) + 14 * 864e5, model: model[i] }));
  const dots = observed.map((o) => ({ t: at(o.date) + (o.resolution === "month" ? 14 * 864e5 : 0), obs: o.value }));
  const data = [...line, ...dots].sort((a, b) => a.t - b.t);
  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">{title}</span>
        <span className="chart-now">
          <span className="key key-line" /> model, monthly mean <span className="key key-dot" /> measured ({observed.length})
        </span>
      </figcaption>
      <ResponsiveContainer width="100%" height={170}>
        <ComposedChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(t: number) => monthLabel(new Date(t).toISOString().slice(0, 10))} tick={{ fontSize: 11, fill: INK }} tickLine={false} axisLine={{ stroke: GRID }} tickCount={7} />
          <YAxis scale={log ? "log" : "auto"} domain={log ? ["auto", "auto"] : [0, "auto"]} tickFormatter={(v: number) => fmt(v, varKey)} tick={{ fontSize: 11, fill: INK }} width={44} tickLine={false} axisLine={false} allowDataOverflow />
          <Tooltip
            formatter={(v: number, name: string) => [`${fmt(v, varKey)} ${info.unit}`, name === "obs" ? "measured" : "model"]}
            labelFormatter={(t: number) => monthLabel(new Date(t).toISOString().slice(0, 10))}
            contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid var(--line)", background: "var(--card)" }}
          />
          {info.limit !== undefined && <ReferenceLine y={info.limit} stroke="var(--bad)" strokeDasharray="4 3" />}
          <Line dataKey="model" stroke="var(--accent)" strokeWidth={1.6} dot={false} connectNulls isAnimationActive={false} />
          <Scatter dataKey="obs" fill="var(--ink)" shape={<Dot />} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </figure>
  );
}

function Dot(props: { cx?: number; cy?: number }) {
  if (props.cx === undefined || props.cy === undefined || Number.isNaN(props.cy)) return null;
  return <circle cx={props.cx} cy={props.cy} r={2.4} fill="var(--ink)" fillOpacity={0.75} />;
}

interface ProfileProps {
  title: string;
  varKey: VarKey;
  info: VarInfo;
  km: number[];
  baseline: number[];
  scenario: number[];
  marks: { name: string; chainage_km: number }[];
  log?: boolean;
}

/** One variable along the river: baseline against a scenario. */
export function ProfileChart({ title, varKey, info, km, baseline, scenario, marks, log }: ProfileProps) {
  const data = km.map((k, i) => ({ km: k, baseline: baseline[i], scenario: scenario[i] }));
  return (
    <figure className="chart">
      <figcaption>
        <span className="chart-title">{title}</span>
        <span className="chart-now">
          <span className="key key-line muted" /> as forecast <span className="key key-line alt" /> scenario
        </span>
      </figcaption>
      <ResponsiveContainer width="100%" height={170}>
        <ComposedChart data={data} margin={{ top: 14, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="km" type="number" domain={[0, "dataMax"]} tickFormatter={(k: number) => `${Math.round(k)} km`} tick={{ fontSize: 11, fill: INK }} tickLine={false} axisLine={{ stroke: GRID }} tickCount={8} />
          <YAxis scale={log ? "log" : "auto"} domain={log ? ["auto", "auto"] : [0, "auto"]} tickFormatter={(v: number) => fmt(v, varKey)} tick={{ fontSize: 11, fill: INK }} width={44} tickLine={false} axisLine={false} allowDataOverflow />
          <Tooltip formatter={(v: number, name: string) => [`${fmt(v, varKey)} ${info.unit}`, name === "baseline" ? "as forecast" : "scenario"]} labelFormatter={(k: number) => `${Math.round(k)} km from Haridwar`} contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid var(--line)", background: "var(--card)" }} />
          {marks.map((m) => (
            <ReferenceLine key={m.name} x={m.chainage_km} stroke={GRID} label={{ value: m.name, fontSize: 9, fill: INK, position: "top" }} />
          ))}
          {info.limit !== undefined && <ReferenceLine y={info.limit} stroke="var(--bad)" strokeDasharray="4 3" />}
          <Line dataKey="baseline" stroke="var(--ink-2)" strokeWidth={1.6} dot={false} isAnimationActive={false} />
          <Line dataKey="scenario" stroke="var(--alt)" strokeWidth={2} dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </figure>
  );
}
