"""Upload handling, night grouping, staging and persistence."""

from __future__ import annotations

import logging
import shutil
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath

import numpy as np
import pandas as pd

from app.db import DATA_DIR, EPOCH_FIELDS, Epoch, Event, Night, SessionLocal
from app.ingest.o2ring import load_o2ring, looks_like_o2ring
from app.ingest.resmed import _RESMED_NAME, classify_resmed_file, load_resmed
from app.staging import engine as heuristic
from app.staging import sleepecg_engine
from app.staging import sync as clocksync
from app.staging.features import EPOCH_S, build_epochs

logger = logging.getLogger(__name__)
UPLOAD_ROOT = Path(DATA_DIR) / "uploads"
APNEA_KINDS = {"Obstructive apnea", "Central apnea", "Apnea", "Hypopnea"}
KEEP_EXT = {".edf", ".csv", ".txt"}
KEEP_RESMED = {"BRP", "EVE"}


@dataclass
class ProcessOptions:
    engine: str = "heuristic"  # heuristic | sleepecg
    cpap_offset_s: float = 0.0  # added to ResMed timestamps (O2Ring/phone = real time)
    dayfirst: bool = False
    auto_sync: bool = False  # apply the detected CPAP clock correction when confident
    age: int | None = None
    gender: str | None = None
    params: dict = field(default_factory=dict)


def sleep_date(ts: datetime) -> date:
    """Nights run noon-to-noon and are named after the evening they start."""
    return (ts - timedelta(hours=12)).date()


# ------------------------------------------------------------------------------------
# Upload staging
# ------------------------------------------------------------------------------------
def extract_upload(files: list[tuple[str, bytes]], workdir: Path) -> list[Path]:
    """Write uploaded files (and zip contents) into ``workdir``; returns useful files."""
    workdir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []

    def keep(name: str, data: bytes):
        base = PurePosixPath(name.replace("\\", "/")).name
        if not base or Path(base).suffix.lower() not in KEEP_EXT:
            return
        m = _RESMED_NAME.search(base)
        if m and m.group(2).upper() not in KEEP_RESMED:
            return  # skip PLD/SAD/CSL - not used
        dest = workdir / base
        i = 1
        while dest.exists():
            dest = workdir / f"{Path(base).stem}_{i}{Path(base).suffix}"
            i += 1
        dest.write_bytes(data)
        out.append(dest)

    for name, data in files:
        if name.lower().endswith(".zip"):
            import io
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if not info.is_dir() and info.file_size < 500 * 1024 * 1024:
                        keep(info.filename, zf.read(info))
        else:
            keep(name, data)
    return out


def group_by_night(paths: list[Path], dayfirst: bool = False) -> dict[date, list[Path]]:
    groups: dict[date, list[Path]] = defaultdict(list)
    for p in paths:
        kind = classify_resmed_file(p) if p.suffix.lower() == ".edf" else None
        if kind:
            m = _RESMED_NAME.search(p.name)
            if m:
                ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
            else:
                from app.ingest.resmed import _parse_edf_header
                ts = _parse_edf_header(p.read_bytes()[:16384])["start"]
            groups[sleep_date(ts)].append(p)
        elif looks_like_o2ring(p):
            try:
                from o2ring_analyzer.parser import parse_file
                t0 = parse_file(p, dayfirst=dayfirst).data["time"].iloc[0].to_pydatetime()
                groups[sleep_date(t0)].append(p)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Ignoring %s: not an O2Ring export (%s)", p.name, exc)
    return dict(groups)


