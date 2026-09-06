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


def test_session_summary_written_on_game_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(pd, "_running_procs", lambda: {"gta5.exe"})
    monkeypatch.setattr(pd.profiles, "snapshot", lambda *a, **k: None)
    monkeypatch.setattr(pd.profiles, "load_profile", lambda n, u=None: {"x": 1})
    monkeypatch.setattr(pd.profiles, "apply_state",
                        lambda st, u=None: [{"key": "x", "ok": True, "msg": ""}])
    monkeypatch.setattr(pd.profiles, "rollback", lambda t, u=None: [])
    monkeypatch.setattr(pd, "_session_path", lambda u: tmp_path / "last_session.json")
    monkeypatch.setattr(pd, "_chown_user", lambda p, u: None)
    monkeypatch.setattr(pd.sensors, "read_ryzenadj_info", lambda: {"tctl_value": 92.0})
    monkeypatch.setattr(pd.sensors, "read_cpu_power_watts", lambda: 55.0)
    monkeypatch.setattr(pd.sensors, "read_cpu_freq_ghz_value", lambda: 3.8)
    monkeypatch.setattr(pd.sensors, "read_dgpu_values", lambda: (1650, 70, 80, 60.0))
    gc = pd.GameProfileController("bean")
    cfg = {"game_profiles": {"enabled": True, "match": {"gta5.exe": "Gaming"},
                             "default": None, "poll_s": 6}}
    for _ in range(4):
        gc.tick(cfg)                       # game running -> samples
    monkeypatch.setattr(pd, "_running_procs", lambda: set())
    gc.tick(cfg)                            # game exits -> writes summary
    import json
    s = json.loads((tmp_path / "last_session.json").read_text())
    assert s["game"] == "gta5.exe"
    assert s["cpu_temp_max_c"] == 92
    assert s["gpu_temp_max_c"] == 70
    assert s["throttle_pct"] == 100        # tctl 92 >= 90 every sample


def test_thermal_fan_stall_recover(monkeypatch):
    tw = pd.ThermalWatcher("bean")
    set_calls = []
    monkeypatch.setattr(pd.sensors, "get_platform_profile", lambda: "balanced")
    monkeypatch.setattr(pd.sensors, "set_platform_profile",
                        lambda p: set_calls.append(p) or (True, ""))
    monkeypatch.setattr(pd.sensors, "read_ryzenadj_info", lambda: {"tctl_value": 60})
    monkeypatch.setattr(pd.sensors, "read_fans",
                        lambda: [{"index": 1, "label": "CPU Fan", "rpm": 0}])
    monkeypatch.setattr(pd, "read_temp", lambda s: 85.0)
    monkeypatch.setattr(pd, "_ac_online", lambda: True)
    cfg = {"thermal_notify": {"enabled": True, "stalled_fan_hot_c": 70,
                              "stalled_fan_recover": True, "stalled_fan_recover_s": 45}}
    tw.tick(cfg)
    assert set_calls == ["performance"]     # kicked G-Mode
    assert tw._recover_prev == "balanced"
    # fans come back, recovery window elapsed -> restore
    monkeypatch.setattr(pd.sensors, "read_fans",
                        lambda: [{"index": 1, "label": "CPU Fan", "rpm": 3200}])
    tw._recover_until = 0.0
    tw.tick(cfg)
    assert set_calls == ["performance", "balanced"]
    assert tw._recover_prev is None


# --- time-of-day schedule -----------------------------------------------------

def test_hhmm_to_min():
    assert pd._hhmm_to_min("00:00") == 0
    assert pd._hhmm_to_min("22:30") == 22 * 60 + 30
    assert pd._hhmm_to_min("7:05") == 7 * 60 + 5
    assert pd._hhmm_to_min("nope") is None
    assert pd._hhmm_to_min(None) is None


class _Clock:
    def __init__(self, hh, mm, wday=2):
        import time as _t
        self._st = _t.struct_time((2026, 9, 1, hh, mm, 0, wday, 244, -1))

    def __call__(self):
        return self._st


def _sched(monkeypatch, hh, mm, wday=2):
    applied = []
    monkeypatch.setattr(pd, "_apply_bundle_or_profile",
                        lambda name, user: applied.append(name))
    monkeypatch.setattr(pd.time, "localtime", _Clock(hh, mm, wday))
    return pd.ScheduleController(user=None), applied


CFG = {"schedule": {"enabled": True, "poll_s": 0,
                    "rules": [{"from": "22:00", "to": "07:00", "apply": "Quiet"}],
                    "outside": "Balanced"}}


def test_schedule_overnight_rule_active_before_midnight(monkeypatch):
    sc, applied = _sched(monkeypatch, 23, 30)
    sc.tick(CFG, game_active=False)
    assert applied == ["Quiet"]


def test_schedule_overnight_rule_active_after_midnight(monkeypatch):
    sc, applied = _sched(monkeypatch, 3, 0)
    sc.tick(CFG, game_active=False)
    assert applied == ["Quiet"]


def test_schedule_outside_rule_daytime(monkeypatch):
    sc, applied = _sched(monkeypatch, 12, 0)
    sc.tick(CFG, game_active=False)
    assert applied == ["Balanced"]


def test_schedule_only_applies_on_change(monkeypatch):
    sc, applied = _sched(monkeypatch, 12, 0)
    sc.tick(CFG, game_active=False)
    sc.tick(CFG, game_active=False)
    assert applied == ["Balanced"]


def test_schedule_yields_to_game(monkeypatch):
    sc, applied = _sched(monkeypatch, 12, 0)
    sc.tick(CFG, game_active=True)
    assert applied == []


def test_schedule_days_filter(monkeypatch):
    cfg = {"schedule": {"enabled": True, "poll_s": 0,
                        "rules": [{"from": "09:00", "to": "17:00",
                                   "apply": "Performance", "days": [0, 1, 2, 3, 4]}],
                        "outside": "Quiet"}}
    sc, applied = _sched(monkeypatch, 12, 0, wday=6)   # Sunday
    sc.tick(cfg, game_active=False)
    assert applied == ["Quiet"]


def test_schedule_disabled_noop(monkeypatch):
    sc, applied = _sched(monkeypatch, 3, 0)
    sc.tick({"schedule": {"enabled": False}}, game_active=False)
    assert applied == []
