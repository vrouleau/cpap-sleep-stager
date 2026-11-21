#!/usr/bin/env python3
"""Generate a synthetic night (ResMed BRP/EVE EDF + O2Ring CSV) for demos and tests.

    python scripts/make_demo_data.py demo/            # one night
    python scripts/make_demo_data.py demo/ --nights 5 # several nights

Writes ``<out>/DATALOG/YYYYMMDD/*_BRP.edf``, ``*_EVE.edf`` and ``<out>/O2Ring_*.csv``,
plus ``truth_*.csv`` with the stage sequence the data was generated from and
``truth-arousals_*.csv`` / ``truth-wakeups_*.csv`` with the simulated arousals and
wake-ups (gradual vs abrupt pulse onset), on the CPAP clock.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyedflib

FS = 25
EPOCH = 30


def hypnogram(rng, n_epochs: int) -> np.ndarray:
    st = []
    st += ["W"] * rng.integers(20, 40)  # 10-20 min to fall asleep
    cycle = 0
    while len(st) < n_epochs - 30:
        st += ["N"] * int(rng.integers(120, 150) - cycle * 15)
        st += ["R"] * int(10 + cycle * 10 + rng.integers(0, 8))
        if rng.random() < 0.5:
            st += ["W"] * int(rng.integers(10, 24))  # 5-12 min awake between cycles
        cycle += 1
    st = st[: n_epochs - 20]
    st += ["W"] * (n_epochs - len(st))  # wake up at the end; also pads a short last cycle
    return np.array(st[:n_epochs])


BREATH = {  # mean interval s, interval CV, amplitude L/s, amplitude CV
    "W": (3.8, 0.30, 0.45, 0.35),
    "N": (4.3, 0.05, 0.38, 0.06),
    "R": (3.6, 0.28, 0.33, 0.30),
}
HR = {"W": (68, 3.0), "N": (55, 1.0), "R": (61, 4.5)}


def make_flow(rng, stages: np.ndarray) -> np.ndarray:
    total = len(stages) * EPOCH
    out = np.zeros(total * FS)
    t = 0.0
    while t < total - 10:
        s = stages[int(t // EPOCH)]
        mi, cv, amp, acv = BREATH[s]
        T = max(1.8, rng.normal(mi, mi * cv))
        A = max(0.05, rng.normal(amp, amp * acv))
        n = int(T * FS)
        ph = np.linspace(0, 2 * np.pi, n, endpoint=False)
        # inspiration ~40% of the cycle, positive flow
        wave = np.where(ph < 0.8 * np.pi, np.sin(ph / 0.8), -0.6 * np.sin((ph - 0.8 * np.pi) / 1.2))
        i0 = int(t * FS)
        out[i0:i0 + n] = A * wave[: len(out[i0:i0 + n])]
        t += T
    return out + rng.normal(0, 0.01, out.shape)


def write_brp(path: Path, start: datetime, flow: np.ndarray) -> None:
    n = len(flow) // (FS * 60) * FS * 60
    flow = flow[:n]
    press = 9.0 + 0.5 * np.clip(flow, 0, None)
    w = pyedflib.EdfWriter(str(path), 2, file_type=pyedflib.FILETYPE_EDF)
    hdrs = []
    for lab, dim, lo, hi in (("Flow.40ms", "L/s", -2.0, 2.0), ("Press.40ms", "cmH2O", 0.0, 25.0)):
        hdrs.append({"label": lab, "dimension": dim, "sample_frequency": FS,
                     "physical_min": lo, "physical_max": hi,
                     "digital_min": -32768, "digital_max": 32767})
    w.setSignalHeaders(hdrs)
    w.setStartdatetime(start)
    w.setDatarecordDuration(60)
    w.writeSamples([np.clip(flow, -2, 2), press])
    w.close()


def write_eve(path: Path, start: datetime, events: list[tuple[float, float, str]], dur: float):
    w = pyedflib.EdfWriter(str(path), 1, file_type=pyedflib.FILETYPE_EDFPLUS)
    w.setSignalHeaders([{"label": "Crc16", "dimension": "", "sample_frequency": 1,
                         "physical_min": 0, "physical_max": 1, "digital_min": 0,
                         "digital_max": 1}])
    w.setStartdatetime(start)
    w.writeSamples([np.zeros(int(dur))])
    for onset, d, desc in events:
        w.writeAnnotation(onset, d, desc)
    w.close()


def make_night(out: Path, evening: datetime, seed: int, ring_ahead_s: float = 0.0) -> None:
    """``ring_ahead_s``: how far the simulated O2Ring clock runs ahead of the CPAP."""
    rng = np.random.default_rng(seed)
    hours = rng.uniform(7.0, 8.0)
    n = int(hours * 3600 / EPOCH)
    stages = hypnogram(rng, n)
    flow = make_flow(rng, stages)

    # bathroom break: mask off for ~6 minutes around 55% of the night
    off0 = int(n * 0.55)
    stages[off0 - 2: off0 + 14] = "W"
    cut_a, cut_b = off0 * EPOCH * FS, (off0 + 12) * EPOCH * FS
    day = evening.strftime("%Y%m%d")
    folder = out / "DATALOG" / day
    folder.mkdir(parents=True, exist_ok=True)

    # hypopneas/apneas in NREM: reduce flow for 12-20 s
    events1, events2 = [], []
    nrem_idx = np.where(stages == "N")[0]
    for e in rng.choice(nrem_idx, size=int(hours * 2.5), replace=False):
        t = e * EPOCH + 5
        d = float(rng.integers(12, 20))
        kind = "Hypopnea" if rng.random() < 0.7 else "Obstructive Apnea"
        f = 0.4 if kind == "Hypopnea" else 0.05
        flow[int(t * FS): int((t + d) * FS)] *= f
        (events1 if t * FS < cut_a else events2).append((t, d, kind))

    blk = FS * 60  # write_brp keeps whole minutes, so files end on a minute boundary
    segs = ((0, cut_a // blk * blk), (cut_b, cut_b + (len(flow) - cut_b) // blk * blk))
    markers = [t / FS for ab in segs for t in ab]  # mask on/off instants (CPAP clock, s)
    for i, (a, b) in enumerate(segs):
        seg_start = evening + timedelta(seconds=a / FS)
        stamp = seg_start.strftime("%Y%m%d_%H%M%S")
        write_brp(folder / f"{stamp}_BRP.edf", seg_start, flow[a:b])
        evs = events1 if i == 0 else [(t - a / FS, d, k) for t, d, k in events2]
        write_eve(folder / f"{stamp}_EVE.edf", seg_start, evs, (b - a) / FS)

    # O2Ring: 4-s samples, started 2 min before the mask, stopped after
    rows = ["Time,Oxygen Level,Pulse Rate,Motion"]
    ring0 = evening - timedelta(minutes=2)
    hr_slow = 0.0
    ev_all = [(t, d) for t, d, _ in events1 + events2]
    # Arousals: most respiratory events end in one; plus spontaneous ones in sleep.
    arousals = [(t + d, "respiratory", False) for t, d in ev_all if rng.random() < 0.8]
    sleep_idx = np.where((stages == "N") | (stages == "R"))[0]
    n_spont = int(len(sleep_idx) * EPOCH / 3600 * 5)  # ~5 per hour of sleep
    for e in rng.choice(sleep_idx, size=n_spont, replace=False):
        t = e * EPOCH + float(rng.uniform(3, 25))
        if all(abs(t - (et + d)) > 90 for et, d in ev_all) and \
                all(abs(t - m) > 120 for m in markers) and \
                all(abs(t - a) > 60 for a, _, _ in arousals):
            arousals.append((t, "spontaneous", bool(rng.random() < 0.3)))
    arousals.sort()
    surge_amp = {a: float(rng.uniform(10, 16)) for a, _, _ in arousals}
    # Wake-ups in the night: about half are preceded by a slow pulse climb over 40 min
    # ("gradual", body-clock-like); the others start from a flat pulse ("abrupt").
    wakeups = []
    first_sleep = int(np.argmax(stages != "W"))
    last_sleep = int(len(stages) - 1 - np.argmax(stages[::-1] != "W"))
    i = first_sleep
    while i <= last_sleep:
        if stages[i] == "W":
            j = i
            while j <= last_sleep and stages[j] == "W":
                j += 1
            on = i * EPOCH
            if j - i >= 2 and all(abs(on - m) > 900 for m in markers):
                wakeups.append((on, "gradual" if rng.random() < 0.5 else "abrupt"))
            i = j
        else:
            i += 1
    for k in range(int((n * EPOCH + 240) / 4)):
        ts = ring0 + timedelta(seconds=4 * k)
        tt = (ts - evening).total_seconds()
        s = stages[min(max(int(tt // EPOCH), 0), n - 1)]
        mu, sd = HR[s]
        hr_slow = 0.9 * hr_slow + rng.normal(0, sd * 0.45)
        hr = mu + hr_slow + rng.normal(0, 0.6)
        spo2 = 96 + rng.normal(0, 0.6)
        for et, d in ev_all:
            if et + d + 15 < tt < et + d + 45:  # finger nadir ~30 s after event end
                spo2 -= 4.5
        for on, kind in wakeups:  # slow climb to +7 bpm over the 40 min before waking
            if kind == "gradual" and on - 2400 <= tt < on:
                hr += 7.0 * (tt - (on - 2400)) / 2400
        for a, _, _ in arousals:  # pulse surge: ~5 s rise, ~12 s recovery
            if a <= tt < a + 17:
                x = tt - a
                hr += surge_amp[a] * (x / 5 if x < 5 else max(0.0, 1 - (x - 5) / 12))
        motion = 0
        if any(abs(tt - m) <= 4 for m in markers):
            motion = int(rng.integers(25, 60))  # hands on the mask
        elif any(mv and 0 <= tt - a <= 6 for a, _, mv in arousals):
            motion = int(rng.integers(3, 15))  # arousal with a twitch
        elif s == "W" and rng.random() < 0.5:
            motion = int(rng.integers(2, 30))
        elif s == "N" and rng.random() < 0.004:
            motion = int(rng.integers(1, 12))  # position shift
        ring_ts = ts + timedelta(seconds=ring_ahead_s)
        rows.append(f"{ring_ts.strftime('%H:%M:%S %b %d %Y')},{int(round(min(spo2, 99)))},"
                    f"{int(round(hr))},{motion}")
    (out / f"O2Ring_{day}{evening.strftime('%H%M%S')}.csv").write_text("\n".join(rows) + "\n")
    times = [evening + timedelta(seconds=i * EPOCH) for i in range(n)]
    lab = {"W": "Awake", "N": "NREM", "R": "REM"}
    (out / f"truth_{day}.csv").write_text(
        "t,stage\n" + "\n".join(f"{t.isoformat()},{lab[s]}" for t, s in zip(times, stages)))
    (out / f"truth-wakeups_{day}.csv").write_text(
        "t,onset\n" + "\n".join(f"{(evening + timedelta(seconds=on)).isoformat(timespec='seconds')},{k}"
                                  for on, k in wakeups))
    (out / f"truth-arousals_{day}.csv").write_text(
        "t,cause\n" + "\n".join(f"{(evening + timedelta(seconds=a)).isoformat(timespec='seconds')},{c}"
                                  for a, c, _ in arousals))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--nights", type=int, default=1)
    ap.add_argument("--start", default="2026-11-12")
    ap.add_argument("--ring-ahead", type=float, default=0.0,
                    help="simulate the O2Ring clock running this many seconds ahead")
    args = ap.parse_args()
    d0 = datetime.fromisoformat(args.start)
    for i in range(args.nights):
        ev = d0 + timedelta(days=i, hours=22, minutes=50 + 7 * i)
        make_night(args.out, ev, seed=1000 + i, ring_ahead_s=args.ring_ahead)
        print("wrote night", ev.date())


if __name__ == "__main__":
    main()
