import tuxthrottle_powerd as pd


CURVE = [[45, 0], [60, 25], [72, 55], [82, 85], [90, 100]]


def test_interp_clamps_below_and_above():
    assert pd.interp(CURVE, 30) == 0
    assert pd.interp(CURVE, 45) == 0
    assert pd.interp(CURVE, 95) == 100
    assert pd.interp(CURVE, 90) == 100


def test_interp_linear_midpoint():
    # halfway between (60,25) and (72,55) -> 66 C -> 40 %
    assert pd.interp(CURVE, 66) == 40


def test_interp_unsorted_points_ok():
    assert pd.interp(list(reversed(CURVE)), 66) == 40


def test_interp_empty_points():
    assert pd.interp([], 70) == 0


def test_load_config_defaults_when_missing(tmp_path):
    cfg = pd.load_config(tmp_path / "nope.json")
    assert cfg["fan_curve"]["enabled"] is False
    assert cfg["autoswitch"]["enabled"] is False
    assert set(cfg) >= {"poll_s", "fan_curve", "autoswitch"}


def test_load_config_deep_merges_partial(tmp_path):
    p = tmp_path / "powerd.json"
    p.write_text('{"fan_curve": {"enabled": true}}')
    cfg = pd.load_config(p)
    assert cfg["fan_curve"]["enabled"] is True
    # untouched sub-keys keep their defaults
    assert cfg["fan_curve"]["sensor"] == "max"
    assert cfg["autoswitch"]["enabled"] is False


def test_load_config_bad_json_falls_back(tmp_path):
    p = tmp_path / "powerd.json"
    p.write_text("{not json")
    assert pd.load_config(p)["fan_curve"]["enabled"] is False


def test_read_temp_sensor_selection(monkeypatch):
    monkeypatch.setattr(pd.sensors, "read_cpu_temp_c_value", lambda: 60.0)
    monkeypatch.setattr(pd.sensors, "read_dgpu_values", lambda: (1000, 75, 0, 10.0))
    assert pd.read_temp("cpu") == 60.0
    assert pd.read_temp("gpu") == 75.0
    assert pd.read_temp("max") == 75.0


def test_read_temp_handles_missing_gpu(monkeypatch):
    monkeypatch.setattr(pd.sensors, "read_cpu_temp_c_value", lambda: 55.0)
    monkeypatch.setattr(pd.sensors, "read_dgpu_values", lambda: (None, None, None, None))
    assert pd.read_temp("max") == 55.0
    assert pd.read_temp("gpu") is None


def test_fancontroller_ramps_up_then_holds(monkeypatch):
    written = []
    monkeypatch.setattr(pd.sensors, "set_fan_boost", lambda i, v: written.append((i, v)) or (True, ""))
    monkeypatch.setattr(pd.sensors, "restore_fan_auto", lambda: (True, ""))
    fc = pd.FanController()
    cfg = {"fan_curve": {"enabled": True, "sensor": "cpu", "hysteresis_c": 3,
                         "points": CURVE}}
    monkeypatch.setattr(pd, "read_temp", lambda _s: 66.0)
    fc.tick(cfg)
    assert fc._applied == 40
    written.clear()
    fc.tick(cfg)                      # same temp -> no new write
    assert written == []


def test_fancontroller_restores_on_disable(monkeypatch):
    calls = {"restore": 0}
    monkeypatch.setattr(pd.sensors, "set_fan_boost", lambda i, v: (True, ""))
    monkeypatch.setattr(pd.sensors, "restore_fan_auto",
                        lambda: calls.__setitem__("restore", calls["restore"] + 1) or (True, ""))
    monkeypatch.setattr(pd, "read_temp", lambda _s: 80.0)
    fc = pd.FanController()
    fc.tick({"fan_curve": {"enabled": True, "sensor": "cpu", "points": CURVE}})
    fc.tick({"fan_curve": {"enabled": False}})
    assert calls["restore"] == 1


def test_gameprofile_applies_on_hit_and_restores(monkeypatch):
    events = []
    monkeypatch.setattr(pd, "_running_procs", lambda: {"gta5.exe"})
    monkeypatch.setattr(pd.profiles, "snapshot", lambda *a, **k: events.append("snapshot"))
    monkeypatch.setattr(pd.profiles, "load_profile", lambda n, u=None: {"platform_profile": "performance"})
    monkeypatch.setattr(pd.profiles, "apply_state",
                        lambda st, u=None: events.append(("apply", st)) or [{"key": "platform_profile", "ok": True, "msg": ""}])
    monkeypatch.setattr(pd.profiles, "rollback",
                        lambda t, u=None: events.append(("rollback", t)) or [{"key": "-", "ok": True, "msg": ""}])
    gc = pd.GameProfileController("bean")
    cfg = {"game_profiles": {"enabled": True, "match": {"gta5.exe": "Gaming"}, "default": None}}
    gc.tick(cfg)
    assert gc._active_key == "gta5.exe"
    assert "snapshot" in events and any(e[0] == "apply" for e in events if isinstance(e, tuple))
    # game exits
    monkeypatch.setattr(pd, "_running_procs", lambda: set())
    gc.tick(cfg)
    assert gc._active_key is None
    assert any(e[0] == "rollback" for e in events if isinstance(e, tuple))


def test_gameprofile_wildcard_needs_gamemode(monkeypatch):
    monkeypatch.setattr(pd, "_running_procs", lambda: set())
    monkeypatch.setattr(pd.sensors, "gamemode_status", lambda: {"active": True})
    monkeypatch.setattr(pd.profiles, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(pd.profiles, "load_profile", lambda n, u=None: {"platform_profile": "performance"})
    applied = []
    monkeypatch.setattr(pd.profiles, "apply_state",
                        lambda st, u=None: applied.append(st) or [{"key": "x", "ok": True, "msg": ""}])
    gc = pd.GameProfileController("bean")
    gc.tick({"game_profiles": {"enabled": True, "match": {"*": "Performance"}, "default": None}})
    assert gc._active_key == "*"
    assert applied
