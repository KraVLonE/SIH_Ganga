-- Idempotent schema. Apply with: python -m ganga.cli migrate

CREATE TABLE IF NOT EXISTS nodes (
    id          SERIAL PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('node', 'confluence', 'flowpoint')),
    chainage_km DOUBLE PRECISION NOT NULL,      -- km downstream of Haridwar, along the river
    lon         DOUBLE PRECISION NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,
    flow_lon    DOUBLE PRECISION,               -- GloFAS grid cell chosen for this node
    flow_lat    DOUBLE PRECISION,
    note        TEXT,
    geom        geometry(Point, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS reaches (
    id          SERIAL PRIMARY KEY,
    from_node   INT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    to_node     INT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    length_km   DOUBLE PRECISION NOT NULL,
    geom        geometry(LineString, 4326) NOT NULL,
    UNIQUE (from_node, to_node)
);

-- Monitoring stations (where observations are taken); not the same as model nodes.
CREATE TABLE IF NOT EXISTS stations (
    code        TEXT PRIMARY KEY,               -- CPCB station code
    name        TEXT NOT NULL,
    river       TEXT,
    state       TEXT,
    lon         DOUBLE PRECISION,
    lat         DOUBLE PRECISION,
    chainage_km DOUBLE PRECISION,               -- NULL if not on the modelled main stem
    offset_km   DOUBLE PRECISION,               -- distance from the centreline
    geom        geometry(Point, 4326)
);

CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,           -- e.g. file path or API name
    kind        TEXT NOT NULL,
    url         TEXT,
    fetched_at  TIMESTAMPTZ DEFAULT now(),
    note        TEXT
);

-- Daily driver series at nodes. member 0 = deterministic/control, 1..N = ensemble members.
CREATE TABLE IF NOT EXISTS timeseries (
    node_id     INT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    variable    TEXT NOT NULL,                  -- flow | rain | air_temp | solar
    source      TEXT NOT NULL,                  -- glofas | era5 | openmeteo_forecast
    member      SMALLINT NOT NULL DEFAULT 0,
    time        DATE NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (node_id, variable, source, member, time)
);
SELECT create_hypertable('timeseries', 'time', if_not_exists => TRUE,
                         chunk_time_interval => INTERVAL '1 year');

-- Real measured water quality. Never holds modelled or synthetic values.
CREATE TABLE IF NOT EXISTS observations (
    id            BIGSERIAL PRIMARY KEY,
    station_code  TEXT NOT NULL REFERENCES stations(code),
    variable      TEXT NOT NULL,                -- do | bod | fc | tc | ph | nitrate | ...
    time          DATE NOT NULL,                -- sampling date, or first of month if only month known
    resolution    TEXT NOT NULL CHECK (resolution IN ('day', 'month', 'year_min', 'year_max')),
    value         DOUBLE PRECISION NOT NULL,
    below_detection BOOLEAN NOT NULL DEFAULT FALSE,
    source_id     INT NOT NULL REFERENCES sources(id),
    source_page   INT,
    UNIQUE (station_code, variable, time, resolution, source_id)
);
CREATE INDEX IF NOT EXISTS observations_station_var_time ON observations (station_code, variable, time);

CREATE TABLE IF NOT EXISTS forecast_runs (
    id          SERIAL PRIMARY KEY,
    issued_on   DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_version TEXT NOT NULL,
    params      JSONB,
    status      TEXT NOT NULL DEFAULT 'ok'
);

-- Model output summarised over the ensemble.
CREATE TABLE IF NOT EXISTS forecasts (
    run_id      INT NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
    chainage_km DOUBLE PRECISION NOT NULL,
    variable    TEXT NOT NULL,
    time        DATE NOT NULL,
    p05         DOUBLE PRECISION,
    p50         DOUBLE PRECISION NOT NULL,
    p95         DOUBLE PRECISION,
    PRIMARY KEY (run_id, variable, chainage_km, time)
);

CREATE TABLE IF NOT EXISTS alerts (
    id          BIGSERIAL PRIMARY KEY,
    run_id      INT REFERENCES forecast_runs(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    chainage_km DOUBLE PRECISION,
    valid_on    DATE,
    message     TEXT NOT NULL,
    details     JSONB,
    delivered   BOOLEAN NOT NULL DEFAULT FALSE
);

-- One value per station/variable/time: where sources overlap, prefer dated samples, then the
-- state board's monthly report, then CPCB's map snapshot.
CREATE OR REPLACE VIEW obs_best AS
SELECT DISTINCT ON (o.station_code, o.variable, o.time, o.resolution)
    o.station_code, o.variable, o.time, o.resolution, o.value, o.below_detection,
    s.kind AS source_kind, s.key AS source_key, o.source_page
FROM observations o JOIN sources s ON s.id = o.source_id
ORDER BY o.station_code, o.variable, o.time, o.resolution,
    CASE s.kind WHEN 'cpcb_interstate' THEN 1 WHEN 'uppcb_monthly' THEN 2
                WHEN 'cpcb_wqi_map' THEN 3 ELSE 4 END;
