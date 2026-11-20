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
    "min_rem_latency_min": 45,
    "sleep_onset_min": 5,
    "min_rem_bout_epochs": 3,
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
    seed = (moving | off_awake).to_numpy()
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
    labels[wake] = WAKE
    labels[(rem_score > p["rem_threshold"]).to_numpy() & ~wake & ~moving_near.to_numpy()] = REM

    if prior_probs is not None:
        known = ~np.isnan(prior_probs).any(axis=1)
        logit = np.log(np.clip(prior_probs[known], 1e-4, 1))
        # cardio-respiratory evidence nudges the model (it only sees pulse-derived beats)
        logit[:, 0] += 2.0 * wake[known] + 1.0 * moving_near.to_numpy()[known]
        logit[:, 2] += 0.5 * (rem_score.to_numpy()[known] - p["rem_threshold"]).clip(-2, 2) * has_flow
        labels[known] = np.array([WAKE, NREM, REM], dtype=object)[logit.argmax(axis=1)]
    labels[moving.to_numpy() | off_awake.to_numpy()] = WAKE

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
        if lb == WAKE and e - s == 1 and 0 < s < len(labels) - 1 and not moving.iloc[s] \
                and not mask_off.iloc[s] and labels[s - 1] == labels[e]:
            labels[s] = labels[s - 1]

    df["stage"] = labels
    df.loc[no_data.to_numpy(), "stage"] = WAKE
    df["wake_score"] = wake_score.round(3)
    df["rem_score"] = rem_score.round(3)
    df["no_data"] = no_data.to_numpy()
    return df
