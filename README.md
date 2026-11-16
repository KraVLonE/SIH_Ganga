# Ganga water quality forecast

A decision-support system for the Ganga between Haridwar and Patna (1,369 km): daily estimates and 5-day forecasts of dissolved oxygen, BOD and fecal coliform along the river, with uncertainty ranges, alerts, what-if scenarios and a dashboard. Built for Smart India Hackathon problem statement 1694 (National Mission for Clean Ganga) — see `docs/problem-statement.pdf`.

**What the numbers are:** model estimates tuned to monthly laboratory measurements. They are not sensor readings. Accuracy is reported in the dashboard's Accuracy tab and in `docs/model.md`; on held-out data the model is about level with predicting from each station's past averages, and its value is in covering unmonitored places, responding to flow and rain, and answering what-if questions.

## How it fits together

```
 Open-Meteo (GloFAS flow, ERA5 + forecast weather) ──┐
 UP Pollution Control Board monthly PDFs ── OCR ─────┤
 CPCB interstate / yearly reports, CPCB map ─────────┼─► PostgreSQL + TimescaleDB + PostGIS
 UP Namami Gange town/treatment table, NGT filings ──┤          │
 Sentinel-2 (turbidity proxy) ───────────────────────┘          ▼
                                             1-D river model (5 km cells, daily step)
                                              │ calibrate (differential evolution)
                                              ▼
                       ensemble forecast (parameter sets × 50 flow members + residual noise)
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                  alerts (Telegram)     FastAPI  /api/*       scenarios, hindcast,
                                              │               investment analysis
                                              ▼
                                React + MapLibre dashboard
```

| Path | What |
|---|---|
| `ganga/network/` | river centreline (OpenStreetMap), nodes, chainage |
| `ganga/ingest/` | drivers, PDF/OCR parsing, loads inventory, satellite |
| `ganga/model/` | the river model (`core.py`) and its forcing (`setup.py`) |
| `ganga/calibrate/` | targets, optimiser, skill and uncertainty |
| `ganga/forecast/` | ensemble forecast, hindcast, verification |
| `ganga/alerts/`, `ganga/scenario.py`, `ganga/assistant.py` | alerts, what-if runs, question answering |
| `ganga/api/` | HTTP API |
| `web/` | dashboard |
| `data/curated/` | versioned inputs and results (network, loads, parsed reports, calibration, skill) |
| `data/raw/` | downloaded source files (not in git) |
| `docs/` | plan, progress, data audit, model description |

## Run it locally

```sh
docker compose -f docker-compose.dev.yml up -d   # database on 127.0.0.1:5544
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m ganga.cli migrate
.venv/bin/python -m ganga.cli network      # needs data/raw/osm/ganga.json
.venv/bin/python -m ganga.cli pick-cells
.venv/bin/python -m ganga.cli backfill     # flow + weather since 2016
.venv/bin/python -m ganga.cli observations # load parsed measurements
.venv/bin/python -m ganga.cli daily        # latest days + forecasts
.venv/bin/python -m ganga.cli forecast     # issue today's forecast and alerts
(cd web && npm install && npm run build)
.venv/bin/python -m ganga.cli serve        # http://127.0.0.1:8010
```

Other commands: `loads`, `satellite`, `calibrate` (about an hour on 12 cores), `hindcast`, `verify`. Rebuilding the measurements from source PDFs needs `tesseract-ocr` with Hindi (`tesseract-ocr-hin`) and takes about an hour of OCR; the parsed results are already in `data/curated/uppcb_parsed/`.

Tests and lint: `.venv/bin/pytest` and `.venv/bin/ruff check .`

## Deploy

`docker-compose.yml` is the server stack: the app (API + dashboard) behind a shared Traefik proxy at `ganga.kravlone.xyz`, a worker that runs `scripts/daily.sh` at 06:30 IST, and the database on a private network. It needs a `.env` beside it with `POSTGRES_PASSWORD` (see `.env.example`).

```sh
# on a machine that already has the data loaded
.venv/bin/python -m ganga.cli seed-export seed        # CSVs of network, drivers, measurements

# on the server
git clone https://github.com/KraVLonE/SIH_Ganga.git ganga && cd ganga
umask 077; printf "POSTGRES_PASSWORD=%s\n" "$(openssl rand -hex 24)" > .env
docker compose up -d --build --wait db
docker compose run --rm -v ./seed:/seed app python -m ganga.cli seed-import /seed   # after copying seed/ over
docker compose run --rm app sh scripts/daily.sh       # first forecast
docker compose up -d --build
```

Optional settings in `.env`:

- `GANGA_GEMINI_API_KEY` or `ANTHROPIC_API_KEY` — the Ask tab answers with Gemini (checked first) or Claude, using lookup tools over the forecast; with neither, or when the provider fails or is out of quota, a built-in responder handles place and "where is it worst" questions.
- `GANGA_TELEGRAM_TOKEN`, `GANGA_TELEGRAM_CHAT` — send alert digests to Telegram.

## Data sources

| Data | Source | Notes |
|---|---|---|
| River flow, history and forecast | Open-Meteo Flood API (GloFAS) | modelled, 50-member forecast; no canal withdrawals |
| Weather | Open-Meteo (ERA5 archive, forecast) | rain, air temperature, sunlight |
| Monthly DO, BOD, coliform | UP Pollution Control Board river reports, 2016–2026 | scanned PDFs read by OCR; 106 months |
| Dated samples at state boundaries | CPCB interstate reports, 2019–2024 | |
| Yearly min/max incl. nitrate | CPCB yearly river data | 2016, 2019, 2020, 2023 parsed |
| Station coordinates, latest month | CPCB "Suitability of River Ganga Water" map | |
| Towns, sewage, treatment | UP Namami Gange drains/treatment table (2024); NGT filings | Bihar partly estimated |
| River line | OpenStreetMap | |
| Turbidity proxy | Sentinel-2 L2A via Earth Search | display and sanity check only |

Full audit, with what could not be obtained: `docs/data-audit.md`.

## Limits

See "Known weaknesses" in `docs/model.md`. In short: flow is modelled not gauged; observations are monthly; the big-city hotspots (Kanpur especially) are under-predicted; nitrate, ammonia and phosphorus are low-confidence; the stretches above Bijnor and below Buxar have little data.
