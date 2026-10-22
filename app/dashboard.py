"""Energy twin dashboard.  Run:  streamlit run app/dashboard.py"""
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from setwin import dispatch, monitor  # noqa: E402
from setwin.dispatch import STRATEGIES, kpis, run  # noqa: E402
from setwin.profiles import build, load_config  # noqa: E402

st.set_page_config(page_title="Site energy twin", layout="wide")
st.title("Multi-energy site: energy twin")
st.caption("Hourly model over one year. All inputs are assumptions from config/site.yaml, not plant data.")

base = load_config()
with st.sidebar:
    st.header("Scenario")
    pv_gwh = st.slider("PV energy (GWh/year)", 0.0, 80.0, base["pv"]["annual_energy_mwh"] / 1000, 1.0)
    batt = st.slider("Battery energy (MWh)", 0.0, 120.0, float(base["battery"]["energy_mwh"]), 1.0)
    hours = st.select_slider("Battery duration (h)", [1, 2, 4], 2)
    strategy = st.selectbox("Battery strategy", list(STRATEGIES), index=list(STRATEGIES).index("lp_forecast"))
    carbon = st.slider("Internal carbon price for the carbon-aware strategy (EUR/t)", 0, 500,
                       int(dispatch.CARBON_EUR_T), 25)


@st.cache_data(show_spinner="Simulating one year...")
def simulate(pv_gwh, batt, hours, strategy, carbon):
    cfg = copy.deepcopy(base)
    cfg["pv"]["annual_energy_mwh"] = max(pv_gwh * 1000, 1e-6)
    cfg["battery"]["energy_mwh"] = max(batt, 1e-6)
    cfg["battery"]["power_mw"] = max(batt / hours, 1e-6)
    dispatch.CARBON_EUR_T = carbon
    df = build(cfg)
    ref = kpis(run(df, cfg, "none"), cfg)
    res = run(df, cfg, strategy if batt > 0 else "none")
    return res, kpis(res, cfg), ref


res, k, ref = simulate(pv_gwh, batt, hours, strategy, carbon)
tab_bal, tab_mon = st.tabs(["Energy balance", "PV performance monitoring"])
with tab_bal:
    c = st.columns(5)
    c[0].metric("Energy cost (kEUR/yr)", f"{k['energy_cost_keur']:,.0f}",
                f"{k['energy_cost_keur'] - ref['energy_cost_keur']:+,.0f} vs no battery", delta_color="inverse")
    c[1].metric("CO2 of imports (t/yr)", f"{k['co2_t']:,.0f}", f"{k['co2_t'] - ref['co2_t']:+,.0f}",
                delta_color="inverse")
    c[2].metric("PV self-consumption", f"{k['self_consumption_pct']:.1f} %")
    c[3].metric("Peak import (MW)", f"{k['peak_import_mw']:.1f}")
    c[4].metric("Battery cycles / year", f"{k['battery_cycles']:.0f}")
    week = st.date_input("Week starting", pd.Timestamp(f"{base['year']}-07-05"))
    w = res.loc[pd.Timestamp(week):pd.Timestamp(week) + pd.Timedelta(days=7)]
    st.line_chart(w[["pv_mw", "load_mw", "import_mw"]])
    st.bar_chart((w["discharge_mw"] - w["charge_mw"]).rename("battery (+ discharge, MW)"))
    st.line_chart(w["price_eur_mwh"])
with tab_mon:
    cfg = copy.deepcopy(base)
    df = build(cfg)
    meas, truth = monitor.metered_pv(df, cfg)
    pr = monitor.daily_pr(df["pv_mw"], pd.Series(meas, index=df.index))
    alarms = monitor.cusum_alarms(pr)
    st.write("Daily performance ratio = metered PV / PV expected by the twin. A downward CUSUM raises alarms.")
    st.line_chart(pr.rename("performance ratio"))
    st.dataframe(pd.DataFrame({"alarm day": [a.date() for a in alarms]}), width="stretch")
