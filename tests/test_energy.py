import copy

import numpy as np
import pandas as pd
import pytest

from setwin import monitor as M
from setwin.dispatch import kpis, lp_day, run, settle
from setwin.profiles import build, clear_sky, load_config


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def df(cfg):
    return build(cfg)


def test_profiles_scale_and_physics(df, cfg):
    assert df["pv_mw"].sum() == pytest.approx(cfg["pv"]["annual_energy_mwh"], rel=1e-9)
    assert (df["pv_mw"] >= 0).all() and (df["load_mw"] > 0).all()
    night = df[df.index.hour.isin([0, 1, 2, 3, 23])]
    assert night["pv_mw"].max() == 0
    summer = df.loc["2027-06"]["pv_mw"].sum()
    winter = df.loc["2027-12"]["pv_mw"].sum()
    assert summer > 2 * winter


def test_clear_sky_noon_higher_in_june():
    idx = pd.DatetimeIndex(["2027-06-21 12:00", "2027-12-21 12:00", "2027-06-21 02:00"])
    cs = clear_sky(idx, 48.5)
    assert cs[0] > cs[1] > 0 and cs[2] == 0


def test_energy_balance_closes_for_every_strategy(df, cfg):
    for s in ["none", "self_consumption", "price_rule", "lp_forecast"]:
        r = run(df, cfg, s)
        lhs = r.pv_mw - r.curtail_mw + r.discharge_mw - r.charge_mw + r.import_mw - r.export_mw + r.unserved_mw
        assert np.allclose(lhs, r.load_mw, atol=1e-6), s
        assert r.unserved_mw.sum() < 1e-6, s
        b = cfg["battery"]
        assert r.soc_mwh.min() >= b["soc_min"] * b["energy_mwh"] - 1e-6
        assert r.soc_mwh.max() <= b["soc_max"] * b["energy_mwh"] + 1e-6
        assert r.import_mw.max() <= cfg["grid"]["connection_mw"] + 1e-6


def test_settle_respects_battery_limits(df, cfg):
    huge = np.full(len(df), 1e6)
    r = settle(df.iloc[:48], huge[:48], cfg)
    assert r.discharge_mw.max() <= cfg["battery"]["power_mw"] + 1e-9


def test_lp_buys_cheap_sells_dear(cfg):
    price = np.array([20.0] * 6 + [150.0] * 6 + [80.0] * 12)
    load = np.full(24, 10.0)
    pv = np.zeros(24)
    soc0 = cfg["battery"]["soc_start"] * cfg["battery"]["energy_mwh"]
    net, _ = lp_day(pv, load, price, cfg, soc0)
    assert net[:6].mean() < 0 < net[6:12].mean()


def test_optimiser_beats_rules_and_perfect_beats_forecast(df, cfg):
    k = {s: kpis(run(df, cfg, s), cfg)["energy_cost_keur"] for s in ["none", "price_rule", "lp_forecast",
                                                                      "lp_perfect"]}
    assert k["lp_perfect"] <= k["lp_forecast"] + 1 < k["price_rule"] < k["none"]


def test_carbon_price_reduces_co2(df, cfg):
    a = kpis(run(df, cfg, "lp_forecast"), cfg)["co2_t"]
    b = kpis(run(df, cfg, "lp_forecast_co2"), cfg)["co2_t"]
    assert b < a


def test_monitoring_finds_injected_faults_without_false_alarm(df, cfg):
    meas, truth = M.metered_pv(df, cfg)
    pr = M.daily_pr(df["pv_mw"], pd.Series(meas, index=df.index))
    alarms = M.cusum_alarms(pr)
    inv = df.index[0] + pd.Timedelta(days=149)
    assert not [a for a in alarms if a < inv]
    assert (M.first_alarm_after(alarms, inv) - inv).days <= 2


def test_no_fault_no_alarm(df, cfg):
    meas, _ = M.metered_pv(df, cfg, inverter_out=(999, 999), soiling=(999, 1000, 0.0))
    pr = M.daily_pr(df["pv_mw"], pd.Series(meas, index=df.index))
    assert len(M.cusum_alarms(pr)) <= 1
