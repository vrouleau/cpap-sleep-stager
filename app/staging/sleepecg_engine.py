"""Optional ML prior from `sleepecg` (wrn-gru-mesa, wake/NREM/REM).

sleepecg's classifier was trained on ECG R-peak times (MESA cohort). The O2Ring only
exports a smoothed pulse rate (1 sample / 1-4 s), so we synthesise beat times by
integrating that pulse rate. Mean heart rate and slow HRV components survive; true
beat-to-beat variability (RMSSD, HF power) does not. Expect it to be a reasonable
wake/sleep separator and a weaker REM detector than with a real ECG.

The packages (keras + jax + sleepecg, ~400 MB) are baked into the Docker image. Whether
the app uses them is a runtime switch: ``SLEEPECG=true`` offers the engine and makes it
the default for new imports; ``SLEEPECG=false`` (the default) hides it. The switch on
the Data import page overrides the variable and is saved in ``DATA_DIR/settings.json``. Build with
``--build-arg WITH_SLEEPECG=false`` for a slimmer image without it.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from datetime import datetime

import numpy as np
import pandas as pd

from app.staging.features import EPOCH_S

logger = logging.getLogger(__name__)
_CLF = None

_ON = {"1", "true", "yes", "on"}
_installed: bool | None = None  # are keras + sleepecg in this image? (checked once)


def _settings_file() -> str:
    return os.path.join(os.environ.get("DATA_DIR", "/data"), "settings.json")


def _setting() -> bool | None:
    """The on/off switch saved from the app (Data import page), if it was ever used."""
    import json
    try:
        with open(_settings_file()) as f:
            v = json.load(f).get("sleepecg")
        return v if isinstance(v, bool) else None
    except (OSError, ValueError):
        return None


def set_enabled(on: bool) -> None:
    """Switch the engine on or off from the app; saved in the data folder, so it
    survives restarts and overrides the SLEEPECG environment variable."""
    import json
    path = _settings_file()
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data["sleepecg"] = bool(on)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    if on:
        bootstrap()  # load the model now so the first analysis is not slow


def wanted() -> bool:
    """Use the engine? The switch in the app wins; otherwise the SLEEPECG environment
    variable (WITH_SLEEPECG accepted as an alias) sets the default."""
    v = _setting()
    if v is not None:
        return v
    env = os.environ.get("SLEEPECG", os.environ.get("WITH_SLEEPECG", ""))
    return env.strip().lower() in _ON


def installed() -> bool:
    """keras + sleepecg importable (baked into the image)."""
    global _installed
    if _installed is None:
        os.environ.setdefault("KERAS_BACKEND", "jax")
        os.environ.setdefault("JAX_PLATFORMS", "cpu")  # no GPU/TPU probing on a NAS
        importlib.invalidate_caches()
        try:
            import keras  # noqa: F401
            import sleepecg  # noqa: F401
            _installed = True
        except Exception:  # noqa: BLE001
            _installed = False
    return _installed


def available() -> bool:
    """The engine can be used: switched on AND present in the image."""
    return wanted() and installed()


def default_engine() -> str:
    """What new imports use when no engine is chosen."""
    return "sleepecg" if available() else "heuristic"


def status() -> dict:
    """For /api/health and the engine pickers."""
    on, have = wanted(), installed()
    st = "on" if on and have else "missing" if on else "off"
    return {"enabled": on, "installed": have, "available": on and have, "status": st,
            "source": "app" if _setting() is not None else "environment"}


def bootstrap(background: bool = True) -> None:
    """At startup with the switch on: load the model once so the first analysis is fast."""
    if not available():
        if wanted():
            logger.warning("SLEEPECG=true but sleepecg is not in this image "
                           "(built with WITH_SLEEPECG=false); using the heuristic engine")
        return

    def warm():
        try:
            _classifier()
            logger.info("sleepecg engine ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load the sleepecg model: %s", exc)

    if background:
        threading.Thread(target=warm, name="sleepecg-warmup", daemon=True).start()
    else:
        warm()


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
