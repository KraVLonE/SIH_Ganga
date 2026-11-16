# Ganga water-quality forecasting DSS (SIH PS 1694) — build plan

## Context
The 2024 hackathon attempt (now archived at `Projects/SIH-Old/`) was an Expo app over synthetic CSVs. This is a from-scratch rebuild in `Projects/SIH/` of what the problem statement actually asks for: daily flow + water-quality forecasts 3–5 days ahead along the Ganga, with probabilities, a map dashboard, alerts, scenario modelling and investment monitoring.

Hard constraint (checked today):
- **Works, free, no key:** Open-Meteo weather forecast/archive, and Open-Meteo Flood API (GloFAS discharge, 50-member ensemble, past + 7-day forecast) — both returned live data for Varanasi.
- **Sparse:** CPCB water-quality observations are monthly/fortnightly and mostly published as PDFs (NWMP tables, interstate-boundary reports, NGT compliance annexures). No confirmed public download for the real-time sensor network.
- **Not available:** measured Ganga discharge (classified); daily measured BOD/FC/nitrate.

So the core is a **process-based river model driven by real daily flow and weather, calibrated on sparse real observations**, with honest uncertainty bands. No synthetic data is ever presented as observed.

## Scope decisions (defaults — say if you want any changed)
- **River domain:** main stem Haridwar → Patna (~1,100 km), ~30 nodes, major tributaries (Ramganga, Kali, Yamuna, Gomti, Ghaghara, Son, Gandak) as boundary inflows. Built first on Kanpur → Varanasi, then extended.
- **Parameters:** flow, rainfall, water temp, DO, BOD, fecal coliform, nitrate (calibrated); ammonia, phosphorus (modelled, flagged low-confidence because few observations).
- **Client:** responsive web app installable on a phone (PWA) rather than a native app.
- **Stack:** Python 3.13 (venv + `pyproject.toml`), FastAPI, PostgreSQL + TimescaleDB + PostGIS in Docker Compose, a scheduler worker container (APScheduler), React + Vite + TypeScript + MapLibre + a charting lib. NumPy/SciPy for the model; geopandas/shapely for the network.
- **Repo:** `git init` in `Projects/SIH/`; I commit only when you say so. `docs/PLAN.md` (this plan) and `docs/PROGRESS.md` (phase checklist) keep state across sessions.

## Repo layout
```
SIH/
  docker-compose.yml  pyproject.toml  .env.example
  docs/        problem-statement.pdf, PLAN.md, PROGRESS.md, data-audit.md, model.md
  data/        raw/ (downloaded PDFs, gitignored)  curated/ (network, loads, obs CSVs — versioned)
  ganga/       db/  ingest/  network/  model/  calibrate/  forecast/  alerts/  api/
  web/         React app
  tests/
```

## Phases

### Phase 0 — Data audit and scaffold (2–3 days) — GATE
- Scaffold repo, Docker Compose with the database, config, test runner, lint.
- Download and inventory every CPCB source for the domain; count real station-month observations per parameter and year. Check the CPCB real-time portal for public access. Write `docs/data-audit.md`.
- **Done when:** the audit table exists and `docker compose up` gives a healthy database.
- **Gate:** if fewer than ~8 stations have ≥3 years of BOD/DO/FC, I stop and show you options (shrink domain, different stretch) before building anything on top.

### Phase 1 — River network and driver ingestion (week 1)
- Build the network from HydroRIVERS/OSM centreline: nodes with chainage (km), reaches, tributary junctions, upstream catchment per node. Stored in PostGIS and `data/curated/network.geojson`.
- Schema: `nodes`, `reaches`, `timeseries` (hypertable: node, variable, time, value, source, member), `observations`, `sources`, `forecast_runs`, `forecasts`, `alerts`.
- Ingestors (idempotent, backfill + daily): weather forecast and ERA5 history per node/catchment; GloFAS discharge history + 50-member forecast per node.
- **Done when:** 10 years of daily flow, rain and temperature for all nodes are in the DB, re-running an ingest adds zero duplicate rows, and flow at Varanasi shows a sane monsoon hydrograph.

### Phase 2 — Observations and pollution loads (week 2)
- PDF table extraction (pdfplumber/camelot) for CPCB sources → `observations`, each row keeping source file, page and station code; unit and below-detection-limit handling; manual spot-check of a sample against the PDFs.
- Loads inventory `data/curated/loads.csv`: per town/drain — population (Census 2011 projected), sewage generated, treatment capacity and commissioning dates (NMCG/CPCB reports), discharge chainage. Each row cites its source.
- Sentinel-2 via a free STAC endpoint (no account): cloud-masked turbidity and chlorophyll indices at node water pixels → `timeseries` as a satellite layer.
- **Done when:** observations match the PDFs on a 50-row random check, loads cover every class-I town on the stretch, and satellite turbidity rises in monsoon as expected.

