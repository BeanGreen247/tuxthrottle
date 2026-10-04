#!/usr/bin/env python3
"""Proton builds and Steam runtimes - install, list, remove, relocate.

Does what ProtonUp-Qt does, the same way it does it, so that tool isn't
needed any more:

  * community builds (GE-Proton, Proton-CachyOS, Proton-EM, Luxtorpeda,
    Boxtron; Wine-GE / Lutris-Wine; DXVK and vkd3d-proton) come from each project's GitHub releases: pick the right asset,
    download it, check it against the release's published sha512sum, unpack
    it into the launcher's own tools folder -
        Steam    <steam root>/compatibilitytools.d/
        Heroic   ~/.config/heroic/tools/proton/
        Lutris   ~/.local/share/lutris/runners/wine/
        Bottles  ~/.local/share/bottles/runners/
    (plus each launcher's Flatpak location). All of these sit under $HOME, so
    a build installed here is always on the OS drive.
  * Valve's own tools (Proton Experimental / numbered Proton, the Steam Linux
    Runtimes, the Proton BattlEye / EasyAntiCheat runtimes) are Steam apps:
    Steam itself installs them (`steam://install/<appid>`), and `move-to-os`
    relocates one that landed in a library on another drive back into the
    Steam root library - a Windows/NTFS games drive is a poor home for the
    thing every game's prefix depends on.

Usage:
    tuxthrottle_compat_tools.py status [--json]
    tuxthrottle_compat_tools.py releases <tool> [--json] [--count N]
    tuxthrottle_compat_tools.py install <tool> [--tag TAG] [--target NAME] [--force]
    tuxthrottle_compat_tools.py remove <target> <name>
    tuxthrottle_compat_tools.py verify [--json]
    tuxthrottle_compat_tools.py steam-validate <appid>
    tuxthrottle_compat_tools.py queue-install <appid> [--dry-run]   # no dialog
    tuxthrottle_compat_tools.py restart-steam                       # picks up queued tools
    tuxthrottle_compat_tools.py set-default <tool> [--all-games] [--restart-steam] [--dry-run]
    tuxthrottle_compat_tools.py steam-install <appid>               # Steam's dialog
    tuxthrottle_compat_tools.py move-to-os <appid> [--dry-run]

Run as the real user, not root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import tuxthrottle_launchopts as lo
from tuxthrottle_prefix_relocate import all_libraries

API = "https://api.github.com/repos/{repo}/releases"
UA = {"User-Agent": "tuxthrottle-compat-tools", "Accept": "application/vnd.github+json"}

# tool → GitHub repo, which launchers it makes sense for, how to pick the asset
TOOLS = {
    "GE-Proton": {"repo": "GloriousEggroll/proton-ge-custom", "ext": ".tar.gz",
                  "launchers": ("steam", "heroic", "lutris", "bottles"),
                  "about": "GloriousEggroll's Proton - newest fixes, media codecs; "
                           "the usual choice for launcher/anti-cheat titles"},
    "Proton-CachyOS": {"repo": "CachyOS/proton-cachyos", "ext": ".tar.xz", "arch": True,
                       "launchers": ("steam", "heroic", "lutris", "bottles"),
                       "about": "CachyOS's performance-patched Proton (ntsync, "
                                "x86-64-v3 build on AVX2 CPUs)"},
    "Proton-EM": {"repo": "Etaash-mathamsetty/Proton", "ext": ".tar.xz",
                  "launchers": ("steam", "heroic", "lutris", "bottles"),
                  "about": "Proton with experimental Wayland / HDR patches"},
    "Luxtorpeda": {"repo": "luxtorpeda-dev/luxtorpeda", "ext": ".tar.xz",
                   "launchers": ("steam",),
                   "about": "runs supported Windows games on native Linux source ports"},
    "Boxtron": {"repo": "dreamer/boxtron", "ext": ".tar.xz", "launchers": ("steam",),
                "about": "runs DOS games through native DOSBox"},
    # --- Wine builds and the D3D→Vulkan layers, for the non-Steam launchers ---
    "Wine-GE": {"repo": "GloriousEggroll/wine-ge-custom", "ext": ".tar.xz", "kind": "wine",
                "launchers": ("lutris", "heroic", "bottles"),
                "about": "GloriousEggroll's Wine for Lutris / Heroic / Bottles - "
                         "discontinued upstream (GE-Proton via umu replaced it); the "
                         "last releases are still downloadable"},
    "Lutris-Wine": {"repo": "lutris/wine", "ext": ".tar.xz", "kind": "wine",
                    "launchers": ("lutris", "bottles"),
                    "about": "Lutris's own Wine builds"},
    "DXVK": {"repo": "doitsujin/dxvk", "ext": ".tar.gz", "kind": "dxvk",
             "launchers": ("lutris", "heroic", "bottles"),
             "about": "Direct3D 8-11 → Vulkan layer used inside a Wine prefix"},
    "vkd3d-proton": {"repo": "HansKristian-Work/vkd3d-proton", "ext": ".tar.zst",
                     "kind": "vkd3d", "launchers": ("lutris", "heroic", "bottles"),
                     "about": "Direct3D 12 → Vulkan layer used inside a Wine prefix"},
}
KINDS = ("proton", "wine", "dxvk", "vkd3d")


def kind_of(tool: str) -> str:
    return TOOLS[tool].get("kind", "proton")

# Valve's own compatibility tools / runtimes (Steam appids)
VALVE_TOOLS = (
    ("1493710", "Proton Experimental"), ("2180100", "Proton Hotfix"),
    ("4628710", "Proton 11.0"), ("3658110", "Proton 10.0"), ("2805730", "Proton 9.0"), ("2348590", "Proton 8.0"),
    ("1887720", "Proton 7.0"),
    ("4183110", "Steam Linux Runtime 4.0"),
    ("1628350", "Steam Linux Runtime 3.0 (sniper)"),
    ("1391110", "Steam Linux Runtime 2.0 (soldier)"),
    ("1070560", "Steam Linux Runtime 1.0 (scout)"),
    ("1161040", "Proton BattlEye Runtime"),
    ("1826330", "Proton EasyAntiCheat Runtime"),
)


# --------------------------------------------------------------------------- #
#  where things go
# --------------------------------------------------------------------------- #
def steam_root() -> Path | None:
    roots = lo._steam_roots()
    return roots[0] if roots else None


def targets() -> list[dict]:
    """Every launcher, whether it is present, and where each kind of build
    goes for it (`dirs`: proton / wine / dxvk / vkd3d - same layout
    ProtonUp-Qt uses)."""
    home = Path.home()
    root = steam_root()
    flat = home / ".var/app"
    cands = []
    if root is not None:
        cands.append(("Steam", "steam", root, {"proton": root / "compatibilitytools.d"}))
    fs = flat / "com.valvesoftware.Steam/data/Steam"
    cands.append(("Steam Flatpak", "steam", fs, {"proton": fs / "compatibilitytools.d"}))
    for name, base in (("Heroic", home / ".config/heroic"),
                       ("Heroic Flatpak", flat / "com.heroicgameslauncher.hgl/config/heroic")):
        t = base / "tools"
        cands.append((name, "heroic", base, {"proton": t / "proton", "wine": t / "wine",
                                             "dxvk": t / "dxvk", "vkd3d": t / "vkd3d"}))
    for name, base in (("Lutris", home / ".local/share/lutris"),
                       ("Lutris Flatpak", flat / "net.lutris.Lutris/data/lutris")):
        cands.append((name, "lutris", base, {
            "proton": base / "runners/wine", "wine": base / "runners/wine",
            "dxvk": base / "runtime/dxvk", "vkd3d": base / "runtime/vkd3d"}))
    for name, base in (("Bottles", home / ".local/share/bottles"),
                       ("Bottles Flatpak", flat / "com.usebottles.bottles/data/bottles")):
        cands.append((name, "bottles", base, {
            "proton": base / "runners", "wine": base / "runners",
            "dxvk": base / "dxvk", "vkd3d": base / "vkd3d"}))
    return [{"name": name, "launcher": launcher, "present": Path(marker).is_dir(),
             "dir": str(dirs["proton"]), "dirs": {k: str(v) for k, v in dirs.items()}}
            for name, launcher, marker, dirs in cands]


def target_by_name(name: str) -> dict | None:
    return next((t for t in targets() if t["name"].lower() == name.lower()), None)


def installed(target_dir) -> list[str]:
    d = Path(target_dir)
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=_natural, reverse=True)


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# --------------------------------------------------------------------------- #
#  GitHub releases
# --------------------------------------------------------------------------- #
def _get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https host
        return json.loads(r.read().decode("utf-8"))


def cpu_arch() -> str:
    """x86_64_v3 when the CPU has AVX2 (what Proton-CachyOS's v3 build needs)."""
    try:
        flags = Path("/proc/cpuinfo").read_text()
    except OSError:
        return "x86_64"
    return "x86_64_v3" if re.search(r"\bavx2\b", flags) else "x86_64"


def pick_assets(tool: str, assets: list, arch: str | None = None) -> dict | None:
    """The archive (+ its sha512sum file, when published) for this machine."""
    spec = TOOLS[tool]
    ext = spec["ext"]
    names = {a.get("name", ""): a for a in assets if a.get("browser_download_url")}
    cands = [n for n in names if n.endswith(ext) and "aarch64" not in n and "arm64" not in n]
    if spec.get("arch"):
        arch = arch or cpu_arch()
        exact = [n for n in cands if n.endswith(f"{arch}{ext}")]
        cands = exact or [n for n in cands if n.endswith(f"x86_64{ext}")] or cands
    if not cands:
        return None
    name = sorted(cands, key=len)[0]
    stem = name[: -len(ext)]
    sums = [n for n in names if n.endswith(".sha512sum")
            and (n.startswith(stem) or len([x for x in names if x.endswith(".sha512sum")]) == 1)]
    a = names[name]
    return {"asset": name, "url": a["browser_download_url"], "size": a.get("size", 0),
            "checksum_url": names[sums[0]]["browser_download_url"] if sums else ""}


def releases(tool: str, count: int = 15) -> list[dict]:
    data = _get_json(API.format(repo=TOOLS[tool]["repo"]) + f"?per_page={count}")
    out = []
    for rel in data if isinstance(data, list) else []:
        pick = pick_assets(tool, rel.get("assets") or [])
        if pick:
            out.append({"tag": rel.get("tag_name", ""),
                        "date": (rel.get("published_at") or "")[:10], **pick})
    return out


# --------------------------------------------------------------------------- #
#  install / remove
# --------------------------------------------------------------------------- #
def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, size: int = 0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as fh:  # noqa: S310
        total = size or int(r.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else 0
            if pct // 10 != last:
                last = pct // 10
                print(f"  downloaded {done >> 20} MiB" + (f" ({pct}%)" if total else ""),
                      flush=True)


def extract(archive: Path, into: Path) -> str:
    """Unpack `archive` under `into`; returns the single top-level folder name.
    Uses tarfile's 'data' filter, so a member can't escape `into`."""
    src = archive
    if archive.name.endswith(".zst"):
        try:
            tarfile.open(archive, "r:*").close()        # Python 3.14+ reads zstd itself
        except tarfile.ReadError:
            if not shutil.which("zstd"):
                raise ValueError("this archive is zstd-compressed - install `zstd`") from None
            src = archive.with_suffix("")               # foo.tar.zst → foo.tar
            subprocess.run(["zstd", "-d", "-q", "-f", str(archive), "-o", str(src)],  # noqa: S603,S607
                           check=True)
    with tarfile.open(src, "r:*") as tar:
        tops = {m.name.split("/", 1)[0] for m in tar.getmembers() if m.name not in (".", "")}
        if len(tops) != 1:
            raise ValueError(f"archive has {len(tops)} top-level entries, expected 1")
        tar.extractall(into, filter="data")
    return tops.pop()


def install(tool: str, tag: str = "", target: str = "Steam", force: bool = False) -> int:
    if tool not in TOOLS:
        print(f"unknown tool {tool!r} - one of: {', '.join(TOOLS)}")
        return 1
    tgt = target_by_name(target)
    if tgt is None:
        print(f"unknown target {target!r}")
        return 1
    if tgt["launcher"] not in TOOLS[tool]["launchers"]:
        print(f"{tool} isn't meant for {tgt['name']}")
        return 1
    rels = releases(tool, 30)
    rel = next((r for r in rels if r["tag"] == tag), None) if tag else (rels[0] if rels else None)
    if rel is None:
        print(f"no downloadable release found for {tool}" + (f" {tag}" if tag else ""))
        return 1
    kind = kind_of(tool)
    if kind not in tgt["dirs"]:
        print(f"{tgt['name']} has no place for a {kind} build")
        return 1
    dest = Path(tgt["dirs"][kind])
    dest.mkdir(parents=True, exist_ok=True)
    work = dest / ".tuxthrottle-download"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        arc = work / rel["asset"]
        print(f"{tool} {rel['tag']} → {dest}")
        _download(rel["url"], arc, rel["size"])
        if rel["checksum_url"]:
            req = urllib.request.Request(rel["checksum_url"],
                                         headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                published = r.read().decode("utf-8", "replace")
            if _sha512(arc) not in published:
                print("sha512 MISMATCH - the download is corrupt or was tampered "
                      "with; nothing installed")
                return 1
            print("  sha512 verified")
        else:
            print("  (this release publishes no checksum - not verified)")
        top = extract(arc, work)
        final = dest / top
        if final.exists():
            if not force:
                print(f"{top} is already installed - nothing to do")
                return 0
            shutil.rmtree(final)
        (work / top).rename(final)
        print(f"installed {top}"
              + (" - restart Steam, then pick it under Properties → Compatibility"
                 if tgt["launcher"] == "steam" else
                 f" - restart {tgt['name'].split()[0]} if it is open, so it rescans "
                 f"its tools folder"))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def remove(target: str, name: str, kind: str = "") -> int:
    tgt = target_by_name(target)
    if tgt is None:
        print(f"unknown target {target!r}")
        return 1
    if "/" in name or name.startswith("."):
        print(f"{name!r} is not a build name")
        return 1
    for k in ([kind] if kind else KINDS):
        d = tgt["dirs"].get(k)
        if d and name in installed(d):
            shutil.rmtree(Path(d) / name)
            print(f"removed {name} from {tgt['name']}")
            return 0
    print(f"{name!r} is not installed in {tgt['name']}")
    return 1


# --------------------------------------------------------------------------- #
#  Valve's own tools (Steam apps)
# --------------------------------------------------------------------------- #
def _manifests() -> dict[str, dict]:
    """{appid: {name, lib, installdir, size, acf}} over every Steam library."""
    out: dict[str, dict] = {}
    root = steam_root()
    if root is None:
        return out
    try:
        libs = all_libraries(root)
    except OSError:
        return out
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                t = acf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            a = re.search(r'"appid"\s+"(\d+)"', t)
            if not a:
                continue
            n = re.search(r'"name"\s+"([^"]*)"', t)
            d = re.search(r'"installdir"\s+"([^"]*)"', t)
            s = re.search(r'"SizeOnDisk"\s+"(\d+)"', t)
            fl = re.search(r'"StateFlags"\s+"(\d+)"', t)
            out.setdefault(a.group(1), {
                "name": n.group(1) if n else a.group(1), "lib": str(lib),
                "installdir": d.group(1) if d else "", "size": int(s.group(1)) if s else 0,
                "state": fl.group(1) if fl else "4",
                "acf": str(acf)})
    return out


def _same_device(a, b) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def steam_tools() -> list[dict]:
    root = steam_root()
    man = _manifests()
    known = dict(VALVE_TOOLS)
    # also anything installed that looks like a Valve tool but isn't in the table
    for aid, m in man.items():
        if aid not in known and re.match(r"(Proton( |$)|Steam Linux Runtime)", m["name"]):
            known[aid] = m["name"]
    rows = []
    for aid, name in known.items():
        m = man.get(aid)
        rows.append({"appid": aid, "name": m["name"] if m else name, "installed": bool(m),
                     "ready": bool(m and m.get("state", "4") == "4"),
                     "library": m["lib"] if m else "", "size": m["size"] if m else 0,
                     "on_os_drive": bool(m and root and (Path(m["lib"]) == root
                                                         or _same_device(m["lib"], root)))})
    return rows


_SESSION_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS",
                 "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")


def session_env(base: dict | None = None) -> dict:
    """Environment that can reach the user's graphical session.

    The GUI runs elevated and calls this script through `su - <user>`, which
    hands over a clean login environment: no DISPLAY, no WAYLAND_DISPLAY, no
    session bus. Steam started from there dies with "Unable to open X11
    display". The desktop publishes those variables to the user's systemd
    manager, so fill in whatever is missing from there."""
    env = dict(os.environ if base is None else base)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if all(env.get(k) for k in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS")):
        return env
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],  # noqa: S603,S607
                             capture_output=True, text=True, timeout=10, env=env).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k in _SESSION_VARS and not env.get(k):
            # systemd quotes values with special characters as $'…'
            env[k] = v[2:-1] if v.startswith("$'") and v.endswith("'") else v
    bus = Path(env["XDG_RUNTIME_DIR"]) / "bus"
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and bus.exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _steam_url(url: str, what: str) -> int:
    """Hand a steam:// URL to Steam inside the user's desktop session (starts
    Steam if it isn't running)."""
    if not shutil.which("steam"):
        print("steam is not on PATH")
        return 1
    env = session_env()
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        print("no graphical session found for this user (no DISPLAY / "
              "WAYLAND_DISPLAY) - log into the desktop first")
        return 1
    print(what)
    # run it under the user's own systemd manager when possible: that is where
    # a normally-launched Steam lives, with the full session environment
    cmd = ["steam", url]
    if shutil.which("systemd-run") and env.get("DBUS_SESSION_BUS_ADDRESS"):
        cmd = ["systemd-run", "--user", "--collect", "--quiet", "--"] + cmd
    try:
        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,  # noqa: S603
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        print(f"could not start Steam: {exc}")
        return 1
    return 0


def steam_install(appid: str) -> int:
    return _steam_url(
        f"steam://install/{appid}",
        f"asking Steam to install AppID {appid} - Steam opens its install dialog; "
        f"pick the library on your OS drive ({steam_root()})")


def appinfo_installdir(appid: str, appinfo: Path | None = None) -> str:
    """The install-folder name Steam uses for an app, read from Steam's own
    appcache/appinfo.vdf ('' if it isn't there). Format v29: a header, one
    binary-VDF blob per app (keys are indices into a string table at the end
    of the file, values are inline)."""
    import struct
    root = steam_root()
    f = appinfo or (root / "appcache" / "appinfo.vdf" if root else None)
    try:
        b = f.read_bytes()
        magic = struct.unpack_from("<I", b, 0)[0]
        if magic not in (0x07564429, 0x07564428):
            return ""
        keyed = magic == 0x07564429
        pos = 16 if keyed else 8
        key = b"\x01installdir\x00"
        if keyed:
            toff = struct.unpack_from("<q", b, 8)[0]
            n = struct.unpack_from("<I", b, toff)[0]
            names = b[toff + 4:].split(b"\x00", n)[:n]
            key = b"\x01" + struct.pack("<I", names.index(b"installdir"))
        want = int(appid)
        while pos + 8 <= len(b):
            aid, size = struct.unpack_from("<II", b, pos)
            if aid == 0:
                break
            blob = b[pos + 8: pos + 8 + size]
            if aid == want:
                i = blob.find(key)
                if i < 0:
                    return ""
                start = i + len(key)
                return blob[start: blob.index(b"\x00", start)].decode("utf-8", "replace")
            pos += 8 + size
    except (OSError, ValueError, struct.error, AttributeError):
        pass
    return ""


def queue_install(appid: str, dry: bool = False) -> int:
    """Install a Valve tool with no dialog: drop a stub manifest for it into
    the Steam root library (the one on the OS drive). Steam picks stub
    manifests up when it starts and downloads the app into that library on
    its own - which library it lands in is decided here, not by a prompt."""
    root = steam_root()
    if root is None:
        print("no Steam install found")
        return 1
    m = _manifests().get(appid)
    if m is not None:
        print(f"{m['name']} is already installed (or queued) in {m['lib']}")
        return 0
    installdir = appinfo_installdir(appid)
    if not installdir:
        print(f"Steam's app cache doesn't know AppID {appid}'s install folder yet - "
              f"falling back to Steam's own install dialog")
        return steam_install(appid)
    name = dict(VALVE_TOOLS).get(appid, installdir)
    acf = root / "steamapps" / f"appmanifest_{appid}.acf"
    text = ('"AppState"\n{\n'
            f'\t"appid"\t\t"{appid}"\n\t"Universe"\t\t"1"\n\t"name"\t\t"{name}"\n'
            f'\t"StateFlags"\t\t"1026"\n\t"installdir"\t\t"{installdir}"\n}}\n')
    print(f"{name}: queued in {root / 'steamapps'} (folder “{installdir}”)")
    if dry:
        return 0
    acf.write_text(text, encoding="utf-8")
    if lo.steam_running():
        print("Steam only reads its library when it starts, and it is running now - "
              "the download begins after Steam is restarted. Queue everything you "
              "want first, then restart Steam once (the tab has a button for it).")
    else:
        print("Steam downloads it by itself when it starts")
    return 0


# --------------------------------------------------------------------------- #
#  which Proton Steam uses by default
# --------------------------------------------------------------------------- #
# Steam keeps this in config/config.vdf under CompatToolMapping: entry "0" is
# the default for "other titles" (Settings -> Compatibility), "<appid>" entries
# are per-game choices (priority 250). For a game with no entry of its own,
# Valve's per-title recommendation (often Proton Hotfix for a title it is
# currently patching) wins over the "0" default - so making one build the
# default for everything means writing a per-game entry too.
_STATIC_TOOL_NAMES = {"proton_experimental": "1493710", "proton_hotfix": "2180100",
                      "proton_11": "4628710", "proton_10": "3658110", "proton_9": "2805730",
                      "proton_8": "2348590", "proton_7": "1887720"}
_MAP_HEAD = re.compile(r'"CompatToolMapping"\s*\{')
_ENTRY_RE = re.compile(r'"(\d+)"\s*\{([^{}]*)\}')


def _appinfo_blobs():
    """Yield (appid, blob, key-names) from Steam's appcache/appinfo.vdf (v29)."""
    import struct
    root = steam_root()
    if root is None:
        return
    try:
        b = (root / "appcache" / "appinfo.vdf").read_bytes()
        if struct.unpack_from("<I", b, 0)[0] != 0x07564429:
            return
        toff = struct.unpack_from("<q", b, 8)[0]
        n = struct.unpack_from("<I", b, toff)[0]
        names = b[toff + 4:].split(b"\x00", n)[:n]
        pos = 16
        while pos + 8 <= len(b):
            aid, size = struct.unpack_from("<II", b, pos)
            if aid == 0:
                break
            yield aid, b[pos + 8: pos + 8 + size], names
            pos += 8 + size
    except (OSError, ValueError, struct.error):
        return


def _blob_str(blob: bytes, names: list, key: bytes) -> str:
    import struct
    try:
        k = b"\x01" + struct.pack("<I", names.index(key))
        i = blob.find(k)
        if i < 0:
            return ""
        return blob[i + 5: blob.index(b"\x00", i + 5)].decode("utf-8", "replace")
    except ValueError:
        return ""


def native_linux_apps() -> set:
    """AppIDs Steam lists as having a native Linux build - forcing Proton on
    those would make them run their Windows version instead."""
    return {str(aid) for aid, blob, names in _appinfo_blobs()
            if "linux" in _blob_str(blob, names, b"oslist").split(",")}


def valve_tool_names() -> dict:
    """{internal name: appid} of Valve's Proton builds, from Steam's own
    Steam Play manifest (app 891390) when it is cached."""
    import struct
    out = dict(_STATIC_TOOL_NAMES)
    for aid, blob, names in _appinfo_blobs():
        if aid != 891390:
            continue
        try:
            ia = b"\x02" + struct.pack("<I", names.index(b"appid"))
        except ValueError:
            break
        for i, nm in enumerate(names):
            if not nm.startswith(b"proton"):
                continue
            j = blob.find(b"\x00" + struct.pack("<I", i))
            a = blob.find(ia, j, j + 400) if j >= 0 else -1
            if a >= 0:
                out[nm.decode()] = str(struct.unpack_from("<I", blob, a + 5)[0])
        break
    return out


def compat_names() -> list[dict]:
    """Proton builds Steam can be pointed at right now: installed Valve tools
    and everything in compatibilitytools.d, with the internal name Steam uses."""
    rows = []
    man = _manifests()
    for name, appid in valve_tool_names().items():
        if appid in man and name != "proton-stable":
            rows.append({"name": name, "label": man[appid]["name"]})
    tgt = target_by_name("Steam")
    for d in installed(tgt["dir"]) if tgt else []:
        internal = d
        try:
            txt = (Path(tgt["dir"]) / d / "compatibilitytool.vdf").read_text(errors="replace")
            m = re.search(r'"compat_tools"\s*\{\s*"([^"]+)"', txt)
            if m:
                internal = m.group(1)
        except OSError:
            continue
        rows.append({"name": internal, "label": d})
    order = {"proton_experimental": 0, "proton_hotfix": 2}
    return sorted(rows, key=lambda r: (order.get(r["name"], 1), _natural(r["label"])),
                  reverse=False)


def _map_span(text: str):
    """(body start, body end, entry indent) of the CompatToolMapping block,
    found by matching braces; None when the block is missing."""
    m = _MAP_HEAD.search(text)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        c = text[i]
        if c == '"':                           # skip a quoted string
            i = text.index('"', i + 1)
            while text[i - 1] == "\\":
                i = text.index('"', i + 1)
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    if depth:
        return None
    close = i - 1                              # the block's closing brace
    line_start = text.rfind("\n", 0, close)
    indent = text[line_start + 1: close] + "\t"
    return m.end(), line_start, indent


def read_mapping(text: str) -> dict:
    """{appid: tool name} from a config.vdf text ('0' = the default)."""
    span = _map_span(text)
    if not span:
        return {}
    out = {}
    for e in _ENTRY_RE.finditer(text[span[0]:span[1]]):
        nm = re.search(r'"name"\s*"([^"]*)"', e.group(2))
        out[e.group(1)] = nm.group(1) if nm else ""
    return out


def write_mapping(text: str, changes: dict) -> str:
    """Return config.vdf text with `changes` ({appid: tool name}) merged into
    CompatToolMapping. Only that block is rewritten; every other byte of the
    file is left exactly as Steam wrote it."""
    span = _map_span(text)
    if not span:
        raise ValueError("no CompatToolMapping block in config.vdf - open Steam's "
                         "Settings -> Compatibility once, then try again")
    start, end, indent = span
    body = text[start:end]
    done = set()

    def entry(aid):
        prio = "75" if aid == "0" else "250"
        return (f'"{aid}"\n{indent}{{\n{indent}\t"name"\t\t"{changes[aid]}"\n'
                f'{indent}\t"config"\t\t""\n{indent}\t"priority"\t\t"{prio}"\n{indent}}}')

    def swap(e):
        if e.group(1) not in changes:
            return e.group(0)
        done.add(e.group(1))
        return entry(e.group(1))
    body = _ENTRY_RE.sub(swap, body)
    for aid in changes:
        if aid not in done:
            body += f"\n{indent}{entry(aid)}"
    return text[:start] + body + text[end:]


def _config_vdf() -> Path | None:
    root = steam_root()
    return root / "config" / "config.vdf" if root else None


def default_plan(tool: str, all_games: bool) -> dict:
    """{appid: tool} to write so `tool` is what Steam uses: the "0" default,
    and with `all_games` every installed Windows game that has no choice of
    its own (or sits on Proton Hotfix). Native Linux games, Steam's own tools
    and games you forced to another build are left alone."""
    cfg = _config_vdf()
    cur = read_mapping(cfg.read_text(encoding="utf-8", errors="replace")) if cfg and cfg.is_file() else {}
    plan = {} if cur.get("0") == tool else {"0": tool}
    if all_games:
        native = native_linux_apps()
        skip = re.compile(r"(Proton( |$)|Steam Linux Runtime|Steamworks Common|SteamVR$)", re.I)
        for aid, m in _manifests().items():
            if aid in native or skip.match(m["name"]):
                continue
            if cur.get(aid) in (None, "", "proton_hotfix"):
                plan[aid] = tool
    return plan


def set_default(tool: str, all_games: bool = False, dry: bool = False,
                restart: bool = False) -> int:
    known = {r["name"]: r["label"] for r in compat_names()}
    if tool not in known:
        print(f"{tool!r} is not an installed Proton build - one of: {', '.join(known)}")
        return 1
    cfg = _config_vdf()
    if cfg is None or not cfg.is_file():
        print("Steam's config.vdf not found")
        return 1
    plan = default_plan(tool, all_games)
    names = {a: m["name"] for a, m in _manifests().items()}
    print(f"{known[tool]} ({tool}):")
    if "0" in plan:
        print("  - Steam's default for other titles (Settings -> Compatibility)")
    for aid in sorted((a for a in plan if a != "0"), key=lambda a: names.get(a, a).lower()):
        print(f"  - {names.get(aid, aid)}")
    if not plan:
        print("  already set - nothing to change")
        return 0
    if dry:
        return 0
    was_running = lo.steam_running()
    if was_running:
        if not restart:
            print("Steam is running - it rewrites config.vdf when it exits, so this "
                  "needs Steam closed (or use the restart option).")
            return 2
        rc = stop_steam()
        if rc:
            return rc
    text = cfg.read_text(encoding="utf-8", errors="replace")
    try:
        new = write_mapping(text, plan)
    except ValueError as exc:
        print(str(exc))
        return 1
    import time
    shutil.copy2(cfg, cfg.with_name(cfg.name + f".tuxthrottle-bak-{int(time.time())}"))
    cfg.write_text(new, encoding="utf-8")
    print(f"written ({len(plan)} entr{'y' if len(plan) == 1 else 'ies'}); backup kept next "
          f"to config.vdf")
    return start_steam() if was_running else 0


def stop_steam(wait: int = 90) -> int:
    """Ask a running Steam to shut down and wait for it. 0 = it is closed."""
    import time
    if not lo.steam_running():
        return 0
    env = session_env()
    print("asking Steam to shut down...", flush=True)
    try:
        subprocess.run(["steam", "-shutdown"], env=env, timeout=30,  # noqa: S603,S607
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    deadline = time.time() + wait
    while lo.steam_running() and time.time() < deadline:
        time.sleep(1)
    if lo.steam_running():
        print(f"Steam did not exit within {wait}s (a game or a dialog may be holding "
              f"it) - close it by hand, then try again")
        return 1
    time.sleep(2)                         # let it finish writing its config
    return 0


def start_steam() -> int:
    env = session_env()
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        print("no graphical session found for this user - start Steam yourself")
        return 1
    cmd = ["steam"]
    if shutil.which("systemd-run") and env.get("DBUS_SESSION_BUS_ADDRESS"):
        cmd = ["systemd-run", "--user", "--collect", "--quiet", "--"] + cmd
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, start_new_session=True)
    print("Steam is starting")
    return 0


def restart_steam(wait: int = 90) -> int:
    """Close Steam cleanly (if it runs) and start it again in the user's
    desktop session - one restart picks up every queued tool."""
    import time
    if not shutil.which("steam"):
        print("steam is not on PATH")
        return 1
    env = session_env()
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        print("no graphical session found for this user - log into the desktop first")
        return 1
    if lo.steam_running():
        print("asking Steam to shut down…", flush=True)
        try:
            subprocess.run(["steam", "-shutdown"], env=env, timeout=30,  # noqa: S603,S607
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
        deadline = time.time() + wait
        while lo.steam_running() and time.time() < deadline:
            time.sleep(1)
        if lo.steam_running():
            print(f"Steam did not exit within {wait}s (a game or a dialog may be "
                  f"holding it) - close it by hand, then start it again")
            return 1
        time.sleep(2)                     # let it finish writing its config
    cmd = ["steam"]
    if shutil.which("systemd-run") and env.get("DBUS_SESSION_BUS_ADDRESS"):
        cmd = ["systemd-run", "--user", "--collect", "--quiet", "--"] + cmd
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, start_new_session=True)
    print("Steam is starting - queued tools download on their own (Steam → Downloads)")
    return 0


def move_to_os(appid: str, dry: bool = False) -> int:
    """Relocate an installed Steam tool into the Steam root library (OS drive)."""
    root = steam_root()
    m = _manifests().get(appid)
    if root is None or m is None:
        print(f"AppID {appid} is not installed")
        return 1
    if Path(m["lib"]) == root:
        print(f"{m['name']} is already in the OS-drive library")
        return 0
    if lo.steam_running():
        print("Steam is running - quit Steam completely first (it owns the library "
              "while it runs).")
        return 2
    src = Path(m["lib"]) / "steamapps" / "common" / m["installdir"]
    dst = root / "steamapps" / "common" / m["installdir"]
    acf_dst = root / "steamapps" / Path(m["acf"]).name
    if not m["installdir"] or not src.is_dir():
        print(f"install folder not found: {src}")
        return 1
    if dst.exists() or acf_dst.exists():
        print(f"{dst} already exists - not overwriting")
        return 1
    free = shutil.disk_usage(root).free
    if free < m["size"] * 1.15:
        print(f"not enough free space on the OS drive: need ~{m['size'] >> 20} MiB, "
              f"have {free >> 20} MiB")
        return 1
    print(f"{m['name']}: {src}  →  {dst}  ({m['size'] >> 20} MiB)")
    if dry:
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)        # copy first, delete after
    shutil.copy2(m["acf"], acf_dst)
    Path(m["acf"]).unlink()
    shutil.rmtree(src, ignore_errors=True)
    print(f"moved {m['name']} to the OS drive - start Steam to pick it up")
    return 0


