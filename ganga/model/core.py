"""1-D river water-quality model: daily transient transport with first-order kinetics.

State is carried as mass flux (flow x concentration) on a fixed grid of cells along the river.
Each day, the water arriving at a cell is traced back one day along the flow (semi-Lagrangian):

    flux(i, t) = flux(source of i, t-1) x decay over the path
               + sum over loads entering along the path, each decayed from its entry point

Cells less than a day's travel from the upstream boundary take the boundary inflow instead.
Because loads and decay are handled analytically along the path, the scheme is exact for steady
first-order problems and has no stability limit, so a one-day step works even when water
travels many cells per day.

Constituents
    bod   carbonaceous BOD (mg/L)        decays (kd) and settles (ks)
    nh4   ammonia-N (mg/L)               nitrifies (kn)
    tin   ammonia + nitrate N (mg/L)     lost slowly by denitrification; nitrate = tin - nh4
    tp    total phosphorus (mg/L)        settles
    fc    fecal coliform (MPN/100 mL)    dies off (temperature and sunlight)
    do    dissolved oxygen (mg/L)        deficit driven by BOD decay, nitrification and
                                         sediment demand, relieved by reaeration

Units: flow m3/s, length km, time days, loads as concentration x flow (mg/L x m3/s = g/s;
for coliform, MPN/100 mL x m3/s).
"""

from dataclasses import dataclass, field, fields

import numpy as np

SPECIES = ("bod", "nh4", "tin", "tp", "fc")
O2_PER_N = 4.57  # g oxygen used per g ammonia-N nitrified
DAY = 86400.0


@dataclass
class Params:
    # Hydraulics: velocity and depth as power laws of flow, referenced to 1000 m3/s.
    u_ref: float = 0.6  # m/s at 1000 m3/s
    h_ref: float = 4.0  # m at 1000 m3/s
    u_exp: float = 0.4
    h_exp: float = 0.4
    # Rates at 20 C (per day) and their temperature coefficients.
    kd: float = 0.30  # BOD decay
    ks: float = 0.10  # BOD settling
    kn: float = 0.25  # nitrification
    kdn: float = 0.02  # denitrification loss of inorganic N
    kp: float = 0.05  # phosphorus settling
    kfc: float = 1.0  # coliform die-off in the dark
    kfc_light: float = 0.02  # extra die-off per (MJ/m2/day of sunlight) per metre depth
    ka_scale: float = 1.0  # multiplier on O'Connor-Dobbins reaeration
    sod: float = 0.5  # sediment oxygen demand, g O2/m2/day
    theta_kd: float = 1.047
    theta_kn: float = 1.083
    theta_ka: float = 1.024
    theta_fc: float = 1.07
    theta_sod: float = 1.065
    # Water temperature from air temperature: Tw = tw_a + tw_b x Ta.
    tw_a: float = 5.0
    tw_b: float = 0.75
    # Sewage strength.
    raw_bod: float = 180.0  # mg/L in untreated sewage
    raw_nh4: float = 30.0
    raw_tp: float = 5.0
    raw_fc: float = 1.0e7  # MPN/100 mL
    eff_bod: float = 25.0  # mg/L in treated effluent
    eff_nh4: float = 12.0
    eff_no3: float = 8.0
    eff_tp: float = 3.0
    eff_fc: float = 5.0e4
    # Storms: share of treated flow that bypasses treatment per mm of rain above a threshold,
    # and street/land wash-off per mm of rain per 100,000 people.
    storm_threshold: float = 10.0  # mm/day
    storm_bypass: float = 0.02  # fraction per mm above threshold (capped at 1)
    wash_bod: float = 40.0  # kg BOD per mm rain per 100,000 people
    wash_fc_ratio: float = 3.0e4  # coliform wash-off relative to BOD wash-off (MPN/100mL per mg/L)
    # Diffuse pollution entering all along the river (villages, livestock, unsewered areas),
    # as BOD per km of river; other species follow raw-sewage ratios. Rain increases it.
    diffuse_bod: float = 100.0  # kg BOD/day per km
    diffuse_fc_ratio: float = 2.0e4  # coliform per unit BOD (MPN/100 mL per mg/L)
    diffuse_rain: float = 0.05  # fractional increase per mm of rain
    # Net oxygen production by algae, g O2/m2/day per MJ/m2/day of sunlight.
    photo: float = 0.0
    # Loads from towns away from the bank reach the river reduced by exp(-distance / this).
    delivery_km: float = 15.0
    # Multiplier on every town's load (per-town multipliers live in Forcing.town_scale).
    load_scale: float = 1.0
    # Loads in 2016 relative to 2024, falling linearly in between and flat afterwards. The
    # inventory describes 2024; these stand in for sewerage and sanitation built since 2016.
    load_2016: float = 1.0
    diffuse_2016: float = 1.0

    def replace(self, **kw) -> "Params":
        out = Params(**{f.name: getattr(self, f.name) for f in fields(self)})
        for k, v in kw.items():
            if not hasattr(out, k):
                raise KeyError(k)
            setattr(out, k, v)
        return out


