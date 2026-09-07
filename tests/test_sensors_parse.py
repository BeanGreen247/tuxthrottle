import subprocess
import types

import sensors

RYZENADJ_I = """\
CPU Family: Cezanne
SMU BIOS Interface Version: 17
Version: v0.16.0

| Name                | Value      | Parameter          |
| STAPM LIMIT         |    54.000  | stapm-limit        |
| STAPM VALUE         |    12.345  |                    |
| PPT LIMIT FAST      |    65.000  | fast-limit         |
| PPT VALUE FAST      |    20.100  |                    |
| PPT LIMIT SLOW      |    54.000  | slow-limit         |
| PPT VALUE SLOW      |     8.900  |                    |
| THM LIMIT CORE      |    95.000  | tctl-temp          |
| THM VALUE CORE      |    61.200  |                    |
"""


def _fake_run(stdout="", returncode=0):
    def run(*_a, **_k):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return run


def test_read_ryzenadj_info_parses_limits(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/ryzenadj")
    monkeypatch.setattr(subprocess, "run", _fake_run(RYZENADJ_I))
    info = sensors.read_ryzenadj_info()
    assert info["stapm_limit"] == 54.0
    assert info["fast_limit"] == 65.0
    assert info["slow_limit"] == 54.0
    assert info["tctl_limit"] == 95.0
    assert info["stapm_value"] == 12.3  # rounded to 1dp


def test_read_ryzenadj_info_none_when_missing(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    assert sensors.read_ryzenadj_info() is None


def test_read_ryzenadj_info_none_on_empty_output(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/ryzenadj")
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    assert sensors.read_ryzenadj_info() is None


def test_f_helper():
    assert sensors._f("80.00") == 80.0
    assert sensors._f("[N/A]") is None
    assert sensors._f("garbage") is None


def test_nvidia_power_limit_firmware_locked(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sensors, "dgpu_is_awake", lambda: True)
    monkeypatch.setattr(subprocess, "run",
                        _fake_run("[N/A], 1.00, 90.00, 80.00\n"))
    info = sensors.nvidia_power_limit_info()
    assert info["supported"] is False
    assert info["current"] is None
    assert info["min"] == 1 and info["max"] == 90 and info["default"] == 80


def test_nvidia_power_limit_supported(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sensors, "dgpu_is_awake", lambda: True)
    monkeypatch.setattr(subprocess, "run",
                        _fake_run("60.00, 20.00, 80.00, 75.00\n"))
    info = sensors.nvidia_power_limit_info()
    assert info["supported"] is True
    assert info["current"] == 60


def test_nvidia_power_limit_none_when_asleep(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sensors, "dgpu_is_awake", lambda: False)
    assert sensors.nvidia_power_limit_info() is None


def test_set_nvidia_power_limit_reports_lock(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sensors, "dgpu_is_awake", lambda: True)
    monkeypatch.setattr(subprocess, "run",
                        _fake_run("[N/A], 1.00, 90.00, 80.00\n"))
    ok, msg = sensors.set_nvidia_power_limit(70)
    assert ok is False and "firmware-locked" in msg


def test_battery_info_shape(monkeypatch):
    monkeypatch.setattr(sensors, "_battery_dir", lambda: None)
    monkeypatch.setattr(sensors, "_smbios_battery_ctl", lambda: None)
    monkeypatch.setattr(sensors, "_dmi", lambda n: "Dell Inc.")
    info = sensors.battery_charge_limit_info()
    assert info["supported"] is False
    assert info["dell_libsmbios_possible"] is True
    assert set(info) >= {"supported", "method", "current", "capacity",
                         "ac_online", "dell_libsmbios_possible"}


# --- panel refresh (kscreen-doctor) -----------------------------------------

KSCREEN_J = """
{"outputs":[
  {"name":"eDP-1","enabled":true,"currentModeId":"1","modes":[
     {"id":"1","name":"1920x1080@144","refreshRate":144.0,"size":{"width":1920,"height":1080}},
     {"id":"2","name":"1920x1080@60","refreshRate":60.019,"size":{"width":1920,"height":1080}},
     {"id":"7","name":"1280x720@144","refreshRate":144.0,"size":{"width":1280,"height":720}}
  ]},
  {"name":"HDMI-1","enabled":false,"currentModeId":"","modes":[]}
]}
"""


def test_panel_modes_parses_and_prefers_internal(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/kscreen-doctor")
    monkeypatch.setattr(sensors, "_session_cmd", lambda a: a)
    monkeypatch.setattr(subprocess, "run", _fake_run(KSCREEN_J))
    pm = sensors.panel_modes()
    assert pm["output"] == "eDP-1"
    assert pm["current_hz"] == 144.0
    assert pm["rates"] == [60, 144]
    assert len(pm["modes"]) == 3


def test_panel_modes_none_without_kscreen(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    assert sensors.panel_modes() is None


def test_session_ready_true_when_not_root(monkeypatch):
    monkeypatch.setattr(sensors.os, "geteuid", lambda: 1000)
    assert sensors._session_ready() is True


def test_session_ready_false_at_boot_no_kwin(monkeypatch):
    # root, a resolvable user, but no wayland socket and no kwin process
    monkeypatch.setattr(sensors.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sensors, "_real_user_uid", lambda: ("bean", 1000))
    monkeypatch.setattr(sensors.os.path, "exists", lambda p: False)
    assert sensors._session_ready() is False


def test_panel_modes_skips_kscreen_doctor_when_session_not_ready(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/kscreen-doctor")
    monkeypatch.setattr(sensors, "_session_ready", lambda: False)
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a) or None)
    assert sensors.panel_modes() is None
    assert called == []          # kscreen-doctor never invoked → can't abort


def test_set_panel_refresh_keeps_resolution(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/kscreen-doctor")
    monkeypatch.setattr(sensors, "_session_cmd", lambda a: a)
    seen = {}

    def run(cmd, *_a, **_k):
        if "-j" in cmd:
            return types.SimpleNamespace(stdout=KSCREEN_J, stderr="", returncode=0)
        seen["spec"] = cmd[-1]
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    ok, tag = sensors.set_panel_refresh(60)
    assert ok is True
    # nearest 60 Hz mode at the current resolution (1920x1080), not the 720p one
    assert seen["spec"].endswith(".mode.2")
    assert tag == "1920x1080@60"


# --- nvidia graphics-clock lock -------------------------------------------------

def test_nvidia_clock_info_parses(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sensors, "dgpu_is_awake", lambda: True)

    def run(cmd, *_a, **_k):
        if "-q" in cmd:
            return types.SimpleNamespace(
                stdout="Graphics : 2100 MHz\nGraphics : 405 MHz\nGraphics : 210 MHz\n",
                stderr="", returncode=0)
        return types.SimpleNamespace(
            stdout="2100, 6001, 210, 405\n", stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    info = sensors.nvidia_clock_info()
    assert info["supported"] is True
    assert info["gr_max"] == 2100
    assert info["gr_min"] == 210
    assert info["gr_cur"] == 210


def test_nvidia_clock_info_none_when_asleep(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(sensors, "dgpu_is_awake", lambda: False)
    assert sensors.nvidia_clock_info() is None


# --- MangoHud label helpers -------------------------------------------------

def test_cpu_model_name(monkeypatch, tmp_path):
    ci = tmp_path / "cpuinfo"
    ci.write_text("processor\t: 0\nvendor_id\t: AuthenticAMD\n"
                  "model name\t: AMD Ryzen 7 5800H with Radeon Graphics\n")
    real_open = open

    def fake_open(p, *a, **k):
        return real_open(ci if p == "/proc/cpuinfo" else p, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    assert sensors.cpu_model_name() == "AMD Ryzen 7 5800H with Radeon Graphics"


LSPCI_MM = (
    '00:08.1 "Display controller" "AMD" "Cezanne [Radeon Vega Series]" -rc9 "Dell" "x"\n'
    '01:00.0 "VGA compatible controller" "NVIDIA Corporation" '
    '"GA107 [GeForce RTX 3050 Ti Mobile]" -ra1 "Dell" "x"\n'
    '02:00.0 "Ethernet controller" "Realtek" "RTL8111" -r15 "Dell" "x"\n'
)


def test_gpu_names_nvidia_and_lspci(monkeypatch):
    monkeypatch.setattr(sensors, "which",
                        lambda c: f"/usr/bin/{c}" if c in ("nvidia-smi", "lspci") else None)

    def run(cmd, *_a, **_k):
        if cmd[0] == "nvidia-smi":
            return types.SimpleNamespace(
                stdout="NVIDIA GeForce RTX 3050 Ti Laptop GPU\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout=LSPCI_MM, stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    names = sensors.gpu_names()
    assert names[0] == "NVIDIA GeForce RTX 3050 Ti Laptop GPU"     # nvidia-smi first
    assert "Radeon Vega Series" in names                           # bracket-name, '/' trimmed
    # the lspci NVIDIA entry is deduped away (nvidia-smi already named it)
    assert sum("RTX 3050" in n for n in names) == 1
    assert not any("Realtek" in n for n in names)                  # non-GPU skipped


def test_gpu_names_empty_without_tools(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    assert sensors.gpu_names() == []


# --- gpu_label_pci_map: keep each MangoHud label glued to the right card ----

LSPCI_DMM = (
    '0000:01:00.0 "VGA compatible controller" "NVIDIA Corporation" '
    '"GA107M [GeForce RTX 3050 Ti Mobile]" -ra1 -p00 "Dell" "Device 0a6e"\n'
    '0000:06:00.0 "VGA compatible controller" '
    '"Advanced Micro Devices, Inc. [AMD/ATI]" '
    '"Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]" -rc5 -p00 '
    '"Dell" "Device 0a6e"\n'
    '0000:03:00.0 "Ethernet controller" "Realtek" "RTL8125" -r05 "Dell" "x"\n'
)


def _lspci_only(monkeypatch, out=LSPCI_DMM):
    monkeypatch.setattr(sensors, "which",
                        lambda c: "/usr/bin/lspci" if c == "lspci" else None)
    monkeypatch.setattr(subprocess, "run", _fake_run(out))


def test_label_map_swapped_config_order(monkeypatch):
    # gpu_text loaded from a config as iGPU-first, detection is dGPU-first:
    # each label must still resolve to its own card, not row-position order.
    _lspci_only(monkeypatch)
    got = sensors.gpu_label_pci_map(["Radeon Vega Series", "RTX 3050 Ti M"])
    assert got == ["0000:06:00.0", "0000:01:00.0"]


def test_label_map_follows_label_order(monkeypatch):
    _lspci_only(monkeypatch)
    got = sensors.gpu_label_pci_map(["RTX 3050 Ti M", "Radeon Vega Series"])
    assert got == ["0000:01:00.0", "0000:06:00.0"]


def test_label_map_unmatched_is_blank(monkeypatch):
    _lspci_only(monkeypatch)
    assert sensors.gpu_label_pci_map(["Potato", "Radeon Vega"]) == \
        ["", "0000:06:00.0"]


def test_label_map_each_pci_used_once(monkeypatch):
    _lspci_only(monkeypatch)
    # two NVIDIA-ish labels, one NVIDIA card → second falls through to blank
    got = sensors.gpu_label_pci_map(["GeForce RTX 3050", "RTX 3050 Ti"])
    assert got[0] == "0000:01:00.0" and got[1] == ""


def test_label_map_no_lspci(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    assert sensors.gpu_label_pci_map(["RTX 3050", "Radeon"]) == ["", ""]


# --- touchpad (KWin InputDevice D-Bus) ------------------------------------

_TOUCHPAD_TREE = """\
├─ /org/kde/KWin/InputDevice/event3
├─ /org/kde/KWin/InputDevice/event6
"""


def _touchpad_busctl_run(touchpad_path="/org/kde/KWin/InputDevice/event6", enabled=True):
    def run(cmd, *_a, **_k):
        if cmd[:3] == ["busctl", "--user", "tree"]:
            return types.SimpleNamespace(stdout=_TOUCHPAD_TREE, stderr="", returncode=0)
        if "get-property" in cmd:
            path, prop = cmd[-3], cmd[-1]
            if prop == "touchpad":
                val = "true" if path == touchpad_path else "false"
                return types.SimpleNamespace(stdout=f"b {val}\n", stderr="", returncode=0)
            if prop == "enabled":
                return types.SimpleNamespace(
                    stdout=f"b {'true' if enabled else 'false'}\n", stderr="", returncode=0)
            if prop == "name":
                return types.SimpleNamespace(
                    stdout='s "DELL0A6E:00 04F3:317E Touchpad"\n', stderr="", returncode=0)
            if prop in ("tapToClick", "naturalScroll", "disableWhileTyping"):
                return types.SimpleNamespace(stdout="b true\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)
    return run


def test_touchpad_device_path_finds_the_touchpad(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/busctl")
    monkeypatch.setattr(sensors, "_session_cmd", lambda a: a)
    monkeypatch.setattr(subprocess, "run", _touchpad_busctl_run())
    assert sensors._touchpad_device_path() == "/org/kde/KWin/InputDevice/event6"


def test_touchpad_device_path_none_without_busctl(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    assert sensors._touchpad_device_path() is None


def test_touchpad_info_full_shape(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/busctl")
    monkeypatch.setattr(sensors, "_session_cmd", lambda a: a)
    monkeypatch.setattr(subprocess, "run", _touchpad_busctl_run(enabled=True))
    info = sensors.touchpad_info()
    assert info == {
        "available": True, "name": "DELL0A6E:00 04F3:317E Touchpad",
        "enabled": True, "tap_to_click": True, "natural_scroll": True,
        "disable_while_typing": True,
    }


def test_touchpad_info_unavailable_without_kwin(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    info = sensors.touchpad_info()
    assert info["available"] is False
    assert info["enabled"] is None


def test_set_touchpad_enabled_success(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/busctl")
    monkeypatch.setattr(sensors, "_session_cmd", lambda a: a)
    seen = {}

    def run(cmd, *_a, **_k):
        if cmd[:3] == ["busctl", "--user", "tree"]:
            return types.SimpleNamespace(stdout=_TOUCHPAD_TREE, stderr="", returncode=0)
        if "get-property" in cmd and cmd[-1] == "touchpad":
            path = cmd[-3]
            val = "true" if path == "/org/kde/KWin/InputDevice/event6" else "false"
            return types.SimpleNamespace(stdout=f"b {val}\n", stderr="", returncode=0)
        if "set-property" in cmd:
            seen["cmd"] = cmd
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    ok, msg = sensors.set_touchpad_enabled(False)
    assert ok is True
    assert seen["cmd"][-2:] == ["b", "false"]
    assert seen["cmd"][-3] == "enabled"


def test_set_touchpad_enabled_no_touchpad(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: None)
    ok, msg = sensors.set_touchpad_enabled(True)
    assert ok is False
    assert "KWin" in msg


def test_set_touchpad_enabled_busctl_failure(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda c: "/usr/bin/busctl")
    monkeypatch.setattr(sensors, "_session_cmd", lambda a: a)

    def run(cmd, *_a, **_k):
        if cmd[:3] == ["busctl", "--user", "tree"]:
            return types.SimpleNamespace(stdout=_TOUCHPAD_TREE, stderr="", returncode=0)
        if "get-property" in cmd and cmd[-1] == "touchpad":
            path = cmd[-3]
            val = "true" if path == "/org/kde/KWin/InputDevice/event6" else "false"
            return types.SimpleNamespace(stdout=f"b {val}\n", stderr="", returncode=0)
        if "set-property" in cmd:
            return types.SimpleNamespace(stdout="", stderr="Access denied", returncode=1)
        return types.SimpleNamespace(stdout="", stderr="", returncode=1)

    monkeypatch.setattr(subprocess, "run", run)
    ok, msg = sensors.set_touchpad_enabled(True)
    assert ok is False
    assert "Access denied" in msg
