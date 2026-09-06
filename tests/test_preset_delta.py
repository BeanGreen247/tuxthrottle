"""Preset before/after sensor deltas (Phase 3a).

`_fmt_snapshot_delta` is a pure staticmethod on ToolkitApp — pulled out here
without a Tk root by grabbing it off the class via the mixin chain would still
import ttkbootstrap, so this reimports the function's logic path guarded.
"""
import pytest

pytest.importorskip("ttkbootstrap", exc_type=ImportError)

import tuxthrottle as tt  # noqa: E402

fmt = tt.ToolkitApp._fmt_snapshot_delta


def test_reports_only_fields_that_moved():
    before = {"cpu_temp_c": 60, "cpu_freq_ghz": 3.0, "stapm_w": 65,
              "dgpu_temp_c": 50, "dgpu_clock_mhz": 300, "dgpu_power_w": 8,
              "fan_rpm": [2000, 2100]}
    after = {"cpu_temp_c": 72, "cpu_freq_ghz": 3.0, "stapm_w": 80,
             "dgpu_temp_c": 50, "dgpu_clock_mhz": 300, "dgpu_power_w": 8,
             "fan_rpm": [3500, 3600]}
    lines = fmt(before, after)
    joined = " ".join(lines)
    assert "CPU temp 60" in joined and "+12" in joined
    assert "STAPM 65" in joined and "+15" in joined
    assert "CPU clock" not in joined      # unchanged, omitted
    assert "dGPU temp" not in joined
    assert any("fan" in ln for ln in lines)


def test_no_change_is_stated():
    s = {"cpu_temp_c": 55, "cpu_freq_ghz": 2.5, "stapm_w": 65,
         "dgpu_temp_c": 45, "dgpu_clock_mhz": 210, "dgpu_power_w": 7,
         "fan_rpm": [1800, 1800]}
    assert fmt(s, dict(s)) == ["no significant sensor change"]


def test_idle_jitter_below_deadband_is_ignored():
    before = {"cpu_temp_c": 55, "cpu_freq_ghz": 2.50, "stapm_w": 65,
              "dgpu_temp_c": 45, "dgpu_clock_mhz": 300, "dgpu_power_w": 7,
              "fan_rpm": [1800, 1800]}
    after = {"cpu_temp_c": 57, "cpu_freq_ghz": 2.60, "stapm_w": 66,
             "dgpu_temp_c": 47, "dgpu_clock_mhz": 645, "dgpu_power_w": 9,
             "fan_rpm": [1830, 1850]}   # dGPU clock +345 = ordinary boost jitter
    # small temp/clock/power wobble under each deadband; dGPU clock not reported
    assert fmt(before, after) == ["no significant sensor change"]


def test_missing_fields_skipped():
    before = {"cpu_temp_c": None, "stapm_w": 65}
    after = {"cpu_temp_c": 70, "stapm_w": None}
    assert fmt(before, after) == ["no significant sensor change"]


def test_snapshot_light_shape():
    import sensors
    snap = sensors.snapshot_light()
    assert set(snap) >= {"cpu_temp_c", "cpu_freq_ghz", "stapm_w",
                         "dgpu_temp_c", "dgpu_clock_mhz", "dgpu_power_w", "fan_rpm"}
    assert isinstance(snap["fan_rpm"], list)
