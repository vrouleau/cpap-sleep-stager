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


@pytest.fixture(scope="session")
def drift_dir(tmp_path_factory):
    """Missed DST plus 32 min of accumulated drift (CPAP 1 h 32 min ahead)."""
    from datetime import datetime

    import make_demo_data

    out = tmp_path_factory.mktemp("drift")
    make_demo_data.make_night(out, datetime(2026, 11, 13, 0, 22), seed=1002,
                              ring_ahead_s=-(3600 + 32 * 60))
    return out


@pytest.fixture(scope="session")
def travel_dir(tmp_path_factory):
    """Toronto CPAP used in Paris: machine reads 16:50 while the phone-synced ring
    reads 22:50 local time (ring 6 h ahead), plus a few minutes of drift."""
    from datetime import datetime

    import make_demo_data

    out = tmp_path_factory.mktemp("travel")
    make_demo_data.make_night(out, datetime(2026, 11, 12, 16, 50), seed=1003,
                              ring_ahead_s=6 * 3600 + 190)
    return out
