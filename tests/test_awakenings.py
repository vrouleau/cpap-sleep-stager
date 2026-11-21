from pathlib import Path

import pandas as pd

from app.pipeline import ProcessOptions, analyze
from app.staging import awakenings


def _files(d: Path):
    return [p for p in d.rglob("*") if p.is_file() and not p.name.startswith("truth")]


def _night(demo_dir, ring=True, cpap=True):
    fs = [p for p in _files(demo_dir) if (ring or p.suffix != ".csv") and (cpap or p.suffix != ".edf")]
    return analyze(fs, ProcessOptions())


def test_finds_wakeups_and_how_they_started(demo_dir):
    r = _night(demo_dir)
    truth = pd.read_csv(next(demo_dir.glob("truth-wakeups_*.csv")), parse_dates=["t"])
    night = [w for w in r["wakeups"] if w["kind"] == "night"]
    assert len(truth) >= 2
    right = 0
    for _, row in truth.iterrows():
        m = [w for w in night if abs((pd.Timestamp(w["t"]) - row.t).total_seconds()) <= 600]
        assert m, f"missed the wake-up at {row.t}"
        right += m[0]["onset"] == row.onset
    assert right >= len(truth) - 1  # gradual vs abrupt, at most one wrong on this night
    assert [w for w in r["wakeups"] if w["kind"] == "final"]


def test_wakeup_fields(demo_dir):
    r = _night(demo_dir)
    for w in r["wakeups"]:
        assert w["onset"] in ("gradual", "abrupt", "quiet")
        assert w["stage_before"] in ("NREM", "REM", None)
        assert isinstance(w["before"], list) and isinstance(w["alert"], bool)
        if w["kind"] == "night":
            assert w["mask_off"] in (True, False) and w["minutes"] >= 1


def test_ring_only_night_has_no_mask_info(demo_dir):
    r = _night(demo_dir, cpap=False)
    assert r["wakeups"] and all(w["mask_off"] is None for w in r["wakeups"])


def test_cpap_only_night_has_no_pulse_shape(demo_dir):
    r = _night(demo_dir, ring=False)
    assert all(w["onset"] is None and w["pulse_after"] is None for w in r["wakeups"])


def test_night_summary_by_hour(demo_dir):
    r = _night(demo_dir)
    s = awakenings.summarize(r["epochs"], r["wakeups"])
    assert s["wakeups_night"] == len([w for w in r["wakeups"] if w["kind"] == "night"])
    assert s["wakeups_gradual"] + s["wakeups_abrupt"] + s["wakeups_quiet"] == s["wakeups_night"]
    assert sum(s["wake_by_hour"].values()) > 0 and all(len(h) == 2 for h in s["wake_by_hour"])
    assert all(isinstance(w["t"], str) for w in s["wakeups"])


def test_reanalyse_all_nights(demo_dir):
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from app import main
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in _files(demo_dir):
            zf.write(p, p.relative_to(demo_dir))
    with TestClient(main.app) as c:
        nid = c.post("/api/nights", files=[("files", ("sd.zip", buf.getvalue(), "application/zip"))]).json()[0]["id"]
        from app.db import Night, SessionLocal
        with SessionLocal() as db:  # pretend it was analysed before this feature existed
            n = db.get(Night, nid)
            n.summary = {k: v for k, v in n.summary.items() if not k.startswith(("wake_", "wakeups"))}
            db.commit()
        assert "Wake-ups" not in c.get(f"/nights/{nid}").text
        assert main.start_reprocess_all(background=False)
        st = c.get("/api/reprocess-all").json()
        assert st["done"] == st["total"] >= 1 and not st["errors"] and not st["running"]
        page = c.get(f"/nights/{nid}").text
        assert "Wake-ups" in page and "How it started" in page
        assert "Re-analyse all nights" in c.get("/nights").text
        assert "wake_by_hour" in c.get("/").text
        c.delete(f"/api/nights/{nid}")
