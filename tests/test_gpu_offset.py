"""GPU clock-offset capability probe (Phase 3c) — parse + unavailable paths."""
import subprocess

import sensors


def test_unavailable_without_x_display(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda n: "/usr/bin/nvidia-settings")
    monkeypatch.delenv("DISPLAY", raising=False)
    info = sensors.nvidia_clock_offset_info()
    assert info["available"] is False
    assert "DISPLAY" in info["reason"]


def test_unavailable_without_binary(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda n: None)
    info = sensors.nvidia_clock_offset_info()
    assert info["available"] is False
    assert "not installed" in info["reason"]


def test_parses_offsets(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda n: "/usr/bin/nvidia-settings")
    monkeypatch.setenv("DISPLAY", ":0")
    blob = (
        "Attribute 'GPUGraphicsClockOffsetAllPerformanceLevels' (h:[gpu:0]): 150.\n"
        "  The valid values for 'GPUGraphicsClockOffsetAllPerformanceLevels' are in the range -200 - 1000 (inclusive).\n"
        "Attribute 'GPUMemoryTransferRateOffsetAllPerformanceLevels' (h:[gpu:0]): 400.\n"
    )
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, blob, ""))
    info = sensors.nvidia_clock_offset_info()
    assert info["available"] is True
    assert info["core"] == 150
    assert info["mem"] == 400
    assert info["core_range"] == (-200, 1000)


def test_error_output_marks_unavailable(monkeypatch):
    monkeypatch.setattr(sensors, "which", lambda n: "/usr/bin/nvidia-settings")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "ERROR: nope"))
    info = sensors.nvidia_clock_offset_info()
    assert info["available"] is False