# --------------------------------------------------------------------------- #
#  verify
# --------------------------------------------------------------------------- #
def _fstype(path) -> str:
    """Filesystem type of the mount holding `path` ('' if unknown)."""
    try:
        real = os.path.realpath(path)
        best, typ = "", ""
        for ln in Path("/proc/mounts").read_text().splitlines():
            parts = ln.split()
            mnt = parts[1].replace("\\040", " ")
            if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                best, typ = mnt, parts[2]
        return typ
    except (OSError, IndexError):
        return ""


def _steam_started() -> float:
    """Start time of the running Steam client (0 = not running)."""
    try:
        out = subprocess.run(["pgrep", "-ox", "steam"], capture_output=True, text=True)  # noqa: S603,S607
        pid = out.stdout.split()[0]
        return os.stat(f"/proc/{pid}").st_mtime
    except (OSError, IndexError):
        return 0.0


def check_build(path, kind: str) -> list[str]:
    """Problems with one unpacked build ([] = looks complete)."""
    p = Path(path)
    probs = []
    if kind == "proton":
        if (p / "proton").is_file():
            if not os.access(p / "proton", os.X_OK):
                probs.append("`proton` launcher is not executable")
            if not (p / "toolmanifest.vdf").is_file():
                probs.append("toolmanifest.vdf missing")
            if not ((p / "files").is_dir() or (p / "dist").is_dir()):
                probs.append("no files/ (or dist/) folder - unpack incomplete")
        elif (p / "bin" / "wine").exists():
            pass                              # a Wine build in a shared runners folder
        elif not (p / "toolmanifest.vdf").is_file():
            probs.append("neither a Proton build (no `proton`) nor a Steam tool "
                         "(no toolmanifest.vdf)")
        if (p / "toolmanifest.vdf").is_file() and not (p / "compatibilitytool.vdf").is_file() \
                and p.parent.name == "compatibilitytools.d":
            probs.append("compatibilitytool.vdf missing - Steam won't list it")
    elif kind == "wine":
        if not (p / "bin" / "wine").exists() and not (p / "proton").is_file():
            probs.append("bin/wine missing")
    elif kind == "dxvk":
        if not any((p / a / "d3d11.dll").is_file() for a in ("x64", "x32")):
            probs.append("x64/d3d11.dll missing")
    elif kind == "vkd3d":
        if not any((p / a / "d3d12.dll").is_file() for a in ("x64", "x86")):
            probs.append("x64/d3d12.dll missing")
    return probs


