from __future__ import annotations

import csv
import io
import logging
import tempfile
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import pipeline
from app.db import EPOCH_FIELDS, Night, SessionLocal, init_db
from app.staging import sleepecg_engine
from app.staging.features import EPOCH_S

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="CPAP Sleep Stager", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _fmt_min(m):
    if m is None:
        return "–"
    m = float(m)
    return f"{int(m // 60)}h{int(round(m % 60)):02d}" if m >= 60 else f"{m:.0f} min"


templates.env.filters["hm"] = _fmt_min


def _fmt_offset(sec):
    """Signed seconds -> '+1 h 0 min 12 s' / '-23 s'."""
    if sec is None:
        return "–"
    sec = int(round(float(sec)))
    sign = "−" if sec < 0 else "+"
    a = abs(sec)
    h, rem = divmod(a, 3600)
    m, s_ = divmod(rem, 60)
    parts = [f"{v} {u}" for v, u in ((h, "h"), (m, "min"), (s_, "s")) if v]
    return sign + " ".join(parts) if sec else "0 s"


templates.env.filters["offset"] = _fmt_offset


# ---------------------------------------------------------------- helpers
def _night_dict(n: Night, with_epochs: bool = False) -> dict:
    d = {
        "id": n.id, "night_date": n.night_date.isoformat(), "start": n.start, "end": n.end,
        "engine": n.engine, "has_resmed": n.has_resmed, "has_o2ring": n.has_o2ring,
        "summary": n.summary, "files": n.files, "notes": n.notes,
        "cpap_offset_s": n.cpap_offset_s, "wake_log": _wake_log_check(n),
    }
    if with_epochs:
        d["epoch_s"] = EPOCH_S
        d["epochs"] = [{"t": e.t.isoformat(), "stage": e.stage,
                        **{f: getattr(e, f) for f in EPOCH_FIELDS}} for e in n.epochs]
        d["events"] = [{"t": e.t.isoformat(), "duration_s": e.duration_s, "kind": e.kind,
                        "source": e.source} for e in n.events]
        d["oximetry"] = n.oximetry
    return d


def _segments(n: Night) -> list[dict]:
    out = []
    for e in n.epochs:
        if out and out[-1]["stage"] == e.stage:
            out[-1]["epochs"] += 1
        else:
            out.append({"stage": e.stage, "start": e.t, "epochs": 1})
    from datetime import timedelta
    for s in out:
        s["end"] = s["start"] + timedelta(seconds=s["epochs"] * EPOCH_S)
        s["minutes"] = s["epochs"] * EPOCH_S / 60
    return out


def _resolve_clock(night_date: date, value: str) -> datetime:
    """'HH:MM' on the night of ``night_date`` (evening -> that date, before noon -> the
    next morning), or a full ISO timestamp."""
    value = value.strip()
    try:
        if "T" in value or len(value) > 8:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        t = time.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"Not a time: {value!r} (use HH:MM)") from exc
    day = night_date if t.hour >= 12 else night_date + timedelta(days=1)
    return datetime.combine(day, t)


def _wake_log_check(n: Night) -> dict:
    """Compare what you logged as awake against what the app staged."""
    entries, tot_min, tot_hit = [], 0.0, 0.0
    epochs = [(e.t, e.stage) for e in n.epochs] if n.wake_log else []
    for i, w in enumerate(n.wake_log or []):
        a, b = datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"])
        inside = [st for t, st in epochs if a <= t + timedelta(seconds=EPOCH_S / 2) < b]
        hit = sum(st == "Awake" for st in inside) * EPOCH_S / 60
        mins = len(inside) * EPOCH_S / 60
        tot_min, tot_hit = tot_min + mins, tot_hit + hit
        entries.append({"i": i, "start": w["start"], "end": w["end"],
                        "minutes": round((b - a).total_seconds() / 60, 1),
                        "recorded_min": mins, "staged_awake_min": hit,
                        "agreement_pct": round(100 * hit / mins) if mins else None})
    return {"entries": entries, "logged_min": tot_min, "staged_awake_min": tot_hit,
            "agreement_pct": round(100 * tot_hit / tot_min) if tot_min else None}


