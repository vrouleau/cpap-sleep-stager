"""Optional ML prior from `sleepecg` (wrn-gru-mesa, wake/NREM/REM).

sleepecg's classifier was trained on ECG R-peak times (MESA cohort). The O2Ring only
exports a smoothed pulse rate (1 sample / 1-4 s), so we synthesise beat times by
integrating that pulse rate. Mean heart rate and slow HRV components survive; true
beat-to-beat variability (RMSSD, HF power) does not. Expect it to be a reasonable
wake/sleep separator and a weaker REM detector than with a real ECG.

Turn it on at runtime with the environment variable ``SLEEPECG=true``: on first start
the app installs keras + jax + sleepecg (~600 MB, needs internet) into the data
volume in the background and keeps serving with the heuristic engine meanwhile.
Restarts reuse that copy; it is reinstalled only when ``requirements-sleepecg.txt``
or the Python version changes. Packages the app already has are pinned to their
installed versions so nothing the app depends on is upgraded underneath it. The
Docker build arg ``WITH_SLEEPECG=true`` still bakes the packages into the image
instead (for offline installs).
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import shutil
import site
import subprocess
import sys
import sysconfig
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.staging.features import EPOCH_S

logger = logging.getLogger(__name__)
_CLF = None

REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements-sleepecg.txt"
_ON = {"1", "true", "yes", "on"}
_lock = threading.Lock()
_state = {"status": "off", "detail": ""}  # off | built-in | installing | ready | failed
_ok = False


def wanted() -> bool:
    """SLEEPECG=true in the environment (WITH_SLEEPECG is accepted as an alias)."""
    v = os.environ.get("SLEEPECG", os.environ.get("WITH_SLEEPECG", ""))
    return v.strip().lower() in _ON


def _importable() -> bool:
    os.environ.setdefault("KERAS_BACKEND", "jax")
    os.environ.setdefault("JAX_PLATFORMS", "cpu")  # no GPU/TPU probing on a NAS
    importlib.invalidate_caches()
    try:
        import keras  # noqa: F401
        import sleepecg  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def available() -> bool:
    """True once keras + sleepecg can be imported (baked in or installed at runtime)."""
    global _ok
    if _ok:
        return True
    if _state["status"] == "installing":
        return False
    _ok = _importable()
    return _ok


def status() -> dict:
    """For /api/health and the engine pickers."""
    return {"enabled": wanted() or _state["status"] == "built-in", "available": available(),
            "status": _state["status"], "detail": _state["detail"]}


def install_dir(data_dir: str | os.PathLike) -> Path:
    """One folder per (Python version, requirements) so an upgrade gets a fresh copy."""
    key = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:10] if REQUIREMENTS.exists() else "none"
    return Path(data_dir) / ".sleepecg" / f"py{sys.version_info[0]}{sys.version_info[1]}-{key}"


def _site_dirs(prefix: Path) -> list[str]:
    paths = sysconfig.get_paths(vars={"base": str(prefix), "platbase": str(prefix)})
    return list(dict.fromkeys([paths["purelib"], paths["platlib"]]))


def _activate(prefix: Path) -> None:
    for d in _site_dirs(prefix):
        if os.path.isdir(d) and d not in sys.path:
            site.addsitedir(d)  # appended: the app's own packages keep priority
    importlib.invalidate_caches()


def bootstrap(data_dir: str | os.PathLike, background: bool = True) -> None:
    """Called at startup: use a baked-in sleepecg, reuse a runtime install, or start
    installing one when SLEEPECG=true."""
    global _ok
    if _importable():
        _ok = True
        _state.update(status="built-in", detail="")
        return
    if not wanted():
        _state.update(status="off", detail="set SLEEPECG=true to enable")
        return
    prefix = install_dir(data_dir)
    if (prefix / ".ok").exists():
        _activate(prefix)
        if _importable():
            _ok = True
            _state.update(status="ready", detail=str(prefix))
            return
        logger.warning("sleepecg runtime install at %s is broken; reinstalling", prefix)
        shutil.rmtree(prefix, ignore_errors=True)
    _state.update(status="installing", detail="first start: downloading ~600 MB")
    if background:
        threading.Thread(target=install, args=(prefix,), name="sleepecg-install", daemon=True).start()
    else:
        install(prefix)


def install(prefix: Path) -> bool:
    """pip-install requirements-sleepecg.txt into ``prefix`` (inside the data volume)."""
    global _ok
    with _lock:
        _state.update(status="installing", detail="first start: downloading ~600 MB")
        logger.info("Installing sleepecg into %s (first start, this takes a few minutes)", prefix)
        try:
            prefix.mkdir(parents=True, exist_ok=True)
            # pin what the app already has, so the install cannot upgrade it underneath us
            frozen = subprocess.run([sys.executable, "-m", "pip", "freeze", "--all"],
                                    capture_output=True, text=True, timeout=120).stdout
            pins = [ln for ln in frozen.splitlines() if "==" in ln and not ln.startswith(("-e", "#"))
                    and ln.split("==")[0].lower() not in {"pip", "setuptools", "wheel"}]
            constraints = prefix / "constraints.txt"
            constraints.write_text("\n".join(pins) + "\n")
            cmd = [sys.executable, "-m", "pip", "install", "--prefix", str(prefix),
                   "--no-warn-script-location", "--disable-pip-version-check", "--no-cache-dir",
                   "-c", str(constraints), "-r", str(REQUIREMENTS)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
                raise RuntimeError("pip install failed: " + " | ".join(tail))
            _activate(prefix)
            if not _importable():
                raise RuntimeError("installed, but keras/sleepecg still cannot be imported")
            _classifier()  # load the model once so the first analysis is not slow
            (prefix / ".ok").write_text(datetime.now().isoformat())
            for old in prefix.parent.glob("py*"):  # drop copies for older requirements
                if old != prefix:
                    shutil.rmtree(old, ignore_errors=True)
            _ok = True
            _state.update(status="ready", detail=str(prefix))
            logger.info("sleepecg ready")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("sleepecg install failed; the heuristic engine stays available: %s", exc)
            _state.update(status="failed", detail=str(exc)[:300])
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
