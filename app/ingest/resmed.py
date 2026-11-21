"""ResMed AirSense / AirCurve SD-card EDF readers.

The machine writes one folder per night under ``DATALOG/YYYYMMDD/``, with one set of
files per mask-on session::

    20261119_231502_BRP.edf   high-rate flow + pressure (25 Hz)   <- what we stage from
    20261119_231502_EVE.edf   EDF+ annotations: apneas, hypopneas, RERAs ...
    20261119_231502_PLD.edf   low-rate pressure / leak / flow limitation / snore (2 s)
    20261119_231502_SAD.edf   SpO2 / pulse when a ResMed oximeter is attached
    20261119_231502_CSL.edf   Cheyne-Stokes annotations

BRP, EVE and PLD are used here. Timestamps in the files are naive local machine time.
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
    pld: list["PldSegment"] = field(default_factory=list)


@dataclass
class PldSegment:
    """Low-rate machine data from a PLD file (one sample every 2 s): what the machine
    did (pressure) and what it saw (leak, flow limitation, snoring)."""

    source: str
    start: datetime
    fs: float
    channels: dict[str, np.ndarray]  # pressure (cmH2O), leak (L/min), flow_lim (0-1), snore

    @property
    def end(self) -> datetime:
        n = max((len(v) for v in self.channels.values()), default=0)
        return self.start + timedelta(seconds=n / self.fs) if self.fs else self.start


# Channel names differ between models (S9: "Therapy Pres", "FFL Index"; AirSense 10/11:
# "Press.2s", "FlowLim.2s", ...), so match by keyword. Order matters: first match wins.
_PLD_CHANNELS = {
    "pressure": ("therapy pres", "press.", "press"),
    "leak": ("leak",),
    "flow_lim": ("flowlim", "flow lim", "ffl"),
    "snore": ("snore",),
}
_PLD_EXCLUDE = {"pressure": ("mask", "epr", "exp")}  # mask / expiratory pressure are not it


def read_pld(path: Path) -> PldSegment:
    import pyedflib

    path = Path(path)
    f = pyedflib.EdfReader(str(path), check_file_size=pyedflib.REPAIR_FILE_SIZE_IF_WRONG)
    try:
        labels = [lb.strip() for lb in f.getSignalLabels()]
        start = f.getStartdatetime()
        chans, fs = {}, None
        for key, words in _PLD_CHANNELS.items():
            idx = None
            for w in words:
                idx = next((i for i, lb in enumerate(labels) if w in lb.lower()
                            and not any(x in lb.lower() for x in _PLD_EXCLUDE.get(key, ()))), None)
                if idx is not None:
                    break
            if idx is None:
                continue
            v = np.asarray(f.readSignal(idx), dtype=float)
            if key == "leak":
                dim = f.getPhysicalDimension(idx).strip().lower()
                if "s" in dim and "min" not in dim:  # ResMed stores L/s: show L/min
                    v = v * 60.0
            chans[key] = v
            fs = fs or float(f.getSampleFrequency(idx))
    finally:
        f.close()
    if not chans:
        raise ValueError(f"{path.name}: no pressure / leak channels")
    return PldSegment(source=path.name, start=start, fs=fs or 0.5, channels=chans)


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


def _fallback_read_annotations(path: Path) -> tuple[datetime, list[tuple[float, float, str]]]:
    """Decode EDF+ annotations (TALs) directly.

    ResMed writes EVE.edf as EDF+D ("discontinuous"), which pyedflib refuses to open.
    Each data record's "EDF Annotations" signal holds Time-stamped Annotation Lists:
    ``+onset[\\x15duration]\\x14text\\x14[text\\x14...]\\x00``, onset in seconds from the
    file start. The record's first TAL (empty text) is only its timekeeping stamp.
    """
    raw = Path(path).read_bytes()
    h = _parse_edf_header(raw)
    idx = [i for i, lb in enumerate(h["labels"]) if lb.lower().startswith("edf annotations")]
    if not idx:
        raise ValueError(f"{Path(path).name}: no EDF Annotations signal")
    rec_bytes = sum(h["nsamp"]) * 2
    body = raw[h["header_bytes"]:]
    out: list[tuple[float, float, str]] = []
    for r in range(len(body) // rec_bytes):
        rec = body[r * rec_bytes:(r + 1) * rec_bytes]
        for i in idx:
            off = sum(h["nsamp"][:i]) * 2
            sig = rec[off: off + h["nsamp"][i] * 2]
            for tal in sig.split(b"\x00"):
                parts = tal.split(b"\x14")
                if len(parts) < 2 or not parts[0]:
                    continue
                head = parts[0].split(b"\x15")
                try:
                    onset = float(head[0])
                    dur = float(head[1]) if len(head) > 1 and head[1] else 0.0
                except ValueError:
                    continue
                for txt in parts[1:]:
                    if txt.strip():
                        out.append((onset, dur, txt.decode("utf-8", "replace").strip()))
    return h["start"], out


def read_eve(path: Path) -> list[RespEvent]:
    """Read respiratory events from a ResMed EVE.edf (EDF+ annotations)."""
    path = Path(path)
    try:
        import pyedflib

        f = pyedflib.EdfReader(str(path), check_file_size=pyedflib.REPAIR_FILE_SIZE_IF_WRONG)
        try:
            start = f.getStartdatetime()
            onsets, durations, descs = f.readAnnotations()
        finally:
            f.close()
    except Exception as exc:  # noqa: BLE001 - EDF+D files are rejected by pyedflib
        logger.debug("pyedflib could not read %s (%s); using fallback reader", path.name, exc)
        start, anns = _fallback_read_annotations(path)
        onsets, durations, descs = zip(*anns) if anns else ((), (), ())
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


STRAY_GAP_S = 2 * 3600  # sessions further than this from the rest form their own block
STRAY_MAX_S = 30 * 60  # ...and such a block shorter than this is a test / mask fitting


def drop_stray_sessions(night: ResmedNight,
                        other: list[tuple[datetime, datetime]] = ()) -> list[dict]:
    """Remove short CPAP sessions far away from the night's main recording.

    Pressing Start in the afternoon to test the machine or a mask, or for a minute the
    next morning, lands in the same noon-to-noon night and would otherwise turn the
    hours in between into "awake". Real nights have gaps of minutes between sessions
    (getting up, mask off); these tests sit hours away and last a few minutes.
    ``other`` are recordings from other devices (the O2Ring) that belong to the night:
    they count towards the main block but are never removed.
    Returns what was removed, for display.
    """
    items = sorted([(s.start, s.end, s) for s in night.segments]
                   + [(a, b, None) for a, b in other], key=lambda x: x[0])
    if len(items) < 2:
        return []
    blocks = [[items[0]]]
    for it in items[1:]:
        if (it[0] - max(x[1] for x in blocks[-1])).total_seconds() > STRAY_GAP_S:
            blocks.append([it])
        else:
            blocks[-1].append(it)
    if len(blocks) == 1:
        return []
    dur = [sum((b_ - a_).total_seconds() for a_, b_, _ in b) for b in blocks]
    main = int(np.argmax(dur))
    dropped = [[x[2] for x in b] for i, b in enumerate(blocks)
               if i != main and dur[i] < STRAY_MAX_S and all(x[2] is not None for x in b)]
    if not dropped:
        return []
    out = []
    for b in dropped:
        a, z = b[0].start, max(s.end for s in b)
        night.segments = [s for s in night.segments if s not in b]
        night.events = [e for e in night.events
                        if not a - timedelta(seconds=60) <= e.start <= z + timedelta(seconds=60)]
        night.pld = [q for q in night.pld
                     if not a - timedelta(seconds=60) <= q.start <= z + timedelta(seconds=60)]
        out.append({"start": a.isoformat(), "end": z.isoformat(),
                    "minutes": round((z - a).total_seconds() / 60, 1),
                    "files": [s.source for s in b]})
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
            elif kind == "PLD":
                night.pld.append(read_pld(p))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s: %s", p.name, exc)
    night.segments.sort(key=lambda s: s.start)
    night.events.sort(key=lambda e: e.start)
    night.pld.sort(key=lambda s: s.start)
    return night
