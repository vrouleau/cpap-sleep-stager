"""Optional ML prior from `sleepecg` (wrn-gru-mesa, wake/NREM/REM).

sleepecg's classifier was trained on ECG R-peak times (MESA cohort). The O2Ring only
exports a smoothed pulse rate (1 sample / 1-4 s), so we synthesise beat times by
integrating that pulse rate. Mean heart rate and slow HRV components survive; true
beat-to-beat variability (RMSSD, HF power) does not. Expect it to be a reasonable
wake/sleep separator and a weaker REM detector than with a real ECG.

Install with the Docker build arg ``WITH_SLEEPECG=true`` (adds keras + jax, ~600 MB).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

from app.staging.features import EPOCH_S

logger = logging.getLogger(__name__)
_CLF = None


def available() -> bool:
    os.environ.setdefault("KERAS_BACKEND", "jax")
    try:
        import keras  # noqa: F401
        import sleepecg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _classifier():
    global _CLF
    if _CLF is None:
        os.environ.setdefault("KERAS_BACKEND", "jax")
        import warnings

        import sleepecg
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _CLF = sleepecg.load_classifier("wrn-gru-mesa", "SleepECG")
    return _CLF


def pulse_to_beats(oxi: pd.DataFrame, t0: datetime) -> np.ndarray:
    """Integrate pulse rate (bpm) on a 1-s grid into synthetic beat times [s from t0]."""
    s = (oxi["time"] - pd.Timestamp(t0)).dt.total_seconds()
    hr = pd.Series(oxi["pulse"].to_numpy(), index=pd.to_timedelta(s, unit="s"))
    hr = hr.resample("1s").mean().interpolate(limit=10, limit_area="inside")
    secs = hr.index.total_seconds().to_numpy()
    rate = hr.to_numpy() / 60.0
    rate = np.where(np.isfinite(rate), rate, 0.0)  # gaps -> no beats -> long RRI, dropped
    phase = np.concatenate([[0.0], np.cumsum(rate)])
    grid = np.concatenate([secs[:1], secs + 1.0])
    k = np.arange(1, int(phase[-1]))
    return np.interp(k, phase, grid)


def prior_probs(epochs: pd.DataFrame, oxi: pd.DataFrame | None,
                age: int | None, gender: str | None) -> np.ndarray | None:
    """Return (n_epochs x 3) probabilities [wake, nrem, rem] aligned to ``epochs``."""
    if oxi is None or oxi.empty or not available():
        return None
    import sleepecg
    from sleepecg.io.sleep_readers import SleepRecord, SubjectData

    t0 = epochs["t"].iloc[0]
    beats = pulse_to_beats(oxi, t0)
    if len(beats) < 500:
        return None
    g = {"female": 0, "male": 1}.get((gender or "").lower())
    rec = SleepRecord(
        sleep_stage_duration=EPOCH_S,
        recording_start_time=t0.time(),
        heartbeat_times=beats,
        subject_data=SubjectData(gender=g, age=age),
    )
    clf = _classifier()
    from sleepecg.utils import _STAGE_NAMES

    probs = sleepecg.stage(clf, rec, return_mode="prob")
    names = ["UNDEFINED"] + list(_STAGE_NAMES[clf.stages_mode])  # e.g. NREM, REM, WAKE
    order = [names.index("WAKE"), names.index("NREM"), names.index("REM")]
    out = np.full((len(epochs), 3), np.nan)
    m = min(len(out), len(probs))
    p = probs[:m][:, order]
    p = p / np.clip(p.sum(axis=1, keepdims=True), 1e-9, None)
    out[:m] = p
    out[epochs["hr"].isna().to_numpy()] = np.nan
    return out