@dataclass
class Forcing:
    """Everything the model needs for a run. Arrays are [day, cell] unless noted."""

    dx_km: float
    flow: np.ndarray  # m3/s in each cell
    air_temp: np.ndarray  # deg C
    solar: np.ndarray  # MJ/m2/day
    rain: np.ndarray  # mm/day
    # Lateral inflows (tributaries and unnamed gains): flow [day, cell] and concentrations.
    inflow: np.ndarray  # m3/s entering at each cell
    inflow_conc: dict[str, np.ndarray]  # species (+ "do") -> [day, cell] concentration
    # Upstream boundary concentrations: species (+ "do") -> [day]
    upstream: dict[str, np.ndarray]
    # Towns: per-town arrays of length n_towns.
    town_cell: np.ndarray  # cell index where each town discharges
    town_untreated: np.ndarray  # MLD reaching the river untreated
    town_treated: np.ndarray  # MLD discharged after treatment
    town_population: np.ndarray  # in units of 100,000 people
    town_offset_km: np.ndarray  # distance of the town from the bank
    town_scale: np.ndarray = field(default_factory=lambda: np.ones(0))  # per-town multiplier
    elevation_m: np.ndarray | None = None  # [cell]
    year: np.ndarray | None = None  # [day] decimal year, for load trends
    # Optional extra loads, e.g. a spill scenario: species -> [day, cell] in g/s (or MPN units).
    extra: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        return self.flow.shape


@dataclass
class Result:
    conc: dict[str, np.ndarray]  # bod, nh4, no3, tp, fc, do -> [day, cell]
    do_sat: np.ndarray
    water_temp: np.ndarray
    velocity: np.ndarray  # m/s
    depth: np.ndarray  # m
    travel_days: np.ndarray  # cumulative travel time from the upstream boundary


def do_saturation(temp_c: np.ndarray, elevation_m: np.ndarray | float = 0.0) -> np.ndarray:
    """Oxygen solubility in fresh water (mg/L), Benson-Krause form, with altitude correction."""
    tk = np.asarray(temp_c, dtype=float) + 273.15
    ln_c = (
        -139.34411
        + 1.575701e5 / tk
        - 6.642308e7 / tk**2
        + 1.243800e10 / tk**3
        - 8.621949e11 / tk**4
    )
    return np.exp(ln_c) * (1.0 - 1.148e-4 * np.asarray(elevation_m, dtype=float))


def reaeration(u: np.ndarray, h: np.ndarray) -> np.ndarray:
    """O'Connor-Dobbins reaeration rate at 20 C, per day (u in m/s, h in m)."""
    return 3.93 * np.sqrt(u) / h**1.5


def hydraulics(flow: np.ndarray, p: Params) -> tuple[np.ndarray, np.ndarray]:
    q = np.maximum(flow, 1.0) / 1000.0
    return p.u_ref * q**p.u_exp, p.h_ref * q**p.h_exp


TREND_FROM, TREND_TO = 2016.0, 2024.0


def trend(f: Forcing, level_2016: float) -> np.ndarray | float:
    """Per-day multiplier going from level_2016 in 2016 to 1 in 2024."""
    if f.year is None or level_2016 == 1.0:
        return 1.0
    frac = np.clip((TREND_TO - f.year) / (TREND_TO - TREND_FROM), 0.0, 1.0)
    return 1.0 + (level_2016 - 1.0) * frac


