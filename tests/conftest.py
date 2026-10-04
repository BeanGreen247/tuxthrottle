import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest


@pytest.fixture(autouse=True)
def _no_real_steam(monkeypatch):
    """No test may touch the real Steam install (a shadercache test once
    repointed the live libraries' symlinks into pytest's tmp dir)."""
    import tuxthrottle_shadercache as sc
    monkeypatch.setattr(sc, "_find_steam_root", lambda: None)
