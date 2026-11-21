from pathlib import Path

import pandas as pd

from app.pipeline import ProcessOptions, analyze
from app.staging import awakenings


def _files(d: Path):
    return [p for p in d.rglob("*") if p.is_file() and not p.name.startswith("truth")]


def _near(w, t, tol=600):
    """The true wake-up falls inside the awakening, or within 10 min of its start (or of
    the pulse-located moment of waking)."""
    start = pd.Timestamp(w["t"])
    if start <= t <= start + pd.Timedelta(minutes=w["minutes"]):
        return True
    times = [w["t"]] + ([w["woke_at"]] if w.get("woke_at") else [])
    return any(abs((pd.Timestamp(x) - t).total_seconds()) <= tol for x in times)


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
        m = [w for w in night if _near(w, row.t)]
        assert m, f"missed the wake-up at {row.t}"
        right += (m[0]["onset"] == "gradual") == (row.onset == "gradual")
    assert right >= len(truth) - 1  # build-up or not, at most one wrong on this night
    assert [w for w in r["wakeups"] if w["kind"] == "final"]


def test_wakeup_fields(demo_dir):
    r = _night(demo_dir)
    for w in r["wakeups"]:
        assert w["onset"] in ("gradual", "none")
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
    assert s["wakeups_gradual"] + s["wakeups_none"] == s["wakeups_night"]
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
        assert "Wake-ups" in page and "Pulse before" in page
        assert "Re-analyse all nights" in c.get("/nights").text
        assert "wake_by_hour" in c.get("/").text
        c.delete(f"/api/nights/{nid}")


def _epochs(stages, flow=None):
    from datetime import datetime, timedelta
    t0 = datetime(2026, 11, 1, 23, 0)
    return pd.DataFrame({"t": [t0 + timedelta(seconds=30 * i) for i in range(len(stages))],
                         "stage": stages,
                         "flow_present": flow if flow is not None else [float("nan")] * len(stages)})


def test_wake_bouts_merge_and_short_ones_are_brief():
    N, W = "NREM", "Awake"
    # 40 sleep, 30 awake, 2 sleep (1 min dozing), 40 awake, 60 sleep, 4 awake (2 min), 60 sleep
    st = [N] * 40 + [W] * 30 + [N] * 2 + [W] * 40 + [N] * 60 + [W] * 4 + [N] * 60
    ws = [w for w in awakenings.detect(_epochs(st), None, []) if w["kind"] == "night"]
    assert len(ws) == 1  # the 1-min doze does not split it; the 2-min wake is too short
    assert ws[0]["minutes"] == 36.0 and ws[0]["awake_min"] == 35.0
    s = awakenings.summarize(_epochs(st), ws)
    assert s["wakeups_night"] == 1 and s["brief_wakes"] == 1


def test_bathroom_timing():
    N, W = "NREM", "Awake"
    # wake at 40, mask off 6 min later (12 epochs) for 4 min, then 10 min awake, then sleep
    st = [N] * 40 + [W] * (12 + 8 + 20) + [N] * 60
    flow = [1.0] * 52 + [0.0] * 8 + [1.0] * 80
    (w,) = [w for w in awakenings.detect(_epochs(st, flow), None, []) if w["kind"] == "night"]
    assert w["mask_off"] is True
    assert w["mask_off_after_min"] == 6.0 and w["back_on_awake_min"] == 10.0


def test_machine_data_before_wakeups(demo_dir):
    """The demo machine raises the pressure (flow limitation first) in the minutes before
    ~40% of wake-ups, and once with no wake-up at all."""
    r = _night(demo_dir)
    ep = r["epochs"]
    assert ep["pressure"].between(7.5, 11.5).mean() > 0.9 and ep["leak"].max() > 24
    truth = pd.read_csv(next(demo_dir.glob("truth-wakeups_*.csv")), parse_dates=["t"])
    night = [w for w in r["wakeups"] if w["kind"] == "night"]
    for _, row in truth.iterrows():
        (w,) = [w for w in night if _near(w, row.t)][:1]
        assert ("pressure rise" in w["before"]) == bool(row.pressure_first), (row.t, w["before"])
        if row.pressure_first:
            assert "flow limitation" in w["before"] and w["pressure_rise"] >= 2.0


def test_pld_reader_units_and_names(demo_dir):
    from app.ingest.resmed import read_pld
    q = read_pld(sorted(demo_dir.rglob("*_PLD.edf"))[0])
    assert set(q.channels) == {"pressure", "leak", "flow_lim", "snore"}
    assert q.fs == 0.5 and 7.9 < q.channels["pressure"].min() < 8.1  # Press.2s, not mask/EPR
    assert q.channels["leak"].max() > 20  # L/s converted to L/min


def test_pld_kept_on_upload(tmp_path):
    from app.pipeline import extract_upload
    names = ["20261101_225000_BRP.edf", "20261101_225000_PLD.edf", "20261101_225000_SAD.edf"]
    kept = extract_upload([(n, b"x") for n in names], tmp_path / "w")
    assert sorted(p.name for p in kept) == names[:2]
