"""ThermalWatcher event/cooldown logic + model-profile loading."""
import json

import pytest

import sensors
import tuxthrottle_powerd as pd

# --------------------------------------------------------------------------- #
#  ThermalWatcher
# --------------------------------------------------------------------------- #

@pytest.fixture()
def watcher():
    return pd.ThermalWatcher(user="tester")


def _cfg(**over):
    tn = {"enabled": True, "tjmax_c": 95, "tjmax_sustain_s": 0,
          "stalled_fan_hot_c": 70, "battery_perf_min_pct": 20, "cooldown_s": 300}
    tn.update(over)
    return {"thermal_notify": tn}


def test_disabled_watcher_does_nothing(watcher, monkeypatch):
    fired = []
    monkeypatch.setattr(watcher, "_fire", lambda *a, **k: fired.append(a))
    watcher.tick({"thermal_notify": {"enabled": False}})
    assert fired == []


def test_sustained_tjmax_fires(watcher, monkeypatch):
    fired = []
    monkeypatch.setattr(watcher, "_fire", lambda kind, *a: fired.append(kind))
    monkeypatch.setattr(sensors, "read_ryzenadj_info", lambda: {"tctl_value": 97})
    monkeypatch.setattr(sensors, "read_fans", lambda: [])
    monkeypatch.setattr(pd, "read_temp", lambda _s: 60)
    monkeypatch.setattr(pd, "_ac_online", lambda: True)
    watcher.tick(_cfg(tjmax_sustain_s=0))
    assert "tjmax" in fired


def test_cool_cpu_does_not_fire(watcher, monkeypatch):
    fired = []
    monkeypatch.setattr(watcher, "_fire", lambda kind, *a: fired.append(kind))
    monkeypatch.setattr(sensors, "read_ryzenadj_info", lambda: {"tctl_value": 70})
    monkeypatch.setattr(sensors, "read_fans", lambda: [{"index": 1, "label": "CPU", "rpm": 2500}])
    monkeypatch.setattr(pd, "read_temp", lambda _s: 70)
    monkeypatch.setattr(pd, "_ac_online", lambda: True)
    watcher.tick(_cfg())
    assert fired == []


def test_stalled_fan_while_hot_fires(watcher, monkeypatch):
    fired = []
    monkeypatch.setattr(watcher, "_fire", lambda kind, *a: fired.append(kind))
    monkeypatch.setattr(sensors, "read_ryzenadj_info", lambda: {})
    monkeypatch.setattr(sensors, "read_fans",
                        lambda: [{"index": 1, "label": "CPU Fan", "rpm": 0}])
    monkeypatch.setattr(pd, "read_temp", lambda _s: 85)
    monkeypatch.setattr(pd, "_ac_online", lambda: True)
    watcher.tick(_cfg())
    assert "stalled_fan" in fired


def test_performance_on_low_battery_fires(watcher, monkeypatch):
    fired = []
    monkeypatch.setattr(watcher, "_fire", lambda kind, *a: fired.append(kind))
    monkeypatch.setattr(sensors, "read_ryzenadj_info", lambda: {})
    monkeypatch.setattr(sensors, "read_fans", lambda: [])
    monkeypatch.setattr(pd, "read_temp", lambda _s: 55)
    monkeypatch.setattr(pd, "_ac_online", lambda: False)
    monkeypatch.setattr(sensors, "battery_charge_limit_info", lambda: {"capacity": 12})
    monkeypatch.setattr(sensors, "get_platform_profile", lambda: "performance")
    watcher.tick(_cfg())
    assert "battery_perf" in fired


def test_fire_respects_cooldown(watcher, monkeypatch):
    calls = []
    monkeypatch.setattr(sensors, "notify", lambda *a, **k: calls.append(a))
    watcher._fire("k", 300, "s", "b")
    watcher._fire("k", 300, "s", "b")   # within cooldown -> suppressed
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
#  model profile
# --------------------------------------------------------------------------- #

def test_model_profile_falls_back_to_g15(monkeypatch):
    sensors._MODEL_CACHE = None
    monkeypatch.setattr(sensors, "_dmi", lambda k: "")
    prof = sensors.model_profile()
    assert prof["id"] == "g15-5515"
    assert prof["fans"]["pwm_floor"] == 77
    sensors._MODEL_CACHE = None


def test_model_profile_matches_on_board_name(monkeypatch):
    sensors._MODEL_CACHE = None
    monkeypatch.setattr(sensors, "_dmi",
                        lambda k: "0R3CDX" if k == "board_name" else "")
    assert sensors.model_id() == "g15-5515"
    sensors._MODEL_CACHE = None


def test_every_model_file_is_valid_json():
    for path in sensors._model_files():
        with open(path) as f:
            prof = json.load(f)
        assert prof["id"] == path.rsplit("/", 1)[-1][:-5]
        assert "match" in prof
