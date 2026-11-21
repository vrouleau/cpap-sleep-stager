"""What each awakening looks like: when, how long, out of which stage, and how it
started — the question being *why* someone wakes (e.g. the 2-5 am pattern).

Per awakening (a wake bout of >= 1 min between sleep onset and the final awakening,
plus the final awakening itself):

* ``onset`` — shape of the pulse in the hour before, from the O2Ring. The pulse is
  taken relative to the night's normal for the current stage (REM runs higher than
  NREM), so drifting into REM is not mistaken for a climb:
    - **gradual**: the pulse had already climbed >= ``gradual_rise_bpm`` over the last
      30-60 min (median of the last 5 min vs. 30-60 min before). A slow, internal build-
      up such as the body clock's morning rise (cortisol, adrenaline) fits this shape,
      as do a filling bladder or a blood-sugar dip. It cannot tell them apart, and it
      cannot measure cortisol.
    - **abrupt**: flat before, then a jump of >= ``abrupt_jump_bpm`` at the moment of
      waking: something woke you (noise, pain, a breathing event, a partner...).
    - **quiet**: no clear pulse change.
* ``from_rem`` — REM in the 5 min before: waking at the end of a REM period is normal;
  what matters is getting back to sleep.
* ``before`` — what happened in the 2 min before: CPAP events, O2 dips, movement.
* ``pulse_after`` — pulse 2-5 min into the wake vs. the hour before (wakes of 3 min or
  more): a pulse that is still up by then (``alert``) suggests a racing mind more than a
  brief wake-and-roll-over.
* ``mask_off`` — the mask came off during it (CPAP nights): you probably got up.

Plus ``wake_by_hour`` (minutes awake after sleep onset, by clock hour) and the minutes
awake between 02:00 and 05:00, so nights before and with CPAP can be compared.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from app.staging.engine import REM, WAKE, _runs
from app.staging.features import EPOCH_S

DEFAULTS = {
    "min_wake_epochs": 2,  # a wake bout of >= 1 min counts as an awakening
    "gradual_rise_bpm": 3.0,  # last 5 min vs. 30-60 min before
    "abrupt_jump_bpm": 8.0,  # peak around onset vs. the 5 min before
    "alert_after_bpm": 6.0,  # pulse while awake vs. the hour before
    "trigger_window_s": (-120, 30),
}
RESP_KINDS = {"Obstructive apnea", "Central apnea", "Apnea", "Hypopnea", "RERA"}
EARLY_HOURS = (2, 5)  # 02:00-05:00


def _pulse_1hz(oxi: pd.DataFrame, t0) -> pd.Series:
    s = (oxi["time"] - pd.Timestamp(t0)).dt.total_seconds()
    ser = pd.Series(oxi["pulse"].to_numpy(dtype=float), index=pd.to_timedelta(s, unit="s"))
    ser = ser[~ser.index.duplicated()].resample("1s").mean().interpolate(limit=8, limit_area="inside")
    ser.index = ser.index.total_seconds().astype(int)
    return ser


def _med(hr: pd.Series | None, a: float, b: float) -> float | None:
    if hr is None:
        return None
    w = hr.loc[int(a):int(b)].dropna()
    return float(w.median()) if len(w) >= max(20, (b - a) * 0.4) else None


def detect(epochs: pd.DataFrame, oxi: pd.DataFrame | None, events: list[dict],
           params: dict | None = None) -> list[dict]:
    p = {**DEFAULTS, **(params or {})}
    st = epochs["stage"].to_numpy()
    sleep = st != WAKE
    if not sleep.any():
        return []
    t0 = pd.Timestamp(epochs["t"].iloc[0])
    first = int(np.argmax(sleep))
    last = int(len(sleep) - 1 - np.argmax(sleep[::-1]))
    hr = _pulse_1hz(oxi, t0) if oxi is not None and not oxi.empty and oxi["pulse"].notna().any() else None
    if hr is not None:
        # REM runs a few bpm above NREM; take that offset out so drifting into REM before
        # waking is not mistaken for a climb. Wake keeps its raw level (that jump is the
        # point).
        ep_idx = np.clip((hr.index.to_numpy() // EPOCH_S).astype(int), 0, len(st) - 1)
        stage_at = st[ep_idx]
        v = hr.to_numpy()
        n_med = np.nanmedian(v[stage_at == "NREM"]) if (stage_at == "NREM").any() else np.nan
        r_med = np.nanmedian(v[stage_at == REM]) if (stage_at == REM).any() else np.nan
        if np.isfinite(n_med) and np.isfinite(r_med):
            v = np.where(stage_at == REM, v - (r_med - n_med), v)
        hr = pd.Series(v, index=hr.index)
    flow = epochs["flow_present"].to_numpy() \
        if "flow_present" in epochs and epochs["flow_present"].notna().any() else None
    ev = [((pd.Timestamp(e["t"]) - t0).total_seconds(), e) for e in events]

    bouts = [(s, e, "night") for s, e, lb in _runs(st[first:last + 1])
             if lb == WAKE and e - s >= p["min_wake_epochs"]]
    bouts = [(s + first, e + first, k) for s, e, k in bouts]
    if last + 1 < len(st):
        bouts.append((last + 1, len(st), "final"))

    out = []
    for s, e, kind in bouts:
        on = s * EPOCH_S  # seconds from t0
        w = {"t": (t0 + timedelta(seconds=on)).to_pydatetime(), "kind": kind,
             "minutes": round((e - s) * EPOCH_S / 60, 1),
             "stage_before": st[s - 1] if s > 0 else None,
             "from_rem": bool((st[max(0, s - 10):s] == REM).any())}
        # pulse shape: find the actual step near the scored wake time (a slowly climbing
        # pulse makes the scoring call wake several minutes early), then look at the hour
        # before that step
        jump, ref_on = None, on
        if hr is not None:
            best = None
            for t in range(int(on - 300), int(on + 601), 5):
                a_, b_ = _med(hr, t - 120, t - 10), _med(hr, t + 10, t + 60)
                if a_ is not None and b_ is not None and (best is None or b_ - a_ > best[0]):
                    best = (b_ - a_, t)
            if best is not None:
                jump = round(best[0], 1)
                if jump >= p["abrupt_jump_bpm"]:
                    ref_on = best[1]
        base = _med(hr, ref_on - 3600, ref_on - 1800)
        late = _med(hr, ref_on - 300, ref_on - 30)
        w["rise_before"] = round(late - base, 1) if base is not None and late is not None else None
        w["jump_at_wake"] = jump
        if w["rise_before"] is None and jump is None:
            w["onset"] = None
        elif w["rise_before"] is not None and w["rise_before"] >= p["gradual_rise_bpm"]:
            w["onset"] = "gradual"
        elif jump is not None and jump >= p["abrupt_jump_bpm"]:
            w["onset"] = "abrupt"
        else:
            w["onset"] = "quiet"
        pre5 = _med(hr, ref_on - 360, ref_on - 60)
        # minutes 2-5 of the wake: a brief wake-and-roll-over has settled by then, a
        # racing mind has not (needs >= 3 min awake)
        during = _med(hr, on + 120, on + 300) if (e - s) * EPOCH_S >= 180 else None
        ref = base if base is not None else pre5
        w["pulse_after"] = round(during - ref, 1) if during is not None and ref is not None else None
        w["alert"] = bool(w["pulse_after"] is not None and w["pulse_after"] >= p["alert_after_bpm"])
        # what happened just before
        a, b = p["trigger_window_s"]
        before = set()
        for x, evd in ev:
            if not (on + a <= x <= on + b):
                continue
            if evd["source"] == "resmed" and evd["kind"] in RESP_KINDS:
                before.add("CPAP event")
            elif evd["source"] == "o2ring":
                before.add("O2 dip")
            elif evd["source"] == "arousal" and "movement" in evd["kind"]:
                before.add("movement")
        w["before"] = sorted(before)
        w["mask_off"] = bool(np.nansum(flow[s:e] == 0) > 0) if flow is not None and kind == "night" else None
        out.append(w)
    return out


def summarize(epochs: pd.DataFrame, wakeups: list[dict]) -> dict:
    """Night-level numbers for the trend comparison."""
    st = epochs["stage"].to_numpy()
    sleep = st != WAKE
    s: dict = {}
    if sleep.any():
        first = int(np.argmax(sleep))
        last = int(len(sleep) - 1 - np.argmax(sleep[::-1]))
        hours = pd.to_datetime(epochs["t"]).dt.hour.to_numpy()
        waso = (st == WAKE) & (np.arange(len(st)) >= first) & (np.arange(len(st)) <= last)
        by_hour = {}
        for h in np.unique(hours[waso]):
            by_hour[f"{int(h):02d}"] = round(float((waso & (hours == h)).sum()) * EPOCH_S / 60, 1)
        s["wake_by_hour"] = by_hour
        lo, hi = EARLY_HOURS
        s["wake_2to5_min"] = round(float((waso & (hours >= lo) & (hours < hi)).sum()) * EPOCH_S / 60, 1)
    night = [w for w in wakeups if w["kind"] == "night"]
    s["wakeups"] = [{**{k: v for k, v in w.items() if k != "t"},
                     "t": w["t"].isoformat(timespec="seconds")} for w in wakeups]
    s["wakeups_night"] = len(night)
    s["wakeups_2to5"] = sum(1 for w in night if EARLY_HOURS[0] <= w["t"].hour < EARLY_HOURS[1])
    for k in ("gradual", "abrupt", "quiet"):
        s[f"wakeups_{k}"] = sum(1 for w in night if w["onset"] == k)
    s["wakeups_from_rem"] = sum(1 for w in night if w["from_rem"])
    s["wakeups_alert"] = sum(1 for w in night if w["alert"])
    return s
