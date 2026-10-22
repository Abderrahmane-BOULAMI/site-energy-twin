"""Battery dispatch strategies and the hourly energy balance of the site.

Balance every hour:  pv - curtailment + discharge - charge + import - export = load
Strategies:
- none:              no battery
- self_consumption:  store PV surplus, give it back when PV is short
- price_rule:        self-consumption + charge on the 6 cheapest hours of the day, discharge on the 6 dearest
- lp_perfect:        daily linear programme with perfect knowledge of the day (upper bound of what is possible)
- lp_forecast:       same LP on forecasts (yesterday's PV and load profile, day-ahead prices known), the plan is
                     then executed against what really happens; the grid absorbs the gap
- lp_forecast_co2:   lp_forecast with an internal carbon price on imported electricity
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import linprog

GRID_FEE = 10.0        # EUR per MWh imported (network tariff), on top of the market price
WEAR_COST = 5.0        # EUR per MWh cycled through the battery (degradation)
CARBON_EUR_T = 200.0   # internal carbon price used by the carbon-aware strategy


def _battery(cfg):
    b = cfg["battery"]
    return b["energy_mwh"], b["power_mw"], b["efficiency_charge"], b["efficiency_discharge"], \
        b["soc_min"] * b["energy_mwh"], b["soc_max"] * b["energy_mwh"], b["soc_start"] * b["energy_mwh"]


def settle(df: pd.DataFrame, batt_net: np.ndarray, cfg: dict) -> pd.DataFrame:
    """Execute a battery set-point (+ discharge / - charge, MW) against actual PV and load.

    The battery follows the set-point within power and energy limits; PV that cannot be used, stored or
    exported is curtailed; the grid covers the rest.
    """
    E, P, ec, ed, smin, smax, s0 = _battery(cfg)
    cap = cfg["grid"]["connection_mw"]
    n = len(df)
    soc = s0
    out = np.zeros((n, 7))
    pv, load = df["pv_mw"].to_numpy(), df["load_mw"].to_numpy()
    for t in range(n):
        want = float(np.clip(batt_net[t], -P, P))
        if want >= 0:                                   # discharge
            d = min(want, (soc - smin) * ed)
            c = 0.0
        else:                                           # charge
            c = min(-want, (smax - soc) / ec)
            d = 0.0
        over = (load[t] + c - d - pv[t]) - cap          # charging must never overload the grid connection
        if over > 0 and c > 0:
            c = max(0.0, c - over)
        soc += c * ec - d / ed
        net = pv[t] + d - c - load[t]                   # >0 surplus to export, <0 deficit to import
        imp = min(max(-net, 0.0), cap)
        exp = min(max(net, 0.0), cap)
        curt = max(net - cap, 0.0)
        unserved = max(-net - cap, 0.0)
        out[t] = [c, d, soc, imp, exp, curt, unserved]
    res = df.copy()
    for j, col in enumerate(["charge_mw", "discharge_mw", "soc_mwh", "import_mw", "export_mw", "curtail_mw",
                             "unserved_mw"]):
        res[col] = out[:, j]
    return res


def plan_self_consumption(df, cfg):
    return (df["load_mw"] - df["pv_mw"]).to_numpy()     # positive = deficit -> discharge


def plan_price_rule(df, cfg):
    surplus = (df["pv_mw"] - df["load_mw"]).to_numpy()
    plan = -np.clip(surplus, 0, None)                    # store any surplus
    _, P, *_ = _battery(cfg)
    for _day, g in df.groupby(df.index.date):
        pos = df.index.get_indexer(g.index)
        order = np.argsort(g["price_eur_mwh"].to_numpy())
        cheap, dear = pos[order[:6]], pos[order[-6:]]
        plan[cheap] = np.minimum(plan[cheap], -P / 2)
        plan[dear] = P
    return plan


def lp_day(pv, load, price, cfg, soc0, co2=None, carbon_eur_t: float = 0.0):
    """Optimal battery schedule for one day; returns net battery power (MW) per hour and end SOC."""
    E, P, ec, ed, smin, smax, _ = _battery(cfg)
    cap = cfg["grid"]["connection_mw"]
    k = cfg["grid"]["export_price_factor"]
    T = len(pv)
    # variables per hour: c, d, imp, exp, curt, soc  -> 6T
    nv = 6 * T
    C, D, I, X, U, S = (np.arange(T) + i * T for i in range(6))
    cost = np.zeros(nv)
    cost[I] = price + GRID_FEE + (0 if co2 is None else carbon_eur_t * np.asarray(co2) / 1000)
    cost[X] = -k * price
    cost[C] = WEAR_COST / 2
    cost[D] = WEAR_COST / 2
    A, b = [], []
    for t in range(T):            # balance
        row = np.zeros(nv)
        row[D[t]], row[C[t]], row[I[t]], row[X[t]], row[U[t]] = 1, -1, 1, -1, -1
        A.append(row)
        b.append(load[t] - pv[t])
    for t in range(T):            # state of charge
        row = np.zeros(nv)
        row[S[t]] = 1
        row[C[t]] = -ec
        row[D[t]] = 1 / ed
        if t:
            row[S[t - 1]] = -1
        A.append(row)
        b.append(soc0 if t == 0 else 0.0)
    bounds = [(0, P)] * T + [(0, P)] * T + [(0, cap)] * T + [(0, cap)] * T + \
             [(0, max(p, 0)) for p in pv] + [(smin, smax)] * T
    bounds[S[-1]] = (min(soc0, smax), smax)              # do not empty the battery at the end of the day
    res = linprog(cost, A_eq=np.array(A), b_eq=np.array(b), bounds=bounds, method="highs")
    if res.status != 0:
        raise RuntimeError(res.message)
    x = res.x
    return x[D] - x[C], x[S][-1]


def plan_lp(df, cfg, forecast: bool, carbon_eur_t: float = 0.0):
    plan = np.zeros(len(df))
    soc = cfg["battery"]["soc_start"] * cfg["battery"]["energy_mwh"]
    pv_all, load_all = df["pv_mw"].to_numpy(), df["load_mw"].to_numpy()
    for i in range(0, len(df), 24):
        sl = slice(i, i + 24)
        if forecast and i >= 24:      # persistence forecast: yesterday's profile
            pv_f, load_f = pv_all[i - 24:i], load_all[i - 24:i]
        else:
            pv_f, load_f = pv_all[sl], load_all[sl]
        net, soc = lp_day(pv_f, load_f, df["price_eur_mwh"].to_numpy()[sl], cfg, soc,
                          df["co2_kg_mwh"].to_numpy()[sl], carbon_eur_t)
        plan[sl] = net
    return plan


STRATEGIES = {
    "none": lambda df, cfg: np.zeros(len(df)),
    "self_consumption": plan_self_consumption,
    "price_rule": plan_price_rule,
    "lp_forecast": lambda df, cfg: plan_lp(df, cfg, forecast=True),
    "lp_perfect": lambda df, cfg: plan_lp(df, cfg, forecast=False),
    "lp_forecast_co2": lambda df, cfg: plan_lp(df, cfg, forecast=True, carbon_eur_t=CARBON_EUR_T),
}


def run(df: pd.DataFrame, cfg: dict, strategy: str) -> pd.DataFrame:
    return settle(df, STRATEGIES[strategy](df, cfg), cfg)


def kpis(res: pd.DataFrame, cfg: dict) -> dict:
    k = cfg["grid"]["export_price_factor"]
    p = res["price_eur_mwh"]
    pv_used = (res["pv_mw"] - res["curtail_mw"] - res["export_mw"]).clip(lower=0)
    E = cfg["battery"]["energy_mwh"]
    return {
        "energy_cost_keur": float(((p + GRID_FEE) * res["import_mw"] - k * p * res["export_mw"]).sum() / 1e3),
        "import_mwh": float(res["import_mw"].sum()),
        "export_mwh": float(res["export_mw"].sum()),
        "curtailed_mwh": float(res["curtail_mw"].sum()),
        "self_consumption_pct": float(100 * pv_used.sum() / res["pv_mw"].sum()),
        "self_sufficiency_pct": float(100 * pv_used.sum() / res["load_mw"].sum()),
        "peak_import_mw": float(res["import_mw"].max()),
        "co2_t": float((res["import_mw"] * res["co2_kg_mwh"]).sum() / 1e3),
        "battery_cycles": float(res["discharge_mw"].sum() / E),
        "unserved_mwh": float(res["unserved_mw"].sum()),
    }
