"""Reproduce every number of the README (about 2 minutes).   python experiments/run_all.py"""
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from setwin import monitor as M  # noqa: E402
from setwin.dispatch import STRATEGIES, kpis, run  # noqa: E402
from setwin.profiles import build, load_config  # noqa: E402

RES, FIG = ROOT / "results", ROOT / "results" / "figures"
INK, MUTED, A, B, C = "#1d2a33", "#8a99a6", "#e07b39", "#2f7f6f", "#30638e"


def style(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def main():
    t0 = time.time()
    FIG.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    df = build(cfg)
    out = {"profiles": {"pv_gwh": round(df.pv_mw.sum() / 1e3, 2), "load_gwh": round(df.load_mw.sum() / 1e3, 1),
                        "pv_peak_mw": round(df.pv_mw.max(), 1), "mean_price": round(df.price_eur_mwh.mean(), 1)}}

    # 1. strategies
    runs = {s: run(df, cfg, s) for s in STRATEGIES}
    tab = pd.DataFrame({s: kpis(r, cfg) for s, r in runs.items()}).T
    tab["saving_vs_none_keur"] = tab.loc["none", "energy_cost_keur"] - tab["energy_cost_keur"]
    tab["co2_vs_none_t"] = tab["co2_t"] - tab.loc["none", "co2_t"]
    tab.round(2).to_csv(RES / "strategies.csv")
    out["strategies"] = tab.round(1).to_dict("index")

    # 2. battery size sweep with the realistic strategy
    sweep = []
    for e in [0, 10, 21.5, 43, 64.5, 86]:
        c = copy.deepcopy(cfg)
        c["battery"]["energy_mwh"] = max(e, 1e-6)
        c["battery"]["power_mw"] = max(e / 2, 1e-6)
        k = kpis(run(df, c, "lp_forecast" if e else "none"), c)
        sweep.append({"battery_mwh": e, **{x: k[x] for x in ("energy_cost_keur", "co2_t", "battery_cycles")}})
    sweep = pd.DataFrame(sweep)
    sweep["saving_keur"] = sweep.loc[0, "energy_cost_keur"] - sweep["energy_cost_keur"]
    sweep["marginal_saving_keur_per_mwh"] = sweep["saving_keur"].diff() / sweep["battery_mwh"].diff()
    sweep.round(2).to_csv(RES / "battery_sweep.csv", index=False)
    out["battery_sweep"] = sweep.round(1).to_dict("records")

    # 3. PV performance monitoring
    meas, truth = M.metered_pv(df, cfg)
    ms = pd.Series(meas, index=df.index)
    pr = M.daily_pr(df["pv_mw"], ms)
    alarms = M.cusum_alarms(pr)
    y0 = df.index[0]
    inv_start, soil_start = y0 + pd.Timedelta(days=149), y0 + pd.Timedelta(days=219)
    first_inv = M.first_alarm_after(alarms, inv_start)
    first_soil = M.first_alarm_after(alarms, soil_start)
    false_before = [a for a in alarms if a < inv_start]
    thr = pr[pr < 0.95]          # naive fixed threshold on PR for comparison
    thr_soil = thr[thr.index >= soil_start]
    soil_end = y0 + pd.Timedelta(days=299)
    clean_at = first_soil + pd.Timedelta(days=7) if first_soil is not None else soil_end
    out["monitoring"] = {
        "inverter_fault_start": str(inv_start.date()),
        "inverter_detected": None if first_inv is None else str(first_inv.date()),
        "inverter_delay_days": None if first_inv is None else (first_inv - inv_start).days,
        "soiling_start": str(soil_start.date()),
        "soiling_detected": None if first_soil is None else str(first_soil.date()),
        "soiling_delay_days": None if first_soil is None else (first_soil - soil_start).days,
        "soiling_loss_at_detection_pct": None if first_soil is None else
        round(100 * truth.loc[str(first_soil.date()), "soiling_loss"].iloc[12], 2),
        "fixed_threshold_95_soiling_delay_days": None if thr_soil.empty else (thr_soil.index[0] - soil_start).days,
        "false_alarms_before_faults": len(false_before),
        "soiling_energy_lost_mwh_if_cleaned_on_schedule": round(M.lost_energy(df, meas, soil_start, soil_end), 1),
        "soiling_energy_lost_mwh_if_cleaned_7_days_after_alarm": round(M.lost_energy(df, meas, soil_start, clean_at), 1),
    }

    # figures
    r = runs["lp_forecast"]
    wk = r.loc["2027-07-05":"2027-07-11"]
    fig, ax = plt.subplots(2, 1, figsize=(10, 5), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    ax[0].fill_between(wk.index, wk.pv_mw, color="#f2b33d", alpha=0.6, label="PV")
    ax[0].plot(wk.index, wk.load_mw, color=INK, lw=1, label="site load")
    ax[0].plot(wk.index, wk.import_mw, color=C, lw=1, label="grid import")
    ax[0].bar(wk.index, wk.discharge_mw - wk.charge_mw, width=0.035, color=B, label="battery (+ discharge)")
    ax[0].set_ylabel("MW")
    ax[0].legend(ncol=4, fontsize=7, frameon=False)
    ax[1].plot(wk.index, wk.price_eur_mwh, color=A, lw=1)
    ax[1].set_ylabel("EUR/MWh")
    for a in ax:
        style(a)
    fig.tight_layout()
    fig.savefig(FIG / "fig1_dispatch_week.png", dpi=150)

    fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
    order = ["self_consumption", "price_rule", "lp_forecast", "lp_forecast_co2", "lp_perfect"]
    ax[0].barh(order, tab.loc[order, "saving_vs_none_keur"], color=[MUTED, MUTED, B, C, MUTED])
    ax[0].set_xlabel("saving vs no battery (kEUR/year)")
    ax[1].barh(order, tab.loc[order, "co2_vs_none_t"], color=[MUTED, MUTED, B, C, MUTED])
    ax[1].axvline(0, color=INK, lw=0.8)
    ax[1].set_xlabel("CO2 vs no battery (t/year)")
    ax[1].set_yticks([])
    for a in ax:
        style(a)
    fig.tight_layout()
    fig.savefig(FIG / "fig2_strategies.png", dpi=150)

    fig, ax = plt.subplots(figsize=(10, 3))
    ax.plot(pr.index, pr.values, "o-", ms=2, lw=0.6, color=B, label="daily performance ratio (metered / twin)")
    for a_ in alarms:
        ax.axvline(a_, color=A, alpha=0.25, lw=1)
    ax.axvspan(inv_start, inv_start + pd.Timedelta(days=21), color=MUTED, alpha=0.2, label="inverter out (truth)")
    ax.axvspan(soil_start, soil_end, color="#f2b33d", alpha=0.15, label="soiling builds up (truth)")
    ax.set_ylim(0.82, 1.06)
    ax.legend(fontsize=7, frameon=False, loc="lower left")
    style(ax)
    fig.tight_layout()
    fig.savefig(FIG / "fig3_pv_monitoring.png", dpi=150)

    out["runtime_s"] = round(time.time() - t0)
    (RES / "summary.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
