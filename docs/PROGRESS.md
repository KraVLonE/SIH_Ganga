# Progress

Plan: `docs/PLAN.md`. On 2026-11-16 the user asked for all phases to run without per-phase approval, and for **no commits until they approve**.

- [x] **Phase 0 — Data audit and scaffold** (2026-11-16). Gate passed. See `docs/data-audit.md`.
  - DB: `docker compose up -d` → TimescaleDB + PostGIS on `127.0.0.1:5544`. API will use port 8010 (8000 is taken by the portfolio backend).
  - `pytest` 2 passed, `ruff` clean.
- [x] **Phase 1 — River network and driver ingestion** (2026-11-16).
  - Commands: `python -m ganga.cli migrate | network | pick-cells | backfill | daily` (use `.venv/bin/python`).
  - Network: OSM relation 1236345 centreline; Haridwar → Patna = 1,369 km; 26 named nodes, 7 confluences, 14 flow points (8 km above/below each confluence). `data/curated/network.geojson`.
  - Stations: 107 CPCB Ganga stations with coordinates from the public suitability map (`data/raw/cpcb/wqi/`), 58 on the modelled main stem with chainage.
  - Drivers in DB: flow (GloFAS) at 40 points 2016-01-01 → today + 7-day forecast with 50 members; rain, air temperature, solar at 26 nodes (ERA5 history, forecast model for the last week + 7 days ahead).
  - Checks: re-running backfill/daily leaves row counts unchanged; Varanasi mean flow ~560 m3/s in Mar–Apr, ~14,000 in Aug; 11 tests pass.
