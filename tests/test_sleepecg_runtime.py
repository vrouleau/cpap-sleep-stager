"""SLEEPECG=true runtime install: pip is mocked, nothing is downloaded."""
import subprocess
from types import SimpleNamespace

import pytest

from app.staging import sleepecg_engine as se


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(se, "_ok", False)
    monkeypatch.setattr(se, "_state", {"status": "off", "detail": ""})
    monkeypatch.setattr(se, "_classifier", lambda: None)
    monkeypatch.delenv("SLEEPECG", raising=False)
    monkeypatch.delenv("WITH_SLEEPECG", raising=False)
    return monkeypatch


def test_flag_values(fresh):
    for v in ("true", "1", "YES", " on "):
        fresh.setenv("SLEEPECG", v)
        assert se.wanted()
    for v in ("false", "0", "", "no"):
        fresh.setenv("SLEEPECG", v)
        assert not se.wanted()
    fresh.delenv("SLEEPECG")
    fresh.setenv("WITH_SLEEPECG", "true")  # old name still works
    assert se.wanted()


def test_off_by_default(fresh, tmp_path):
    fresh.setattr(se, "_importable", lambda: False)
    se.bootstrap(tmp_path, background=False)
    st = se.status()
    assert st["status"] == "off" and not st["available"] and not st["enabled"]
    assert not (tmp_path / ".sleepecg").exists()


def test_baked_into_image(fresh, tmp_path):
    fresh.setattr(se, "_importable", lambda: True)
    se.bootstrap(tmp_path, background=False)
    assert se.status()["status"] == "built-in" and se.available()


def test_install_pins_app_packages_and_cleans_old_copies(fresh, tmp_path):
    fresh.setenv("SLEEPECG", "true")
    installed = {"done": False}
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if "freeze" in cmd:
            return SimpleNamespace(returncode=0, stdout="numpy==2.3.0\npip==25.0\nscipy==1.16.0\n-e git+x\n", stderr="")
        installed["done"] = True
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    fresh.setattr(subprocess, "run", run)
    fresh.setattr(se, "_importable", lambda: installed["done"])
    old = tmp_path / ".sleepecg" / "py30-oldhash"
    old.mkdir(parents=True)
    se.bootstrap(tmp_path, background=False)

    prefix = se.install_dir(tmp_path)
    assert se.status()["status"] == "ready" and se.available()
    assert (prefix / ".ok").exists() and not old.exists()
    pins = (prefix / "constraints.txt").read_text()
    assert "numpy==2.3.0" in pins and "scipy==1.16.0" in pins and "pip==" not in pins and "-e" not in pins
    cmd = calls[-1]
    assert cmd[cmd.index("--prefix") + 1] == str(prefix)
    assert "-c" in cmd and str(se.REQUIREMENTS) in cmd


def test_restart_reuses_install_without_pip(fresh, tmp_path):
    fresh.setenv("SLEEPECG", "true")
    prefix = se.install_dir(tmp_path)
    prefix.mkdir(parents=True)
    (prefix / ".ok").write_text("x")
    state = {"active": False}
    fresh.setattr(se, "_activate", lambda p: state.update(active=True))
    fresh.setattr(se, "_importable", lambda: state["active"])
    fresh.setattr(subprocess, "run", lambda *a, **k: pytest.fail("pip should not run"))
    se.bootstrap(tmp_path, background=False)
    assert se.status()["status"] == "ready"


def test_failed_install_keeps_heuristic(fresh, tmp_path):
    fresh.setenv("SLEEPECG", "true")
    fresh.setattr(se, "_importable", lambda: False)
    fresh.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=0 if "freeze" in cmd else 1, stdout="", stderr="ERROR: no network"))
    se.bootstrap(tmp_path, background=False)
    st = se.status()
    assert st["status"] == "failed" and "no network" in st["detail"] and not st["available"]
    assert not (se.install_dir(tmp_path) / ".ok").exists()


def test_new_requirements_get_a_new_folder(tmp_path, monkeypatch):
    req = tmp_path / "req.txt"
    req.write_text("sleepecg>=0.6\n")
    monkeypatch.setattr(se, "REQUIREMENTS", req)
    a = se.install_dir(tmp_path)
    req.write_text("sleepecg>=0.7\n")
    assert se.install_dir(tmp_path) != a


def test_pickers_and_health_show_install_state(fresh):
    from fastapi.testclient import TestClient

    from app import main
    fresh.setattr(se, "bootstrap", lambda *a, **k: None)
    fresh.setattr(se, "_importable", lambda: False)
    with TestClient(main.app) as c:
        se._state.update(status="installing", detail="")
        assert "installing…" in c.get("/import").text
        h = c.get("/api/health").json()
        assert h["sleepecg"] is False and h["sleepecg_status"]["status"] == "installing"
        se._state.update(status="off", detail="")
        assert "off: set SLEEPECG=true" in c.get("/import").text
