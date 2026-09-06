"""New Fixes-tab backend pieces: fixlog, crashwatch signature matching,
launchopts remove-token, and steamperf's mount-wait wrapper preservation.
No Steam / systemd / network needed — pure logic + tmp_path isolation.
"""
import json

import tuxthrottle_crashwatch as cw
import tuxthrottle_fixlog as fixlog
import tuxthrottle_launchopts as lo
import tuxthrottle_steamperf as sp


# --------------------------------------------------------------------------- #
#  fixlog
# --------------------------------------------------------------------------- #
def test_fixlog_round_trip(monkeypatch, tmp_path):
    p = tmp_path / "fixlog.jsonl"
    monkeypatch.setattr(fixlog, "log_path", lambda user=None: p)
    fixlog.log_event("test", "hello", level="warn")
    fixlog.log_event("test", "world")
    entries = fixlog.read_recent(10)
    assert len(entries) == 2
    assert entries[0]["message"] == "world"        # newest first
    assert entries[1]["level"] == "warn"


def test_fixlog_caps_length(monkeypatch, tmp_path):
    p = tmp_path / "fixlog.jsonl"
    monkeypatch.setattr(fixlog, "log_path", lambda user=None: p)
    monkeypatch.setattr(fixlog, "MAX_ENTRIES", 5)
    for i in range(20):
        fixlog.log_event("test", f"event {i}")
    lines = p.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[-1])["message"] == "event 19"


# --------------------------------------------------------------------------- #
#  crashwatch signature matching
# --------------------------------------------------------------------------- #
def test_classify_benign_proton_bootstrap():
    sig = cw._classify("/path/wine-preloader", "wine c:\\...\\d3ddriverquery64.exe")
    assert sig is not None and sig["benign"] is True


def test_classify_steamwebhelper_crash_is_not_benign():
    sig = cw._classify("/path/steamwebhelper", "")
    assert sig is not None and sig["benign"] is False


def test_classify_unknown_exe_returns_none():
    assert cw._classify("/path/some-random-binary", "") is None


def test_coredump_events_parses_json_with_spaces_in_exe(monkeypatch):
    """Regression: the old whitespace-split text parser corrupted exactly
    this real-world row (space in the Proton path, no cmdline needed here)."""
    payload = json.dumps([
        {"time": 1788626723000000, "pid": 4593, "uid": 1000, "gid": 1001, "sig": 3,
         "corefile": "present",
         "exe": "/mnt/Voidstride/SteamLibrary/steamapps/common/Proton - Experimental"
                "/files/lib/wine/x86_64-unix/wine-preloader",
         "size": 43724},
    ])

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["coredumpctl", "list"]:
            return FakeProc(payload)
        return FakeProc("Command Line: wine steam.exe\n")

    monkeypatch.setattr(cw.subprocess, "run", fake_run)
    events = cw._coredump_events(60)
    assert len(events) == 1
    assert events[0]["exe"].endswith("wine-preloader")
    assert "Proton - Experimental" in events[0]["exe"]
    assert events[0]["pid"] == "4593"


def test_coredump_events_handles_null_exe(monkeypatch):
    """Regression: a row with no resolvable exe ('-' in the text table, null
    in JSON) must not crash and must not classify as blank-labeled junk."""
    payload = json.dumps([
        {"time": 1, "pid": 999, "uid": 0, "gid": 0, "sig": 6,
         "corefile": "none", "exe": None, "size": None},
    ])

    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = 0

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["coredumpctl", "list"]:
            return FakeProc(payload)
        return FakeProc("")

    monkeypatch.setattr(cw.subprocess, "run", fake_run)
    events = cw._coredump_events(60)
    assert events[0]["exe"] == ""


def test_scan_dedupes_across_calls(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(cw, "_state_path", lambda user=None: state)
    monkeypatch.setattr(cw, "_journal_events", lambda since: [])
    calls = {"n": 0}

    def fake_coredumps(since):
        calls["n"] += 1
        return [{"key": "core:123:t1", "timestamp": "t1", "pid": "123",
                "exe": "/x/wine-preloader", "cmdline": ""}]

    monkeypatch.setattr(cw, "_coredump_events", fake_coredumps)
    first = cw.scan(60)
    second = cw.scan(60)
    assert len(first) == 1
    assert len(second) == 0          # already-seen key is suppressed


def test_scan_labels_unclassified_empty_exe_readably(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    monkeypatch.setattr(cw, "_state_path", lambda user=None: state)
    monkeypatch.setattr(cw, "_journal_events", lambda since: [])
    monkeypatch.setattr(cw, "_coredump_events", lambda since: [
        {"key": "core:999:t1", "timestamp": "t1", "pid": "999", "exe": "", "cmdline": ""},
    ])
    findings = cw.scan(60)
    assert findings[0]["label"] == "An unidentified process crashed"
    assert "- crashed" not in findings[0]["label"]


# --------------------------------------------------------------------------- #
#  launchopts remove-token
# --------------------------------------------------------------------------- #
def test_remove_token_strips_only_matching_flag():
    cfg = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {
        "10": {"LaunchOptions": "gamemoderun mangohud %command%"},
        "20": {"LaunchOptions": "mangohud %command%"},
        "30": {"LaunchOptions": "%command%"},
    }}}}}}
    apps = lo._apps_dict(cfg)
    changed = 0
    for _aid, entry in lo._iter_games(apps):
        cur = lo._ci_get(entry, "LaunchOptions") or ""
        if "mangohud" not in cur:
            continue
        entry["LaunchOptions"] = " ".join(p for p in cur.split() if p != "mangohud")
        changed += 1
    assert changed == 2
    assert apps["10"]["LaunchOptions"] == "gamemoderun %command%"
    assert apps["20"]["LaunchOptions"] == "%command%"
    assert apps["30"]["LaunchOptions"] == "%command%"


