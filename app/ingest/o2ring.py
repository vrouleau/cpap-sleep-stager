"""Wellue / Viatom O2Ring ingest: CSV exports and the ring's raw recordings.

CSV parsing, dropout cleaning and the standard oximetry metrics (ODI3/ODI4, T90, nadir,
desaturation events) are delegated to the ``o2ring-analyzer`` package.

The raw recordings are what O2 Insight Pro / ViHealth download from the ring, one file
per session, named after its start time with no extension (e.g. ``20261101214802`` in
``%LOCALAPPDATA%/O2 Insight Pro/DATA/<device id>``). Format version 3:

    0   u8   version (3)          9  u32  file size        13  u32  duration, s
    1   u8   0                    17  ...  the ring's own summary (avg/min SpO2, drops)
    2   u16  year, then u8 month, day, hour, minute, second (local, phone-synced)
    40  records every 4 s, 5 bytes each: SpO2 %, pulse bpm, flag, motion, vibration
        (255 in SpO2/pulse = no reading, e.g. ring off the finger)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from o2ring_analyzer.cleaning import clean
from o2ring_analyzer.metrics import compute_metrics
from o2ring_analyzer.parser import ParserError, parse_file


@dataclass
class OximetryNight:
    data: pd.DataFrame  # columns: time, spo2, pulse, motion (motion may be NaN)
    metrics: dict  # SessionMetrics.to_dict()


RAW_HEADER = 40
RAW_RECORD = 5
RAW_STEP_S = 4


def is_o2ring_raw(head: bytes, size: int | None = None) -> bool:
    """True for a raw O2Ring recording, judged by content (the files have no extension).
    ``head`` is at least the 40-byte header; ``size`` the full length (default len(head))."""
    size = len(head) if size is None else size
    if len(head) < RAW_HEADER or head[0] != 3 or head[1] != 0:
        return False
    fsize, dur = struct.unpack_from("<II", head, 9)
    try:
        _raw_start(head)
    except ValueError:
        return False
    return fsize == size and dur == (size - RAW_HEADER) // RAW_RECORD * RAW_STEP_S


def _raw_start(raw: bytes) -> datetime:
    y, mo, d, h, mi, s = struct.unpack_from("<HBBBBB", raw, 2)
    return datetime(y, mo, d, h, mi, s)


def raw_start(path: Path) -> datetime:
    with open(path, "rb") as f:
        return _raw_start(f.read(RAW_HEADER))


def read_raw(path: Path) -> pd.DataFrame:
    """Decode a raw O2Ring recording to the canonical time/spo2/pulse/motion frame."""
    raw = Path(path).read_bytes()
    if not is_o2ring_raw(raw):
        raise ValueError(f"{Path(path).name}: not a raw O2Ring recording")
    n = (len(raw) - RAW_HEADER) // RAW_RECORD
    r = np.frombuffer(raw, dtype=np.uint8, count=n * RAW_RECORD,
                      offset=RAW_HEADER).reshape(n, RAW_RECORD).astype(float)
    spo2, pulse = r[:, 0], r[:, 1]
    bad = (spo2 == 255) | (pulse == 255)
    spo2[bad] = np.nan
    pulse[bad] = np.nan
    start = _raw_start(raw)
    return pd.DataFrame({
        "time": pd.Timestamp(start) + pd.to_timedelta(np.arange(n) * RAW_STEP_S, unit="s"),
        "spo2": spo2, "pulse": pulse, "motion": r[:, 3],
    })


def looks_like_o2ring(path: Path) -> bool:
    if path.suffix.lower() in {".csv", ".txt"}:
        return True
    if path.suffix.lower() in {"", ".dat", ".bin"} and path.is_file():
        with open(path, "rb") as f:
            return is_o2ring_raw(f.read(RAW_HEADER), path.stat().st_size)
    return False


def load_o2ring(paths: list[Path], dayfirst: bool = False) -> OximetryNight | None:
    """Load and merge O2Ring CSVs and raw recordings. The ring's clock is phone-synced
    and used as-is."""
    frames = []
    for p in paths:
        try:
            if p.suffix.lower() in {".csv", ".txt"}:
                frames.append(parse_file(p, dayfirst=dayfirst).data)
            else:
                frames.append(read_raw(p))
        except (ParserError, ValueError):
            continue
    if not frames:
        return None
    df = pd.concat(frames).sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if "motion" not in df:
        df["motion"] = float("nan")
    cleaned = clean(df)
    metrics = compute_metrics(cleaned, source=", ".join(p.name for p in paths)).to_dict()
    data = cleaned.data[["time", "spo2", "pulse", "motion"]].copy()
    return OximetryNight(data=data, metrics=metrics)