# ------------------------------------------------------------------------------------
# Analysis
# ------------------------------------------------------------------------------------
def analyze(paths: list[Path], opts: ProcessOptions) -> dict:
    edf = [p for p in paths if p.suffix.lower() == ".edf"]
    csv = [p for p in paths if looks_like_o2ring(p)]
    resmed = load_resmed(edf) if edf else None
    if resmed and not resmed.segments:
        resmed = None if not resmed.events else resmed
    if resmed and opts.cpap_offset_s:
        shift = timedelta(seconds=opts.cpap_offset_s)
        for seg in resmed.segments:
            seg.start += shift
        for ev in resmed.events:
            ev.start += shift
    oxi = load_o2ring(csv, dayfirst=opts.dayfirst) if csv else None

    epochs = build_epochs(resmed, oxi)
    engine_used = "heuristic"
    prior = None
    if opts.engine == "sleepecg" and oxi is not None:
        if sleepecg_engine.available():
            try:
                prior = sleepecg_engine.prior_probs(epochs, oxi.data, opts.age, opts.gender)
                engine_used = "sleepecg+resp" if resmed else "sleepecg"
            except Exception as exc:  # noqa: BLE001
                logger.warning("sleepecg failed, falling back to heuristic: %s", exc)
        else:
            logger.warning("sleepecg requested but not installed; using heuristic")
    staged = heuristic.stage(epochs, opts.params, prior_probs=prior)

    events = []
    if resmed:
        events += [dict(t=e.start, duration_s=e.duration_s, kind=e.kind, source="resmed")
                   for e in resmed.events]
    if oxi is not None:
        for ev in (oxi.metrics.get("events", {}).get("4") or []):
            t = ev.get("start") or ev.get("start_time")
            if t:
                events.append(dict(t=pd.Timestamp(t).to_pydatetime(),
                                   duration_s=float(ev.get("duration_s", 0) or 0),
                                   kind="Desat ≥4%", source="o2ring"))
    events.sort(key=lambda e: e["t"])
    sync = None
    if resmed and resmed.segments and oxi is not None:
        try:
            sync = clocksync.estimate(resmed.segments, events, oxi.data, opts.cpap_offset_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Clock-sync estimate failed: %s", exc)
    if sync and opts.auto_sync and sync.get("confidence") == "high" and sync.get("needs_change"):
        from dataclasses import replace
        logger.info("Auto-sync: CPAP clock correction %s -> %s s", opts.cpap_offset_s,
                    sync["suggested_offset_s"])
        again = analyze(paths, replace(opts, cpap_offset_s=float(sync["suggested_offset_s"]),
                                       auto_sync=False))
        again["sync"] = {**(again.get("sync") or {}), "auto_applied_from": opts.cpap_offset_s}
        return again
    return {
        "sync": sync,
        "offset_used": opts.cpap_offset_s,
        "epochs": staged,
        "events": events,
        "oximetry": oxi.metrics if oxi is not None else None,
        "has_resmed": resmed is not None and bool(resmed.segments),
        "has_o2ring": oxi is not None,
        "engine": engine_used,
    }


def segments(epochs: pd.DataFrame) -> list[dict]:
    out = []
    st = epochs["stage"].to_numpy()
    for s, e, lb in heuristic._runs(st):
        t0 = epochs["t"].iloc[s]
        out.append({"stage": lb, "start": t0, "end": t0 + timedelta(seconds=(e - s) * EPOCH_S),
                    "minutes": (e - s) * EPOCH_S / 60})
    return out


def summarize(epochs: pd.DataFrame, events: list[dict], oxi: dict | None) -> dict:
    st = epochs["stage"].to_numpy()
    ep_min = EPOCH_S / 60
    sleep = st != heuristic.WAKE
    s: dict = {"epochs": int(len(st)), "tib_min": round(len(st) * ep_min, 1)}
    if not sleep.any():
        s.update(tst_min=0, sleep_efficiency=0)
        return s
    first, last = int(np.argmax(sleep)), int(len(sleep) - 1 - np.argmax(sleep[::-1]))
    period = st[first:last + 1]
    tst = sleep.sum() * ep_min
    s.update(
        sleep_onset=epochs["t"].iloc[first].isoformat(),
        final_wake=(epochs["t"].iloc[last] + timedelta(seconds=EPOCH_S)).isoformat(),
        sol_min=round(first * ep_min, 1),
        tst_min=round(tst, 1),
        waso_min=round((period == heuristic.WAKE).sum() * ep_min, 1),
        sleep_efficiency=round(100 * tst / (len(st) * ep_min), 1),
        awake_min=round((st == heuristic.WAKE).sum() * ep_min, 1),
        nrem_min=round((st == heuristic.NREM).sum() * ep_min, 1),
        rem_min=round((st == heuristic.REM).sum() * ep_min, 1),
    )
    s["nrem_pct_tst"] = round(100 * s["nrem_min"] / tst, 1)
    s["rem_pct_tst"] = round(100 * s["rem_min"] / tst, 1)
    rem_idx = np.where(st == heuristic.REM)[0]
    s["rem_latency_min"] = round((rem_idx[0] - first) * ep_min, 1) if len(rem_idx) else None
    s["rem_periods"] = sum(1 for _, _, lb in heuristic._runs(period) if lb == heuristic.REM)
    s["awakenings"] = sum(1 for a, b, lb in heuristic._runs(period)
                          if lb == heuristic.WAKE and b - a >= 2)
    t0 = epochs["t"].iloc[0]
    apneas = [e for e in events if e["source"] == "resmed" and e["kind"] in APNEA_KINDS]
    if any(e["source"] == "resmed" for e in events) or "resp_events" in epochs and \
            epochs["resp_events"].notna().any():
        in_sleep = [e for e in apneas
                    if 0 <= int((e["t"] - t0).total_seconds() // EPOCH_S) < len(st)
                    and sleep[int((e["t"] - t0).total_seconds() // EPOCH_S)]]
        s["ahi_sleep"] = round(len(in_sleep) / (tst / 60), 1) if tst else None
        s["resp_events"] = len(apneas)
    hr_sleep = epochs.loc[sleep, "hr"]
    if hr_sleep.notna().any():
        s["hr_sleep_mean"] = round(float(hr_sleep.mean()), 1)
        s["hr_rem_mean"] = round(float(epochs.loc[st == heuristic.REM, "hr"].mean()), 1) \
            if (st == heuristic.REM).any() else None
        s["hr_nrem_mean"] = round(float(epochs.loc[st == heuristic.NREM, "hr"].mean()), 1)
    rr = epochs.loc[sleep, "resp_rate"]
    if rr.notna().any():
        s["resp_rate_sleep"] = round(float(rr.median()), 1)
    if oxi:
        s["spo2_mean"] = (oxi.get("spo2") or {}).get("mean")
        s["spo2_min"] = (oxi.get("spo2") or {}).get("min")
        s["odi4"] = (oxi.get("odi") or {}).get("odi4")
        t90 = next((t for t in oxi.get("threshold_times", []) if t["label"] == "T90"), None)
        s["t90_min"] = t90["minutes"] if t90 else None
    return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in s.items()}


# ------------------------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------------------------
def _clean(v):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else float(v)


def save_night(night_date: date, paths: list[Path], opts: ProcessOptions,
               replace: bool = True, notes: str | None = None) -> tuple[Night | None, str]:
    with SessionLocal() as db:
        existing = db.query(Night).filter_by(night_date=night_date).one_or_none()
        if existing and not replace:
            return existing, "skipped (already exists)"
        # move files into the night's permanent folder (kept for re-processing)
        night_dir = UPLOAD_ROOT / night_date.isoformat()
        if existing and night_dir.exists():
            # merge: keep previous files that are not being replaced by name
            pass
        night_dir.mkdir(parents=True, exist_ok=True)
        for p in paths:
            if p.parent != night_dir:
                shutil.move(str(p), night_dir / p.name)
        all_paths = sorted(q for q in night_dir.iterdir() if q.is_file())
        opts, inherited = _maybe_inherit(db, night_date, all_paths, opts)
        result = analyze(all_paths, opts)
        if inherited:
            result["sync"] = inherited
        night = existing or Night(night_date=night_date)
        if existing:
            night.epochs.clear()
            night.events.clear()
        _fill(night, result, opts, all_paths)
        if notes is not None:
            night.notes = notes
        db.add(night)
        db.commit()
        return night, "replaced" if existing else "created"


def _fill(night: Night, result: dict, opts: ProcessOptions, paths: list[Path]) -> None:
    ep = result["epochs"]
    night.start = ep["t"].iloc[0].to_pydatetime() if hasattr(ep["t"].iloc[0], "to_pydatetime") \
        else ep["t"].iloc[0]
    night.end = night.start + timedelta(seconds=len(ep) * EPOCH_S)
    night.engine = result["engine"]
    night.has_resmed = result["has_resmed"]
    night.has_o2ring = result["has_o2ring"]
    night.cpap_offset_s = result["offset_used"]
    night.age, night.gender = opts.age, opts.gender
    night.files = [p.name for p in paths]
    night.summary = summarize(ep, result["events"], result["oximetry"])
    if result.get("sync"):
        night.summary["sync"] = _jsonable(result["sync"])
    night.oximetry = _jsonable(result["oximetry"]) if result["oximetry"] else None
    for i, row in enumerate(ep.itertuples(index=False)):
        night.epochs.append(Epoch(idx=i, t=pd.Timestamp(row.t).to_pydatetime(), stage=row.stage,
                                  **{f: _clean(getattr(row, f)) for f in EPOCH_FIELDS}))
    for e in result["events"]:
        night.events.append(Event(**e))


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items() if k != "events"}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    return o


INHERIT_MAX_DAYS = 60


def _maybe_inherit(db, night_date: date, paths: list[Path], opts: ProcessOptions):
    """CPAP-only night + auto-sync: reuse the correction found on the nearest earlier
    night that had both devices (e.g. a run of mask-only nights after a DST change)."""
    from dataclasses import replace as dc_replace

    has_csv = any(looks_like_o2ring(p) for p in paths)
    has_edf = any(p.suffix.lower() == ".edf" for p in paths)
    if not opts.auto_sync or opts.cpap_offset_s or has_csv or not has_edf:
        return opts, None
    prev = (db.query(Night)
            .filter(Night.night_date < night_date, Night.has_o2ring.is_(True),
                    Night.has_resmed.is_(True),
                    Night.night_date >= night_date - timedelta(days=INHERIT_MAX_DAYS))
            .order_by(Night.night_date.desc()).all())
    for n in prev:
        sync = (n.summary or {}).get("sync") or {}
        if sync.get("confidence") == "high" and not sync.get("needs_change"):
            if not n.cpap_offset_s:
                return opts, None  # nearest reliable night says the clock was fine
            return dc_replace(opts, cpap_offset_s=float(n.cpap_offset_s)), {
                "inherited_from": n.night_date.isoformat(), "inherited_id": n.id,
                "confidence": "inherited", "needs_change": False,
                "suggested_offset_s": n.cpap_offset_s,
                "dst_suspected": abs(abs(n.cpap_offset_s) - 3600) <= 600,
            }
    return opts, None


def reprocess(night_id: int, opts: ProcessOptions) -> Night:
    with SessionLocal() as db:
        night = db.get(Night, night_id)
        if night is None:
            raise KeyError(night_id)
        night_dir = UPLOAD_ROOT / night.night_date.isoformat()
        paths = sorted(q for q in night_dir.iterdir() if q.is_file())
        opts, inherited = _maybe_inherit(db, night.night_date, paths, opts)
        result = analyze(paths, opts)
        if inherited:
            result["sync"] = inherited
        night.epochs.clear()
        night.events.clear()
        db.flush()
        _fill(night, result, opts, paths)
        db.commit()
        return night
