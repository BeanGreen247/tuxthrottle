"""tuxthrottle_compat_tools.py - asset choice, safe extraction, install/remove,
moving a Steam tool to the OS drive. No network."""
import hashlib
import io
import tarfile

import tuxthrottle_compat_tools as ct


def _assets(*names):
    return [{"name": n, "browser_download_url": f"https://x/{n}", "size": 1} for n in names]


def test_pick_assets_ge_proton_with_checksum():
    p = ct.pick_assets("GE-Proton", _assets("GE-Proton11-7.sha512sum", "GE-Proton11-7.tar.gz",
                                           "GE-Proton11-7-aarch64.tar.gz"))
    assert p["asset"] == "GE-Proton11-7.tar.gz"
    assert p["checksum_url"].endswith("GE-Proton11-7.sha512sum")


def test_pick_assets_cachyos_follows_cpu_level():
    a = _assets("proton-cachyos-11-x86_64.tar.xz", "proton-cachyos-11-x86_64_v3.tar.xz",
                "proton-cachyos-11-x86_64.sha512sum", "proton-cachyos-11-x86_64_v3.sha512sum")
    assert ct.pick_assets("Proton-CachyOS", a, arch="x86_64_v3")["asset"].endswith("_v3.tar.xz")
    p = ct.pick_assets("Proton-CachyOS", a, arch="x86_64")
    assert p["asset"] == "proton-cachyos-11-x86_64.tar.xz"
    assert p["checksum_url"].endswith("x86_64.sha512sum")
    assert ct.pick_assets("GE-Proton", _assets("notes.txt")) is None


def _tar(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))


def test_extract_refuses_path_escape(tmp_path):
    good = tmp_path / "good.tar.gz"
    _tar(good, [("Tool-1/proton", b"x"), ("Tool-1/files/a", b"y")])
    out = tmp_path / "out"
    out.mkdir()
    assert ct.extract(good, out) == "Tool-1"
    assert (out / "Tool-1" / "files" / "a").read_bytes() == b"y"
    two = tmp_path / "two.tar.gz"
    _tar(two, [("A/x", b"1"), ("B/y", b"2")])
    try:
        ct.extract(two, out)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def _fake_release(monkeypatch, tmp_path, checksum_ok=True):
    arc = tmp_path / "GE-Proton1-1.tar.gz"
    _tar(arc, [("GE-Proton1-1/proton", b"#!/bin/sh\n")])
    blob = arc.read_bytes()
    digest = hashlib.sha512(blob).hexdigest() if checksum_ok else "0" * 128
    tgt = {"name": "Steam", "launcher": "steam", "dir": str(tmp_path / "ctd"), "present": True,
           "dirs": {"proton": str(tmp_path / "ctd")}}
    monkeypatch.setattr(ct, "target_by_name", lambda n: tgt)
    monkeypatch.setattr(ct, "releases", lambda tool, count=15: [
        {"tag": "GE-Proton1-1", "asset": arc.name, "url": "u", "size": len(blob),
         "checksum_url": "https://x/sum", "date": ""}])
    monkeypatch.setattr(ct, "_download", lambda url, dest, size=0: dest.write_bytes(blob))

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(ct.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(f"{digest}  {arc.name}\n".encode()))
    return tmp_path / "ctd"


def test_install_verifies_and_unpacks_then_removes(monkeypatch, tmp_path):
    ctd = _fake_release(monkeypatch, tmp_path)
    assert ct.install("GE-Proton") == 0
    assert (ctd / "GE-Proton1-1" / "proton").is_file()
    assert not (ctd / ".tuxthrottle-download").exists()
    assert ct.installed(ctd) == ["GE-Proton1-1"]
    assert ct.install("GE-Proton") == 0                   # already there → no-op
    assert ct.remove("Steam", "../x") == 1
    assert ct.remove("Steam", "GE-Proton1-1") == 0 and ct.installed(ctd) == []


def test_install_aborts_on_checksum_mismatch(monkeypatch, tmp_path):
    ctd = _fake_release(monkeypatch, tmp_path, checksum_ok=False)
    assert ct.install("GE-Proton") == 1
    assert ct.installed(ctd) == []


