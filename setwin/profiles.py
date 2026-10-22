"""Hourly profiles for one year: PV production, site load, grid price and grid carbon intensity.

Everything is generated from the assumptions in config/site.yaml with a fixed seed, so a scenario is
reproducible. Real meter and market data replace these functions one by one (same column names).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

DEFAULT = Path(__file__).resolve().parent.parent / "config" / "site.yaml"


def load_config(path=None) -> dict:
    with open(path or DEFAULT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def clear_sky(index: pd.DatetimeIndex, latitude_deg: float) -> np.ndarray:
    """Relative clear-sky irradiance on a south-facing plane (0..1), simple solar geometry."""
    doy = index.dayofyear.to_numpy()
    hour = index.hour.to_numpy() + 0.5
    decl = np.radians(23.44) * np.sin(2 * np.pi * (284 + doy) / 365)
    lat = np.radians(latitude_deg)
    ha = np.radians(15 * (hour - 12))
    sin_alt = np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(ha)
    sin_alt = np.clip(sin_alt, 0, None)
    air_mass_loss = np.where(sin_alt > 0, np.exp(-0.15 / np.maximum(sin_alt, 0.05)), 0)
    return sin_alt * air_mass_loss


def build(cfg: dict | None = None, seed: int = 0) -> pd.DataFrame:
    cfg = cfg or load_config()
    rng = np.random.default_rng(seed)
    idx = pd.date_range(f"{cfg['year']}-01-01", periods=8760, freq="h")
    doy = idx.dayofyear.to_numpy()
    hour = idx.hour.to_numpy()
    days = 365

    # --- PV: clear sky x daily clearness (cloudier in winter) x small hourly noise, scaled to the annual energy
    pv = cfg["pv"]
    season = 0.5 + 0.5 * np.sin(2 * np.pi * (np.arange(days) + 1 - 80) / 365)  # 0 in Dec, 1 in Jun
    clear = rng.beta(pv["cloud_alpha"], pv["cloud_beta"], days) * (0.55 + 0.45 * season)
    clear_h = clear[doy - 1] * np.clip(1 + rng.normal(0, 0.08, 8760), 0.5, 1.3)
    shape = clear_sky(idx, cfg["latitude_deg"]) * clear_h
    pv_mw = shape / shape.sum() * pv["annual_energy_mwh"]

    # --- load: base + day/night swing + noise, planned shutdown, random trips
    ld = cfg["load"]
    load = ld["base_mw"] + ld["daily_swing_mw"] * np.sin(np.pi * np.clip(hour - 6, 0, 12) / 12) \
        + rng.normal(0, ld["noise_mw"], 8760)
    for a, b in ld["shutdown_days"]:
        load[(doy >= a) & (doy <= b)] = ld["shutdown_mw"] + rng.normal(0, 0.3, ((doy >= a) & (doy <= b)).sum())
    trips = np.zeros(8760, bool)
    for t in rng.choice(8760 - 12, ld["trips_per_year"], replace=False):
        dur = int(rng.integers(3, 12))
        load[t:t + dur] -= ld["trip_drop_mw"]
        trips[t:t + dur] = True
    load = np.clip(load, 1.0, None)

    # --- market price: base, evening peak, mid-day solar dip on sunny days, winter premium, noise
    g = cfg["grid"]
    evening = np.exp(-0.5 * ((hour - 19) / 2.0) ** 2) + 0.6 * np.exp(-0.5 * ((hour - 8) / 1.5) ** 2)
    sunny = clear[doy - 1] / clear.max()
    dip = g["price_solar_dip"] * sunny * np.exp(-0.5 * ((hour - 13) / 2.5) ** 2)
    winter = 1 + 0.25 * np.cos(2 * np.pi * (doy - 15) / 365)
    # market noise is persistent from hour to hour (AR(1)), not white: white noise would let the battery
    # "arbitrage" random hourly jumps and overstate the savings
    phi, eps = 0.9, rng.normal(0, g["price_noise"] * np.sqrt(1 - 0.9 ** 2), 8760)
    noise = np.zeros(8760)
    for t in range(1, 8760):
        noise[t] = phi * noise[t - 1] + eps[t]
    price = (g["price_base_eur_mwh"] + g["price_daily_swing"] * evening) * winter - dip + noise

    # --- grid carbon intensity (g/kWh = t/GWh = kg/MWh)
    co2 = g["co2_base_g_kwh"] + g["co2_winter_peak_g_kwh"] * np.clip(np.cos(2 * np.pi * (doy - 15) / 365), 0, None) \
        * (0.4 + 0.6 * evening) + rng.normal(0, 5, 8760)

    return pd.DataFrame({"pv_mw": pv_mw, "load_mw": load, "price_eur_mwh": price, "co2_kg_mwh": np.clip(co2, 10, None),
                         "trip": trips}, index=idx)
