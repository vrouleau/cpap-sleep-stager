"""Arousal detection and cause tagging (O2Ring nights).

Two kinds of arousal are collected into one list:

1. **Pulse surges inside sleep.** A cortical arousal (3-15 s of wake-like EEG) is
   almost always accompanied by a sympathetic surge: the pulse jumps within a few beats
   and recovers over ~10-20 s. Most never turn a 30-s epoch into "awake", so the
   epoch-level staging cannot see them. We look for them directly on the 1-Hz pulse:

       rise(t) = pulse(t) - median(pulse over the preceding 60 s, ending 5 s before t)

   A surge counts when ``rise`` peaks at >= ``min_rise_bpm`` and >= ``noise_k`` times
   the pulse's normal wobble over the last 5 min (so REM's naturally jumpier pulse
   needs a bigger jump), the peak is reached within ``max_rise_time_s`` (a surge, not a
   slow drift), and the epoch and the one before it are asleep.

2. **Brief wake marks that stayed asleep.** The stager already turns a <= 1 min wake
   mark without a real awakening into an arousal (``epochs.arousal``). Any such run
   with no surge inside it is added too (e.g. turning over without a pulse change).

Each arousal is tagged with its likely trigger, first match wins:

* respiratory - a ResMed apnea / hypopnea / RERA ended just before it
* desaturation - follows an O2Ring >= 4% desaturation with no scored CPAP event
* movement - comes with movement on the ring
* spontaneous - nothing obvious (pain, bladder, temperature, alcohol, noise, ...)

Pulse-surge scoring is an established surrogate (several home sleep tests rely on
similar signals) but it is not EEG scoring: on simulated nights it finds ~80-90% of
arousals and over-counts by ~10%. Use the index for trends.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from app.staging.engine import WAKE
from app.staging.features import EPOCH_S

DEFAULTS = {
    "min_rise_bpm": 7.0,  # surge size vs. the preceding minute
    "noise_k": 3.0,  # ...and at least this many local-noise SDs
    "max_rise_time_s": 10,  # onset -> peak must be quick (a surge, not REM drift)
    "min_separation_s": 30,
    "resp_window_s": (-15, 12),  # CPAP event end relative to arousal onset
    "desat_window_s": (-45, 10),  # desaturation start relative to arousal onset
}
RESP_KINDS = {"Obstructive apnea", "Central apnea", "Apnea", "Hypopnea", "RERA"}
CAUSES = ("respiratory", "desaturation", "movement", "spontaneous")
KIND_PREFIX = "Arousal"


def kind_for(cause: str) -> str:
    return f"{KIND_PREFIX} ({cause})"


def cause_of(kind: str) -> str | None:
    """'Arousal (movement)' -> 'movement'; None for non-arousal events."""
    if not kind.startswith(KIND_PREFIX):
        return None
    c = kind[len(KIND_PREFIX):].strip(" ()")
    return c if c in CAUSES else "spontaneous"


def _series(oxi: pd.DataFrame, col: str, t0) -> pd.Series:
    s = (oxi["time"] - pd.Timestamp(t0)).dt.total_seconds()
    ser = pd.Series(oxi[col].to_numpy(dtype=float), index=pd.to_timedelta(s, unit="s"))
    ser = ser[~ser.index.duplicated()].resample("1s").mean()
    ser.index = ser.index.total_seconds().astype(int)
    return ser


def _surges(hr: pd.Series, p: dict) -> list[tuple[int, int, float]]:
    """(onset_s, end_s, rise_bpm) for each sharp pulse surge, seconds from t0."""
    idx0 = int(hr.index[0])
    v = hr.to_numpy()
    base = pd.Series(v).rolling(55, min_periods=30).median().shift(5).to_numpy()
    rise = v - base
    rise_f = np.nan_to_num(rise, nan=0.0)
    # local noise: robust SD of the rise signal over the previous 5 min
    r = pd.Series(rise)
    noise = (r - r.rolling(300, min_periods=60).median()).abs() \
        .rolling(300, min_periods=60).median().shift(20) * 1.4826
    thr = np.maximum(p["min_rise_bpm"], p["noise_k"] * noise.fillna(0).to_numpy())
    peaks, props = find_peaks(rise_f, height=p["min_rise_bpm"], distance=p["min_separation_s"],
                              prominence=p["min_rise_bpm"] * 0.7)
    out = []
    for pk, height in zip(peaks, props["peak_heights"]):
        if height < thr[pk]:
            continue
        lo = max(0, pk - 20)
        below = np.where(rise_f[lo:pk] < height * 0.3)[0]
        onset = lo + below[-1] + 1 if len(below) else pk - 5
        if pk - onset > p["max_rise_time_s"]:
            continue  # slow drift, not a surge
        after = np.where(rise_f[pk:pk + 60] < height * 0.3)[0]
        end = pk + (after[0] if len(after) else 20)
        out.append((idx0 + onset, idx0 + end, float(height)))
    return out


def detect(epochs: pd.DataFrame, oxi: pd.DataFrame | None, events: list[dict],
           params: dict | None = None) -> list[dict]:
    """Return arousals as dicts: t, duration_s, cause, stage, rise_bpm (None for
    epoch-flag arousals without a surge), motion."""
    if oxi is None or oxi.empty or oxi["pulse"].notna().sum() < 300:
        return []
    p = {**DEFAULTS, **(params or {})}
    t0 = pd.Timestamp(epochs["t"].iloc[0])
    hr = _series(oxi, "pulse", t0).interpolate(limit=8, limit_area="inside")
    if hr.empty:
        return []
    mot = _series(oxi, "motion", t0).fillna(0) if "motion" in oxi else None
    stages = epochs["stage"].to_numpy()
    n = len(stages)

    resp_ends = np.array(sorted((pd.Timestamp(e["t"]) - t0).total_seconds() + e["duration_s"]
                                for e in events
                                if e["source"] == "resmed" and e["kind"] in RESP_KINDS))
    desat_starts = np.array(sorted((pd.Timestamp(e["t"]) - t0).total_seconds()
                                   for e in events if e["source"] == "o2ring"))

    def moved(a: float, b: float) -> bool:
        if mot is None:
            return False
        m = mot.reindex(range(int(a), int(b) + 1)).fillna(0)
        return bool((m > 0).any())

    def tag(t_on: float, t_last: float) -> str:
        """Cause for an arousal starting at t_on; t_last = latest plausible onset."""
        a, b = p["resp_window_s"]
        if len(resp_ends) and np.any((resp_ends >= t_on + a) & (resp_ends <= t_last + b)):
            return "respiratory"
        a, b = p["desat_window_s"]
        if len(desat_starts) and np.any((desat_starts >= t_on + a) & (desat_starts <= t_last + b)):
            return "desaturation"
        if moved(t_on - 5, t_last + 10):
            return "movement"
        return "spontaneous"

    out: list[dict] = []
    # 1) pulse surges during sleep
    for on, end, rise in _surges(hr, p):
        ep = int(on // EPOCH_S)
        if not (1 <= ep < n) or stages[ep] == WAKE or stages[ep - 1] == WAKE:
            continue
        out.append({"t_s": on, "duration_s": float(end - on), "rise_bpm": round(rise, 1),
                    "cause": tag(on, on), "stage": stages[ep], "motion": moved(on - 5, on + 10)})

    # 2) brief wake marks the stager kept asleep, unless a surge already covers them
    if "arousal" in epochs:
        flags = epochs["arousal"].fillna(0).to_numpy() > 0
        i = 0
        while i < n:
            if not flags[i]:
                i += 1
                continue
            j = i
            while j < n and flags[j]:
                j += 1
            a_s, b_s = i * EPOCH_S, j * EPOCH_S
            if not any(a_s - 15 <= x["t_s"] <= b_s + 15 for x in out):
                out.append({"t_s": a_s, "duration_s": float(b_s - a_s), "rise_bpm": None,
                            "cause": tag(a_s, b_s), "stage": stages[i],
                            "motion": moved(a_s, b_s)})
            i = j

    out.sort(key=lambda x: x["t_s"])
    for x in out:
        x["t"] = (t0 + timedelta(seconds=int(x.pop("t_s")))).to_pydatetime()
    return out


def summarize(arousals: list[dict], tst_min: float) -> dict:
    """Arousal count, index (per hour asleep) and per-cause breakdown."""
    hours = tst_min / 60 if tst_min else 0
    s: dict = {"arousals": len(arousals),
               "arousal_index": round(len(arousals) / hours, 1) if hours else None}
    by = {c: sum(1 for a in arousals if a["cause"] == c) for c in CAUSES}
    s["arousals_by_cause"] = by
    s["arousal_index_by_cause"] = {c: round(v / hours, 2) for c, v in by.items()} if hours else {}
    return s
