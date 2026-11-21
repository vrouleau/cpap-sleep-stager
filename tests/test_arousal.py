from pathlib import Path

import numpy as np
import pandas as pd

from app.ingest.o2ring import load_o2ring
from app.pipeline import ProcessOptions, analyze, summarize
from app.staging import arousal
from app.staging.engine import _runs


def _files(d: Path):
    return [p for p in d.rglob("*") if p.is_file() and not p.name.startswith("truth")]


def _secs(ts) -> np.ndarray:
    return (pd.to_datetime(pd.Series(ts)) - pd.Timestamp(0)).dt.total_seconds().to_numpy()


def _truth(d: Path):
    t = pd.read_csv(next(d.glob("truth_arousals_*.csv")))
    return _secs(t["t"]), t["cause"].to_numpy()


def _arousals(r):
    return [e for e in r["events"] if e["source"] == "arousal"]


def test_finds_simulated_arousals_and_their_cause(demo_dir):
    r = analyze(_files(demo_dir), ProcessOptions())
    det = _arousals(r)
    dt = _secs([e["t"] for e in det])
    tt, causes = _truth(demo_dir)
    hit = np.array([np.any(np.abs(dt - x) <= 15) for x in tt])
    assert hit.mean() >= 0.75
    assert hit[causes == "respiratory"].mean() >= 0.85
    assert sum(not np.any(np.abs(tt - x) <= 15) for x in dt) <= 0.35 * len(tt)
    for e, x in zip(det, dt):  # arousals right after a CPAP event are attributed to it
        j = np.argmin(np.abs(tt - x))
        if abs(tt[j] - x) <= 15 and causes[j] == "respiratory":
            assert arousal.cause_of(e["kind"]) == "respiratory"


def test_extends_the_epoch_flag_count(demo_dir):
    """Every brief wake mark kept asleep is still an arousal; surges come on top."""
    r = analyze(_files(demo_dir), ProcessOptions())
    ep = r["epochs"]
    flags = ep["arousal"].fillna(0).to_numpy() > 0
    t0 = pd.Timestamp(ep["t"].iloc[0])
    dt = _secs([e["t"] for e in _arousals(r)]) - (t0 - pd.Timestamp(0)).total_seconds()
    for s, e, lb in _runs(flags):
        if lb:
            assert np.any((dt >= s * 30 - 15) & (dt <= e * 30 + 15))
    assert len(dt) > sum(1 for *_, lb in _runs(flags) if lb)


def test_summary_index_and_causes(demo_dir):
    r = analyze(_files(demo_dir), ProcessOptions())
    s = summarize(r["epochs"], r["events"], r["oximetry"])
    assert 2 < s["arousal_index"] < 15
    assert s["arousals"] == sum(s["arousals_by_cause"].values()) == len(_arousals(r))
    assert set(s["arousal_index_by_cause"]) == set(arousal.CAUSES)


def test_arousals_only_while_asleep(demo_dir):
    r = analyze(_files(demo_dir), ProcessOptions())
    ep = r["epochs"]
    for e in _arousals(r):
        i = int((pd.Timestamp(e["t"]) - pd.Timestamp(ep["t"].iloc[0])).total_seconds() // 30)
        assert ep["stage"].iloc[i] != "Awake"


def test_no_arousals_without_ring(demo_dir):
    edf = [p for p in _files(demo_dir) if p.suffix == ".edf"]
    r = analyze(edf, ProcessOptions())
    assert not _arousals(r)
    assert "arousals" not in summarize(r["epochs"], r["events"], r["oximetry"])


def test_flat_pulse_has_no_surges(demo_dir):
    r = analyze(_files(demo_dir), ProcessOptions())
    oxi = load_o2ring([p for p in _files(demo_dir) if p.suffix == ".csv"]).data.copy()
    oxi["pulse"] = 60.0
    ep = r["epochs"].assign(arousal=0.0)
    assert arousal.detect(ep, oxi, r["events"]) == []


def test_cause_kind_roundtrip():
    for c in arousal.CAUSES:
        assert arousal.cause_of(arousal.kind_for(c)) == c
    assert arousal.cause_of("Hypopnea") is None
    assert len(arousal.kind_for("desaturation")) <= 32  # fits events.kind
