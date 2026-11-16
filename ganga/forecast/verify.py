"""Score stored forecasts against measurements that arrive later.

Measurements are monthly, so each one is compared with the average of the "today" estimates
from every forecast run issued in that month, at the station's location.
"""

CHECKS = """
CREATE TABLE IF NOT EXISTS forecast_checks (
    station_code TEXT NOT NULL,
    variable     TEXT NOT NULL,
    month        DATE NOT NULL,
    observed     DOUBLE PRECISION NOT NULL,
    predicted    DOUBLE PRECISION NOT NULL,
    p05          DOUBLE PRECISION,
    p95          DOUBLE PRECISION,
    runs         INT NOT NULL,
    inside_band  BOOLEAN,
    PRIMARY KEY (station_code, variable, month)
);
"""

SCORE = """
INSERT INTO forecast_checks
SELECT o.station_code, o.variable, date_trunc('month', o.time)::date,
       avg(o.value), avg(f.p50), avg(f.p05), avg(f.p95), count(DISTINCT r.id),
       avg(o.value) BETWEEN avg(f.p05) AND avg(f.p95)
FROM obs_best o
JOIN stations s ON s.code = o.station_code AND s.chainage_km IS NOT NULL
JOIN forecast_runs r ON date_trunc('month', r.issued_on) = date_trunc('month', o.time)
JOIN LATERAL (
    SELECT p05, p50, p95 FROM forecasts
    WHERE run_id = r.id AND variable = o.variable AND time = r.issued_on
    ORDER BY abs(chainage_km - s.chainage_km) LIMIT 1
) f ON TRUE
WHERE o.variable IN ('do', 'bod', 'fc') AND o.resolution IN ('month', 'day')
GROUP BY 1, 2, 3
ON CONFLICT (station_code, variable, month) DO UPDATE SET
    observed = EXCLUDED.observed, predicted = EXCLUDED.predicted, p05 = EXCLUDED.p05,
    p95 = EXCLUDED.p95, runs = EXCLUDED.runs, inside_band = EXCLUDED.inside_band
"""


def verify(conn) -> dict:
    """Refresh the checks table; returns counts and how often the band held."""
    conn.execute(CHECKS)
    conn.execute(SCORE)
    conn.commit()
    rows = conn.execute(
        "SELECT variable, count(*), avg(inside_band::int) FROM forecast_checks GROUP BY 1"
    ).fetchall()
    return {
        v: {"checks": n, "inside_band": float(share) if share is not None else None}
        for v, n, share in rows
    }
