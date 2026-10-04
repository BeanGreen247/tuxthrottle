"""tuxthrottle_mangohud_games.py - launch-options surgery + per-game conf."""
import tuxthrottle_mangohud_games as mhg


def test_enable_inserts_wrapper_before_command():
    assert mhg.transform("gamemoderun %command%", True) == "gamemoderun mangohud %command%"
    assert mhg.transform("", True) == "mangohud %command%"


def test_enable_is_idempotent_and_drops_force_off():
    once = mhg.transform("MANGOHUD=0 A=1 gamemoderun %command%", True)
    assert once == "A=1 gamemoderun mangohud %command%"
    assert mhg.transform(once, True) == once


def test_disable_keeps_everything_else():
    opts = 'P="/a b/c" MANGOHUD_CONFIGFILE=/x/y.conf gamemoderun mangohud %command% -w'
    assert mhg.transform(opts, False) == 'P="/a b/c" gamemoderun %command% -w'
    # MANGOHUD_DLSYM is not the wrapper
    assert mhg.transform("MANGOHUD_DLSYM=1 %command%", False) == "MANGOHUD_DLSYM=1 %command%"


def test_conffile_goes_after_gamescope_separator():
    got = mhg.transform("gamescope -f -- env A=1 gamemoderun %command%", True, "/c/g.conf")
    assert got == ("gamescope -f -- env MANGOHUD_CONFIGFILE=/c/g.conf A=1 "
                   "gamemoderun mangohud %command%")
    got = mhg.transform("gamescope -f -- %command%", True, "/c/g.conf")
    assert got == "gamescope -f -- env MANGOHUD_CONFIGFILE=/c/g.conf mangohud %command%"
    assert mhg.transform("A=1 %command%", True, "/c/g.conf") == \
        "MANGOHUD_CONFIGFILE=/c/g.conf A=1 mangohud %command%"


def test_state_reads_wrapper_and_conf():
    assert mhg.state("gamemoderun %command%") == {"enabled": False, "conf": ""}
    st = mhg.state("MANGOHUD_CONFIGFILE=/c/g.conf mangohud %command%")
    assert st == {"enabled": True, "conf": "/c/g.conf"}


def test_render_conf_overrides_owned_keys_only():
    base = "# c\nposition=top-left\noffset_x=5\nfont_size=20\ncpu_text=R7\nfps\n"
    out = mhg.render_conf("10", {"position": "bottom-right", "font_size": "28",
                                 "fps_limit": "", "detail": "fps_only"}, base)
    lines = out.splitlines()
    assert "cpu_text=R7" in lines and "fps" in lines
    assert "position=bottom-right" in lines and "position=top-left" not in lines
    assert "offset_x=5" not in lines
    assert "font_size=28" in lines and "font_size=20" not in lines
    assert lines[-1] == "fps_only"
    # no own position → the global anchor + offsets carry over
    keep = mhg.render_conf("10", {"detail": "global"}, base).splitlines()
    assert "position=top-left" in keep and "offset_x=5" in keep


def test_settings_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(mhg, "conf_dir", lambda: tmp_path)
    (tmp_path / "MangoHud.conf").write_text("cpu_text=R7\nfps\n")
    mhg.write_conf("42", {"position": "top-right", "detail": "full",
                          "font_size": "24", "fps_limit": "60"})
    assert mhg.read_settings("42") == {"position": "top-right", "detail": "full",
                                       "font_size": "24", "fps_limit": "60"}
    assert mhg.read_settings("43")["detail"] == "global"


