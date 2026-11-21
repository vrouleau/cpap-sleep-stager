"""sleepecg is baked into the image; SLEEPECG=true/false decides whether it is used."""
import pytest

from app.staging import sleepecg_engine as se


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))  # no settings.json from other tests
    monkeypatch.delenv("SLEEPECG", raising=False)
    monkeypatch.delenv("WITH_SLEEPECG", raising=False)
    monkeypatch.setattr(se, "_classifier", lambda: None)
    return monkeypatch


def test_switch_values(env):
    for v in ("true", "1", "YES", " on "):
        env.setenv("SLEEPECG", v)
        assert se.wanted()
    for v in ("false", "0", "", "no"):
        env.setenv("SLEEPECG", v)
        assert not se.wanted()
    env.delenv("SLEEPECG")
    env.setenv("WITH_SLEEPECG", "true")  # old name still works
    assert se.wanted()


@pytest.mark.parametrize("switch,in_image,status,engine", [
    ("true", True, "on", "sleepecg"),
    ("false", True, "off", "heuristic"),
    ("true", False, "missing", "heuristic"),  # image built with WITH_SLEEPECG=false
    ("false", False, "off", "heuristic"),
])
def test_switch_and_image(env, switch, in_image, status, engine):
    env.setenv("SLEEPECG", switch)
    env.setattr(se, "_installed", in_image)
    st = se.status()
    assert st["status"] == status and st["installed"] is in_image
    assert se.available() is (status == "on")
    assert se.default_engine() == engine


def test_switched_off_never_runs_the_model(env, demo_dir):
    from pathlib import Path

    from app.pipeline import ProcessOptions, analyze
    env.setenv("SLEEPECG", "false")
    env.setattr(se, "_installed", True)
    env.setattr(se, "prior_probs", lambda *a, **k: pytest.fail("model used while switched off"))
    files = [p for p in Path(demo_dir).rglob("*") if p.is_file() and not p.name.startswith("truth")]
    assert analyze(files, ProcessOptions(engine="sleepecg"))["engine"] == "heuristic"


def test_pages_and_health_follow_the_switch(env):
    from fastapi.testclient import TestClient

    from app import main
    env.setattr(se, "_installed", True)
    with TestClient(main.app) as c:
        env.setenv("SLEEPECG", "true")
        page = c.get("/import").text
        assert 'value="sleepecg" selected' in page.replace("  ", " ")
        h = c.get("/api/health").json()
        assert h["sleepecg"] is True and h["sleepecg_status"]["status"] == "on"
        assert main._opts("auto", 0, False, None, None).engine == "sleepecg"
        assert main._opts("heuristic", 0, False, None, None).engine == "heuristic"

        env.setenv("SLEEPECG", "false")
        assert "off: switch it on under Data import" in c.get("/import").text
        assert main._opts("auto", 0, False, None, None).engine == "heuristic"

        env.setattr(se, "_installed", False)
        env.setenv("SLEEPECG", "true")
        assert "not in this image" in c.get("/import").text


def test_switch_in_the_app_overrides_environment(env):
    from fastapi.testclient import TestClient

    from app import main
    env.setattr(se, "_installed", True)
    env.setattr(se, "bootstrap", lambda *a, **k: None)
    env.setenv("SLEEPECG", "false")
    with TestClient(main.app) as c:
        page = c.get("/import").text
        assert 'action="/settings/sleepecg"' in page and "Use the <b>sleepecg</b> engine" in page
        r = c.post("/settings/sleepecg", data={"enabled": "true"}, follow_redirects=False)
        assert r.status_code == 303
        st = c.get("/api/settings").json()["sleepecg"]
        assert st["enabled"] and st["available"] and st["source"] == "app"
        assert main._opts("auto", 0, False, None, None).engine == "sleepecg"
        c.post("/settings/sleepecg", data={}, follow_redirects=False)  # unticked = off
        assert not c.get("/api/settings").json()["sleepecg"]["enabled"]
        env.setenv("SLEEPECG", "true")  # the saved switch still wins over the variable
        assert not se.wanted()
        assert c.put("/api/settings", json={"sleepecg": True}).json()["sleepecg"]["enabled"]


def test_switch_hidden_when_not_in_image(env):
    from fastapi.testclient import TestClient

    from app import main
    env.setattr(se, "_installed", False)
    with TestClient(main.app) as c:
        assert 'action="/settings/sleepecg"' not in c.get("/import").text