def check_steam_tool(m: dict) -> list[str]:
    """Problems with one installed Valve tool (from its manifest row)."""
    probs = []
    try:
        text = Path(m["acf"]).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["manifest unreadable"]
    flags = re.search(r'"StateFlags"\s+"(\d+)"', text)
    if flags and flags.group(1) != "4":
        probs.append(f"Steam reports it as not fully installed (StateFlags "
                     f"{flags.group(1)}) - let Steam finish / verify it")
    d = Path(m["lib"]) / "steamapps" / "common" / m["installdir"]
    if not d.is_dir():
        return probs + [f"install folder missing: {d}"]
    try:
        empty = not any(d.iterdir())
    except OSError:
        empty = True
    if empty:
        probs.append("install folder is empty")
    elif re.match(r"Proton \d|Proton (Experimental|Hotfix)", m["name"]):
        if not (d / "proton").is_file():
            probs.append("`proton` launcher missing")
        elif not os.access(d / "proton", os.X_OK):
            probs.append("`proton` launcher is not executable")
        if not (d / "toolmanifest.vdf").is_file():
            probs.append("toolmanifest.vdf missing")
    elif (m["name"].startswith("Steam Linux Runtime") and "scout" not in m["name"]
          and not (d / "toolmanifest.vdf").is_file()):
        probs.append("toolmanifest.vdf missing")
    return probs


