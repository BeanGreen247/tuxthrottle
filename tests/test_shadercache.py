"""tuxthrottle_shadercache.py — config persistence, the 3-number size split,
and clean(). No Steam / network needed.
"""
import tuxthrottle_shadercache as sc


def _isolate(monkeypatch, tmp_path):
    cfg = tmp_path / "shadercache.json"
    monkeypatch.setattr(sc, "_config_path", lambda: cfg)
    return cfg


def test_defaults_when_no_config(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert sc.load() == {}
    assert str(sc.cache_dir()).endswith("tuxthrottle-shaders")
    assert sc.max_size_gb() == sc.DEFAULT_SIZE_GB


def test_set_config_persists_and_creates_subdirs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    target = tmp_path / "mycache"
    base = sc.set_config(str(target), 120)
    assert base == target
    for s in sc.SUBDIRS:
        assert (target / s).is_dir()
    # reload from disk
    assert sc.load()["dir"] == str(target)
    assert sc.max_size_gb() == 120


def test_status_splits_steam_from_the_rest(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = sc.set_config(str(tmp_path / "c"), 80)
    (base / "steam-shadercache" / "a").write_bytes(b"x" * 4096)
    (base / "mesa-shader-cache" / "b").write_bytes(b"y" * 2048)
    (base / "dxvk-state-cache" / "c").write_bytes(b"z" * 1024)
    st = sc.status()
    b = st["bytes"]
    assert b["steam"] >= 4096
    assert b["other"] >= 3072
    assert b["total"] == b["steam"] + b["other"]
    assert "iB" in st["total"] and "iB" in st["steam_shader_cache"]


def test_clean_empties_subdirs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    base = sc.set_config(str(tmp_path / "c"))
    (base / "mesa-shader-cache" / "junk").write_bytes(b"1" * 10)
    (base / "dxvk-state-cache" / "sub").mkdir()
    monkeypatch.setattr(sc, "steam_running", lambda: False)
    ok, msg = sc.clean("all")
    assert ok
    assert not any((base / s).iterdir().__next__() if list((base / s).iterdir()) else False
                   for s in sc.SUBDIRS)


def test_human_readable():
    assert sc._human(512) == "512 B"
    assert sc._human(1536).endswith("KiB")
    assert sc._human(5 * 1024**3).endswith("GiB")
