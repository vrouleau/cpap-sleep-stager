"""What each awakening looks like: when, how long, out of which stage, and how it
started — the question being *why* someone wakes (e.g. the 2-5 am pattern).

Per awakening (wake bouts between sleep onset and the final awakening, merged when
separated by <= 5 min of sleep, kept when >= 5 min long; plus the final awakening):

* ``onset`` — did the pulse build up before waking (O2Ring, relative to the night's
  normal for the current stage, as REM runs higher than NREM)?
    - **gradual**: it had already climbed >= ``gradual_rise_bpm`` over the last 30-60
      min — a slow internal build-up such as the body clock's morning rise (cortisol,
      adrenaline), a filling bladder or a blood-sugar dip; it cannot tell them apart
      and cannot measure cortisol.
    - **none**: no build-up. The pulse still jumps at the moment of waking, but waking
      itself does that, so the jump (``jump_at_wake``, kept for information) says
      nothing about the cause.
* ``from_rem`` — REM in the 5 min before: waking at the end of a REM period is normal;
  what matters is getting back to sleep.
* ``before`` — what happened just before: CPAP events, O2 dips, movement (2 min), and
  from the machine's PLD data: a pressure rise (auto-CPAP responding to flow
  limitation / snoring), flow limitation or snoring from 10 min before the scored start
  to 25 min into it (they mean you were still asleep; slow wake-ups are often scored
  early), and large leak from 10 min before to 2 min after (later leak is usually from
  moving once awake).
* ``pulse_after`` — pulse 2-5 min into the wake vs. the hour before (wakes of 3 min or
  more): a pulse that is still up by then (``alert``) suggests a racing mind more than a
  brief wake-and-roll-over.
* ``mask_off`` — the mask came off during it (CPAP nights): you probably got up.
  ``mask_off_after_min``: how long after waking it came off (straight away = an urge or
  a decision to get up; after lying awake a while = going "while at it"), and
  ``back_on_awake_min``: time still awake after the mask went back on.

Plus ``wake_by_hour`` (minutes awake after sleep onset, by clock hour) and the minutes
awake between 02:00 and 05:00.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from app.staging.engine import REM, WAKE, _runs
from app.staging.features import EPOCH_S

DEFAULTS = {
    # Wake bouts separated by <= 5 min of "sleep" are one awakening (a minute of dozing
    # inside a long wake is not two wake-ups), and only awakenings of >= 5 min are listed:
    # shorter ones are rarely remembered; they are counted as brief wakes instead.
    "merge_gap_epochs": 10,
    "min_wake_epochs": 10,
    "gradual_rise_bpm": 3.0,  # last 5 min vs. 30-60 min before
    "wake_step_bpm": 6.0,  # a sustained pulse step this big marks the moment of waking
    "alert_after_bpm": 6.0,  # pulse while awake vs. the hour before
    "trigger_window_s": (-120, 30),
    # machine data (ResMed PLD) in the 10 min before waking
    "machine_window_epochs": 20,
    "pressure_rise_cmh2o": 1.0,  # vs. the median 10-20 min before
    "large_leak_lpm": 24.0,  # ResMed's "large leak" level
    "flow_lim_level": 0.3,  # 0-1 scale
    "snore_level": 0.2,
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
    hr_raw = hr
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

    raw = [(s + first, e + first) for s, e, lb in _runs(st[first:last + 1]) if lb == WAKE]
    merged: list[list[int]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] <= p["merge_gap_epochs"]:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    bouts = [(s, e, "night") for s, e in merged if e - s >= p["min_wake_epochs"]]
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
            # The moment you actually woke: the first point after which the pulse *stays*
            # up (3-min median vs. the 5 min before). Brief arousal surges (10-15 s) do not
            # move a 3-min median; getting up later is a bigger step, hence the first.
            # Searched up to 25 min into a long wake-up.
            best = first = None
            stop = min(on + 1500, e * EPOCH_S) if kind == "night" else on + 600
            for t in range(int(on - 300), int(max(stop, on + 600)) + 1, 10):
                # raw pulse here: the REM adjustment would put a fake step at every
                # REM -> wake boundary of the scoring
                a_, b_ = _med(hr_raw, t - 300, t - 10), _med(hr_raw, t + 10, t + 180)
                if a_ is None or b_ is None:
                    continue
                if best is None or b_ - a_ > best[0]:
                    best = (b_ - a_, t)
                if first is None and b_ - a_ >= p["wake_step_bpm"]:
                    first = (b_ - a_, t)
            pick = first or best
            if pick is not None:
                jump = round(pick[0], 1)
                if jump >= p["wake_step_bpm"]:
                    ref_on = pick[1]
        w["woke_at"] = (t0 + timedelta(seconds=int(ref_on))).to_pydatetime() \
            if abs(ref_on - on) >= 120 else None
        base = _med(hr, on - 3600, on - 1800)
        late = _med(hr, on - 300, on - 30)
        w["rise_before"] = round(late - base, 1) if base is not None and late is not None else None
        w["jump_at_wake"] = jump
        # Waking itself raises the pulse, so a jump at the moment of waking says nothing
        # about the cause; only a climb *before* waking is informative.
        if w["rise_before"] is None:
            w["onset"] = None
        elif w["rise_before"] >= p["gradual_rise_bpm"]:
            w["onset"] = "gradual"
        else:
            w["onset"] = "none"
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
            if not (ref_on + a <= x <= ref_on + b):
                continue
            if evd["source"] == "resmed" and evd["kind"] in RESP_KINDS:
                before.add("CPAP event")
            elif evd["source"] == "o2ring":
                before.add("O2 dip")
            elif evd["source"] == "arousal" and "movement" in evd["kind"]:
                before.add("movement")
        # what the machine did / saw in the 10 min before
        # Machine data: 10 min either side of the scored start (or of the pulse-located
        # moment of waking, whichever is earlier/later). The scoring can be minutes off,
        # and an auto-CPAP raising pressure for flow limitation means you were still
        # asleep, so a rise in a scored "wake" still came before you really woke.
        # Pressure rise, flow limitation and snoring imply you were still asleep, so they
        # count up to 25 min into a scored wake (a slowly building wake-up is often
        # scored that early). Leak is often the *result* of moving once awake, so it
        # only counts until 2 min after the scored start.
        s_ref = min(len(st) - 1, int(ref_on // EPOCH_S))
        mw = p["machine_window_epochs"]
        k0 = max(0, min(s, s_ref) - mw)
        k1 = min(len(st), max(s + 4, s_ref + 4, s + min(50, e - s)))
        k1_leak = min(len(st), min(s, s_ref) + 4)
        def col(name):
            return epochs[name].to_numpy(dtype=float) if name in epochs else None
        pr, lk, fl, sn = col("pressure"), col("leak"), col("flow_lim"), col("snore")
        if pr is not None and np.isfinite(pr[k0:k1]).any():
            ref = pr[max(0, k0 - mw):k0]  # the 10 min before the window
            ref = np.nanmedian(ref) if np.isfinite(ref).any() else np.nanmin(pr[k0:k1])
            w["pressure_rise"] = round(float(np.nanmax(pr[k0:k1]) - ref), 1)
            if w["pressure_rise"] >= p["pressure_rise_cmh2o"]:
                before.add("pressure rise")
        if lk is not None and np.isfinite(lk[k0:k1_leak]).any():
            w["leak_max"] = round(float(np.nanmax(lk[k0:k1_leak])), 1)
            if w["leak_max"] >= p["large_leak_lpm"]:
                before.add("leak")
        if fl is not None and np.isfinite(fl[k0:k1]).any() and np.nanmax(fl[k0:k1]) >= p["flow_lim_level"]:
            before.add("flow limitation")
        if sn is not None and np.isfinite(sn[k0:k1]).any() and np.nanmax(sn[k0:k1]) >= p["snore_level"]:
            before.add("snoring")
        w["before"] = sorted(before)
        w["mask_off"] = bool(np.nansum(flow[s:e] == 0) > 0) if flow is not None and kind == "night" else None
        w["awake_min"] = round(float((st[s:e] == WAKE).sum()) * EPOCH_S / 60, 1)
        w["mask_off_after_min"] = w["back_on_awake_min"] = None
        if w["mask_off"]:
            off = s + int(np.argmax(flow[s:e] == 0))
            on_again = off
            while on_again < e and flow[on_again] == 0:
                on_again += 1
            w["mask_off_after_min"] = round((off - s) * EPOCH_S / 60, 1)
            w["back_on_awake_min"] = round(float((st[on_again:e] == WAKE).sum()) * EPOCH_S / 60, 1)
        out.append(w)
    return out


def _brief_wakes(st: np.ndarray, max_epochs: int = DEFAULTS["min_wake_epochs"]) -> int:
    """Wake marks of 1 to < 5 min inside the night (not listed as awakenings)."""
    sleep = st != WAKE
    if not sleep.any():
        return 0
    first = int(np.argmax(sleep))
    last = int(len(sleep) - 1 - np.argmax(sleep[::-1]))
    return sum(1 for a, b, lb in _runs(st[first:last + 1]) if lb == WAKE and 2 <= b - a < max_epochs)


def summarize(epochs: pd.DataFrame, wakeups: list[dict]) -> dict:
    """Night-level wake-up numbers."""
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
    s["brief_wakes"] = _brief_wakes(st)
    s["wakeups"] = [{**{k: v for k, v in w.items() if k not in ("t", "woke_at")},
                     "t": w["t"].isoformat(timespec="seconds"),
                     "woke_at": w["woke_at"].isoformat(timespec="seconds") if w.get("woke_at") else None}
                    for w in wakeups]
    s["wakeups_night"] = len(night)
    s["wakeups_2to5"] = sum(1 for w in night if EARLY_HOURS[0] <= w["t"].hour < EARLY_HOURS[1])
    for k in ("gradual", "none"):
        s[f"wakeups_{k}"] = sum(1 for w in night if w["onset"] == k)
    s["wakeups_triggered"] = sum(1 for w in night if w["before"])
    for k in ("pressure rise", "leak", "flow limitation", "snoring", "CPAP event", "O2 dip", "movement"):
        s[f"wakeups_before_{k.replace(' ', '_').lower()}"] = sum(1 for w in night if k in w["before"])
    s["wakeups_from_rem"] = sum(1 for w in night if w["from_rem"])
    s["wakeups_alert"] = sum(1 for w in night if w["alert"])
    return s