def town_loads(f: Forcing, p: Params) -> dict[str, np.ndarray]:
    """Mass flux from towns into each cell, [day, cell], for each species plus oxygen deficit."""
    n_days, n_cells = f.shape
    out = {s: np.zeros((n_days, n_cells)) for s in (*SPECIES, "flow")}
    if f.town_cell.size == 0:
        return out
    scale = p.load_scale * (f.town_scale if f.town_scale.size else np.ones(f.town_cell.size))
    delivery = np.exp(-f.town_offset_km / p.delivery_km)
    mld = 1000.0 / DAY  # 1 MLD in m3/s
    for k, cell in enumerate(f.town_cell):
        rain = f.rain[:, cell]
        bypass = np.clip(p.storm_bypass * np.maximum(rain - p.storm_threshold, 0.0), 0.0, 1.0)
        untreated = (f.town_untreated[k] + bypass * f.town_treated[k]) * mld  # m3/s, [day]
        treated = (1.0 - bypass) * f.town_treated[k] * mld
        wash = p.wash_bod * rain * f.town_population[k] * 1000.0 / DAY  # g BOD/s
        w = scale[k] * delivery[k] * trend(f, p.load_2016)
        out["bod"][:, cell] += w * (untreated * p.raw_bod + treated * p.eff_bod + wash)
        out["nh4"][:, cell] += w * (untreated * p.raw_nh4 + treated * p.eff_nh4)
        out["tin"][:, cell] += w * (untreated * p.raw_nh4 + treated * (p.eff_nh4 + p.eff_no3))
        out["tp"][:, cell] += w * (untreated * p.raw_tp + treated * p.eff_tp)
        out["fc"][:, cell] += w * (
            untreated * p.raw_fc + treated * p.eff_fc + wash * p.wash_fc_ratio
        )
        out["flow"][:, cell] += untreated + treated
    return out


def _advect(
    prev: np.ndarray,
    load: np.ndarray,
    rate_cum: np.ndarray,
    src: np.ndarray,
    fresh: np.ndarray,
    idx: np.ndarray,
) -> np.ndarray:
    """One day of transport for a first-order species, in flux form.

    prev      flux at the end of the previous day, per cell
    load      flux entering at the upstream face of each cell today (boundary included in cell 0)
    rate_cum  integral of the decay rate over travel time, from the boundary to each cell's
              downstream face
    src       fractional cell index one day upstream of each cell (valid where not fresh)
    fresh     True where the cell is less than a day's travel from the boundary
    """
    # Load j decays over rate_cum[i] - rate_cum[j-1] on its way to cell i.
    before = np.concatenate(([0.0], rate_cum[:-1]))
    grown = np.cumsum(load * np.exp(before))
    # Interpolate at the fractional source position. Flux falls off exponentially along the
    # river, so interpolate flux x exp(rate_cum), which is smooth, rather than flux itself.
    g_src = np.interp(src, idx, grown)
    carried = np.interp(src, idx, prev * np.exp(rate_cum))
    return np.exp(-rate_cum) * np.where(fresh, grown, carried + grown - g_src)


def _advect_log(
    prev: np.ndarray,
    load: np.ndarray,
    rate_cum: np.ndarray,
    src: np.ndarray,
    fresh: np.ndarray,
    idx: np.ndarray,
) -> np.ndarray:
    """Same as _advect for non-negative fluxes, computed with logarithms.

    Used when the cumulative decay exponent is too large for exp() (fast die-off over a very
    long travel time), where the direct form would overflow.
    """
    before = np.concatenate(([0.0], rate_cum[:-1]))
    with np.errstate(divide="ignore"):
        log_grown = np.logaddexp.accumulate(np.log(load) + before)
        log_prev = np.log(np.maximum(prev, 0.0)) + rate_cum
    floor = -1e300
    log_grown = np.maximum(log_grown, floor)
    log_prev = np.maximum(log_prev, floor)
    total = np.exp(np.maximum(log_grown - rate_cum, -745.0))
    upto_src = np.exp(np.maximum(np.interp(src, idx, log_grown) - rate_cum, -745.0))
    carried = np.exp(np.maximum(np.interp(src, idx, log_prev) - rate_cum, -745.0))
    return np.where(fresh, total, carried + np.maximum(total - upto_src, 0.0))


EXP_SAFE = 600.0  # above this cumulative exponent, switch to the logarithmic form