def test_move_to_os_copies_then_removes(monkeypatch, tmp_path):
    root, other = tmp_path / "root", tmp_path / "other"
    (root / "steamapps").mkdir(parents=True)
    src = other / "steamapps" / "common" / "Proton X"
    src.mkdir(parents=True)
    (src / "proton").write_text("p")
    acf = other / "steamapps" / "appmanifest_5.acf"
    acf.write_text('"appid" "5"')
    man = {"5": {"name": "Proton X", "lib": str(other), "installdir": "Proton X",
                 "size": 1, "acf": str(acf)}}
    monkeypatch.setattr(ct, "steam_root", lambda: root)
    monkeypatch.setattr(ct, "_manifests", lambda: man)
    monkeypatch.setattr(ct.lo, "steam_running", lambda: True)
    assert ct.move_to_os("5") == 2 and src.is_dir()       # refuses while Steam runs
    monkeypatch.setattr(ct.lo, "steam_running", lambda: False)
    assert ct.move_to_os("5", dry=True) == 0 and src.is_dir()
    assert ct.move_to_os("5") == 0
    assert (root / "steamapps" / "common" / "Proton X" / "proton").read_text() == "p"
    assert (root / "steamapps" / "appmanifest_5.acf").is_file()
    assert not src.exists() and not acf.exists()


def test_kinds_route_to_the_launchers_own_folders(monkeypatch, tmp_path):
    monkeypatch.setattr(ct.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ct, "steam_root", lambda: None)
    t = {x["name"]: x for x in ct.targets()}
    assert t["Lutris"]["dirs"]["dxvk"].endswith(".local/share/lutris/runtime/dxvk")
    assert t["Lutris"]["dirs"]["wine"].endswith("lutris/runners/wine")
    assert t["Heroic"]["dirs"]["vkd3d"].endswith("heroic/tools/vkd3d")
    assert t["Bottles"]["dirs"]["dxvk"].endswith("bottles/dxvk")
    assert ct.kind_of("DXVK") == "dxvk" and ct.kind_of("GE-Proton") == "proton"
    assert "steam" not in ct.TOOLS["DXVK"]["launchers"]


def test_check_build_spots_incomplete_unpacks(tmp_path):
    good = tmp_path / "compatibilitytools.d" / "GE-Proton1"
    (good / "files").mkdir(parents=True)
    (good / "proton").write_text("#!/bin/sh\n")
    (good / "proton").chmod(0o755)
    (good / "toolmanifest.vdf").write_text("x")
    (good / "compatibilitytool.vdf").write_text("x")
    assert ct.check_build(good, "proton") == []
    (good / "proton").chmod(0o644)
    assert any("not executable" in p for p in ct.check_build(good, "proton"))
    dx = tmp_path / "dxvk-2"
    dx.mkdir()
    assert ct.check_build(dx, "dxvk") == ["x64/d3d11.dll missing"]
    (dx / "x64").mkdir()
    (dx / "x64" / "d3d11.dll").write_text("")
    assert ct.check_build(dx, "dxvk") == []


def test_check_steam_tool_reads_state_and_layout(tmp_path):
    lib = tmp_path / "lib"
    d = lib / "steamapps" / "common" / "Proton 9.0"
    d.mkdir(parents=True)
    acf = lib / "steamapps" / "appmanifest_1.acf"
    acf.write_text('"StateFlags"\t\t"1026"\n')
    m = {"name": "Proton 9.0", "lib": str(lib), "installdir": "Proton 9.0", "acf": str(acf)}
    probs = ct.check_steam_tool(m)
    assert any("StateFlags 1026" in p for p in probs) and "install folder is empty" in probs
    acf.write_text('"StateFlags"\t\t"4"\n')
    (d / "proton").write_text("")
    (d / "proton").chmod(0o755)
    (d / "toolmanifest.vdf").write_text("")
    assert ct.check_steam_tool(m) == []


