"""Heuristic cardio-respiratory sleep staging (Awake / NREM / REM).

Physiology the rules lean on:

* **NREM** - breathing is metronomic (low breath-to-breath interval and tidal-volume
  variability), heart rate drops and is steady, the body is still.
* **REM** - breathing becomes irregular in both rate and depth, heart rate becomes
  more variable and drifts up, but the body is still (muscle atonia). REM rarely
  appears in the first ~45-60 min after sleep onset.
* **Awake** - movement, mask off / no flow, elevated heart rate, irregular breathing.

Each feature is z-scored against the night's own robust baseline (median / MAD), so
the rules adapt to the person and night. This is NOT a validated scorer and is not a
substitute for polysomnography - treat the output as a trend indicator.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.staging.features import EPOCH_S

WAKE, NREM, REM = "Awake", "NREM", "REM"

DEFAULTS = {
    "wake_threshold": 1.5,
    "rem_threshold": 1.0,
    "wake_hr_fraction": 0.7,  # HR cut between NREM-HR (0) and movement-wake HR (1)
    "quiet_wake_epochs": 8,  # >= 4 min of aroused signals without movement -> awake
    "motion_wake": 3.0,  # O2Ring motion units summed per epoch
    "arousal_hr_rise": 5.0,  # a movement is wake if the pulse rises this much (bpm) above
    "big_motion": 50.0,  # the previous 3 min, if it is this big (rolling over), or if it
    #                      lasts more than one epoch; otherwise it is a twitch / arousal
    "min_rem_latency_min": 45,
    "sleep_onset_min": 5,
    "min_rem_bout_epochs": 3,
    # CPAP-only lying-awake detection (see _breathing_wake)
    "bw_rate_drop": 0.15,  # breathing rate >= 15 % below this night's sleep rate...
    "bw_cv_ratio": 1.5,  # ...and breath intervals >= 1.5x as irregular as in sleep
    "bw_window_epochs": 40,  # judged over a 20-min window
    "bw_core_frac": 0.6,  # >= 60 % of that window off-baseline seeds a wake period
    "bw_grow_frac": 0.35,  # a seeded period grows while >= 35 % stays off-baseline
    "bw_min_core_epochs": 40,  # seeds must last >= 20 min (REM bouts rarely look like this)
    "bw_bridge_epochs": 30,  # sleep-like gaps <= 15 min inside a wake period are wake too
    # deep sleep (N3-like) estimate inside NREM, needs the ring's pulse (see _deep)
    "deep_core": 0.55,  # calm score that starts a deep bout...
    "deep_core_epochs": 20,  # ...held for >= 10 min
    "deep_grow": 0.3,  # the bout extends while the score stays above this
}


def _rz(x: pd.Series) -> pd.Series:
    """Robust z-score; NaN-safe; returns 0 where the feature is missing."""
    x = x.astype(float)
    if x.notna().sum() < 10:
        return pd.Series(0.0, index=x.index)
    med = x.median()
    mad = (x - med).abs().median() * 1.4826
    # nights dominated by very regular NREM have a tiny MAD; floor the scale with the
    # overall spread so a z of ~1.3 still means "upper decile of this night"
    spread = (x.quantile(0.9) - x.quantile(0.1)) / 2.56
    scale = np.nanmax([mad, spread, 1e-9])
    return ((x - med) / scale).clip(-5, 5).fillna(0.0)


def _smooth(x: pd.Series, w: int = 5) -> pd.Series:
    return x.rolling(w, center=True, min_periods=1).median()


def _runs(labels: np.ndarray):
    """Yield (start, end_exclusive, label) runs."""
    i = 0
    while i < len(labels):
        j = i
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        yield i, j, labels[i]
        i = j


def _breathing_wake(df: pd.DataFrame, seed: np.ndarray, p: dict) -> np.ndarray:
    """Long lying-awake periods from breathing alone (CPAP without the ring).

    Quietly lying awake with the mask on leaves no movement or mask-off marker, but the
    breathing settles into a different regime from sleep. On real nights with a known
    awake period it was ~40 % *slower* than sleep breathing (13 vs 21 /min) and about
    twice as irregular, for hours, flipping back the moment sleep returned. REM is also
    irregular but tends to run at or slightly *above* the NREM rate, so the test is
    "clearly slower than this night's sleep rate, and irregular", sustained >= 20 min.

    The sleep baseline comes from the night's most regular epochs, not its median,
    because a long awake period can be a big share of the night.
    """
    rr, cv = df["resp_rate"], df["breath_cv"]
    ok = rr.notna() & cv.notna()
    n = len(df)
    if ok.sum() < 2 * p["bw_window_epochs"]:
        return np.zeros(n, dtype=bool)
    regular = ok & (cv <= cv[ok].quantile(0.3))
    base_rr, base_cv = rr[regular].median(), cv[regular].median()
    off = ((rr <= (1 - p["bw_rate_drop"]) * base_rr)
           & (cv >= p["bw_cv_ratio"] * base_cv)).astype(float).where(ok)
    w = p["bw_window_epochs"]
    frac = off.rolling(w, center=True, min_periods=w // 2).mean().fillna(0).to_numpy()

    core = np.zeros(n, dtype=bool)
    for s_, e_, lb in _runs(frac >= p["bw_core_frac"]):
        if lb and e_ - s_ >= p["bw_min_core_epochs"]:
            core[s_:e_] = True
    if not core.any():
        return core
    # grow through weaker evidence, then bridge short sleep-like gaps between wake
    # stretches (including mask-off / movement seeds) so one awake period stays whole
    grow = frac >= p["bw_grow_frac"]
    wake = core.copy()
    for s_, e_, lb in _runs(grow):
        if lb and wake[s_:e_].any():
            wake[s_:e_] = True
    anchor = wake | seed
    for s_, e_, lb in _runs(anchor):
        if not lb and 0 < s_ and e_ < n and e_ - s_ <= p["bw_bridge_epochs"] \
                and (wake[max(0, s_ - 1)] or wake[min(n - 1, e_)]):
            wake[s_:e_] = True
    wake |= anchor & np.convolve(wake, np.ones(3), "same").astype(bool)
    return wake


def _rz_ref(x: pd.Series, ref: pd.Series) -> pd.Series:
    """Robust z-score against the epochs selected by ``ref`` (e.g. sleep only)."""
    x = x.astype(float)
    r = x[ref.to_numpy()].dropna()
    if len(r) < 20:
        return pd.Series(0.0, index=x.index)
    med = r.median()
    scale = max((r - med).abs().median() * 1.4826, (r.quantile(0.9) - r.quantile(0.1)) / 2.56, 1e-9)
    return ((x - med) / scale).clip(-5, 5).fillna(0.0)


def _deep(df: pd.DataFrame, labels: np.ndarray, moving: np.ndarray, p: dict) -> np.ndarray:
    """Deep-sleep (N3-like) estimate within NREM.

    Deep sleep is the calmest NREM: the night's lowest and steadiest pulse, very regular
    breathing, no movement, in bouts of 10 min or more. The score compares each epoch
    with this night's sleep (pulse 0.4, pulse variability 0.3, breathing irregularity
    0.3 when the CPAP is there), smoothed over 5 min; a bout starts where it stays high
    for 10 min and extends while it stays moderately high.

    Needs the ring: on nights with both devices, breathing regularity alone did not
    agree with the pulse-based estimate at all, so CPAP-only nights get none. Not
    validated against EEG; pulse also has a circadian low in the early morning, so late
    bouts may be calm light sleep rather than true N3.
    """
    sleep = pd.Series(labels != WAKE, index=df.index)
    hr = df["hr"].rolling(3, center=True, min_periods=1).median()
    parts, w = [_rz_ref(hr, sleep), _rz_ref(df["hr_std"], sleep)], [0.4, 0.3]
    if df["breath_cv"].notna().any():
        parts.append(_rz_ref(df["breath_cv"] * 0.6 + df["amp_cv"] * 0.4, sleep))
        w.append(0.3)
    calm = -sum(wi * pi for wi, pi in zip(w, parts)) / sum(w)
    score = calm.rolling(10, center=True, min_periods=3).mean().to_numpy()
    nrem = labels == NREM
    seed = np.zeros(len(df), dtype=bool)
    for s_, e_, lb in _runs(nrem & (score > p["deep_core"]) & ~moving):
        if lb and e_ - s_ >= p["deep_core_epochs"]:
            seed[s_:e_] = True
    deep = np.zeros(len(df), dtype=bool)
    for s_, e_, lb in _runs(nrem & (score > p["deep_grow"]) & ~moving):
        if lb and seed[s_:e_].any():
            deep[s_:e_] = True
    return deep


def stage(epochs: pd.DataFrame, params: dict | None = None,
          prior_probs: np.ndarray | None = None) -> pd.DataFrame:
    """Stage epochs. ``prior_probs`` (n x 3: wake, nrem, rem; NaN rows = unknown) comes
    from an ML model such as sleepecg; when given, the cardio-respiratory scores act as
    extra evidence on top of the model instead of fixed thresholds."""
    p = {**DEFAULTS, **(params or {})}
    df = epochs.copy()
    has_flow = df["flow_present"].notna().any()
    has_oxi = df["hr"].notna().any()

    irr = _smooth(_rz(df["breath_cv"]) * 0.6 + _rz(df["amp_cv"]) * 0.4)
    rr = _smooth(_rz(df["resp_rate"]))
    hr = _smooth(_rz(df["hr"]))
    hrv = _smooth(_rz(df["hr_std"]))
    motion = df["motion"].fillna(0.0)
    moving = motion > p["motion_wake"]
    # movement in the neighbouring epochs also suggests arousal
    moving_near = moving.rolling(3, center=True, min_periods=1).max().astype(bool)
    # A movement on its own is not wake: a twitch or a position change in sleep is an
    # arousal at most (an epoch is wake only if awake for most of it). It counts as wake
    # when the pulse jumps, the movement is big, or it carries on into the next epoch.
    moving_wake = moving
    if has_oxi:
        hr_prev = df["hr"].shift(1).rolling(6, min_periods=2).median()
        rise = (df["hr"].rolling(3, center=True, min_periods=1).max() - hr_prev).fillna(0)
        sustained = moving & (moving.shift(1, fill_value=False) | moving.shift(-1, fill_value=False))
        moving_wake = moving & ((rise >= p["arousal_hr_rise"]) | (motion >= p["big_motion"])
                                | sustained)

    labels = np.full(len(df), NREM, dtype=object)
    no_data = df[["flow_present", "hr"]].isna().all(axis=1) | (
        df["flow_present"].eq(0) & df["hr"].isna())
    mask_off = df["flow_present"].eq(0) if has_flow else pd.Series(False, index=df.index)

    # REM vs quiet wake is the hard call without EEG. Both show irregular breathing and
    # a higher heart rate than NREM; wake is told apart by movement, mask removal, and a
    # clearly higher heart rate. So wake is "seeded" by hard evidence and then grown
    # into neighbouring epochs that still look aroused (hysteresis), and REM is what is
    # irregular + still + not attached to a wake bout.
    if has_oxi:
        rem_score = (0.7 * irr + 0.5 * hrv + 0.2 * hr) if has_flow else (0.8 * hrv + 0.4 * hr)
        wake_score = 0.8 * hr + 0.3 * irr + 0.2 * rr if has_flow else 1.0 * hr + 0.3 * hrv
    else:
        rem_score = 0.8 * irr + 0.2 * rr
        wake_score = 0.4 * irr + 0.6 * rr - 0.5  # only grows wake out of mask-off
    off_awake = mask_off & (~has_oxi | moving_near | (hr > 0.5))
    if has_oxi:
        off_awake |= mask_off & df["hr"].isna()
    seed = (moving_wake | off_awake).to_numpy()
    aroused = (wake_score > p["wake_threshold"]).to_numpy()
    if has_oxi:
        # Adaptive heart-rate cut: somewhere between this night's NREM-like HR
        # (regular breathing / low HR variability, still) and its movement-confirmed
        # wake HR. REM usually sits between the two.
        hr_s = df["hr"].rolling(3, center=True, min_periods=1).median()
        quiet = (~moving_near).to_numpy() & ((irr if has_flow else hrv) < 0).to_numpy()
        hr_w, hr_n = np.nanmedian(hr_s[seed]) if seed.sum() >= 6 else np.nan, \
            np.nanmedian(hr_s[quiet]) if quiet.sum() >= 20 else np.nan
        if np.isfinite(hr_w) and np.isfinite(hr_n) and hr_w > hr_n + 3:
            cut = hr_n + p["wake_hr_fraction"] * (hr_w - hr_n)
            aroused = (hr_s > cut).to_numpy()
    wake = seed.copy()
    for _ in range(len(wake)):
        nb = np.zeros_like(wake)
        nb[1:] |= wake[:-1]
        nb[:-1] |= wake[1:]
        new = wake | (nb & aroused)
        if (new == wake).all():
            break
        wake = new
    # quiet wakefulness: sustained aroused stretch with no movement at all. Needs the
    # ring's heart rate; flow alone cannot tell quiet wake from REM.
    if has_oxi:
        for s_, e_, lb in _runs(aroused):
            if lb and e_ - s_ >= p["quiet_wake_epochs"]:
                wake[s_:e_] = True
    bwake = np.zeros(len(df), dtype=bool)
    if not has_oxi and has_flow:
        # without the ring, long awake periods can only be read from the breathing
        bwake = _breathing_wake(df, seed, p)
        wake |= bwake
    labels[wake] = WAKE
    labels[(rem_score > p["rem_threshold"]).to_numpy() & ~wake & ~moving_near.to_numpy()] = REM

    if prior_probs is not None:
        known = ~np.isnan(prior_probs).any(axis=1)
        logit = np.log(np.clip(prior_probs[known], 1e-4, 1))
        # cardio-respiratory evidence nudges the model (it only sees pulse-derived beats)
        logit[:, 0] += 2.0 * wake[known] + 1.0 * moving_near.to_numpy()[known]
        logit[:, 2] += 0.5 * (rem_score.to_numpy()[known] - p["rem_threshold"]).clip(-2, 2) * has_flow
        labels[known] = np.array([WAKE, NREM, REM], dtype=object)[logit.argmax(axis=1)]
    labels[moving_wake.to_numpy() | off_awake.to_numpy() | bwake] = WAKE

    # --- sleep onset & final awakening ---
    onset_n = int(p["sleep_onset_min"] * 60 / EPOCH_S)
    onset = next((s for s, e, lb in _runs(labels) if lb != WAKE and e - s >= onset_n), None)
    if onset is None:
        labels[:] = WAKE
    else:
        labels[:onset] = WAKE
        last_sleep = max(i for i, lb in enumerate(labels) if lb != WAKE)
        labels[last_sleep + 1:] = WAKE
        # no REM within the first N minutes of sleep
        lat = onset + int(p["min_rem_latency_min"] * 60 / EPOCH_S)
        early = labels[onset:lat]
        early[early == REM] = NREM

    # --- bout cleanup: short REM blips -> NREM; isolated 1-epoch wake w/o motion -> neighbour
    for s, e, lb in list(_runs(labels)):
        if lb == REM and e - s < p["min_rem_bout_epochs"]:
            labels[s:e] = NREM
    for s, e, lb in list(_runs(labels)):
        if lb == WAKE and e - s == 1 and 0 < s < len(labels) - 1 and not moving_wake.iloc[s] \
                and not mask_off.iloc[s] and labels[s - 1] == labels[e]:
            labels[s] = labels[s - 1]

    df["stage"] = labels
    df.loc[no_data.to_numpy(), "stage"] = WAKE
    # deep-sleep flag inside NREM: 1/0 with the ring, NaN (unknown) without it
    df["deep"] = (_deep(df, df["stage"].to_numpy(), moving.to_numpy(), p).astype(float)
                  if has_oxi else np.nan)
    df["wake_score"] = wake_score.round(3)
    df["rem_score"] = rem_score.round(3)
    df["no_data"] = no_data.to_numpy()
    return df