# --------------------------------------------------------------------------- #
#  steamperf: mount-wait wrapper survives regeneration
# --------------------------------------------------------------------------- #
def test_reapply_mountwait_is_idempotent():
    wrapped = ("Exec=/usr/local/bin/tuxthrottle-wait-mounts systemd-run --user "
              "--scope -- /usr/bin/steam -silent %U\n")
    assert sp._reapply_mountwait(wrapped) == wrapped


def test_reapply_mountwait_wraps_primary_exec_only():
    text = "Exec=/usr/bin/steam %U\nExec=/usr/bin/steam steam://store\n"
    out = sp._reapply_mountwait(text)
    lines = out.splitlines()
    assert lines[0].startswith("Exec=/usr/local/bin/tuxthrottle-wait-mounts ")
    assert lines[1] == "Exec=/usr/bin/steam steam://store"


def test_diagnose_runs_without_steam_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "status_igpu", lambda: "off")
    monkeypatch.setattr(sp, "_SYS_DESKTOP", str(tmp_path / "nope.desktop"))
    monkeypatch.setattr(sp, "_user_desktop", lambda: tmp_path / "shadow.desktop")
    results = sp.diagnose(user=None)
    assert all(s in ("ok", "bad") for s, _ in results)


def test_flags_no_longer_suppress_steam_self_repair():
    # -noverifyfiles / -nobootstrapupdate / -norepairfiles were dropped after
    # they caused a steamclient.so SIGSEGV when a Steam client update landed.
    for f in ("-noverifyfiles", "-nobootstrapupdate", "-norepairfiles"):
        assert f not in sp.FLAGS
    assert "-cef-disable-gpu" in sp.FLAGS   # the real low-resource lever stays


def test_diagnose_flags_a_shadow_still_carrying_the_repair_suppression(monkeypatch, tmp_path):
    monkeypatch.setattr(sp, "status_igpu", lambda: "on")
    monkeypatch.setattr(sp, "_SYS_DESKTOP", str(tmp_path / "nope.desktop"))
    shadow = tmp_path / "shadow.desktop"
    shadow.write_text("[Desktop Entry]\nExec=/usr/bin/steam -silent -noverifyfiles %U\n")
    monkeypatch.setattr(sp, "_user_desktop", lambda: shadow)
    bad = [m for s, m in sp.diagnose(user=None) if s == "bad"]
    assert any("noverifyfiles" in m for m in bad)


# --------------------------------------------------------------------------- #
#  steamperf: the autostart backup must not sit in ~/.config/autostart —
#  Plasma's autostart scanner launches it as a 2nd Steam that fights the
#  singleton lock, leaving one steamwebhelper stuck respawning.
# --------------------------------------------------------------------------- #
def _wire_autostart(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "cfg" / "autostart").mkdir(parents=True)


def test_autostart_backup_lands_outside_the_autostart_dir(monkeypatch, tmp_path):
    _wire_autostart(monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "status_igpu", lambda: "off")
    monkeypatch.setattr(sp, "_apply_client_settings", lambda _on: ["(skipped)"])
    monkeypatch.setattr(sp, "_base_desktop_body",
                        lambda: "[Desktop Entry]\nExec=/usr/bin/steam %U\n")
    au = sp._autostart()
    au.write_text("[Desktop Entry]\nExec=/usr/bin/steam -bigpicture %U\n")  # user's own
    sp.enable(autostart=True)
    strays = [f.name for f in au.parent.iterdir() if f.name != au.name]
    assert strays == [], f"stray file left in autostart dir: {strays}"
    assert sp._autostart_bak("tuxthrottle-bak").is_file()


def test_migrate_moves_a_legacy_stray_bak_out(monkeypatch, tmp_path):
    _wire_autostart(monkeypatch, tmp_path)
    au = sp._autostart()
    legacy = au.with_name(au.name + ".tuxthrottle-bak")
    legacy.write_text("[Desktop Entry]\nExec=/usr/bin/steam %U\n")
    sp._migrate_stray_bak()
    assert not legacy.exists()
    assert sp._autostart_bak("tuxthrottle-bak").read_text().startswith("[Desktop Entry]")


def test_diagnose_flags_a_stray_steam_autostart_file(monkeypatch, tmp_path):
    _wire_autostart(monkeypatch, tmp_path)
    monkeypatch.setattr(sp, "status_igpu", lambda: "on")
    monkeypatch.setattr(sp, "_SYS_DESKTOP", str(tmp_path / "nope.desktop"))
    monkeypatch.setattr(sp, "_user_desktop", lambda: tmp_path / "shadow.desktop")
    sp._autostart().with_name("steam.desktop.tuxthrottle-bak").write_text("x")
    bad = [m for s, m in sp.diagnose(user=None) if s == "bad"]
    assert any("autostart" in m.lower() for m in bad)
