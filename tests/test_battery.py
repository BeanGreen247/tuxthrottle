"""Phase 3.1 — battery_health_info() reads the power-supply sysfs and derives
wear %. Model-agnostic; tested against a faked sysfs tree.
"""
import sensors


def _write(bat, **files):
    bat.mkdir(parents=True, exist_ok=True)
    for k, v in files.items():
        (bat / k).write_text(str(v))


def test_no_battery_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sensors.glob, "glob", lambda pat: [])
    assert sensors.battery_health_info() == {}


def test_energy_based_gauge_wear_and_rate(monkeypatch, tmp_path):
    bat = tmp_path / "BAT0"
    _write(bat,
           energy_full=42_300_000, energy_full_design=45_300_000,
           energy_now=21_150_000,
           cycle_count=193, capacity=99, status="Discharging",
           technology="Li-poly", manufacturer="SMP", model_name="01AV446",
           power_now=15_500_000, voltage_now=12_240_000)
    monkeypatch.setattr(sensors.glob, "glob",
                        lambda pat: [str(bat)] if "BAT" in pat else [])
    i = sensors.battery_health_info()
    assert i["present"] is True
    assert i["unit"] == "Wh"
    assert i["design"] == 45.3 and i["full"] == 42.3
    assert i["wear_pct"] == 6.6          # 1 - 42.3/45.3
    assert i["cycle_count"] == 193
    assert i["power_w"] == 15.5
    assert i["voltage_v"] == 12.24
    assert i["status"] == "Discharging"
    assert i["eta_kind"] == "to empty"
    assert i["eta_min"] == 82            # 21.15 Wh / 15.5 W -> ~1 h 22 m


def test_eta_to_full_when_charging(monkeypatch, tmp_path):
    bat = tmp_path / "BAT0"
    _write(bat,
           energy_full=40_000_000, energy_full_design=40_000_000,
           energy_now=30_000_000, status="Charging",
           power_now=20_000_000, voltage_now=12_000_000)
    monkeypatch.setattr(sensors.glob, "glob",
                        lambda pat: [str(bat)] if "BAT" in pat else [])
    i = sensors.battery_health_info()
    assert i["eta_kind"] == "to full"
    assert i["eta_min"] == 30            # (40-30) Wh / 20 W -> 0.5 h


def test_no_eta_when_full(monkeypatch, tmp_path):
    bat = tmp_path / "BAT0"
    _write(bat, energy_full=40_000_000, energy_full_design=40_000_000,
           energy_now=40_000_000, status="Full", power_now=0)
    monkeypatch.setattr(sensors.glob, "glob",
                        lambda pat: [str(bat)] if "BAT" in pat else [])
    i = sensors.battery_health_info()
    assert i["eta_min"] is None and i["eta_kind"] is None


def test_charge_based_gauge_falls_back_to_ah(monkeypatch, tmp_path):
    bat = tmp_path / "BAT1"
    _write(bat,
           charge_full=4_000_000, charge_full_design=5_000_000,
           current_now=2_000_000, voltage_now=11_100_000, capacity=50)
    monkeypatch.setattr(sensors.glob, "glob",
                        lambda pat: [str(bat)] if "BAT" in pat else [])
    i = sensors.battery_health_info()
    assert i["unit"] == "Ah"
    assert i["wear_pct"] == 20.0
    # power derived from current_now * voltage_now
    assert i["power_w"] == round(2_000_000 * 11_100_000 / 1_000_000 / 1_000_000, 1)


def test_missing_design_capacity_gives_null_wear(monkeypatch, tmp_path):
    bat = tmp_path / "BAT0"
    _write(bat, energy_full=40_000_000, capacity=80, status="Full")
    monkeypatch.setattr(sensors.glob, "glob",
                        lambda pat: [str(bat)] if "BAT" in pat else [])
    i = sensors.battery_health_info()
    assert i["wear_pct"] is None
    assert i["full"] == 40.0 and i["design"] is None
