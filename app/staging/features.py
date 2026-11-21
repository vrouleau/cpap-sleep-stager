"""Per-epoch feature extraction from ResMed flow and O2Ring pulse/motion."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy.signal import butter, find_peaks, sosfiltfilt

from app.ingest.o2ring import OximetryNight
from app.ingest.resmed import FlowSegment, ResmedNight

EPOCH_S = 30
CONTEXT_S = 45  # features use epoch +/- 45 s (2-minute window) for stability
MIN_BREATHS = 6


def detect_breaths(seg: FlowSegment) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (breath peak times [s from seg.start], tidal amplitudes, filtered flow)."""
    fs = seg.fs
    flow = np.nan_to_num(seg.flow)
    if len(flow) < fs * 20:
        return np.array([]), np.array([]), flow
    sos = butter(2, [0.08, min(1.2, fs / 2 * 0.9)], btype="band", fs=fs, output="sos")
    filt = sosfiltfilt(sos, flow)
    ref = np.percentile(np.abs(filt), 90) or 1e-6
    peaks, props = find_peaks(filt, distance=int(1.2 * fs), prominence=0.2 * ref)
    # tidal proxy: peak-to-preceding-trough excursion
    amps = props["prominences"]
    return peaks / fs, amps, filt


def build_epochs(resmed: ResmedNight | None, oxi: OximetryNight | None) -> pd.DataFrame:
    """Build the 30-s epoch feature table covering the union of all recordings."""
    starts, ends = [], []
    if resmed and resmed.segments:
        starts.append(resmed.segments[0].start)
        ends.append(max(s.end for s in resmed.segments))
    if oxi is not None and not oxi.data.empty:
        starts.append(oxi.data["time"].iloc[0].to_pydatetime())
        ends.append(oxi.data["time"].iloc[-1].to_pydatetime())
    if not starts:
        raise ValueError("No usable ResMed BRP or O2Ring data found in upload")
    t0 = min(starts).replace(microsecond=0)
    t0 = t0 - timedelta(seconds=t0.second % EPOCH_S)
    n = int(np.ceil((max(ends) - t0).total_seconds() / EPOCH_S))
    ep_start = np.arange(n) * EPOCH_S
    ep_mid = ep_start + EPOCH_S / 2
    df = pd.DataFrame({"t": [t0 + timedelta(seconds=int(s)) for s in ep_start]})

    # ---------------- respiratory (ResMed BRP) ----------------
    cols = {k: np.full(n, np.nan) for k in
            ("resp_rate", "breath_cv", "amp_cv", "flow_present", "resp_events")}
    cols["flow_present"][:] = 0.0 if resmed and resmed.segments else np.nan
    if resmed and resmed.segments:
        all_t, all_a = [], []
        for seg in resmed.segments:
            off = (seg.start - t0).total_seconds()
            bt, ba, _ = detect_breaths(seg)
            all_t.append(bt + off)
            all_a.append(ba)
            # flow present = epoch where >=50% of 1-s blocks carry real breathing signal
            fsi = max(1, int(round(seg.fs)))
            nsec = len(seg.flow) // fsi
            sec_std = np.nanstd(seg.flow[: nsec * fsi].reshape(nsec, fsi), axis=1) > 0.02
            sec_ep = ((off + np.arange(nsec)) // EPOCH_S).astype(int)
            ok = (sec_ep >= 0) & (sec_ep < n)
            on = np.bincount(sec_ep[ok], weights=sec_std[ok], minlength=n)
            frac = on / EPOCH_S
            cols["flow_present"] = np.where(frac >= 0.5, 1.0, cols["flow_present"])
        bt = np.concatenate(all_t)
        ba = np.concatenate(all_a)
        order = np.argsort(bt)
        bt, ba = bt[order], ba[order]
        lo = np.searchsorted(bt, ep_mid - EPOCH_S / 2 - CONTEXT_S)
        hi = np.searchsorted(bt, ep_mid + EPOCH_S / 2 + CONTEXT_S)
        for i in range(n):
            if cols["flow_present"][i] < 1:
                continue
            t, a = bt[lo[i]:hi[i]], ba[lo[i]:hi[i]]
            if len(t) < MIN_BREATHS:
                continue
            ii = np.diff(t)
            ii = ii[ii < 15]  # ignore gaps between sessions
            if len(ii) < MIN_BREATHS - 1:
                continue
            cols["resp_rate"][i] = 60.0 / np.median(ii)
            cols["breath_cv"][i] = np.std(ii) / np.mean(ii)
            cols["amp_cv"][i] = np.std(a) / max(np.mean(a), 1e-6)
        cols["resp_events"][:] = 0
        for ev in resmed.events:
            e = int((ev.start - t0).total_seconds() // EPOCH_S)
            if 0 <= e < n:
                cols["resp_events"][e] += 1
    for k, v in cols.items():
        df[k] = v

    # ---------------- machine data (ResMed PLD, every 2 s) ----------------
    # what the machine did and saw: therapy pressure and leak (median per epoch), flow
    # limitation and snoring (max per epoch)
    for k in ("pressure", "leak", "flow_lim", "snore"):
        df[k] = np.nan
    if resmed and resmed.pld:
        acc: dict[str, list[pd.Series]] = {}
        for q in resmed.pld:
            off = (q.start - t0).total_seconds()
            for k, v in q.channels.items():
                ep = ((off + np.arange(len(v)) / q.fs) // EPOCH_S).astype(int)
                ok = (ep >= 0) & (ep < n) & np.isfinite(v)
                acc.setdefault(k, []).append(pd.Series(v[ok], index=ep[ok]))
        for k, parts in acc.items():
            ser = pd.concat(parts)
            agg = ser.groupby(level=0).max() if k in ("flow_lim", "snore") else ser.groupby(level=0).median()
            df.loc[agg.index, k] = agg.to_numpy()

    # ---------------- oximetry (O2Ring) ----------------
    for k in ("hr", "hr_std", "spo2", "spo2_min", "motion"):
        df[k] = np.nan
    if oxi is not None and not oxi.data.empty:
        o = oxi.data.copy()
        o["s"] = (o["time"] - pd.Timestamp(t0)).dt.total_seconds()
        o["ep"] = (o["s"] // EPOCH_S).astype(int)
        o = o[(o["ep"] >= 0) & (o["ep"] < n)]
        g = o.groupby("ep")
        df.loc[g.size().index, "hr"] = g["pulse"].mean()
        df.loc[g.size().index, "spo2"] = g["spo2"].mean()
        df.loc[g.size().index, "spo2_min"] = g["spo2"].min()
        df.loc[g.size().index, "motion"] = g["motion"].sum(min_count=1)
        # pulse variability over the 2-minute context window (1 Hz grid)
        hr = o.set_index(pd.to_timedelta(o["s"], unit="s"))["pulse"].resample("1s").mean() \
            .interpolate(limit=6, limit_area="inside")
        win = int(EPOCH_S + 2 * CONTEXT_S)
        hr_std = hr.rolling(win, center=True, min_periods=win // 3).std()
        idx = pd.to_timedelta(ep_mid, unit="s")
        df["hr_std"] = hr_std.reindex(idx, method="nearest", tolerance=pd.Timedelta("20s")).to_numpy()
    return df