def test_set_game_rewrites_only_that_game(monkeypatch, tmp_path):
    lc = tmp_path / "localconfig.vdf"
    lc.write_text("x")
    cfg = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {
        "10": {"LaunchOptions": "gamemoderun %command%"},
        "20": {"LaunchOptions": "gamemoderun mangohud %command%"},
    }}}}}}
    monkeypatch.setattr(mhg.lo, "steam_running", lambda: False)
    monkeypatch.setattr(mhg.lo, "find_localconfigs", lambda: [lc])
    monkeypatch.setattr(mhg.lo, "_load", lambda p: cfg)
    monkeypatch.setattr(mhg.lo, "_dump", lambda obj, p: None)
    monkeypatch.setattr(mhg, "gate_installed", lambda: False)
    apps = mhg.lo._apps_dict(cfg)
    assert mhg.set_game("10", True) == 0
    assert apps["10"]["LaunchOptions"] == "gamemoderun mangohud %command%"
    assert mhg.disable_all() == 0
    assert apps["10"]["LaunchOptions"] == "gamemoderun %command%"
    assert apps["20"]["LaunchOptions"] == "gamemoderun %command%"


def test_refuses_while_steam_runs(monkeypatch):
    monkeypatch.setattr(mhg, "gate_installed", lambda: False)
    monkeypatch.setattr(mhg.lo, "steam_running", lambda: True)
    assert mhg.set_game("10", True) == 2


def _gated(monkeypatch, tmp_path, opts):
    monkeypatch.setattr(mhg, "gate_installed", lambda: True)
    monkeypatch.setattr(mhg, "flags_dir", lambda: tmp_path / "flags")
    monkeypatch.setattr(mhg, "conf_dir", lambda: tmp_path / "mh")
    monkeypatch.setattr(mhg, "_launch_options", lambda: opts)
    monkeypatch.setattr(mhg.lo, "steam_running", lambda: True)
    monkeypatch.setattr(mhg, "_anticheat_map", lambda ids: {})
    monkeypatch.setattr(mhg, "_force_file", lambda: tmp_path / "force.json")


def test_gate_toggles_live_while_steam_runs(monkeypatch, tmp_path):
    _gated(monkeypatch, tmp_path, {"10": "gamemoderun mangohud %command%"})
    assert mhg.set_game("10", True, {"position": "top-right", "detail": "fps_only"}) == 0
    assert mhg.flag_on("10") and mhg.conf_path("10").is_file()
    assert mhg.set_game("10", True) == 0          # global layout → own conf dropped
    assert not mhg.conf_path("10").exists()
    assert mhg.set_game("10", False) == 0
    assert not mhg.flag_on("10")


def test_gate_needs_steam_closed_only_to_add_the_hook(monkeypatch, tmp_path):
    _gated(monkeypatch, tmp_path, {"10": "gamemoderun %command%"})
    assert mhg.set_game("10", True) == 2
    assert not mhg.flag_on("10")


def test_gate_disable_all_clears_flags(monkeypatch, tmp_path):
    _gated(monkeypatch, tmp_path, {})
    mhg.set_flag("10", True)
    mhg.set_flag("20", True)
    assert mhg.disable_all() == 0
    assert not mhg.flag_on("10") and not mhg.flag_on("20")


def test_gate_script_is_marked_and_passes_through_outside_steam():
    assert mhg.GATE_MARK in mhg.GATE_SCRIPT
    assert 'exec "$real" "$@"' in mhg.GATE_SCRIPT and 'exec "$@"' in mhg.GATE_SCRIPT


def test_apply_plan_batches_live_and_defers_only_missing_hooks(monkeypatch, tmp_path):
    _gated(monkeypatch, tmp_path, {"10": "gamemoderun mangohud %command%",
                                   "20": "gamemoderun %command%",
                                   "30": "mangohud %command%"})
    monkeypatch.setattr(mhg, "installed_games", lambda: {"20": "Game Twenty"})
    mhg.set_flag("30", True)
    rc = mhg.apply_plan({"on": {"10": {"own": True, "position": "top-right",
                                       "detail": "global"}, "20": None},
                         "off": ["30"]})
    assert rc == 3                                   # 20 still needs its hook
    assert mhg.flag_on("10") and mhg.flag_on("20") and not mhg.flag_on("30")
    assert mhg.conf_path("10").is_file()