def test_session_env_recovers_display_from_the_user_manager(monkeypatch):
    class _R:
        stdout = "DISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\nXAUTHORITY=$'/run/user/1000/x y'\nFOO=1\n"
    monkeypatch.setattr(ct.subprocess, "run", lambda *a, **k: _R())
    env = ct.session_env({"HOME": "/home/u", "XDG_RUNTIME_DIR": "/nonexistent"})
    assert env["DISPLAY"] == ":0" and env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["XAUTHORITY"] == "/run/user/1000/x y" and "FOO" not in env
    # an environment that already has a display is left alone
    monkeypatch.setattr(ct.subprocess, "run", lambda *a, **k: 1 / 0)
    full = {"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
    assert ct.session_env(full)["DISPLAY"] == ":1"


def test_steam_url_refuses_without_a_session(monkeypatch):
    monkeypatch.setattr(ct.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(ct, "session_env", lambda: {"HOME": "/h"})
    started = []
    monkeypatch.setattr(ct.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert ct.steam_install("5") == 1 and not started
    monkeypatch.setattr(ct, "session_env", lambda: {"DISPLAY": ":0",
                                                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b"})
    assert ct.steam_install("5") == 0
    assert started[0][0] == ["systemd-run", "--user", "--collect", "--quiet", "--",
                             "steam", "steam://install/5"]


def _appinfo(tmp_path, apps):
    """Minimal v29 appinfo.vdf: {appid: installdir}."""
    import struct
    names = [b"appinfo", b"installdir"]
    body = b""
    for aid, d in apps.items():
        blob = b"\x00" * 60 + b"\x01" + struct.pack("<I", 1) + d.encode() + b"\x00\x08"
        body += struct.pack("<II", aid, len(blob)) + blob
    body += struct.pack("<I", 0)
    table = struct.pack("<I", len(names)) + b"".join(n + b"\x00" for n in names)
    head = struct.pack("<IIq", 0x07564429, 1, 16 + len(body))
    f = tmp_path / "appinfo.vdf"
    f.write_bytes(head + body + table)
    return f


def test_appinfo_installdir_reads_steams_cache(tmp_path):
    f = _appinfo(tmp_path, {10: "Proton 8.0", 2805730: "Proton 9.0 (Beta)"})
    assert ct.appinfo_installdir("2805730", f) == "Proton 9.0 (Beta)"
    assert ct.appinfo_installdir("10", f) == "Proton 8.0"
    assert ct.appinfo_installdir("11", f) == ""
    assert ct.appinfo_installdir("10", tmp_path / "missing.vdf") == ""


def test_queue_install_writes_a_stub_in_the_root_library(monkeypatch, tmp_path):
    root = tmp_path / "root"
    (root / "steamapps").mkdir(parents=True)
    monkeypatch.setattr(ct, "steam_root", lambda: root)
    monkeypatch.setattr(ct, "_manifests", lambda: {})
    monkeypatch.setattr(ct, "appinfo_installdir", lambda a: "Proton 8.0")
    monkeypatch.setattr(ct.lo, "steam_running", lambda: False)
    acf = root / "steamapps" / "appmanifest_2348590.acf"
    assert ct.queue_install("2348590", dry=True) == 0 and not acf.exists()
    assert ct.queue_install("2348590") == 0
    text = acf.read_text()
    assert '"StateFlags"\t\t"1026"' in text and '"installdir"\t\t"Proton 8.0"' in text
    # already there → untouched
    monkeypatch.setattr(ct, "_manifests", lambda: {"2348590": {"name": "Proton 8.0", "lib": "x"}})
    assert ct.queue_install("2348590") == 0
    # unknown to Steam's cache → falls back to the dialog
    monkeypatch.setattr(ct, "_manifests", lambda: {})
    monkeypatch.setattr(ct, "appinfo_installdir", lambda a: "")
    monkeypatch.setattr(ct, "steam_install", lambda a: 7)
    assert ct.queue_install("999") == 7


def test_restart_steam_waits_for_exit_then_starts(monkeypatch):
    calls, running = [], [True, True, False, False]
    monkeypatch.setattr(ct.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(ct, "session_env", lambda: {"DISPLAY": ":0",
                                                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/b"})
    monkeypatch.setattr(ct.lo, "steam_running", lambda: running.pop(0) if running else False)
    monkeypatch.setattr(ct.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(ct.subprocess, "Popen", lambda cmd, **k: calls.append(cmd))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    assert ct.restart_steam() == 0
    assert calls[0] == ["steam", "-shutdown"]
    assert calls[-1][-1] == "steam" and calls[-1][0] == "systemd-run"
    # never exits → gives up without starting a second copy
    calls.clear()
    monkeypatch.setattr(ct.lo, "steam_running", lambda: True)
    assert ct.restart_steam(wait=0) == 1 and len(calls) == 1


_CFG = '''"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"10"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"GE-Proton9"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t\t"0"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"GE-Proton9"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"75"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\t"DownloadThrottleKbps"\t\t"0"
\t\t\t}
\t\t}
\t}
\t"SDL_GamepadBind"\t\t"a:b0,
b:{b1}"
}
'''


def test_mapping_is_read_with_matched_braces():
    assert ct.read_mapping(_CFG) == {"10": "GE-Proton9", "0": "GE-Proton9"}
    assert ct.read_mapping("no mapping here") == {}


def test_write_mapping_touches_only_the_block():
    new = ct.write_mapping(_CFG, {"0": "proton_experimental", "20": "proton_experimental"})
    assert ct.read_mapping(new) == {"10": "GE-Proton9", "0": "proton_experimental",
                                    "20": "proton_experimental"}
    assert '"priority"\t\t"75"' in new and new.count('"priority"\t\t"250"') == 2
    head, tail = _CFG.split('"CompatToolMapping"')[0], _CFG.split('"DownloadThrottleKbps"')[1]
    assert new.startswith(head) and new.endswith(tail)
    assert ct.write_mapping(new, {}) == new
    try:
        ct.write_mapping("nothing", {"0": "x"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_default_plan_skips_native_forced_and_tools(monkeypatch, tmp_path):
    cfg = tmp_path / "config.vdf"
    cfg.write_text(_CFG.replace('"10"', '"10"').replace("GE-Proton9", "GE-Proton9", 1))
    man = {"10": {"name": "Forced Game"}, "20": {"name": "Windows Game"},
           "30": {"name": "Native Game"}, "40": {"name": "Proton Experimental"},
           "50": {"name": "Steam Linux Runtime 3.0 (sniper)"}}
    monkeypatch.setattr(ct, "_config_vdf", lambda: cfg)
    monkeypatch.setattr(ct, "_manifests", lambda: man)
    monkeypatch.setattr(ct, "native_linux_apps", lambda: {"30"})
    assert ct.default_plan("proton_experimental", True) == {"0": "proton_experimental",
                                                           "20": "proton_experimental"}
    assert ct.default_plan("proton_experimental", False) == {"0": "proton_experimental"}
    assert ct.default_plan("GE-Proton9", False) == {}


def test_set_default_refuses_while_steam_runs_and_writes_when_closed(monkeypatch, tmp_path):
    cfg = tmp_path / "config.vdf"
    cfg.write_text(_CFG)
    monkeypatch.setattr(ct, "_config_vdf", lambda: cfg)
    monkeypatch.setattr(ct, "_manifests", lambda: {"20": {"name": "Windows Game"}})
    monkeypatch.setattr(ct, "native_linux_apps", lambda: set())
    monkeypatch.setattr(ct, "compat_names", lambda: [{"name": "proton_experimental",
                                                      "label": "Proton Experimental"}])
    monkeypatch.setattr(ct.lo, "steam_running", lambda: True)
    assert ct.set_default("proton_experimental", True) == 2
    assert ct.read_mapping(cfg.read_text())["0"] == "GE-Proton9"
    assert ct.set_default("nope") == 1
    monkeypatch.setattr(ct.lo, "steam_running", lambda: False)
    assert ct.set_default("proton_experimental", True, dry=True) == 0
    assert ct.read_mapping(cfg.read_text())["0"] == "GE-Proton9"
    assert ct.set_default("proton_experimental", True) == 0
    assert ct.read_mapping(cfg.read_text()) == {"10": "GE-Proton9", "0": "proton_experimental",
                                                "20": "proton_experimental"}
    assert len(list(tmp_path.glob("config.vdf.tuxthrottle-bak-*"))) == 1