### Phase 3 — Process model (week 3)
- `ganga/model/`: daily 1D routing down the network. Travel time from flow via hydraulic geometry (velocity = a·Q^b, depth = c·Q^d). Mass balance at each load and junction (dilution).
- Kinetics per reach, temperature-corrected: BOD decay + settling; DO with saturation, reaeration (O'Connor–Dobbins) and BOD/nitrification demand (Streeter–Phelps form); fecal coliform first-order die-off; ammonia → nitrate nitrification; phosphorus settling.
- Loads: continuous point loads from the inventory; rainfall-triggered wash-off/sewer-overflow term so storms produce pollution pulses.
- **Done when:** unit tests pass against analytical solutions (single reach Streeter–Phelps sag, pure dilution, conservative tracer mass balance within 0.1%), and a 10-year historical simulation runs in seconds.

### Phase 4 — Calibration, validation, uncertainty (week 4)
- Calibrate rate constants and load multipliers against observations (log-space error for coliform), using SciPy global optimisation; calibrate on earlier years, validate on held-out years and held-out stations.
- Uncertainty: keep the set of parameter draws that fit acceptably (GLUE), run each against the 50 flow members → percentile bands and exceedance probabilities.
- Optional gradient-boosted residual correction; kept only if it improves held-out skill.
- `docs/model.md`: equations, parameters, skill table (NSE/KGE/bias per parameter per station), and where the model is weak.
- **Done when:** skill table exists for held-out data, the 90% band contains roughly 90% of held-out observations, and results beat a "monthly climatology" baseline — or the doc says plainly where they don't.

### Phase 5 — Forecast pipeline, API, alerts (week 5)
- Daily job: ingest → run model for past 10 days + next 5 days with ensemble → store a `forecast_run`. Every stored forecast is later scored against new observations (forecast-vs-observed table; periodic recalibration job).
- FastAPI: nodes, history, forecast with bands, any-point query (interpolate by chainage), observations, run metadata, scenario run, alerts.
- Alert rules: probability of breaching bathing-water limits (DO < 5, BOD > 3 mg/L, FC > 2500 MPN/100 mL), pulse detection moving downstream with arrival-time estimate, storm overflow risk. Delivery: in-app + Telegram bot (email optional).
- **Done when:** the daily job runs unattended for 3 consecutive days, API tests pass, and a seeded synthetic spill in a test produces an alert with a downstream arrival time.

### Phase 6 — Dashboard (weeks 5–6)
- Map of the river coloured by chosen parameter and day (slider: −10 to +5 days), stations and load points as layers, satellite turbidity layer.
- Node panel: time series of rainfall, flow, DO, BOD, FC, nitrate — past 10 days + forecast with bands, observed points overlaid, bathing suitability with probability.
- Alerts page, model-skill page (honest accuracy), data-provenance labels everywhere ("modelled" vs "observed").
- **Done when:** I've driven it in a browser on desktop and phone width, every chart traces to an API call, and no console errors.

### Phase 7 — Decision-support features (week 6)
- Scenario tool: treatment plant offline, load reduction %, rainfall event, low-flow — reruns the model live and shows the difference along the river.
- Investment monitoring (PS item 3): for each treatment plant commissioned in the data window, observed before/after at the downstream station (flow-adjusted) next to the modelled with/without counterfactual.
- Query assistant ("adaptive, query based"): chat box that answers questions by calling the API as tools via Claude. Needs an Anthropic API key from you; skipped if you'd rather not.
- **Done when:** each scenario type changes results in the physically expected direction, and the assistant's answers quote numbers that match the API.

### Phase 8 — Deploy and document (2–3 days)
- Production Compose, backups, README with architecture diagram, data sources, limitations, screenshots.
- Deploy to your Oracle server behind Traefik (e.g. `ganga.kravlone.xyz`) — I will ask before touching the server, and update the Notion services registry for any new timer/unit.
- **Done when:** public URL serves today's forecast and the daily job has run there on its own.

## How we work through it
- One phase at a time; at the end of each I show what was built, the "done when" evidence, and anything that failed, then wait for your go-ahead.
- Tests and lint run after every change; failures reported verbatim.
- Phase 0's gate and the Phase 4 skill table are the two points where the plan may need to change based on what the data says.

## Verification (end to end)
`docker compose up` → `pytest` green → run backfill + one forecast run → open the dashboard, pick Varanasi, see 10 days history + 5 days forecast with bands and observed points → trigger a "plant offline" scenario and see BOD rise downstream → check the skill page numbers match `docs/model.md`.

## Sources checked
- CPCB water quality monitoring overview for the Ganga: https://cpcb.nic.in/ngrba/WQM_overvew.php
- CPCB interstate-boundary monthly report example: https://cpcb.nic.in/NGTMC/ganga_Interstate7.pdf
- Down To Earth on CPCB publishing only monthly data: https://www.downtoearth.org.in/amp/story/environment/sewage-overload-why-the-ganga-remains-polluted-despite-cleanliness-drives-88651
- Open-Meteo Flood API (probed live): https://flood-api.open-meteo.com/v1/flood