def simulate(f: Forcing, p: Params, spinup_days: int = 60) -> Result:
    """Run the model over the forcing period. The first day is repeated to spin up."""
    n_days, n = f.shape
    idx = np.arange(n, dtype=float)
    u, h = hydraulics(f.flow, p)
    tau = f.dx_km * 1000.0 / u / DAY  # days to cross each cell
    travel = np.cumsum(tau, axis=1)

    tw = p.tw_a + p.tw_b * f.air_temp
    elev = f.elevation_m if f.elevation_m is not None else 0.0
    sat = do_saturation(np.clip(tw, 0.0, 40.0), elev)
    dt20 = tw - 20.0
    k = {
        "bod": (p.kd * p.theta_kd**dt20) + p.ks,
        "nh4": p.kn * p.theta_kn**dt20 + p.kdn,
        "tin": np.full_like(tw, p.kdn),
        "tp": np.full_like(tw, p.kp),
        "fc": p.kfc * p.theta_fc**dt20 + p.kfc_light * f.solar / h,
    }
    kd_t = p.kd * p.theta_kd**dt20
    kn_t = p.kn * p.theta_kn**dt20
    ka_t = p.ka_scale * reaeration(u, h) * p.theta_ka**dt20
    # Sediment demand minus algal production, mg/L/day (negative = net oxygen source).
    sod_t = (p.sod * p.theta_sod**dt20 - p.photo * f.solar) / h

    towns = town_loads(f, p)
    diffuse_bod = p.diffuse_bod * f.dx_km * 1000.0 / DAY * (1.0 + p.diffuse_rain * f.rain)  # g/s
    diffuse_bod = diffuse_bod * np.reshape(trend(f, p.diffuse_2016), (-1, 1))
    per_bod = {
        "bod": 1.0,
        "nh4": p.raw_nh4 / p.raw_bod,
        "tin": p.raw_nh4 / p.raw_bod,
        "tp": p.raw_tp / p.raw_bod,
        "fc": p.diffuse_fc_ratio,
    }
    loads = {}
    for s in SPECIES:
        lat = f.inflow * f.inflow_conc[s]
        # Diffuse load enters evenly along the cell, so on average it decays over half of it;
        # the transport step applies a full cell of decay, hence the correction factor.
        kt = np.maximum(k[s] * tau, 1e-9)
        spread = (1.0 - np.exp(-kt)) / kt * np.exp(kt)
        loads[s] = towns[s] + lat + diffuse_bod * per_bod[s] * spread + f.extra.get(s, 0.0)
        loads[s][:, 0] += f.flow[:, 0] * f.upstream[s]
    # Oxygen deficit carried in by inflows, sewage (taken as fully depleted) and the boundary.
    sat_in = sat
    deficit_load = f.inflow * np.maximum(sat_in - f.inflow_conc["do"], 0.0) + towns["flow"] * sat
    deficit_load[:, 0] += f.flow[:, 0] * np.maximum(sat[:, 0] - f.upstream["do"], 0.0)

    out = {s: np.empty((n_days, n)) for s in (*SPECIES, "do")}
    flux = {s: np.zeros(n) for s in SPECIES}
    deficit_flux = np.zeros(n)

    order = [0] * spinup_days + list(range(n_days))
    for step, t in enumerate(order):
        q = f.flow[t]
        tr = travel[t]
        fresh = tr <= 1.0
        src = np.where(fresh, 0.0, np.interp(tr - 1.0, tr, idx))
        conc = {}
        for s in SPECIES:
            rc = np.cumsum(k[s][t] * tau[t])
            step_fn = _advect if rc[-1] < EXP_SAFE else _advect_log
            flux[s] = step_fn(flux[s], loads[s][t], rc, src, fresh, idx)
            conc[s] = flux[s] / q
        # Oxygen: deficit decays by reaeration. Oxygen demand exerted inside each cell is
        # added as a load there, using the mean concentration across the cell.
        ra_cell = ka_t[t] * tau[t]
        ra = np.cumsum(ra_cell)
        demand = kd_t[t] * conc["bod"] + O2_PER_N * kn_t[t] * conc["nh4"] + sod_t[t]
        inflow_face = np.concatenate(([demand[0]], demand[:-1]))
        within = (1.0 - np.exp(-ra_cell)) / np.maximum(ra_cell, 1e-9)  # reaeration in-cell
        made = q * 0.5 * (demand + inflow_face) * tau[t] * within * np.exp(ra_cell)
        deficit_flux = _advect(deficit_flux, deficit_load[t] + made, ra, src, fresh, idx)
        # Deficit may go slightly negative (supersaturation from algae) but not below zero DO.
        deficit_flux = np.clip(deficit_flux, -0.4 * q * sat[t], q * sat[t])
        if step >= spinup_days:
            for s in SPECIES:
                out[s][t] = conc[s]
            out["do"][t] = sat[t] - deficit_flux / q

    conc_out = {
        "bod": out["bod"],
        "nh4": out["nh4"],
        "no3": np.maximum(out["tin"] - out["nh4"], 0.0),
        "tp": out["tp"],
        "fc": out["fc"],
        "do": out["do"],
    }
    return Result(conc_out, sat, tw, u, h, travel)
