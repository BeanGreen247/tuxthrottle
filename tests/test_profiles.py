import json

import tuxthrottle_profiles as tp


class FakeSensors:
    """Minimal stand-in for the sensors module used by the profile engine."""
    def __init__(self):
        self.profile = "balanced"
        self.tdp = {"stapm": 54, "fast": 65, "slow": 54}
        self.battery = 80
        self.gpu = "hybrid"
        self.applied = []

    # capture side
    def get_platform_profile(self): return self.profile
    def read_ryzenadj_info(self):
        return {"stapm_limit": self.tdp["stapm"], "fast_limit": self.tdp["fast"],
                "slow_limit": self.tdp["slow"]}
    def battery_charge_limit_info(self): return {"current": self.battery}
    def nvidia_power_limit_info(self): return {"supported": False}
    def gpu_mode_get(self): return self.gpu

    # apply side
    def set_platform_profile(self, v): self.applied.append(("profile", v)); self.profile = v; return True, ""
    def set_ryzenadj_limits(self, fast_w=None, slow_w=None, stapm_w=None):
        self.applied.append(("tdp", (stapm_w, fast_w, slow_w))); return True, ""
    def set_battery_charge_limit(self, p): self.applied.append(("battery", p)); self.battery = p; return True, ""
    def set_nvidia_power_limit(self, w): return False, "locked"
    def gpu_mode_set(self, m): self.applied.append(("gpu", m)); return True, ""


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(tp, "_config_dir", lambda user=None: tmp_path)
    fake = FakeSensors()
    monkeypatch.setattr(tp, "sensors", fake)
    return fake


def test_capture_roundtrips_through_apply(monkeypatch, tmp_path):
    fake = _isolate(monkeypatch, tmp_path)
    state = tp.capture_state()
    assert state["platform_profile"] == "balanced"
    assert state["tdp"] == {"stapm": 54, "fast": 65, "slow": 54}
    assert state["battery"] == {"percent": 80}
    assert state["gpu_mode"] == "hybrid"

    # change the hardware, then re-apply the captured state
    fake.profile, fake.battery = "performance", 100
    rows = tp.apply_state(state)
    assert all(r["ok"] for r in rows if r["key"] != "nvpl")
    assert fake.profile == "balanced"
    assert fake.battery == 80
    # gpu mode is NOT applied unless asked
    assert ("gpu", "hybrid") not in fake.applied


def test_apply_with_gpu_mode_flag(monkeypatch, tmp_path):
    fake = _isolate(monkeypatch, tmp_path)
    fake.gpu = "nvidia"
    tp.apply_state({"gpu_mode": "integrated"}, with_gpu_mode=True)
    assert ("gpu", "integrated") in fake.applied


def test_apply_records_active_state(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    tp.apply_state({"platform_profile": "performance"})
    active = json.loads((tmp_path / "active_state.json").read_text())
    assert active["platform_profile"] == "performance"
    assert "applied" in active


def test_reassert_reapplies_active_state(monkeypatch, tmp_path):
    fake = _isolate(monkeypatch, tmp_path)
    tp.apply_state({"platform_profile": "performance"})
    fake.profile = "balanced"
    tp.reassert()
    assert fake.profile == "performance"


def test_reassert_noop_without_active_state(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    rows = tp.reassert()
    assert rows == [{"key": "-", "ok": True, "msg": "nothing to reassert"}]


def test_profile_save_load_list_delete(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    tp.save_profile("Gaming Rig", {"platform_profile": "performance"})
    assert "Gaming Rig" in tp.list_profiles()
    assert tp.load_profile("Gaming Rig")["platform_profile"] == "performance"
    assert tp.delete_profile("Gaming Rig") is True
    assert tp.list_profiles() == []


def test_snapshot_prunes_to_keep(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(tp, "KEEP_SNAPSHOTS", 3)
    import time
    for i in range(6):
        tp.snapshot(label=f"s{i}")
        time.sleep(0.01)  # distinct mtimes / names
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 3


def test_rollback_last_applies_and_snapshots_current(monkeypatch, tmp_path):
    fake = _isolate(monkeypatch, tmp_path)
    fake.profile = "performance"
    tp.snapshot(label="before")           # captures profile=performance
    fake.profile = "balanced"
    rows = tp.rollback("last")
    assert all(r["ok"] for r in rows)
    assert fake.profile == "performance"
    # rollback also dropped a 'pre-rollback' snapshot
    labels = [json.loads(p.read_text()).get("label")
              for p in (tmp_path / "snapshots").glob("*.json")]
    assert "pre-rollback" in labels


def test_safe_name_strips_junk():
    assert tp._safe_name("../etc/passwd") == "etcpasswd"
    assert tp._safe_name("  ok name 1 ") == "ok name 1"
    assert tp._safe_name("///") == "unnamed"