def test_apply_plan_adds_all_hooks_in_one_write(monkeypatch, tmp_path):
    opts = {"10": "gamemoderun %command%", "20": "%command%"}
    _gated(monkeypatch, tmp_path, opts)
    lc = tmp_path / "localconfig.vdf"
    lc.write_text("x")
    cfg = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {
        a: {"LaunchOptions": o} for a, o in opts.items()}}}}}}
    writes = []
    monkeypatch.setattr(mhg.lo, "steam_running", lambda: False)
    monkeypatch.setattr(mhg.lo, "find_localconfigs", lambda: [lc])
    monkeypatch.setattr(mhg.lo, "_load", lambda p: cfg)
    monkeypatch.setattr(mhg.lo, "_dump", lambda obj, p: writes.append(1))
    assert mhg.apply_plan({"on": {"10": None, "20": None}, "off": []}) == 0
    assert len(writes) == 1
    apps = mhg.lo._apps_dict(cfg)
    assert apps["10"]["LaunchOptions"] == "gamemoderun mangohud %command%"
    assert apps["20"]["LaunchOptions"] == "mangohud %command%"


def test_hook_all_needs_the_gate(monkeypatch):
    monkeypatch.setattr(mhg, "gate_installed", lambda: False)
    assert mhg.hook_all() == 1


def test_hook_all_is_idempotent_per_game():
    h = mhg._HookAll(["10"])
    assert h("10", "gamemoderun %command%") == "gamemoderun mangohud %command%"
    assert h("10", "MANGOHUD_DLSYM=1 mangohud %command%") == "MANGOHUD_DLSYM=1 mangohud %command%"


def test_anticheat_blocks_unless_forced(monkeypatch, tmp_path):
    _gated(monkeypatch, tmp_path, {"10": "mangohud %command%", "20": "mangohud %command%"})
    monkeypatch.setattr(mhg, "_anticheat_map",
                        lambda ids: {"steam:10": "BattlEye", "steam:20": "EasyAntiCheat"})
    assert mhg.apply_plan({"on": {"10": None, "20": None}, "off": [], "force": ["20"]}) == 0
    assert not mhg.flag_on("10") and mhg.flag_on("20")
    assert mhg.forced() == {"steam:20"}
    # the force is remembered, and dropped again when the game is switched off
    assert mhg.apply_plan({"on": {"20": None}, "off": []}) == 0 and mhg.flag_on("20")
    assert mhg.apply_plan({"on": {}, "off": ["20"]}) == 0
    assert mhg.forced() == set()


def test_other_launcher_ids_go_to_the_launcher(monkeypatch, tmp_path):
    _gated(monkeypatch, tmp_path, {})
    calls = []
    monkeypatch.setattr(mhg.launchers, "set_enabled", lambda g, on: calls.append((g, on)) or True)
    assert mhg.apply_plan({"on": {"heroic:Fortnite": None}, "off": ["lutris:7"]}) == 0
    assert calls == [("lutris:7", False), ("heroic:Fortnite", True)]


def test_profile_is_parked_not_deleted_and_keeps_editor_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(mhg, "conf_dir", lambda: tmp_path)
    (tmp_path / "MangoHud.conf").write_text("position=middle-left\nfont_size=20\ncpu_text=R7\nfps\n")
    assert mhg.profile_state("7") == "none"
    p = mhg.ensure_profile("7", {"font_size": "", "fps_limit": "60"})
    assert mhg.profile_state("7") == "on"
    assert "cpu_text=R7" in p.read_text() and "fps_limit=60" in p.read_text()
    # the overlay editor then rewrites the layout - a later apply must not undo it
    p.write_text("position=top-right\ngpu_stats\nfps_limit=60\n")
    mhg.ensure_profile("7", {"font_size": "28", "fps_limit": "60"})
    assert "position=top-right" in p.read_text() and "font_size=28" in p.read_text()
    mhg.disable_profile("7")
    assert mhg.profile_state("7") == "off" and not p.exists()
    mhg.ensure_profile("7")                                  # comes back as it was
    assert "position=top-right" in mhg.conf_path("7").read_text()
    mhg.delete_profile("7")
    assert mhg.profile_state("7") == "none"


