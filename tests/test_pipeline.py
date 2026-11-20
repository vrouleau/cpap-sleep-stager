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
