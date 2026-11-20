from pathlib import Path

from app.pipeline import ProcessOptions, analyze


def _files(d: Path):
    return [p for p in d.rglob("*") if p.is_file() and not p.name.startswith("truth")]


# The skewed fixture has the ring 23 s ahead of the CPAP, so the CPAP needs +23 s.

def test_aligned_clocks_need_no_change(demo_dir):
    s = analyze(_files(demo_dir), ProcessOptions())["sync"]
    assert abs(s["suggested_offset_s"]) <= 4
    assert not s["needs_change"] and not s["dst_suspected"]


def test_detects_small_skew(skewed_dir):
    s = analyze(_files(skewed_dir), ProcessOptions())["sync"]
    assert abs(s["suggested_offset_s"] - 23) <= 4
    assert s["confidence"] == "high" and s["needs_change"]
    assert s["desat"]["usable"] and s["desat_agrees"]


def test_estimate_is_relative_to_current_offset(skewed_dir):
    s = analyze(_files(skewed_dir), ProcessOptions(cpap_offset_s=-60))["sync"]
    assert abs(s["suggested_offset_s"] - 23) <= 4


def test_auto_sync_applies_offset(skewed_dir):
    r = analyze(_files(skewed_dir), ProcessOptions(auto_sync=True))
    assert abs(r["offset_used"] - 23) <= 4
    assert r["sync"]["auto_applied_from"] == 0
    assert not r["sync"]["needs_change"]


def test_detects_missed_dst(dst_dir):
    r = analyze(_files(dst_dir), ProcessOptions(auto_sync=True))
    assert abs(r["offset_used"] - (-3600)) <= 4
    assert r["sync"]["dst_suspected"]
    # after correction the timeline starts at the ring's (real) time
    assert r["epochs"]["t"].iloc[0].hour == 22


def test_no_sync_without_both_sources(demo_dir):
    edf = [p for p in _files(demo_dir) if p.suffix == ".edf"]
    assert analyze(edf, ProcessOptions())["sync"] is None


def test_cpap_only_night_inherits_correction(tmp_path):
    import io
    import zipfile
    from datetime import datetime

    import make_demo_data
    from fastapi.testclient import TestClient

    from app.main import app

    for i in range(2):  # two nights, machine 1 h ahead; second night without the ring
        make_demo_data.make_night(tmp_path, datetime(2026, 11, 3 + i, 23, 50), seed=2000 + i,
                                  ring_ahead_s=-3600)
    ring2 = next(tmp_path.glob("O2Ring_20261104*.csv"))
    ring2.unlink()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in _files(tmp_path):
            zf.write(p, p.relative_to(tmp_path))
    with TestClient(app) as c:
        res = c.post("/api/nights", data={"auto_sync": "true"},
                     files=[("files", ("sd.zip", buf.getvalue(), "application/zip"))]).json()
        assert [r["status"] for r in res] == ["created", "created"], res
        n1, n2 = (c.get(f"/api/nights/{r['id']}").json() for r in res)
        assert abs(n1["cpap_offset_s"] + 3600) <= 4
        assert n2["cpap_offset_s"] == n1["cpap_offset_s"]
        assert n2["summary"]["sync"]["inherited_from"] == "2026-11-03"
        page = c.get(f"/nights/{res[1]['id']}").text
        assert "reused the CPAP clock correction" in page and "daylight-saving" in page
        page1 = c.get(f"/nights/{res[0]['id']}").text
        assert "Auto-corrected" in page1
        for r in res:
            c.delete(f"/api/nights/{r['id']}")


def test_detects_large_drift_plus_dst(drift_dir):
    r = analyze(_files(drift_dir), ProcessOptions(auto_sync=True))
    assert abs(r["offset_used"] - (-(3600 + 32 * 60))) <= 4
    assert not r["sync"]["dst_suspected"]  # 1 h 32 min is not a clean DST hour


def test_travel_time_zone(travel_dir):
    from app.pipeline import group_by_night

    groups = group_by_night(_files(travel_dir))
    assert len(groups) == 1  # CPAP and ring files still land on the same night
    r = analyze(_files(travel_dir), ProcessOptions(auto_sync=True))
    assert abs(r["offset_used"] - (6 * 3600 + 190)) <= 4
    assert r["sync"]["timezone_suspected"]
    assert r["epochs"]["t"].iloc[0].hour == 22  # timeline in local (Paris) time


def test_morning_end_anchors_missed_dst():
    """CPAP an hour behind (as before the Nov 1 fall-back on a real machine). A chance
    cluster of movement lines up better with the mask markers at +46 min; the morning
    mask-off vs ring-off difference must keep the estimate on the hour."""
    from datetime import datetime, timedelta

    import numpy as np
    import pandas as pd

    from app.ingest.resmed import FlowSegment
    from app.staging import sync

    def seg(start, minutes):
        return FlowSegment(source="x", start=start, fs=25, flow=np.zeros(int(minutes * 60 * 25)))

    segs = [seg(datetime(2026, 10, 28, 20, 11), 220), seg(datetime(2026, 10, 29, 0, 2), 240)]
    true_off = 3600  # ring (real) time = CPAP time + 1 h
    t = pd.date_range("2026-10-28 21:05", "2026-10-29 05:04:20", freq="4s")
    ring = pd.DataFrame({"time": t, "spo2": 95.0, "pulse": 58.0, "motion": 0.0})

    def burst(at, size):
        ring.loc[(ring.time - at).abs() <= pd.Timedelta("8s"), "motion"] = size

    for s_ in segs:
        for m in (s_.start, s_.end):
            burst(m + timedelta(seconds=true_off), 20)
            burst(m + timedelta(seconds=2748), 60)  # decoy, stronger
    ring = ring[ring.time <= max(s_.end for s_ in segs) + timedelta(seconds=true_off + 40)]
    out = sync.estimate(segs, [], ring, 0.0)
    assert out["anchored_hour_s"] == 3600
    assert abs(out["suggested_offset_s"] - true_off) <= 8
    assert out["confidence"] == "high" and out["dst_suspected"]
