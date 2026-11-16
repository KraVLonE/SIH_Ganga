import { useEffect, useState } from "react";
import type { Alert, Forecast, History, Investment, Meta, Observation, Place, ScenarioRequest, ScenarioResult, Skill, Town, VarKey } from "./api";
import { api } from "./api";
import { ForecastChart, HistoryChart, ProfileChart, RainChart } from "./Charts";
import { dayLabel, fmt, verdict } from "./format";

const CORE: VarKey[] = ["do", "bod", "fc"];

export function PlacePanel({ meta, forecast, day }: { meta: Meta; forecast: Forecast | null; day: string }) {
  const [view, setView] = useState<"forecast" | "history">("forecast");
  const [history, setHistory] = useState<History | null>(null);
  const [obs, setObs] = useState<Partial<Record<string, Observation[]>>>({});
  const km = forecast?.chainage_km;

  useEffect(() => {
    if (view !== "history" || km === undefined) return;
    let live = true;
    setHistory(null);
    api.history(km).then((h) => live && setHistory(h)).catch(() => live && setHistory(null));
    Promise.all(["do", "bod", "fc", "no3"].map((v) => api.observations(km, v).then((o) => [v, o] as const)))
      .then((pairs) => live && setObs(Object.fromEntries(pairs)))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [view, km]);

  if (!forecast) return <div className="empty">Select a place on the river.</div>;
  const at = (v: VarKey) => forecast.series[v]?.find((p) => p.date === day);
  const p = at("p_unfit")?.p50 ?? 0;
  const v = verdict(p);
  const failing = CORE.filter((k) => {
    const info = meta.variables[k];
    const x = at(k)?.p50;
    return x !== undefined && info.limit !== undefined && (info.bad === "below" ? x < info.limit : x > info.limit);
  });
  return (
    <div className="panel-body">
      <header className="place">
        <div>
          <h2>{forecast.place}</h2>
          <p className="sub">{forecast.chainage_km.toFixed(0)} km downstream of Haridwar</p>
        </div>
        <div className="seg" role="tablist">
          <button role="tab" aria-selected={view === "forecast"} onClick={() => setView("forecast")}>15 days</button>
          <button role="tab" aria-selected={view === "history"} onClick={() => setView("history")}>History</button>
        </div>
      </header>

      <section className={`verdict ${v.tone}`}>
        <div>
          <span className="verdict-day">{dayLabel(day)}{day > meta.issued_on ? " · forecast" : day === meta.issued_on ? " · today" : ""}</span>
          <strong>{v.text}</strong>
          <span className="verdict-why">
            {Math.round(p * 100)}% chance of failing
            {failing.length > 0 && <> · outside limit: {failing.map((k) => meta.variables[k].label.toLowerCase()).join(", ")}</>}
          </span>
        </div>
        <dl>
          {CORE.map((k) => (
            <div key={k}>
              <dt>{meta.variables[k].label}</dt>
              <dd>
                {fmt(at(k)?.p50, k)} <small>{meta.variables[k].unit}</small>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {view === "forecast" ? (
        <>
          <RainChart rain={forecast.rain} today={meta.issued_on} />
          <ForecastChart title="River flow" varKey="flow" info={meta.variables.flow} points={forecast.series.flow ?? []} today={meta.issued_on} />
          <ForecastChart title="Dissolved oxygen" varKey="do" info={meta.variables.do} points={forecast.series.do ?? []} today={meta.issued_on} />
          <ForecastChart title="BOD" varKey="bod" info={meta.variables.bod} points={forecast.series.bod ?? []} today={meta.issued_on} />
          <ForecastChart title="Fecal coliform" varKey="fc" info={meta.variables.fc} points={forecast.series.fc ?? []} today={meta.issued_on} log />
          <ForecastChart title="Nitrate-N (low confidence)" varKey="no3" info={meta.variables.no3} points={forecast.series.no3 ?? []} today={meta.issued_on} />
          <p className="fine">Solid line: model estimate for past days. Dashed: forecast. Shaded: range that should contain the true value 9 times in 10. Red dashed: bathing limit.</p>
        </>
      ) : history ? (
        <>
          <HistoryChart title="Dissolved oxygen" varKey="do" info={meta.variables.do} months={history.months} model={history.model.do} observed={obs.do ?? []} />
          <HistoryChart title="BOD" varKey="bod" info={meta.variables.bod} months={history.months} model={history.model.bod} observed={obs.bod ?? []} />
          <HistoryChart title="Fecal coliform" varKey="fc" info={meta.variables.fc} months={history.months} model={history.model.fc} observed={obs.fc ?? []} log />
          <HistoryChart title="River flow (modelled only)" varKey="flow" info={meta.variables.flow} months={history.months} model={history.model.flow} observed={[]} />
          <p className="fine">Dots are laboratory measurements from monitoring stations within 12 km (state pollution board and CPCB reports). The line is the model's historical simulation.</p>
        </>
      ) : (
        <div className="empty">Loading history…</div>
      )}
    </div>
  );
}

export function AlertsPanel({ alerts, onGo }: { alerts: Alert[] | null; onGo: (km: number) => void }) {
  if (!alerts) return <div className="empty">Loading alerts…</div>;
  if (alerts.length === 0)
    return (
      <div className="panel-body">
        <h2>Alerts</h2>
        <div className="empty">No alerts in the last 7 days. Alerts are raised when a place is forecast to get clearly worse than its own recent level, not for places that are routinely over the limit.</div>
      </div>
    );
  return (
    <div className="panel-body">
      <h2>Alerts</h2>
      <p className="sub">Last 7 days · {alerts.length} raised</p>
      <ul className="alerts">
        {alerts.map((a) => (
          <li key={a.id} className={`alert ${a.severity}`}>
            <div className="alert-top">
              <span className="tag">{a.kind}</span>
              <span className="when">{a.valid_on ? dayLabel(a.valid_on) : ""}</span>
            </div>
            <p>{a.message}</p>
            <button className="link" onClick={() => onGo(a.chainage_km)}>Show on map</button>
          </li>
        ))}
      </ul>
    </div>
  );
}

const SPLIT_LABEL: Record<string, string> = {
  train: "Data used to tune the model",
  later_years: "Later years, never used in tuning",
  unseen_stations: "Stations never used in tuning",
};
const VAR_LABEL: Record<string, string> = { do: "Dissolved oxygen (mg/L)", bod: "BOD (mg/L)", fc: "Fecal coliform (×, factor)" };

export function SkillPanel({ skill, investment }: { skill: Skill | null; investment: Investment | null }) {
  if (!skill) return <div className="empty">Loading accuracy…</div>;
  const err = (v: string, rmse?: number) => (rmse === undefined ? "–" : v === "fc" ? `×${(10 ** rmse).toFixed(1)}` : rmse.toFixed(2));
  return (
    <div className="panel-body">
      <h2>How accurate is this?</h2>
      <p className="sub">
        Checked against {skill.counts.total.toLocaleString("en-IN")} laboratory measurements at {skill.counts.stations} stations, {skill.period[0].slice(0, 4)}–{skill.period[1].slice(0, 4)}. Tuned on data before {skill.split_date.slice(0, 4)}; {skill.counts.unseen_stations.length} stations were held out entirely.
      </p>
      {Object.keys(SPLIT_LABEL).map((split) => (
        <section key={split} className="skill">
          <h3>{SPLIT_LABEL[split]}</h3>
          <table>
            <thead>
              <tr>
                <th>Parameter</th>
                <th>Checks</th>
                <th>Typical error</th>
                <th>Using past averages instead</th>
                <th>Inside 90% range</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(VAR_LABEL).map((v) => {
                const m = skill.model[split]?.[v];
                const b = skill.climatology_baseline[split]?.[v];
                const cover = skill.coverage_90[split]?.[v];
                const better = m?.rmse !== undefined && b?.rmse !== undefined && m.rmse <= b.rmse;
                return (
                  <tr key={v}>
                    <td>{VAR_LABEL[v]}</td>
                    <td>{m?.n ?? 0}</td>
                    <td className={better ? "good" : ""}>{err(v, m?.rmse)}</td>
                    <td className={!better ? "good" : ""}>{err(v, b?.rmse)}</td>
                    <td>{cover === undefined ? "–" : `${Math.round(cover * 100)}%`}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ))}
      <p className="fine">
        "Typical error" is the root-mean-square difference between model and measurement; for coliform it is a multiplying factor (×2 means typically within a factor of two). "Using past averages" predicts each month from that station's own past measurements, with no model: a hard benchmark where a station has history, but one that cannot forecast a storm, a spill or a what-if. Nitrate, ammonia and phosphorus have no monthly measurements and are not scored.
      </p>
      {investment && (
        <section className="skill">
          <h3>Has sewage investment helped?</h3>
          <table>
            <thead>
              <tr>
                <th>Below</th>
                <th>Parameter</th>
                <th>Measured 2016–19</th>
                <th>Measured 2023–26</th>
                <th>Model, with / without works</th>
              </tr>
            </thead>
            <tbody>
              {investment.clusters.flatMap((c) =>
                (["bod", "fc"] as const).map((v, i) => (
                  <tr key={c.name + v}>
                    <td>{i === 0 ? c.name : ""}</td>
                    <td>{v === "bod" ? "BOD" : "Fecal coliform"}</td>
                    <td>{fmt(c.observed[v]?.before, v)}</td>
                    <td>{fmt(c.observed[v]?.after, v)}</td>
                    <td>
                      {fmt(c.modelled[v]?.with_investment, v)} / {fmt(c.modelled[v]?.without_investment, v)}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
          <p className="fine">{investment.note}</p>
        </section>
      )}
    </div>
  );
}

export function ScenarioPanel({ meta, towns, places }: { meta: Meta; towns: Town[]; places: Place[] }) {
  const big = towns.filter((t) => Number(t.sewage_mld) >= 10);
  const [req, setReq] = useState<ScenarioRequest>({ kind: "plant_offline", town: "Kanpur Nagar", percent: -30, rain_mm: 60, day_offset: 1, tonnes_bod: 50, chainage_km: 600 });
  const [res, setRes] = useState<ScenarioResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dayIdx, setDayIdx] = useState(0);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.scenario(req);
      setRes(r);
      setDayIdx(r.days.length - 1);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const set = (patch: Partial<ScenarioRequest>) => setReq({ ...req, ...patch });
  const marks = places.filter((p) => ["Kanpur upstream", "Varanasi upstream", "Buxar", "Farrukhabad"].includes(p.name) || p.slug === "prayagraj_us");
  return (
    <div className="panel-body">
      <h2>What if…</h2>
      <p className="sub">Re-runs the model with one change and compares it with the current forecast.</p>
      <div className="form">
        <label>
          Scenario
          <select value={req.kind} onChange={(e) => set({ kind: e.target.value as ScenarioRequest["kind"] })}>
            <option value="plant_offline">A town's treatment plants go offline</option>
            <option value="load_change">Sewage load changes (long term)</option>
            <option value="rain">Heavy rain over a town</option>
            <option value="low_flow">River flow drops</option>
            <option value="spill">One-day spill</option>
          </select>
        </label>
        {(req.kind === "plant_offline" || req.kind === "load_change" || req.kind === "rain") && (
          <label>
            Town
            <select value={req.town} onChange={(e) => set({ town: e.target.value })}>
              {req.kind === "load_change" && <option value="">All towns</option>}
              {big.map((t) => (
                <option key={t.town} value={t.town}>
                  {t.town} ({Number(t.sewage_mld).toFixed(0)} MLD)
                </option>
              ))}
            </select>
          </label>
        )}
        {(req.kind === "load_change" || req.kind === "low_flow") && (
          <label>
            Change (%)
            <input type="number" min={-90} max={200} step={5} value={req.percent} onChange={(e) => set({ percent: Number(e.target.value) })} />
          </label>
        )}
        {req.kind === "rain" && (
          <label>
            Rain (mm in a day)
            <input type="number" min={10} max={300} step={10} value={req.rain_mm} onChange={(e) => set({ rain_mm: Number(e.target.value) })} />
          </label>
        )}
        {req.kind === "spill" && (
          <>
            <label>
              Where (km from Haridwar)
              <input type="number" min={0} max={meta.length_km} step={5} value={req.chainage_km} onChange={(e) => set({ chainage_km: Number(e.target.value) })} />
            </label>
            <label>
              Size (tonnes of BOD)
              <input type="number" min={1} max={2000} step={10} value={req.tonnes_bod} onChange={(e) => set({ tonnes_bod: Number(e.target.value) })} />
            </label>
          </>
        )}
        {(req.kind === "rain" || req.kind === "spill" || req.kind === "plant_offline") && (
          <label>
            Starting
            <select value={req.day_offset} onChange={(e) => set({ day_offset: Number(e.target.value) })}>
              <option value={0}>today</option>
              <option value={1}>tomorrow</option>
              <option value={2}>in 2 days</option>
            </select>
          </label>
        )}
        <button className="primary" onClick={run} disabled={busy}>
          {busy ? "Running…" : "Run scenario"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {res && (
        <>
          <section className="verdict neutral">
            <div>
              <strong>{res.description}</strong>
              {res.summary.map((s) => (
                <span key={s} className="verdict-why">{s}</span>
              ))}
            </div>
          </section>
          <label className="slider">
            Day shown: <b>{dayLabel(res.days[dayIdx])}</b>
            <input type="range" min={0} max={res.days.length - 1} value={dayIdx} onChange={(e) => setDayIdx(Number(e.target.value))} />
          </label>
          <ProfileChart title="BOD along the river" varKey="bod" info={meta.variables.bod} km={res.chainage_km} baseline={res.baseline.bod[dayIdx]} scenario={res.scenario.bod[dayIdx]} marks={marks} />
          <ProfileChart title="Dissolved oxygen" varKey="do" info={meta.variables.do} km={res.chainage_km} baseline={res.baseline.do[dayIdx]} scenario={res.scenario.do[dayIdx]} marks={marks} />
          <ProfileChart title="Fecal coliform" varKey="fc" info={meta.variables.fc} km={res.chainage_km} baseline={res.baseline.fc[dayIdx]} scenario={res.scenario.fc[dayIdx]} marks={marks} log />
          {res.places.some((p) => p.arrival_days !== null) && (
            <p className="fine">
              Travel time from the source: {res.places.filter((p) => p.arrival_days !== null).map((p) => `${p.name} ${p.arrival_days!.toFixed(1)} d`).join(" · ")}
            </p>
          )}
        </>
      )}
    </div>
  );
}

export function AskPanel() {
  const [q, setQ] = useState("");
  const [log, setLog] = useState<{ q: string; a: string; used?: string[] }[]>([]);
  const [busy, setBusy] = useState(false);
  const ask = async (text: string) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setQ("");
    try {
      const r = await api.ask(text);
      setLog((l) => [...l, { q: text, a: r.answer, used: r.used }]);
    } catch (e) {
      setLog((l) => [...l, { q: text, a: `Could not answer: ${String(e)}` }]);
    } finally {
      setBusy(false);
    }
  };
  const examples = ["Is it safe to bathe at Varanasi tomorrow?", "Where is the river worst today?", "How does Kanpur compare with Prayagraj this week?"];
  return (
    <div className="panel-body">
      <h2>Ask</h2>
      <p className="sub">Answers come from the current forecast and measurements, with the numbers quoted.</p>
      <div className="chat">
        {log.length === 0 && (
          <div className="examples">
            {examples.map((e) => (
              <button key={e} className="chip" onClick={() => ask(e)}>{e}</button>
            ))}
          </div>
        )}
        {log.map((m, i) => (
          <div key={i} className="turn">
            <p className="q">{m.q}</p>
            <p className="a">{m.a}</p>
            {m.used && m.used.length > 0 && <p className="fine">Looked up: {m.used.join(", ")}</p>}
          </div>
        ))}
        {busy && <p className="fine">Looking that up…</p>}
      </div>
      <form className="ask" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask about any place on the river" aria-label="Question" />
        <button className="primary" disabled={busy || !q.trim()}>Ask</button>
      </form>
    </div>
  );
}

export function AboutPanel({ meta }: { meta: Meta }) {
  return (
    <div className="panel-body prose">
      <h2>About the data</h2>
      <p>{meta.note}</p>
      <h3>What is measured and what is modelled</h3>
      <ul>
        <li><b>Measured:</b> dissolved oxygen, BOD and coliform, about once a month at ~58 stations on this stretch (Uttar Pradesh Pollution Control Board monthly reports and CPCB reports). Shown as dots.</li>
        <li><b>Modelled:</b> everything shown as a line or a colour on the map. A river model moves water and pollution downstream day by day, driven by river flow and weather, and is tuned to the measurements.</li>
        <li><b>River flow:</b> from the GloFAS global flood model (not gauges; measured Ganga discharge is not public). It ignores canal withdrawals, so dry-season flow above Kanpur is probably too high.</li>
        <li><b>Weather:</b> ERA5 history and Open-Meteo forecasts.</li>
        <li><b>Sewage:</b> town populations, sewage volumes and treatment capacity from the Uttar Pradesh Namami Gange inventory (2024); Bihar and Uttarakhand towns are partly estimated.</li>
      </ul>
      <h3>Limits to keep in mind</h3>
      <ul>
        <li>Nitrate, ammonia and phosphorus are low-confidence: there are no monthly measurements to tune them.</li>
        <li>The stretches above Bijnor and below Buxar have few measurements; ranges there are less reliable.</li>
        <li>In the dry season the forecast changes slowly; most of the uncertainty is about the model, not the weather.</li>
        <li>An unreported spill cannot be forecast. The scenario tab shows what one would do.</li>
      </ul>
      <p className="fine">Forecast issued {meta.issued_on}, run #{meta.run_id}, {meta.run.members ?? "?"} ensemble members. Model tuned {meta.run.calibrated ?? "–"}.</p>
    </div>
  );
}
