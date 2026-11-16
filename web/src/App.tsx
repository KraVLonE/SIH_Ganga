import { useEffect, useMemo, useState } from "react";
import type { Alert, Forecast, Investment, Meta, Place, Profile, Skill, Station, Town, VarKey } from "./api";
import { api } from "./api";
import { colour, dayLabel, fmt } from "./format";
import MapView from "./MapView";
import { AboutPanel, AlertsPanel, AskPanel, PlacePanel, ScenarioPanel, SkillPanel } from "./Panels";

type Tab = "place" | "alerts" | "scenario" | "ask" | "skill" | "about";
const MAP_VARS: VarKey[] = ["p_unfit", "fc", "bod", "do", "flow"];
const SHORT: Record<string, string> = { p_unfit: "Bathing risk", fc: "Coliform", bod: "BOD", do: "Oxygen", flow: "Flow" };

function days(first: string, last: string): string[] {
  const out: string[] = [];
  for (let d = new Date(first + "T00:00:00Z"); d <= new Date(last + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + 1)) out.push(d.toISOString().slice(0, 10));
  return out;
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [places, setPlaces] = useState<Place[]>([]);
  const [stations, setStations] = useState<Station[]>([]);
  const [towns, setTowns] = useState<Town[]>([]);
  const [river, setRiver] = useState<GeoJSON.FeatureCollection | null>(null);
  const [variable, setVariable] = useState<VarKey>("p_unfit");
  const [day, setDay] = useState<string | null>(null);
  const [km, setKm] = useState<number | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [forecast, setForecast] = useState<Forecast | null>(null);
  const [alerts, setAlerts] = useState<Alert[] | null>(null);
  const [skill, setSkill] = useState<Skill | null>(null);
  const [investment, setInvestment] = useState<Investment | null>(null);
  const [tab, setTab] = useState<Tab>("place");

  useEffect(() => {
    api
      .meta()
      .then((m) => {
        setMeta(m);
        setDay(m.issued_on);
      })
      .catch((e) => setError(String(e)));
    api.nodes().then((p) => {
      setPlaces(p);
      const start = p.find((x) => x.slug === "varanasi_us") ?? p[0];
      if (start) setKm(start.chainage_km);
    }).catch(() => undefined);
    api.stations().then(setStations).catch(() => undefined);
    api.loads().then(setTowns).catch(() => undefined);
    api.river().then(setRiver).catch(() => undefined);
    api.alerts().then(setAlerts).catch(() => setAlerts([]));
  }, []);

  useEffect(() => {
    if (!day) return;
    let live = true;
    api.profile(variable, day).then((p) => live && setProfile(p)).catch(() => live && setProfile(null));
    return () => {
      live = false;
    };
  }, [variable, day]);

  useEffect(() => {
    if (km === null) return;
    let live = true;
    api.forecast(km).then((f) => live && setForecast(f)).catch(() => live && setForecast(null));
    return () => {
      live = false;
    };
  }, [km]);

  useEffect(() => {
    if (tab !== "skill") return;
    if (!skill) api.skill().then(setSkill).catch(() => undefined);
    if (!investment) api.investment().then(setInvestment).catch(() => undefined);
  }, [tab, skill, investment]);

  const allDays = useMemo(() => (meta ? days(meta.first_day, meta.last_day) : []), [meta]);
  const info = meta ? meta.variables[variable] : null;
  const select = (k: number) => {
    setKm(k);
    setTab("place");
  };

  if (error)
    return (
      <div className="fatal">
        <h1>Forecast unavailable</h1>
        <p>The forecast service did not respond. {error}</p>
      </div>
    );
  if (!meta || !day) return <div className="fatal">Loading the latest forecast…</div>;

  const dayIdx = allDays.indexOf(day);
  const todayIdx = allDays.indexOf(meta.issued_on);
  const legend = info?.limit !== undefined || variable === "p_unfit";
  const worst = profile ? profile.p50.reduce((b, v, i) => (variable === "do" ? (v < profile.p50[b] ? i : b) : v > profile.p50[b] ? i : b), 0) : 0;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <h1>Ganga water quality forecast</h1>
          <p>Haridwar to Patna · issued {dayLabel(meta.issued_on)} · model estimates, not sensor readings</p>
        </div>
        <nav className="tabs" role="tablist">
          {(["place", "alerts", "scenario", "ask", "skill", "about"] as Tab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              {{ place: "Place", alerts: `Alerts${alerts && alerts.length ? ` (${alerts.length})` : ""}`, scenario: "What if", ask: "Ask", skill: "Accuracy", about: "Data" }[t]}
            </button>
          ))}
        </nav>
      </header>

      <main className="main">
        <section className="mapwrap">
          <div className="mapbar">
            <div className="seg" role="tablist" aria-label="Map layer">
              {MAP_VARS.map((v) => (
                <button key={v} role="tab" aria-selected={variable === v} onClick={() => setVariable(v)}>
                  {SHORT[v]}
                </button>
              ))}
            </div>
          </div>
          <MapView river={river} places={places} stations={stations} towns={towns} profile={profile} info={info} selectedKm={km} onSelect={select} />
          <div className="mapfoot">
            <div className="slider-row">
              <button className="step" onClick={() => setDay(allDays[Math.max(0, dayIdx - 1)])} aria-label="Previous day">‹</button>
              <div className="slider-wrap">
                <input type="range" min={0} max={allDays.length - 1} value={dayIdx} onChange={(e) => setDay(allDays[Number(e.target.value)])} aria-label="Day" />
                <div className="ticks">
                  <span>{dayLabel(allDays[0])}</span>
                  <span className="today" style={{ left: `${(todayIdx / (allDays.length - 1)) * 100}%` }}>today</span>
                  <span>{dayLabel(allDays[allDays.length - 1])}</span>
                </div>
              </div>
              <button className="step" onClick={() => setDay(allDays[Math.min(allDays.length - 1, dayIdx + 1)])} aria-label="Next day">›</button>
              <div className="daybox">
                <b>{dayLabel(day)}</b>
                <span>{dayIdx > todayIdx ? `forecast +${dayIdx - todayIdx} d` : dayIdx === todayIdx ? "today" : `${todayIdx - dayIdx} d ago`}</span>
              </div>
            </div>
            <div className="legend">
              {legend ? (
                <>
                  <span>{variable === "p_unfit" ? "low risk" : "within limit"}</span>
                  <i style={{ background: `linear-gradient(90deg, ${[0, 0.6, 1, 1.5, 2.2].map((s) => colour(s)).join(",")})` }} />
                  <span>{variable === "p_unfit" ? "likely unfit for bathing" : "far outside limit"}</span>
                </>
              ) : (
                <span>{info?.label} has no bathing limit; the river is shown uncoloured.</span>
              )}
              {profile && info && (
                <span className="worst">
                  {variable === "do" ? "Lowest" : "Highest"} {fmt(profile.p50[worst], variable)} {variable === "p_unfit" ? "" : info.unit} at {profile.chainage_km[worst].toFixed(0)} km
                </span>
              )}
            </div>
          </div>
        </section>

        <aside className="panel">
          {tab === "place" && <PlacePanel meta={meta} forecast={forecast} day={day} />}
          {tab === "alerts" && <AlertsPanel alerts={alerts} onGo={select} />}
          {tab === "scenario" && <ScenarioPanel meta={meta} towns={towns} places={places} />}
          {tab === "ask" && <AskPanel />}
          {tab === "skill" && <SkillPanel skill={skill} investment={investment} />}
          {tab === "about" && <AboutPanel meta={meta} />}
        </aside>
      </main>
    </div>
  );
}