def verify() -> dict:
    """Check every installed build and Valve tool. Returns
    {'items': [{what, where, ok, problems, notes}], 'bad': n}."""
    items = []
    started = _steam_started()
    for t in targets():
        if not t["present"]:
            continue
        seen = set()
        for kind in KINDS:
            d = t["dirs"].get(kind)
            if not d or d in seen:
                continue
            seen.add(d)
            for name in installed(d):
                path = Path(d) / name
                notes = []
                if t["launcher"] == "steam" and started:
                    try:
                        if path.stat().st_mtime > started:
                            notes.append("installed after Steam started - restart "
                                         "Steam so it shows up under Compatibility")
                    except OSError:
                        pass
                probs = check_build(path, kind)
                items.append({"what": name, "where": t["name"], "ok": not probs,
                              "problems": probs, "notes": notes})
    man = _manifests()
    for row in steam_tools():
        if not row["installed"]:
            continue
        m = man[row["appid"]]
        probs = check_steam_tool(m)
        notes = []
        fs = _fstype(m["lib"])
        if not row["on_os_drive"]:
            notes.append(f"on another drive ({m['lib']}"
                         + (f", {fs}" if fs else "") + ") - “Move to OS drive” relocates it")
        if fs in ("ntfs", "ntfs3", "fuseblk", "exfat", "vfat"):
            notes.append(f"{fs} can't keep Unix permissions or symlinks reliably - a "
                         f"Proton/runtime there is fragile")
        items.append({"what": row["name"], "where": "Steam tool", "ok": not probs,
                      "problems": probs, "notes": notes, "appid": row["appid"]})
    return {"items": items, "bad": sum(1 for i in items if not i["ok"])}


