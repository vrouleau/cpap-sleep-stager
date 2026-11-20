import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from app.ingest.resmed import _fallback_read_signal, read_brp, read_eve
from app.pipeline import ProcessOptions, analyze, group_by_night, summarize


def _files(d: Path, kind=None):
    fs = [p for p in d.rglob("*") if p.is_file() and not p.name.startswith("truth")]
    if kind == "edf":
        return [p for p in fs if p.suffix == ".edf"]
    if kind == "csv":
        return [p for p in fs if p.suffix == ".csv"]
    return fs


def _accuracy(demo_dir, paths):
    r = analyze(paths, ProcessOptions())
    truth = pd.read_csv(next(demo_dir.glob("truth_*.csv")), parse_dates=["t"])
    m = r["epochs"].merge(truth, on="t", suffixes=("", "_true"))
    return (m.stage == m.stage_true).mean(), r


def test_brp_reader_matches_fallback(demo_dir):
    brp = sorted(demo_dir.rglob("*_BRP.edf"))[0]
    seg = read_brp(brp)
    start, fs, flow = _fallback_read_signal(brp, "flow")
    assert seg.fs == fs == 25
    assert start == seg.start
    np.testing.assert_allclose(seg.flow[:5000], flow[:5000], atol=1e-3)


def test_eve_events(demo_dir):
    evs = [e for p in demo_dir.rglob("*_EVE.edf") for e in read_eve(p)]
    assert evs and {e.kind for e in evs} <= {"Hypopnea", "Obstructive apnea"}


def test_eve_edf_plus_d(demo_dir, tmp_path):
    """ResMed writes EVE.edf as EDF+D, which pyedflib refuses; the fallback decoder
    must return the same events."""
    src = sorted(demo_dir.rglob("*_EVE.edf"))[0]
    raw = bytearray(src.read_bytes())
    assert raw[192:197] == b"EDF+C"
    raw[192:197] = b"EDF+D"
    dst = tmp_path / src.name
    dst.write_bytes(bytes(raw))
    want = [(e.start, e.kind) for e in read_eve(src)]
    got = [(e.start, e.kind) for e in read_eve(dst)]
    assert want and got == want


def test_grouping(demo_dir):
    groups = group_by_night(_files(demo_dir))
    assert len(groups) == 1
    assert len(next(iter(groups.values()))) == 5  # 2 BRP + 2 EVE + 1 CSV


def test_combined_staging(demo_dir):
    acc, r = _accuracy(demo_dir, _files(demo_dir))
    assert acc > 0.9
    s = summarize(r["epochs"], r["events"], r["oximetry"])
    assert 15 < s["rem_pct_tst"] < 45
    assert s["ahi_sleep"] > 0 and s["odi4"] is not None


def test_single_source_staging(demo_dir):
    assert _accuracy(demo_dir, _files(demo_dir, "edf"))[0] > 0.8
    assert _accuracy(demo_dir, _files(demo_dir, "csv"))[0] > 0.8