def _add_wake(night_id: int, start: str, end: str) -> None:
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        a, b = _resolve_clock(n.night_date, start), _resolve_clock(n.night_date, end)
        if b <= a:
            b += timedelta(days=1) if (a - b) > timedelta(hours=12) else timedelta(0)
        if b <= a:
            raise HTTPException(400, "End must be after start")
        log = list(n.wake_log or []) + [{"start": a.isoformat(), "end": b.isoformat()}]
        n.wake_log = sorted(log, key=lambda w: w["start"])
        db.commit()


def _remove_wake(night_id: int, i: int) -> None:
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        log = list(n.wake_log or [])
        if not 0 <= i < len(log):
            raise HTTPException(404)
        del log[i]
        n.wake_log = log
        db.commit()


async def _ingest(files: list[UploadFile], opts: pipeline.ProcessOptions, replace: bool,
                  notes: str | None) -> list[dict]:
    payload = [(f.filename or "upload", await f.read()) for f in files]
    with tempfile.TemporaryDirectory() as tmp:
        paths = pipeline.extract_upload(payload, Path(tmp))
        if not paths:
            raise HTTPException(400, "No BRP.edf / EVE.edf / O2Ring .csv files found in upload")
        groups = pipeline.group_by_night(paths, dayfirst=opts.dayfirst)
        if not groups:
            raise HTTPException(400, "Could not recognise any ResMed or O2Ring recordings")
        results = []
        for d, ps in sorted(groups.items()):
            try:
                night, status = pipeline.save_night(d, ps, opts, replace=replace, notes=notes)
                results.append({"night_date": d.isoformat(), "status": status,
                                **({"id": night.id} if night else {})})
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed night %s", d)
                results.append({"night_date": d.isoformat(), "status": f"error: {exc}"})
        return results


def _opts(engine: str, offset: float, dayfirst: bool, age: str | None, gender: str | None,
          auto_sync: bool = False):
    return pipeline.ProcessOptions(
        engine=engine, cpap_offset_s=offset, dayfirst=dayfirst, auto_sync=auto_sync,
        age=int(age) if age else None, gender=gender or None)


# ---------------------------------------------------------------- HTML
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with SessionLocal() as db:
        nights = db.query(Night).order_by(Night.night_date.desc()).all()
        rows = [_night_dict(n) for n in nights]
    trend = [{"id": r["id"], "d": r["night_date"], "s": r["summary"]} for r in reversed(rows)]
    return templates.TemplateResponse(request, "index.html", {
        "nights": rows, "trend": trend, "sleepecg": sleepecg_engine.available()})


@app.post("/upload")
async def upload_form(files: list[UploadFile] = File(...), engine: str = Form("heuristic"),
                      offset: float = Form(0.0), dayfirst: bool = Form(False),
                      replace: bool = Form(False), age: str | None = Form(None),
                      gender: str | None = Form(None), notes: str | None = Form(None),
                      auto_sync: bool = Form(False)):
    results = await _ingest(files, _opts(engine, offset, dayfirst, age, gender, auto_sync), replace,
                            notes or None)
    ok = [r for r in results if "id" in r]
    if len(ok) == 1:
        return RedirectResponse(f"/nights/{ok[0]['id']}", status_code=303)
    return RedirectResponse("/?imported=" + str(len(ok)), status_code=303)


@app.get("/nights/{night_id}", response_class=HTMLResponse)
def night_page(request: Request, night_id: int):
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        prev_n = db.query(Night).filter(Night.night_date < n.night_date) \
            .order_by(Night.night_date.desc()).first()
        next_n = db.query(Night).filter(Night.night_date > n.night_date) \
            .order_by(Night.night_date).first()
        ctx = {"n": _night_dict(n), "segments": _segments(n),
               "events": [e for e in n.events if e.source == "resmed"],
               "prev_id": prev_n.id if prev_n else None, "next_id": next_n.id if next_n else None,
               "sleepecg": sleepecg_engine.available()}
    return templates.TemplateResponse(request, "night.html", ctx)


