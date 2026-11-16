# Data audit (Phase 0) — 2026-11-16

Question: is there enough **real** observed water-quality data to calibrate a model? **Yes — gate passed**, mainly because of the UP board's monthly station reports.

Counts below are from files actually downloaded into `data/raw/` and sampled; "est." marks numbers inferred from file listings rather than parsed.

## Observed water quality

| Source | What it is | Coverage | Parameters | Format / effort | Status |
|---|---|---|---|---|---|
| **UPPCB monthly "NWMP" reports** | One value per station per month | 32 Ganga main-stem stations, Bijnor → Ghazipur, plus tributaries (Ramganga, Kali, Yamuna, Gomti, Varuna…); ~90 station codes per file | DO, BOD, total coliform, fecal coliform, class | PDFs. 2016–2019: text in legacy Hindi font (numbers and station codes readable). ~2020–2023: scanned typed tables, no text layer → OCR. 2024+: scans with a rough text layer | ~62 distinct monthly files 2016 → Feb 2024 in the Wayback Machine (est. from listing); Jan–Sep 2026 live on `uppcb.up.gov.in`. **Mar 2024 – Dec 2025 not yet located** |
| **CPCB interstate-boundary reports** | Dated samples, about twice a month | Buxar (UP/Bihar), Bihar exit, Jharkhand entry/exit, West Bengal entry; UP/Uttarakhand boundary stations only quarterly | DO, pH, conductivity, BOD, fecal + total coliform | 12 PDFs, Mar 2019 → Dec 2024; 9 have clean text tables, 2 are scans, 1 is a single-page summary | Downloaded. **575 dated sample rows** matched in the 8 parseable files |
| **CPCB yearly river data** | Annual min and max per station | ~108 Ganga-named rows per year, Devprayag → West Bengal; yearly files 2012 → 2023 | Temperature, DO, pH, conductivity, BOD, **nitrate**, fecal + total coliform, fecal streptococci | Text PDFs, clean | 2023 downloaded and inspected; other years listed, not yet downloaded |
| CPCB real-time sensor network | Continuous sensors on the Ganga | 36+ stations | 17 parameters | Web portal | **Not checked** — portal responds, but my attempt to inspect it was blocked by the session's permission layer; needs your OK |
| NGT compliance annexures | Raw monthly tables by state | Uttarakhand, West Bengal, 2024–2025 | DO, BOD, coliform etc. | PDFs on greentribunal.gov.in (site reachable) | Not downloaded yet; Phase 2 |

## Drivers (all verified live)

| Need | Source | Result |
|---|---|---|
| Rain, temperature — history and forecast | Open-Meteo forecast + archive | Returned data for Varanasi |
| River discharge — history and forecast | Open-Meteo Flood API (GloFAS) | Returned 50 ensemble members, past + 7-day forecast |

## What this means for the build

- **Calibrated core:** Bijnor → Ghazipur/Buxar, where there are ~32 stations with monthly DO, BOD and fecal coliform over roughly 8–10 years. This is far above the gate threshold (8 stations × 3 years).
- **Thinner ends:** Haridwar → Bijnor and Buxar → Patna rely on CPCB annual min/max plus the interstate fortnightly samples at Buxar. Forecasts there will carry wider bands.
- **Nitrate:** no monthly observations anywhere found so far — only annual min/max. It can be modelled and loosely constrained, not properly calibrated. Ammonia and phosphorus have no observations at all in these sources.
- **Flow:** modelled (GloFAS) only; no measured discharge to check against.
- **OCR is required** for roughly the 2020–2023 UP files. The scans are clean typed tables (viewed one: September 2022), so digits and station codes should read well. `tesseract` is not installed on this machine.
- **Station names:** UP files are in Hindi; station codes match the CPCB yearly file, which gives English names. Coordinates are in the interstate reports for boundary stations only; the rest need geocoding from the name plus a manual check.

## Access notes

- `cpcb.nic.in` refused connections from this machine today (TCP timeout on 80 and 443). All CPCB files were fetched from Wayback Machine copies of the same URLs. Wayback truncates large files at 1 MiB steps; downloads must be resumed with range requests until complete.
- `uppcb.com` (old domain) is dead; the board moved to `uppcb.up.gov.in`, which is reachable.

## Open items carried into Phase 2

1. Locate the UP monthly files for Mar 2024 – Dec 2025.
2. Install OCR tooling (needs `sudo apt install tesseract-ocr`, or a pip-only OCR library).
3. Decide whether to use the CPCB real-time portal (pending your permission to inspect it).
4. Look for Bihar and Uttarakhand state-board monthly files to thicken the two ends.

## Source URLs

- UP board monthly data page: https://uppcb.up.gov.in/en/page/river-water-quality-data-
- CPCB interstate reports index: https://cpcb.nic.in/wq-interstate/ (files `NGTMC/ganga_Interstate{,1..11}.pdf`)
- CPCB yearly data index: https://cpcb.nic.in/nwmp-data/ (2023 file: `wqm/2023/WQuality_River-Data-2023.pdf`)
- Wayback listing used for old UP files: `web.archive.org/cdx/search/cdx?url=uppcb.com/pdf/&matchType=prefix`
