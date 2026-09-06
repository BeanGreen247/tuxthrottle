"""Phase 0 — the hardware specifics in sensors.py / tuxthrottle_kbd.py /
hotkey_listener.py are now read from the model profile, with the 5515 values
as the fallback. These tests pin both directions: a profile field is used
when present, and its absence falls back to the reference value.
"""
import importlib

import sensors


def _reset_cache():
    sensors._MODEL_CACHE = None


def teardown_function():
    _reset_cache()


# --- profile field present -> used ---------------------------------------- #

def test_accessors_read_from_profile(monkeypatch):
    fake = {
        "id": "acme-x1",
        "cpu": {"hwmon": "coretemp"},
        "fans": {
            "hwmon": "acme_ec",
            "pwm_hwmon": "acme_pwm",
            "pwm_floor": 90,
            "count": 3,
            "platform_profile_path": "/sys/acme/profile",
        },
    }
    fake["fans"].update({"additive_boost": ["pwm1_boost", "pwm2_boost"],
                         "rpm_max": 5200})
    fake["game_mode"] = {"value": "turbo"}
    monkeypatch.setattr(sensors, "model_profile", lambda: fake)
    assert sensors._cpu_temp_hwmon() == "coretemp"
    assert sensors._fan_hwmon() == "acme_ec"
    assert sensors._fan_pwm_hwmon() == "acme_pwm"
    assert sensors._pwm_floor() == 90
    assert sensors._fan_indices() == (1, 2, 3)
    assert sensors._platform_profile_path() == "/sys/acme/profile"
    assert sensors._fan_boost_attr(1) == "pwm1_boost"
    assert sensors._fan_boost_attr(3) == "fan3_boost"   # past the list -> convention
    assert sensors._fan_rpm_max() == 5200
    assert sensors._game_mode_value() == "turbo"


# --- profile field absent -> 5515 fallback ------------------------------- #

def test_accessors_fall_back_when_profile_empty(monkeypatch):
    monkeypatch.setattr(sensors, "model_profile", lambda: {"id": "bare"})
    assert sensors._cpu_temp_hwmon() == "k10temp"
    assert sensors._fan_hwmon() == "alienware_wmi"
    assert sensors._fan_pwm_hwmon() == "dell_smm"
    assert sensors._pwm_floor() == sensors.PWM_FLOOR == 77
    assert sensors._fan_indices() == (1, 2)
    assert sensors._platform_profile_path() == "/sys/firmware/acpi/platform_profile"
    assert sensors._fan_boost_attr(1) == "fan1_boost"
    assert sensors._fan_rpm_max() == 4700
    assert sensors._game_mode_value() == "performance"


def test_fan_indices_derives_count_from_rpm_inputs(monkeypatch):
    monkeypatch.setattr(sensors, "model_profile",
                        lambda: {"fans": {"rpm_inputs": ["fan1_input"]}})
    assert sensors._fan_indices() == (1,)


def test_reference_profile_still_resolves_to_5515_values(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(sensors, "_dmi",
                        lambda k: "Dell G15 5515" if k == "product_name" else "")
    assert sensors.model_id() == "g15-5515"
    assert sensors._fan_hwmon() == "alienware_wmi"
    assert sensors._pwm_floor() == 77
    assert sensors._fan_indices() == (1, 2)


# --- tuxthrottle_kbd routes its device name/USB id through the profile --- #

def test_env_override_selects_named_profile(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("TUXTHROTTLE_MODEL", "_test-fixture")
    monkeypatch.setattr(sensors, "_dmi",
                        lambda k: "Dell G15 5515" if k == "product_name" else "")
    prof = sensors.model_profile()
    assert prof["id"] == "_test-fixture"          # DMI ignored
    assert sensors._fan_hwmon() == "acme_ec"
    assert sensors._pwm_floor() == 90
    assert sensors._fan_indices() == (1, 2, 3)
    _reset_cache()


def test_env_override_unknown_slug_falls_back(monkeypatch):
    _reset_cache()
    monkeypatch.setenv("TUXTHROTTLE_MODEL", "does-not-exist")
    monkeypatch.setattr(sensors, "_dmi", lambda k: "")
    assert sensors.model_profile()["id"] == "g15-5515"
    _reset_cache()


def test_underscore_files_are_not_auto_matched():
    files = list(sensors._model_files())
    assert not any(f.rsplit("/", 1)[-1].startswith("_") for f in files)


def test_gating_helpers(monkeypatch):
    monkeypatch.setattr(sensors, "model_profile",
                        lambda: {"id": "acme-x1",
                                 "tweaks_skip": ["RyzenAdjTDP"]})
    # models list gate
    assert sensors.model_allows(None) is True
    assert sensors.model_allows([]) is True
    assert sensors.model_allows(["acme-x1", "g15-5515"]) is True
    assert sensors.model_allows(["g15-5515"]) is False
    # tweaks_skip gate
    assert sensors.model_skips_tweak("RyzenAdjTDP") is True
    assert sensors.model_skips_tweak("FanCurveDaemon") is False


def test_kbd_no_server_restart_when_reasserting_the_same_effect(monkeypatch):
    """A spectrum re-assert (tray / boot / resume, saved mode == 'spectrum')
    must NOT bounce the OpenRGB SDK server — overlapping re-asserts used to
    turn that into a restart storm that SIGSEGV'd Steam's HID enumeration."""
    import tuxthrottle_kbd as kbd
    calls = []
    monkeypatch.setattr(kbd, "restart_server", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(kbd, "load_meta", lambda: {"mode": "spectrum"})
    kbd._leave_effect_kick("spectrum")          # re-asserting spectrum
    assert calls == []
    kbd._leave_effect_kick(None)                # spectrum -> static: a real transition
    assert len(calls) == 1


def test_kbd_restart_server_is_rate_limited(monkeypatch, tmp_path):
    import tuxthrottle_kbd as kbd
    stamp = tmp_path / "stamp"
    monkeypatch.setattr(kbd, "_RESTART_STAMP", str(stamp))
    ran = []
    monkeypatch.setattr(kbd.subprocess, "run",
                        lambda *a, **k: ran.append(a) or type("R", (), {"returncode": 1})())
    monkeypatch.setattr(kbd.time, "sleep", lambda _s: None)
    kbd.restart_server()                        # first call: allowed
    n1 = len(ran)
    assert n1 > 0
    kbd.restart_server()                        # immediately again: rate-limited
    assert len(ran) == n1
    kbd.restart_server(force=True)              # force overrides
    assert len(ran) > n1


def test_kbd_reads_device_from_profile(monkeypatch):
    _reset_cache()
    monkeypatch.setattr(
        sensors, "model_profile",
        lambda: {"keyboard": {"openrgb_device": "Acme LED", "usb": "1a2b:3c4d"}})
    import tuxthrottle_kbd
    kbd = importlib.reload(tuxthrottle_kbd)
    assert kbd.DEVICE == "Acme LED"
    assert kbd._USB_VID == "1a2b"
    assert "3c4d" in kbd._USB_PIDS
    # restore the real module for other tests
    _reset_cache()
    importlib.reload(kbd)