@app.post("/nights/{night_id}/reprocess")
def reprocess_form(night_id: int, engine: str = Form("heuristic"), offset: float = Form(0.0),
                   dayfirst: bool = Form(False), age: str | None = Form(None),
                   gender: str | None = Form(None), auto_sync: bool = Form(False)):
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        age = age or (str(n.age) if n.age else None)
        gender = gender or n.gender
    pipeline.reprocess(night_id, _opts(engine, offset, dayfirst, age, gender, auto_sync))
    return RedirectResponse(f"/nights/{night_id}", status_code=303)


@app.post("/nights/{night_id}/wake-log")
def wake_log_form(night_id: int, start: str = Form(...), end: str = Form(...)):
    _add_wake(night_id, start, end)
    return RedirectResponse(f"/nights/{night_id}#wake-log", status_code=303)


@app.post("/nights/{night_id}/wake-log/{i}/delete")
def wake_log_delete_form(night_id: int, i: int):
    _remove_wake(night_id, i)
    return RedirectResponse(f"/nights/{night_id}#wake-log", status_code=303)


@app.post("/nights/{night_id}/delete")
def delete_form(night_id: int):
    _delete(night_id)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- JSON API
@app.get("/api/health")
def health():
    return {"ok": True, "sleepecg": sleepecg_engine.available()}


@app.get("/api/nights")
def api_nights():
    with SessionLocal() as db:
        return [_night_dict(n) for n in db.query(Night).order_by(Night.night_date.desc())]


@app.post("/api/nights")
async def api_upload(files: list[UploadFile] = File(...), engine: str = Form("heuristic"),
                     offset: float = Form(0.0), dayfirst: bool = Form(False),
                     replace: bool = Form(True), age: str | None = Form(None),
                     gender: str | None = Form(None), auto_sync: bool = Form(False),
                     notes: str | None = Form(None)):
    return await _ingest(files, _opts(engine, offset, dayfirst, age, gender, auto_sync),
                         replace, notes or None)


@app.get("/api/nights/{night_id}")
def api_night(night_id: int):
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        return JSONResponse(_jsonsafe(_night_dict(n, with_epochs=True)))


@app.get("/api/nights/{night_id}/segments")
def api_segments(night_id: int):
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        return [{**s, "start": s["start"].isoformat(), "end": s["end"].isoformat()}
                for s in _segments(n)]


@app.get("/api/nights/{night_id}/epochs.csv")
def api_epochs_csv(night_id: int):
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "stage", *EPOCH_FIELDS])
        for e in n.epochs:
            w.writerow([e.t.isoformat(), e.stage, *[getattr(e, f) for f in EPOCH_FIELDS]])
        name = f"sleep_{n.night_date.isoformat()}.csv"
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/nights/{night_id}/wake-log")
def api_wake_log_add(night_id: int, body: dict):
    """Body: {"start": "01:30", "end": "05:15"} (or ISO timestamps)."""
    _add_wake(night_id, str(body.get("start", "")), str(body.get("end", "")))
    with SessionLocal() as db:
        return _wake_log_check(db.get(Night, night_id))


@app.delete("/api/nights/{night_id}/wake-log/{i}")
def api_wake_log_delete(night_id: int, i: int):
    _remove_wake(night_id, i)
    with SessionLocal() as db:
        return _wake_log_check(db.get(Night, night_id))


@app.delete("/api/nights/{night_id}")
def api_delete(night_id: int):
    _delete(night_id)
    return {"deleted": night_id}


def _delete(night_id: int):
    import shutil
    with SessionLocal() as db:
        n = db.get(Night, night_id)
        if n is None:
            raise HTTPException(404)
        shutil.rmtree(pipeline.UPLOAD_ROOT / n.night_date.isoformat(), ignore_errors=True)
        db.delete(n)
        db.commit()


def _jsonsafe(o):
    from datetime import datetime
    if isinstance(o, dict):
        return {k: _jsonsafe(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonsafe(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return o
