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
