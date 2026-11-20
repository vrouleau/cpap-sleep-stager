"""ResMed AirSense / AirCurve SD-card EDF readers.

The machine writes one folder per night under ``DATALOG/YYYYMMDD/``, with one set of
files per mask-on session::

    20261119_231502_BRP.edf   high-rate flow + pressure (25 Hz)   <- what we stage from
    20261119_231502_EVE.edf   EDF+ annotations: apneas, hypopneas, RERAs ...
    20261119_231502_PLD.edf   low-rate leak / minute vent / resp rate (2 s)
    20261119_231502_SAD.edf   SpO2 / pulse when a ResMed oximeter is attached
    20261119_231502_CSL.edf   Cheyne-Stokes annotations

Only BRP and EVE are used here. Timestamps in the files are naive local machine time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_RESMED_NAME = re.compile(r"(\d{8}_\d{6})_([A-Z]{3})\.edf$", re.IGNORECASE)


@dataclass
class FlowSegment:
    """One mask-on session from a BRP file."""

    source: str
    start: datetime
    fs: float
    flow: np.ndarray  # L/s (ResMed reports L/s; sign: + = inspiration)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=len(self.flow) / self.fs)


@dataclass
class RespEvent:
    start: datetime
    duration_s: float
    kind: str
    source: str = "resmed"


@dataclass
class ResmedNight:
    segments: list[FlowSegment] = field(default_factory=list)
    events: list[RespEvent] = field(default_factory=list)


def classify_resmed_file(path: Path) -> str | None:
    """Return the ResMed file kind ('BRP', 'EVE', ...) or None if not a ResMed EDF."""
    m = _RESMED_NAME.search(path.name)
    if m:
        return m.group(2).upper()
    if path.suffix.lower() == ".edf":
        # Unknown naming: sniff labels
        try:
            labels = _edf_labels(path)
        except Exception:  # noqa: BLE001
            return None
        if any("flow" in lb.lower() for lb in labels):
            return "BRP"
        if any("annotation" in lb.lower() for lb in labels):
            return "EVE"
    return None


# --------------------------------------------------------------------------------------
# Low-level EDF access: pyedflib first, minimal pure-python fallback for the odd
# non-compliant header that ResMed firmware sometimes produces.
# --------------------------------------------------------------------------------------
def _edf_labels(path: Path) -> list[str]:
    hdr = _parse_edf_header(path.read_bytes()[:256 * 64])
    return hdr["labels"]


def _parse_edf_header(raw: bytes) -> dict:
    ns = int(raw[252:256].decode("ascii", "replace").strip())
    hdr_bytes = int(raw[184:192].decode("ascii", "replace").strip())
    start_date = raw[168:176].decode("ascii", "replace").strip()  # dd.mm.yy
    start_time = raw[176:184].decode("ascii", "replace").strip()  # hh.mm.ss
    n_records = int(raw[236:244].decode("ascii", "replace").strip())
    rec_dur = float(raw[244:252].decode("ascii", "replace").strip())

    def field_(offset: int, width: int) -> list[str]:
        return [
            raw[offset + i * width: offset + (i + 1) * width].decode("ascii", "replace").strip()
            for i in range(ns)
        ]

    o = 256
    labels = field_(o, 16); o += 16 * ns
    o += 80 * ns  # transducer
    o += 8 * ns  # phys dim
    pmin = [float(x) for x in field_(o, 8)]; o += 8 * ns
    pmax = [float(x) for x in field_(o, 8)]; o += 8 * ns
    dmin = [float(x) for x in field_(o, 8)]; o += 8 * ns
    dmax = [float(x) for x in field_(o, 8)]; o += 8 * ns
    o += 80 * ns  # prefilter
    nsamp = [int(x) for x in field_(o, 8)]
    dd, mm, yy = (int(x) for x in start_date.split("."))
    hh, mi, ss = (int(x) for x in start_time.split("."))
    year = 2000 + yy if yy < 85 else 1900 + yy
    return {
        "ns": ns, "header_bytes": hdr_bytes, "labels": labels, "n_records": n_records,
        "record_duration": rec_dur, "pmin": pmin, "pmax": pmax, "dmin": dmin, "dmax": dmax,
        "nsamp": nsamp, "start": datetime(year, mm, dd, hh, mi, ss),
    }


def _fallback_read_signal(path: Path, label_substr: str) -> tuple[datetime, float, np.ndarray]:
    raw = path.read_bytes()
    h = _parse_edf_header(raw)
    idx = next(i for i, lb in enumerate(h["labels"]) if label_substr in lb.lower())
    rec_samples = sum(h["nsamp"])
    rec_bytes = rec_samples * 2
    body = raw[h["header_bytes"]:]
    n_rec = len(body) // rec_bytes  # tolerate truncated trailing record
    data = np.frombuffer(body[: n_rec * rec_bytes], dtype="<i2").reshape(n_rec, rec_samples)
    off = sum(h["nsamp"][:idx])
    dig = data[:, off: off + h["nsamp"][idx]].astype(float).ravel()
    scale = (h["pmax"][idx] - h["pmin"][idx]) / (h["dmax"][idx] - h["dmin"][idx])
    phys = (dig - h["dmin"][idx]) * scale + h["pmin"][idx]
    fs = h["nsamp"][idx] / h["record_duration"]
    return h["start"], fs, phys


def read_brp(path: Path) -> FlowSegment:
    """Read the flow channel of a ResMed BRP.edf file."""
    path = Path(path)
    try:
        import pyedflib

        f = pyedflib.EdfReader(str(path), check_file_size=pyedflib.REPAIR_FILE_SIZE_IF_WRONG)
        try:
            labels = f.getSignalLabels()
            idx = next(i for i, lb in enumerate(labels) if "flow" in lb.lower())
            fs = float(f.getSampleFrequency(idx))
            flow = f.readSignal(idx)
            start = f.getStartdatetime()
        finally:
            f.close()
    except StopIteration as exc:
        raise ValueError(f"{path.name}: no Flow channel found") from exc
    except Exception as exc:  # noqa: BLE001 - pyedflib raises OSError on odd headers
        logger.warning("pyedflib could not read %s (%s); using fallback reader", path.name, exc)
        start, fs, flow = _fallback_read_signal(path, "flow")
    return FlowSegment(source=path.name, start=start, fs=fs, flow=np.asarray(flow, dtype=float))


_EVENT_MAP = {
    "obstructive apnea": "Obstructive apnea",
    "central apnea": "Central apnea",
    "apnea": "Apnea",
    "hypopnea": "Hypopnea",
    "arousal": "RERA",
    "rera": "RERA",
    "unclassified apnea": "Apnea",
}


def read_eve(path: Path) -> list[RespEvent]:
    """Read respiratory events from a ResMed EVE.edf (EDF+ annotations)."""
    import pyedflib

    path = Path(path)
    f = pyedflib.EdfReader(str(path), check_file_size=pyedflib.REPAIR_FILE_SIZE_IF_WRONG)
    try:
        start = f.getStartdatetime()
        onsets, durations, descs = f.readAnnotations()
    finally:
        f.close()
    out: list[RespEvent] = []
    for onset, dur, desc in zip(onsets, durations, descs):
        d = str(desc).strip()
        key = d.lower()
        if not key or key.startswith("recording"):
            continue
        kind = next((v for k, v in _EVENT_MAP.items() if k in key), d)
        out.append(RespEvent(start=start + timedelta(seconds=float(onset)),
                             duration_s=float(dur) if dur and dur > 0 else 10.0, kind=kind))
    return out


def load_resmed(paths: list[Path]) -> ResmedNight:
    night = ResmedNight()
    for p in sorted(paths):
        kind = classify_resmed_file(p)
        try:
            if kind == "BRP":
                night.segments.append(read_brp(p))
            elif kind == "EVE":
                night.events.extend(read_eve(p))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s: %s", p.name, exc)
    night.segments.sort(key=lambda s: s.start)
    night.events.sort(key=lambda e: e.start)
    return night