def test_transform_dgpu_adds_and_removes_only_the_prime_vars():
    nv = mhg.dgpu_vars(True)
    assert mhg.transform_dgpu("gamemoderun %command%", True, True) == f"{nv} gamemoderun %command%"
    assert mhg.transform_dgpu("", True, False) == "DRI_PRIME=1 %command%"
    once = mhg.transform_dgpu(f"{nv} A=1 gamemoderun %command%", True, True)
    assert once == f"{nv} A=1 gamemoderun %command%"                 # idempotent
    assert mhg.transform_dgpu(once, False) == "A=1 gamemoderun %command%"
    assert mhg.transform_dgpu(f"{nv} %command%", False) == ""
    gs = mhg.transform_dgpu("gamescope -f -- gamemoderun %command%", True, True)
    assert gs == f"gamescope -f -- env {nv} gamemoderun %command%"
    assert mhg.transform_dgpu(gs, False) == "gamescope -f -- env gamemoderun %command%"
    assert mhg.has_dgpu(gs) and not mhg.has_dgpu("MANGOHUD=0 %command%")


def test_dgpu_is_live_with_the_hook_and_edits_options_without_it(monkeypatch, tmp_path):
    opts = {"10": "gamemoderun mangohud %command%", "20": "gamemoderun %command%",
            "30": f"{mhg.dgpu_vars(True)} %command%"}
    _gated(monkeypatch, tmp_path, opts)
    monkeypatch.setattr(mhg, "has_nvidia", lambda: True)
    monkeypatch.setattr(mhg, "installed_games", lambda: {"20": "Twenty", "30": "Thirty"})
    rc = mhg.apply_plan({"on": {}, "off": [], "dgpu_on": ["10", "20"], "dgpu_off": ["30"]})
    assert rc == 3                                   # 20 and 30 need the Steam-closed edit
    assert mhg.dgpu_state("10", opts["10"], True) == {"wanted": True, "active": True}
    assert mhg.dgpu_state("20", opts["20"], True) == {"wanted": True, "active": False}
    assert mhg.dgpu_state("30", opts["30"], True) == {"wanted": False, "active": True}
    # Steam closed: one write does both, and the pending markers clear
    lc = tmp_path / "localconfig.vdf"
    lc.write_text("x")
    cfg = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {
        a: {"LaunchOptions": o} for a, o in opts.items()}}}}}}
    writes = []
    monkeypatch.setattr(mhg.lo, "steam_running", lambda: False)
    monkeypatch.setattr(mhg.lo, "find_localconfigs", lambda: [lc])
    monkeypatch.setattr(mhg.lo, "_load", lambda p: cfg)
    monkeypatch.setattr(mhg.lo, "_dump", lambda obj, p: writes.append(1))
    assert mhg.apply_plan({"on": {}, "off": []}) == 0 and len(writes) == 1
    apps = mhg.lo._apps_dict(cfg)
    assert apps["10"]["LaunchOptions"] == "gamemoderun mangohud %command%"      # untouched
    assert mhg.has_dgpu(apps["20"]["LaunchOptions"])
    assert apps["30"]["LaunchOptions"] == ""
    assert mhg._marked("nodgpu") == set()


def test_gate_script_carries_the_dgpu_switch_and_version_check(monkeypatch, tmp_path):
    assert "$appid.dgpu" in mhg.GATE_SCRIPT and "__NV_PRIME_RENDER_OFFLOAD=1" in mhg.GATE_SCRIPT
    gate = tmp_path / "mangohud"
    monkeypatch.setattr(mhg, "GATE_PATH", gate)
    assert not mhg.gate_current()
    gate.write_text("#!/bin/sh\n# tuxthrottle-mangohud-gate old\n")
    assert mhg.gate_installed() and not mhg.gate_current()
    gate.write_text(mhg.GATE_SCRIPT)
    assert mhg.gate_current()
