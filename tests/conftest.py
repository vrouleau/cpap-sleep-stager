import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sleepstager-test-"))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory):
    from datetime import datetime

    import make_demo_data

    out = tmp_path_factory.mktemp("demo")
    make_demo_data.make_night(out, datetime(2026, 11, 12, 22, 50), seed=1000)
    return out


@pytest.fixture(scope="session")
def skewed_dir(tmp_path_factory):
    """Same night, but the O2Ring clock runs 23 s ahead of the CPAP."""
    from datetime import datetime

    import make_demo_data

    out = tmp_path_factory.mktemp("skewed")
    make_demo_data.make_night(out, datetime(2026, 11, 12, 22, 50), seed=1000, ring_ahead_s=23)
    return out


@pytest.fixture(scope="session")
def dst_dir(tmp_path_factory):
    """CPAP still on summer time after the fall-back: its clock reads 1 h ahead,
    i.e. the ring is 1 h behind the CPAP."""
    from datetime import datetime

    import make_demo_data

    out = tmp_path_factory.mktemp("dst")
    make_demo_data.make_night(out, datetime(2026, 11, 12, 23, 50), seed=1001,
                              ring_ahead_s=-3600)
    return out
