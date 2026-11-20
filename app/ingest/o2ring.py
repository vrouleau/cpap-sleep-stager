"""Wellue / Viatom O2Ring CSV ingest, delegated to the ``o2ring-analyzer`` package.

o2ring-analyzer handles the messy part (encodings, delimiters, locale decimals, header
synonyms across O2 Insight Pro / ViHealth versions, dropout cleaning) and computes the
standard oximetry metrics (ODI3/ODI4, T90, nadir, desaturation events).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from o2ring_analyzer.cleaning import clean
from o2ring_analyzer.metrics import compute_metrics
from o2ring_analyzer.parser import ParserError, parse_file


@dataclass
class OximetryNight:
    data: pd.DataFrame  # columns: time, spo2, pulse, motion (motion may be NaN)
    metrics: dict  # SessionMetrics.to_dict()


def looks_like_o2ring(path: Path) -> bool:
    return path.suffix.lower() in {".csv", ".txt"}


def load_o2ring(paths: list[Path], dayfirst: bool = False) -> OximetryNight | None:
    """Load and merge O2Ring CSVs. The ring's clock is phone-synced and used as-is."""
    frames = []
    for p in paths:
        try:
            frames.append(parse_file(p, dayfirst=dayfirst).data)
        except ParserError:
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
