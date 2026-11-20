from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.staging.engine import stage


def _cpap_night(rng, spans):
    """CPAP-only epoch features. ``spans``: list of (kind, epochs) where kind is
    N (regular ~21/min), R (irregular, slightly faster), Q (lying awake: slow +
    irregular, as seen on real nights) or O (mask off)."""
    rows = []
    for kind, n in spans:
        for _ in range(n):
            if kind == "O":
                rows.append((0.0, np.nan, np.nan, np.nan))
                continue
            rr, cv, acv = {"N": (21, 0.07, 0.08), "R": (23, 0.22, 0.25),
                           "Q": (12.5, 0.24, 0.24)}[kind]
            rows.append((1.0, rr + rng.normal(0, 1.0), abs(cv + rng.normal(0, 0.03)),
                         abs(acv + rng.normal(0, 0.03))))
    df = pd.DataFrame(rows, columns=["flow_present", "resp_rate", "breath_cv", "amp_cv"])
    t0 = datetime(2026, 11, 19, 21, 30)
    df.insert(0, "t", [t0 + timedelta(seconds=30 * i) for i in range(len(df))])
    df["resp_events"] = 0.0
    for k in ("hr", "hr_std", "spo2", "spo2_min", "motion"):
        df[k] = np.nan
    return df


def test_cpap_only_lying_awake_after_mask_off():
    # asleep 4 h, up for 5 min (mask off), then 3.5 h lying awake breathing slowly,
    # then back to sleep for 45 min
    rng = np.random.default_rng(0)
    spans = [("N", 200), ("R", 30), ("N", 250), ("O", 10), ("Q", 420), ("N", 90)]
    out = stage(_cpap_night(rng, spans))
    edges = np.cumsum([0] + [n for _, n in spans])
    awake = out["stage"].eq("Awake").to_numpy()
    assert awake[edges[3]:edges[5]].mean() > 0.9  # mask-off + lying awake
    assert awake[edges[5] + 10:].mean() < 0.2  # asleep again afterwards
    assert awake[5:edges[3]].mean() < 0.05  # including the REM bout
    assert (out["stage"].iloc[edges[1]:edges[2]] == "REM").mean() > 0.5


def test_cpap_only_long_rem_is_not_wake():
    rng = np.random.default_rng(1)
    spans = [("N", 300), ("R", 60), ("N", 200), ("R", 70), ("N", 60)]
    out = stage(_cpap_night(rng, spans))
    assert out["stage"].iloc[10:-10].eq("Awake").mean() < 0.03


def test_deep_sleep_estimate_needs_ring():
    """A 40-min stretch with the night's lowest, steadiest pulse and very regular
    breathing (as on a real night, 01:17-02:06) is marked deep; ordinary NREM is not,
    and without the ring the estimate is unknown (NaN), not zero."""
    rng = np.random.default_rng(3)
    spans = [("N", 240), ("N", 80), ("R", 30), ("N", 300)]  # second span: the calm one
    df = _cpap_night(rng, spans)
    edges = np.cumsum([0] + [n for _, n in spans])
    n = len(df)
    hr = 58 + rng.normal(0, 1.5, n)
    hr_std = np.abs(2.0 + rng.normal(0, 0.4, n))
    calm = slice(edges[1], edges[2])
    hr[calm] = 53.5 + rng.normal(0, 0.4, edges[2] - edges[1])
    hr_std[calm] = np.abs(1.0 + rng.normal(0, 0.15, edges[2] - edges[1]))
    cv, acv = df["breath_cv"].to_numpy().copy(), df["amp_cv"].to_numpy().copy()
    cv[calm] = np.abs(0.05 + rng.normal(0, 0.01, edges[2] - edges[1]))
    acv[calm] = np.abs(0.06 + rng.normal(0, 0.01, edges[2] - edges[1]))
    df["breath_cv"], df["amp_cv"] = cv, acv
    rem = slice(edges[2], edges[3])
    hr[rem] += 3
    df["hr"], df["hr_std"], df["spo2"], df["motion"] = hr, hr_std, 95.0, 0.0
    df.loc[[60, 600], "motion"] = 40.0  # a couple of position shifts
    out = stage(df)
    deep = out["deep"].to_numpy() > 0
    assert deep[edges[1] + 10: edges[2] - 10].mean() > 0.9
    assert deep[: edges[1] - 10].mean() < 0.1 and deep[edges[3] + 10:].mean() < 0.1
    assert not deep[out["stage"].to_numpy() != "NREM"].any()
    # CPAP only: no estimate
    assert stage(_cpap_night(rng, spans))["deep"].isna().all()


def test_twitch_is_not_wake():
    """Inside sleep, a short movement with no pulse change (twitch, turning over) and a
    pulse blip without movement are arousals: still asleep, but flagged. A pulse jump
    together with movement is a brief awakening."""
    rng = np.random.default_rng(5)
    df = _cpap_night(rng, [("N", 600)])
    df["hr"] = 56 + rng.normal(0, 0.8, len(df))
    df["hr_std"], df["spo2"], df["motion"] = 1.5, 95.0, 0.0
    twitches, rolls, blips, wakes = [100, 180], [260], [340], [450, 520]
    df.loc[twitches, "motion"] = 5.0
    df.loc[rolls + wakes, "motion"] = 90.0
    hr = df["hr"].to_numpy().copy()
    for i in blips + wakes:
        hr[i:i + 2] += 10
    df["hr"] = hr
    out = stage(df)
    st, ar = out["stage"], out["arousal"]
    assert (st.iloc[twitches + rolls + blips] != "Awake").all()
    assert (ar.iloc[rolls] > 0).all()
    assert (st.iloc[wakes] == "Awake").all() and (ar.iloc[wakes] == 0).all()
    assert stage(_cpap_night(rng, [("N", 100)]))["arousal"].isna().all()  # needs the ring
