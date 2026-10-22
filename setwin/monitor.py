"""PV performance twin: compare metered production with what the twin expects, and flag losses early.

The twin's expected production comes from the irradiance-driven model; the meter sees the real plant.
Injected faults (unknown to the detector): one inverter block out for some days, and soiling that builds
up slowly until the panels are cleaned. A one-sided CUSUM on the daily performance ratio raises an alarm.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def metered_pv(df: pd.DataFrame, cfg: dict, seed: int = 1, inverter_out=(150, 170), soiling=(220, 300, 0.06)):
    """Simulated meter: expected PV x (1 - inverter loss) x (1 - soiling) x measurement noise."""
    rng = np.random.default_rng(seed)
    doy = df.index.dayofyear.to_numpy()
    loss_inv = np.where((doy >= inverter_out[0]) & (doy <= inverter_out[1]), 1 / cfg["pv"]["inverters"], 0.0)
    a, b, depth = soiling
    loss_soil = np.where((doy >= a) & (doy < b), depth * (doy - a) / (b - a), 0.0)
    meas = df["pv_mw"].to_numpy() * (1 - loss_inv) * (1 - loss_soil) * (1 + rng.normal(0, 0.02, len(df)))
    truth = pd.DataFrame({"inverter_loss": loss_inv, "soiling_loss": loss_soil}, index=df.index)
    return np.clip(meas, 0, None), truth


def daily_pr(expected: pd.Series, measured: pd.Series, min_mwh: float = 20.0) -> pd.Series:
    """Daily performance ratio, skipping very dark days where the ratio is mostly noise."""
    e = expected.resample("D").sum()
    m = measured.resample("D").sum()
    pr = m / e
    return pr[e >= min_mwh]


def cusum_alarms(pr: pd.Series, ref_days: int = 30, k: float = 0.5, h: float = 5.0) -> list:
    """One-sided (downward) CUSUM on the daily PR; reset after each alarm. Returns alarm dates."""
    ref = pr.iloc[:ref_days]
    mu, sd = ref.mean(), max(ref.std(), 1e-3)
    s, alarms = 0.0, []
    for day, x in pr.iloc[ref_days:].items():
        s = max(0.0, s + (mu - x) / sd - k)
        if s > h:
            alarms.append(day)
            s = 0.0
    return alarms


def first_alarm_after(alarms, start) -> pd.Timestamp | None:
    later = [a for a in alarms if a >= start]
    return later[0] if later else None


def lost_energy(df: pd.DataFrame, measured: np.ndarray, start, end) -> float:
    m = (df.index >= start) & (df.index < end)
    return float((df["pv_mw"].to_numpy()[m] - measured[m]).sum())
