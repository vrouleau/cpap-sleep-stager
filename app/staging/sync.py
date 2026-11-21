"""Estimate the clock offset between the ResMed machine and the O2Ring.

The O2Ring takes its time from the phone app on every sync, so it is treated as the
reference clock. ResMed machines never adjust for daylight saving time (AirSense 10
needs a manual clock change; AirSense 11 has no user-settable clock), so the CPAP is
the one that drifts or ends up an hour off.

Convention: ``cpap_offset_s`` is ADDED to ResMed timestamps to put them on real (ring)
time. A machine still on summer time after the fall-back -> offset -3600.

Primary method - mask on/off vs. ring motion
    Every BRP file starts when the mask goes on and ends when it comes off; both
    involve hand movement the ring records. We slide the ring's motion trace by up to
    +/-2 h (drift plus a possible 1 h DST error) and pick the shift that lands the most
    motion energy right on those instants. Mid-night mask-off/on pairs are the sharpest
    markers because the rest of the night is still.

Anchor - morning mask-off vs ring-off
    You take the mask and the ring off within a minute or two of each other, so the
    difference between the last CPAP session's end and the ring's last reading is the
    clock error to within a few minutes. When it is close to a whole hour (no error, or
    a missed DST change) the motion search is narrowed to +/-2 min around that hour.
    Without the anchor the +/-2 h motion search can lock onto a chance cluster of
    movement 45 or 95 min away on nights with few mask on/off markers.

Cross-check - event-triggered SpO2
    Average SpO2 around the end of every ResMed apnea/hypopnea. The finger nadir
    normally lands ~20-40 s after the event ends (circulation delay), so this can only
    confirm the motion estimate to within ~+/-15 s, never set it precisely.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# AirSense 11 units have been reported drifting by tens of minutes over months (they
# never resync), on top of a possible 1 h DST error, so search one continuous range.
SEARCH_S = 7200  # +/- 2 hours
MARKER_WIN_S = 6  # motion within +/- this many seconds of a marker counts
ANCHOR_TOL_S = 600  # morning end difference within 10 min of a whole hour -> anchor to it
ANCHORED_SPAN_S = 120  # then search motion only +/- 2 min around the anchor
EXPECTED_DESAT_LAG_S = 30.0  # typical apnea-end -> finger SpO2 nadir delay
APNEA_KINDS = {"Obstructive apnea", "Central apnea", "Apnea", "Hypopnea"}


def _ring_seconds(oxi: pd.DataFrame, t0: datetime, col: str) -> pd.Series:
    """Resample a ring column onto a 1-s grid (seconds since t0, ring clock)."""
    s = (oxi["time"] - pd.Timestamp(t0)).dt.total_seconds()
    ser = pd.Series(oxi[col].to_numpy(dtype=float), index=pd.to_timedelta(s, unit="s"))
    ser = ser[~ser.index.duplicated()].resample("1s").mean()
    ser.index = ser.index.total_seconds().astype(int)
    return ser


def coarse_shift(segments, oxi: pd.DataFrame) -> float:
    """Rough ring-vs-CPAP clock difference (ring shift convention, seconds) from when
    each device's recording starts and ends. Only meaningful for big gaps such as
    travelling across time zones; returns 0 when the clocks are already within 2 h."""
    ring0 = oxi["time"].iloc[0].to_pydatetime()
    ring1 = oxi["time"].iloc[-1].to_pydatetime()
    d0 = (segments[0].start - ring0).total_seconds()
    d1 = (max(s.end for s in segments) - ring1).total_seconds()
    c = float(np.median([d0, d1]))
    return c if abs(c) > SEARCH_S else 0.0


def end_offset(segments, oxi: pd.DataFrame) -> float | None:
    """CPAP correction (seconds) implied by the morning: ring's last reading minus the
    end of the last CPAP session."""
    valid = oxi.loc[oxi["spo2"].notna() | oxi["pulse"].notna(), "time"]
    if valid.empty:
        return None
    return (valid.iloc[-1].to_pydatetime() - max(s.end for s in segments)).total_seconds()


def motion_offset(markers: list[datetime], oxi: pd.DataFrame,
                  centre: float = 0.0, span: int = SEARCH_S) -> dict | None:
    """Residual offset from mask on/off markers (machine clock) vs ring motion."""
    if not markers or "motion" not in oxi or oxi["motion"].fillna(0).sum() == 0:
        return None
    t0 = min(min(markers) - timedelta(seconds=abs(centre) + span),
             oxi["time"].iloc[0].to_pydatetime())
    mot = _ring_seconds(oxi, t0, "motion").ffill(limit=4).fillna(0.0)
    if mot.empty:
        return None
    # energy = motion smeared over the marker window, normalised by night-level activity
    energy = np.log1p(mot.clip(lower=0).to_numpy())
    kernel = np.ones(2 * MARKER_WIN_S + 1)
    energy = np.convolve(energy, kernel, mode="same")
    base_idx = int(mot.index[0])
    m_sec = np.array([(m - t0).total_seconds() for m in markers])

    lags = int(round(centre)) + np.arange(-span, span + 1)
    scores = np.zeros(len(lags))
    hits = np.zeros(len(lags), dtype=int)
    for k, off in enumerate(lags):
        idx = (m_sec - off).astype(int) - base_idx  # ring time = machine time - offset
        ok = (idx >= 0) & (idx < len(energy))
        vals = energy[idx[ok]]
        scores[k] = vals.sum()
        hits[k] = int((vals > 0).sum())
    if scores.max() <= 0:
        return None
    # prefer the smallest |offset| among near-ties (clock errors are usually small)
    best_val = scores.max()
    cand = np.where(scores >= best_val * 0.98)[0]
    best = cand[np.argmin(np.abs(lags[cand]))]
    off = int(lags[best])
    # Confidence: how far the peak stands above the best *other* candidate shift
    # (anything more than 30 s away), and how many markers it explains.
    far = np.abs(lags - off) > 30
    runner_up = float(scores[far].max()) if far.any() else 0.0
    prominence = min(best_val / max(runner_up, 1e-9), 99.0)
    n_hit = int(hits[best])
    conf = "high" if (prominence >= 1.5 and n_hit >= 3) else "medium" if prominence >= 1.15 else "low"
    return {
        "residual_s": float(off),
        "markers": len(markers),
        "markers_matched": n_hit,
        "prominence": round(float(prominence), 2),
        "confidence": conf,
    }


def desat_offset(events: list[dict], oxi: pd.DataFrame) -> dict | None:
    """Residual offset implied by the average SpO2 response to ResMed events."""
    ev_ends = [e["t"].timestamp() + e["duration_s"] for e in events
               if e["source"] == "resmed" and e["kind"] in APNEA_KINDS]
    if len(ev_ends) < 5 or oxi["spo2"].notna().sum() < 100:
        return None
    t0 = oxi["time"].iloc[0].to_pydatetime()
    spo2 = _ring_seconds(oxi, t0, "spo2").interpolate(limit=8, limit_area="inside")
    base = int(spo2.index[0])
    arr = spo2.to_numpy()
    rel = np.arange(-60, 121)
    curves = []
    for end in ev_ends:
        c = int(end - t0.timestamp()) - base
        idx = c + rel
        if idx[0] < 0 or idx[-1] >= len(arr):
            continue
        seg = arr[idx]
        if np.isnan(seg).mean() > 0.2:
            continue
        curves.append(seg - np.nanmedian(seg[:40]))  # baseline: 60..20 s before end
    if len(curves) < 5:
        return None
    avg = pd.Series(np.nanmean(curves, axis=0)).rolling(7, center=True, min_periods=1).mean()
    nadir_i = int(avg.idxmin())
    depth = float(-avg.min())
    if depth < 1.0:
        return {"events_used": len(curves), "depth_pct": round(depth, 2), "usable": False}
    lag = float(rel[nadir_i])
    return {
        "events_used": len(curves),
        "depth_pct": round(depth, 2),
        "nadir_lag_s": lag,
        "residual_s": round(EXPECTED_DESAT_LAG_S - lag, 1),
        "usable": True,
    }


def estimate(segments, events: list[dict], oxi: pd.DataFrame | None,
             current_offset: float) -> dict | None:
    """Sync report. ``segments``/``events`` must already carry ``current_offset``.

    The helpers above measure how far the *ring* would have to move; the CPAP has to
    move the same amount the other way.
    """
    if oxi is None or oxi.empty or not segments:
        return None
    markers = []
    for s in segments:
        markers += [s.start, s.end]
    end = end_offset(segments, oxi)
    # The whole hour is about the machine's clock, so judge it on the full correction
    # (current + what the morning shows), not on the residual: otherwise a night already
    # nudged to e.g. +58:32 would anchor to "+0 from here" and keep chasing itself.
    hour_abs = round((current_offset + end) / 3600) * 3600 if end is not None else None
    anchored = end is not None and abs(current_offset + end - hour_abs) <= ANCHOR_TOL_S \
        and abs(hour_abs) <= 3600
    hour = hour_abs - current_offset if anchored else None  # residual to reach that hour
    if anchored:
        mo = motion_offset(markers, oxi, centre=-hour, span=ANCHORED_SPAN_S)
    else:
        mo = motion_offset(markers, oxi, coarse_shift(segments, oxi))
    if mo:
        mo["residual_s"] = -mo["residual_s"]  # ring shift -> CPAP correction
        if anchored:
            # two independent clues agree: the morning end difference says "this hour",
            # and ring movement lands on the mask on/off instants right there
            # (a peak on the window edge means the real error lies further out: drift)
            inside = abs(mo["residual_s"] - hour) < ANCHORED_SPAN_S - 5
            mo["confidence"] = "high" if inside and mo["markers_matched"] >= 2 else "medium"
    # Cross-check SpO2 *at the motion-based alignment* (an hour-off clock would hide
    # every dip otherwise); its residual is then relative to that alignment.
    pre = mo["residual_s"] if mo else 0.0
    shifted = [{**e, "t": e["t"] + timedelta(seconds=pre)} for e in events]
    de = desat_offset(shifted, oxi)
    if de and "residual_s" in de:
        de["residual_s"] = round(pre - de["residual_s"], 1)
    out: dict = {"current_offset_s": current_offset, "motion": mo, "desat": de}
    if end is not None:
        out["morning_end_s"] = round(end, 1)
        out["anchored_hour_s"] = hour_abs if anchored else None  # full correction
    if mo:
        out["suggested_offset_s"] = _snap(current_offset + mo["residual_s"])
        out["confidence"] = mo["confidence"]
        if anchored and mo["confidence"] != "high":
            # movement does not pin it down near the hour: the hour is the better guess
            out["suggested_offset_s"] = int(current_offset + hour)
        if de and de.get("usable"):
            agree = abs(de["residual_s"] - mo["residual_s"]) <= 20
            out["desat_agrees"] = bool(agree)
            if not agree and out["confidence"] == "high":
                out["confidence"] = "medium"
            elif agree and out["confidence"] == "medium" and not anchored \
                    and mo["markers_matched"] >= max(3, mo["markers"]):
                # two independent methods (movement at every mask on/off, SpO2 lag after
                # events) land within 20 s of each other: that is a high-confidence answer
                out["confidence"] = "high"
    elif anchored:
        out["suggested_offset_s"] = int(current_offset + hour)
        out["confidence"] = "low"
    elif de and de.get("usable") and abs(de["residual_s"]) <= 600:
        out["suggested_offset_s"] = _snap(current_offset + de["residual_s"])
        out["confidence"] = "low"
    if "suggested_offset_s" in out:
        out["needs_change"] = abs(out["suggested_offset_s"] - current_offset) >= 4
        off = abs(out["suggested_offset_s"])
        out["dst_suspected"] = abs(off - 3600) <= 600
        out["timezone_suspected"] = off >= 5400  # e.g. CPAP left on home time abroad
    return out


def _snap(offset: float) -> int:
    """Within the ring's 4-s sampling of a whole-hour (DST) or zero shift, snap to it."""
    for target in (0, 3600, -3600):
        if abs(offset - target) <= 4:
            return target
    return int(round(offset))
