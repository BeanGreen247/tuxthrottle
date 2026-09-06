"""tuxthrottle_mangohud_status.py — enable/disable state and the targeted
custom_text_center= line rewrite. No real MangoHud/tray needed.
"""
import tuxthrottle_mangohud_status as mhs


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(mhs, "_config_home", lambda user=None: tmp_path)


def test_disabled_by_default(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert mhs.is_enabled() is False


def test_enable_disable_round_trip(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    mhs.set_enabled(True)
    assert mhs.is_enabled() is True
    mhs.set_enabled(False)
    assert mhs.is_enabled() is False


def test_set_status_line_noop_when_disabled(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    mhs.set_status_line("hello")
    assert not mhs._mangohud_conf_path().exists()


def test_set_status_line_writes_only_that_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    conf = mhs._mangohud_conf_path()
    conf.parent.mkdir(parents=True)
    conf.write_text("fps_limit=144\ngpu_stats\n")
    mhs.set_enabled(True)

    mhs.set_status_line("Game Mode ON | fan 50%")
    text = conf.read_text()
    assert "fps_limit=144" in text
    assert "gpu_stats" in text
    assert "custom_text_center=Game Mode ON | fan 50%" in text


def test_set_status_line_replaces_not_duplicates(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    mhs.set_enabled(True)
    mhs.set_status_line("first")
    mhs.set_status_line("second")
    lines = mhs._mangohud_conf_path().read_text().splitlines()
    matches = [l for l in lines if l.startswith("custom_text_center=")]
    assert matches == ["custom_text_center=second"]


def test_clear_status_line_removes_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    mhs.set_enabled(True)
    mhs.set_status_line("visible")
    mhs.clear_status_line()
    text = mhs._mangohud_conf_path().read_text()
    assert "custom_text_center" not in text


def test_disabling_clears_existing_line(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    mhs.set_enabled(True)
    mhs.set_status_line("visible")
    mhs.set_enabled(False)
    text = mhs._mangohud_conf_path().read_text()
    assert "custom_text_center" not in text