- [x] **Phase 2 — Observations and pollution loads** (2026-11-16).
  - Commands: `python -m ganga.cli observations | loads | satellite`. Report download + OCR: `ganga.ingest.uppcb_manifest.fetch_all()`, `ganga.ingest.uppcb_run.parse_all()` (cached per file in `data/curated/uppcb_parsed/`).
  - UP monthly reports: 126 files found (32 on the board's site for Feb 2024 – Sep 2026, 94 archived candidates for 2015 – early 2024); 107 yielded rows; **106 months, 28,568 values** loaded (DO, BOD, total and fecal coliform). Read with RapidOCR on a line-removed page image, tesseract only for missed station codes and Hindi month headers.
  - Other observations: CPCB interstate reports (3,652 dated values, 10 stations), CPCB yearly min/max for 2016, 2019, 2020, 2023 (the only nitrate), CPCB map snapshot (latest month at 107 stations).
  - OCR check: 93–99.6% of monthly values fall inside CPCB's independently published yearly min–max for the same station and year (about 8,200 comparisons); two pages also compared by eye. The planned 50-row manual check was replaced by this automated one.
  - Loads: `data/curated/loads.csv`, 30 towns. UP towns from the state Namami Gange drains/treatment table (official, 2024). Haridwar and Patna treatment capacity from NGT filings; Bihar populations and sewage are estimates and flagged.
  - Satellite: 2,112 node-month Sentinel-2 samples (2019–2026). Red reflectance over water rises from 0.055 in February to 0.18 in July, as expected for monsoon sediment.
  - Gaps: 2021 has only 3 months (8 archive copies are cut off at 1 MiB; 5 partly salvaged); a few 2016 files and March 2025 gave nothing. CPCB yearly files for 2017, 2018, 2021 have a different layout and are not parsed.
- [x] **Phase 3 — Process model** (2026-11-16). `ganga/model/core.py`, `setup.py`; see `docs/model.md`.
  - Closed-form checks pass (decay, Streeter–Phelps, dilution, mass balance, pulse travel, diffuse equilibrium). A 10-year run takes about 1.5 s.
  - Added beyond the plan: diffuse bank-side load, algal oxygen production, and fitted 2016→2024 load trends.
- [x] **Phase 4 — Calibration, validation, uncertainty** (2026-11-16). `python -m ganga.cli calibrate`; results in `data/curated/calibration.json`, `skill.json`; write-up in `docs/model.md`.
  - 18,176 model runs; 139 behavioural parameter sets. 10,152 measurements at 58 stations, 4,334 used for fitting; 2023+ and 12 stations held out.
  - Held-out RMSE, model vs station-climatology baseline: later years DO 1.01 vs 0.92, BOD 0.98 vs 1.10, coliform 0.51 vs 0.49 (log10); unseen stations DO 0.91 vs 0.83, BOD 1.57 vs 1.59, coliform 0.47 vs 0.57. So: about level with the baseline, not clearly better. NSE is low (0.1–0.4; BOD slightly negative).
  - 90% band holds 93–97% of held-out measurements (slightly wide).
  - The optional machine-learning residual correction was not built.
- [x] **Phase 5 — Forecast pipeline, API, alerts** (2026-11-16). `python -m ganga.cli daily | forecast | verify | serve`.
  - First forecast issued for 2026-11-16 (run 1, 96 ensemble members, 0 alerts). Forecast takes about 2 s.
  - Alerts: pulse, deterioration, storm, breach; Telegram delivery if configured. Test: a seeded spill raises a pulse alert with downstream arrival times.
  - **Not met:** "daily job runs unattended for 3 consecutive days" — only one run exists so far; the scheduler (`scripts/scheduler.py`) is written but has not been left running.
- [x] **Phase 6 — Dashboard** (2026-11-16). `web/`; served by `ganga.cli serve` on port 8010.
  - Checked in Chrome at desktop width: map, day slider, 15-day and history charts, scenario, accuracy; no console errors.
  - **Not verified:** phone-width layout (the browser window would not resize); the Alerts tab with real alerts (none exist yet); the Ask and Data tabs were not opened in the browser.
- [x] **Phase 7 — Decision-support features** (2026-11-16).
  - Scenarios (`ganga/scenario.py`): plant offline, load change, rain, low flow, spill; each tested to move in the expected direction.
  - Investment analysis (`data/curated/investment.json`): measured 2016–19 vs 2023–26 below Kanpur, Prayagraj, Varanasi, next to the model with loads held at their 2016 level.
  - Ask (`ganga/assistant.py`): Claude with lookup tools when credentials exist; **the Claude path has not been run** (no API key here). The built-in fallback is tested against the API's numbers.
- [ ] **Phase 8 — Deploy and document.** README, Dockerfile, `docker-compose.prod.yml`, daily and backup scripts are written. **Not done:** the image has not been built, nothing is deployed, and the Notion services registry is untouched — deployment to the Oracle server needs the user's go-ahead.

## Changes to the plan from what the data showed
- Calibrated core is Bijnor → Ghazipur/Buxar (UP monthly data), not just Kanpur → Varanasi.
- Nitrate has only annual min/max observations: modelled, loosely constrained.
- Phase 2 needs OCR for ~2020–2023 UP reports.
- Model loads are a 2024 inventory with fitted 2016→2024 trend factors, because measured BOD and coliform fell over the period.
- Basemap is OpenStreetMap raster tiles (Carto's now require a key).

## Known approximations (revisit in Phase 3)
- Confluence positions are approximate; in GloFAS the Ramganga, Gomti and Ghaghara jumps appear upstream of the 8 km flow points, and the Son's appears between `son_dn` and Patna. Tributary inflow should be taken as the flow increase between consecutive flow points and placed where the jump is, with positions refined from the "before/after confluence" CPCB stations.
- GloFAS has no canal withdrawals (Haridwar, Bijnor, Narora), so dry-season flow between Haridwar and Kanpur is probably too high.
- The flow ensemble has almost no spread in the dry season; forecast uncertainty there must come from model parameters.
- Weather is taken at each node, not averaged over its catchment (flow already integrates the catchment).
- CPCB lists station 30076 "Manjhighat, Chhapra" at coordinates near Ghazipur (chainage 1168); treat its location as suspect.

## Resolved
- OCR: `tesseract` with `eng` + `hin` installed.
- CPCB portals: the public suitability map is usable (station coordinates + latest month, should be snapshotted regularly). The real-time sensor dashboard is a JavaScript app, not inspected further. The data-entry portal is login-only and out of bounds.