def test_api_roundtrip(demo_dir):
    from fastapi.testclient import TestClient

    from app.main import app

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in _files(demo_dir):
            zf.write(p, p.relative_to(demo_dir))
    with TestClient(app) as c:
        r = c.post("/api/nights", files=[("files", ("sd.zip", buf.getvalue(), "application/zip"))])
        assert r.status_code == 200, r.text
        nid = r.json()[0]["id"]
        d = c.get(f"/api/nights/{nid}").json()
        assert d["has_resmed"] and d["has_o2ring"]
        assert {e["stage"] for e in d["epochs"]} == {"Awake", "NREM", "REM"}
        assert c.get("/").status_code == 200
        assert c.get(f"/nights/{nid}").status_code == 200
        csv = c.get(f"/api/nights/{nid}/epochs.csv").text
        assert csv.startswith("time,stage")
        segs = c.get(f"/api/nights/{nid}/segments").json()
        assert segs[0]["stage"] == "Awake"
        r = c.post(f"/nights/{nid}/reprocess", data={"engine": "heuristic", "offset": "0"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert c.delete(f"/api/nights/{nid}").status_code == 200
        assert c.get("/api/nights").json() == []


def test_wake_log(demo_dir):
    from fastapi.testclient import TestClient

    from app.main import app

    files = [("files", (p.name, p.read_bytes(), "application/octet-stream"))
             for p in _files(demo_dir)]
    with TestClient(app) as c:
        nid = c.post("/api/nights", files=files).json()[0]["id"]
        # the synthetic night opens with 10-20 min awake from 22:50, so "22:50-23:00"
        # should mostly agree; times after midnight land on the next morning
        r = c.post(f"/api/nights/{nid}/wake-log", json={"start": "22:50", "end": "23:00"})
        assert r.status_code == 200, r.text
        log = c.post(f"/api/nights/{nid}/wake-log", json={"start": "03:00", "end": "03:20"}).json()
        assert [e["start"][11:16] for e in log["entries"]] == ["22:50", "03:00"]
        assert log["entries"][1]["start"].startswith("2026-11-13")
        assert log["entries"][0]["agreement_pct"] >= 50
        assert log["logged_min"] == 30
        assert c.post(f"/api/nights/{nid}/wake-log", json={"start": "04:00", "end": "03:00"}).status_code == 400
        # survives a re-run, shows on the page
        c.post(f"/nights/{nid}/reprocess", data={"engine": "heuristic", "offset": "0"})
        assert len(c.get(f"/api/nights/{nid}").json()["wake_log"]["entries"]) == 2
        assert "Your wake log" in c.get(f"/nights/{nid}").text
        assert len(c.delete(f"/api/nights/{nid}/wake-log/0").json()["entries"]) == 1
        c.delete(f"/api/nights/{nid}")


def test_drop_stray_sessions():
    """Nov 13 on a real card: a 4-min test at 13:31, the night 21:42-05:02 with a 3-min
    gap, and a 1-min test at 09:41 the next morning."""
    from datetime import datetime

    from app.ingest.resmed import FlowSegment, RespEvent, ResmedNight, drop_stray_sessions

    def seg(name, start, minutes):
        return FlowSegment(source=name, start=start, fs=25, flow=np.zeros(int(minutes * 60 * 25)))

    night = ResmedNight(
        segments=[seg("a", datetime(2026, 11, 13, 13, 31), 4),
                  seg("b", datetime(2026, 11, 13, 21, 42), 269),
                  seg("c", datetime(2026, 11, 14, 2, 13), 169),
                  seg("d", datetime(2026, 11, 14, 9, 41), 1)],
        events=[RespEvent(start=datetime(2026, 11, 13, 13, 33), duration_s=10, kind="Apnea"),
                RespEvent(start=datetime(2026, 11, 14, 0, 30), duration_s=10, kind="Apnea")])
    dropped = drop_stray_sessions(night)
    assert [d["files"] for d in dropped] == [["a"], ["d"]]
    assert [s.source for s in night.segments] == ["b", "c"]
    assert len(night.events) == 1
    # a long separate block (e.g. a nap) is kept, as is a normal mid-night break
    nap = ResmedNight(segments=[seg("n", datetime(2026, 11, 13, 14, 0), 60),
                                seg("b", datetime(2026, 11, 13, 22, 0), 400)])
    assert drop_stray_sessions(nap) == [] and len(nap.segments) == 2


def test_short_night_skipped(tmp_path):
    """A 10-min machine test with nothing else that night is not imported."""
    from datetime import datetime

    from fastapi.testclient import TestClient

    import make_demo_data
    from app.main import app

    start = datetime(2026, 10, 28, 9, 21)
    p = tmp_path / f"{start:%Y%m%d_%H%M%S}_BRP.edf"
    t = np.arange(10 * 60 * make_demo_data.FS) / make_demo_data.FS
    make_demo_data.write_brp(p, start, 0.4 * np.sin(2 * np.pi * t / 4))
    with TestClient(app) as c:
        r = c.post("/api/nights", files=[("files", (p.name, p.read_bytes(), "application/octet-stream"))])
        assert r.status_code == 200, r.text
        assert r.json()[0]["status"].startswith("skipped (only 10 min")
        assert "id" not in r.json()[0]
        assert all(n["night_date"] != "2026-10-27" for n in c.get("/api/nights").json())


def _csv_to_raw(csv_path, out_dir):
    """Write the demo night's ring CSV as a raw O2Ring recording (format v3)."""
    import struct

    from o2ring_analyzer.parser import parse_file

    d = parse_file(csv_path).data
    t0 = d["time"].iloc[0].to_pydatetime()
    n = len(d)
    recs = bytearray()
    for r in d.itertuples(index=False):
        recs += bytes([int(r.spo2), int(r.pulse), 0, int(r.motion), 0])
    head = bytearray(40)
    head[0] = 3
    struct.pack_into("<HBBBBB", head, 2, t0.year, t0.month, t0.day, t0.hour, t0.minute, t0.second)
    struct.pack_into("<II", head, 9, 40 + len(recs), n * 4)
    out = out_dir / t0.strftime("%Y%m%d%H%M%S")
    out.write_bytes(bytes(head + recs))
    return out, d


def test_o2ring_raw_recording(demo_dir, tmp_path):
    from app.ingest.o2ring import looks_like_o2ring, read_raw
    from app.pipeline import extract_upload

    raw, d = _csv_to_raw(_files(demo_dir, "csv")[0], tmp_path)
    assert looks_like_o2ring(raw) and not looks_like_o2ring(_files(demo_dir, "edf")[0])
    got = read_raw(raw)
    assert (got["time"].to_numpy() == d["time"].to_numpy()).all()
    np.testing.assert_array_equal(got["pulse"], d["pulse"])
    # zipped DATA folder: extension-less raw files are kept, other junk is not
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(raw, f"DATA/2652200387/{raw.name}")
        zf.writestr("DATA/2652200387/notes", b"not a recording")
    kept = extract_upload([("o2.zip", buf.getvalue())], tmp_path / "x")
    assert [p.name for p in kept] == [raw.name]
    groups = group_by_night(kept)
    assert list(groups) == list(group_by_night(_files(demo_dir, "csv")))
    # staging from the raw file is as good as from the CSV
    acc, r = _accuracy(demo_dir, _files(demo_dir, "edf") + [raw])
    assert acc > 0.9 and r["has_o2ring"]


def test_stray_cpap_vs_ring_night():
    """Only CPAP data is a morning test, but the ring recorded the night: drop the test."""
    from datetime import datetime

    from app.ingest.resmed import FlowSegment, ResmedNight, drop_stray_sessions

    night = ResmedNight(segments=[FlowSegment(source="t", start=datetime(2026, 10, 28, 9, 21),
                                              fs=25, flow=np.zeros(25 * 60 * 22))])
    ring = [(datetime(2026, 10, 27, 21, 25), datetime(2026, 10, 28, 5, 40))]
    assert [d["files"] for d in drop_stray_sessions(night, ring)] == [["t"]]
    assert night.segments == []


def test_nights_pager():
    from app.main import _pager

    assert _pager(1, 1) == [1]
    assert _pager(1, 2) == [1, 2]
    assert _pager(1, 10) == [1, 2, 3, None, 10]
    assert _pager(6, 13) == [1, None, 4, 5, 6, 7, 8, None, 13]
    assert _pager(13, 13) == [1, None, 11, 12, 13]