def steam_validate(appid: str) -> int:
    """Ask Steam to verify the files of one of its own tools."""
    return _steam_url(
        f"steam://validate/{appid}",
        f"asking Steam to verify AppID {appid} - progress shows in Steam's Downloads page")


def status() -> dict:
    tg = targets()
    for t in tg:
        inst, seen = [], set()
        for kind in KINDS:
            d = t["dirs"].get(kind)
            if d and d not in seen:
                seen.add(d)
                inst += [{"name": n, "kind": kind} for n in installed(d)]
        t["installed"] = inst
    cfg = _config_vdf()
    try:
        mapping = read_mapping(cfg.read_text(encoding="utf-8", errors="replace")) if cfg else {}
    except OSError:
        mapping = {}
    return {"targets": tg, "steam_tools": steam_tools(), "steam_root": str(steam_root() or ""),
            "compat_names": compat_names(), "compat_default": mapping.get("0", ""),
            "compat_forced": {a: t for a, t in mapping.items() if a != "0"},
            "steam_running": lo.steam_running(),
            "tools": {k: {"launchers": v["launchers"], "about": v["about"],
                          "kind": kind_of(k)} for k, v in TOOLS.items()}}


def main() -> int:
    if os.geteuid() == 0 and not os.environ.get("TUXTHROTTLE_ALLOW_ROOT"):
        sys.exit("run this as your normal user, not root")
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    rl = sub.add_parser("releases")
    rl.add_argument("tool")
    rl.add_argument("--json", action="store_true")
    rl.add_argument("--count", type=int, default=15)
    ins = sub.add_parser("install")
    ins.add_argument("tool")
    ins.add_argument("--tag", default="")
    ins.add_argument("--target", default="Steam")
    ins.add_argument("--force", action="store_true")
    rm = sub.add_parser("remove")
    rm.add_argument("target")
    rm.add_argument("name")
    rm.add_argument("--kind", default="", choices=("",) + KINDS)
    si = sub.add_parser("steam-install")
    si.add_argument("appid")
    sub.add_parser("restart-steam")
    sd = sub.add_parser("set-default")
    sd.add_argument("tool")
    sd.add_argument("--all-games", action="store_true")
    sd.add_argument("--restart-steam", action="store_true")
    sd.add_argument("--dry-run", action="store_true")
    qi = sub.add_parser("queue-install")
    qi.add_argument("appid")
    qi.add_argument("--dry-run", action="store_true")
    vf = sub.add_parser("verify")
    vf.add_argument("--json", action="store_true")
    sv = sub.add_parser("steam-validate")
    sv.add_argument("appid")
    mv = sub.add_parser("move-to-os")
    mv.add_argument("appid")
    mv.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        if args.cmd == "status":
            s = status()
            if args.json:
                print(json.dumps(s))
                return 0
            for t in s["targets"]:
                if t["present"]:
                    print(f"[{t['name']}] {t['dir']}")
                    for n in t["installed"]:
                        print(f"    {n['name']}  ({n['kind']})")
            print("[Steam tools]")
            for r in s["steam_tools"]:
                where = ("not installed" if not r["installed"] else
                         "OS drive" if r["on_os_drive"] else f"OTHER DRIVE: {r['library']}")
                print(f"    {r['appid']:<8} {r['name']:<36} {where}")
            return 0
        if args.cmd == "releases":
            if args.tool not in TOOLS:
                sys.exit(f"unknown tool - one of: {', '.join(TOOLS)}")
            rels = releases(args.tool, args.count)
            if args.json:
                print(json.dumps(rels))
            else:
                for r in rels:
                    print(f"  {r['tag']:<28} {r['date']}  {r['size'] >> 20} MiB  {r['asset']}")
            return 0
        if args.cmd == "install":
            return install(args.tool, args.tag, args.target, args.force)
        if args.cmd == "remove":
            return remove(args.target, args.name, args.kind)
        if args.cmd == "verify":
            res = verify()
            if args.json:
                print(json.dumps(res))
                return 1 if res["bad"] else 0
            for i in res["items"]:
                print(f"  [{'ok ' if i['ok'] else 'BAD'}] {i['what']}  ({i['where']})")
                for pr in i["problems"]:
                    print(f"          ✗ {pr}")
                for nt in i["notes"]:
                    print(f"          · {nt}")
            n = len(res["items"])
            print(f"{n - res['bad']}/{n} OK" + (f", {res['bad']} with problems" if res["bad"] else ""))
            return 1 if res["bad"] else 0
        if args.cmd == "restart-steam":
            return restart_steam()
        if args.cmd == "set-default":
            return set_default(args.tool, args.all_games, args.dry_run, args.restart_steam)
        if not args.appid.isdigit():
            sys.exit("appid must be numeric")
        if args.cmd == "steam-install":
            return steam_install(args.appid)
        if args.cmd == "queue-install":
            return queue_install(args.appid, args.dry_run)
        if args.cmd == "steam-validate":
            return steam_validate(args.appid)
        return move_to_os(args.appid, args.dry_run)
    except (OSError, ValueError, tarfile.TarError) as exc:
        print(f"failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
