#!/usr/bin/env python3
"""One configurable, persistent home for the shader/pipeline caches that
Proton (DXVK), Mesa (AMD iGPU), the NVIDIA driver, and Steam itself each
maintain separately — so they can all live on a drive/folder of the user's
choosing instead of scattered under ~/.cache and each game's own prefix.

Config: ~/.config/tuxthrottle/shadercache.json
    {"dir": "/path/to/cache", "max_size_gb": 80}
`dir` defaults to ~/.cache/tuxthrottle-shaders when unset; the setting is a
plain file, so it survives a reboot on its own. What actually *uses* the
chosen location:
  - `tuxthrottle.py`'s launch-options builder points MESA_SHADER_CACHE_DIR /
    DXVK_STATE_CACHE_PATH / __GL_SHADER_DISK_CACHE_PATH here (pasted into a
    game's Steam/Lutris launch options — persists because Steam remembers
    per-game launch options; regenerate + re-paste if you move the location).
  - the `NvidiaShaderCache` tweak's boot-time /etc/environment.d file reads
    this config at apply time (system-wide, survives reboot without re-pasting
    anything into any game).
  - `link-steam` below moves Steam's own steamapps/shadercache folder(s) in
    here too and leaves a symlink, so du/backup/quota tools see one place.

Usage:
    tuxthrottle_shadercache.py show [--json]
    tuxthrottle_shadercache.py set <dir> [--size GB]
    tuxthrottle_shadercache.py link-steam [--undo]
    tuxthrottle_shadercache.py rebuild [all|<AppID>]
    tuxthrottle_shadercache.py steam-bg-shaders {on|off|status}
    tuxthrottle_shadercache.py clean [mesa-shader-cache|dxvk-state-cache|
                                       nv-shader-cache|steam-shadercache|all]

`rebuild` deletes **Steam's** shader cache only (the fossilize cache in
`steamapps/shadercache`) so Steam regenerates it from scratch on the next
launch — the Mesa / DXVK / NVIDIA caches are left untouched; `steam-bg-shaders
off` unticks Steam's "Allow background processing of Vulkan shaders" (stops the
`fossilize_replay` background compiles). Both refuse while Steam is running.

Run as the real user (not root); `link-steam` / `clean` refuse while Steam
is running. Nothing here is required — cleaning in particular is optional,
the caches are self-limiting (Mesa/NVIDIA respect a max size; DXVK and
Steam's own cache do not expose one).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from tuxthrottle_prefix_relocate import all_libraries, steam_running

SUBDIRS = ("mesa-shader-cache", "dxvk-state-cache", "nv-shader-cache", "steam-shadercache")
DEFAULT_DIR = "~/.cache/tuxthrottle-shaders"
DEFAULT_SIZE_GB = 80

_STEAM_ROOTS = ["~/.local/share/Steam", "~/.steam/steam", "~/.steam/root"]


def _config_path() -> Path:
    return Path("~/.config/tuxthrottle/shadercache.json").expanduser()


def load() -> dict:
    try:
        d = json.loads(_config_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, sort_keys=True))


def cache_dir() -> Path:
    return Path(load().get("dir") or DEFAULT_DIR).expanduser()


def max_size_gb() -> int:
    try:
        return int(load().get("max_size_gb") or DEFAULT_SIZE_GB)
    except (TypeError, ValueError):
        return DEFAULT_SIZE_GB


def ensure_dirs() -> Path:
    base = cache_dir()
    for s in SUBDIRS:
        (base / s).mkdir(parents=True, exist_ok=True)
    return base


def set_config(directory: str, size_gb: int | None = None) -> Path:
    cfg = load()
    cfg["dir"] = str(Path(directory).expanduser())
    if size_gb is not None:
        cfg["max_size_gb"] = int(size_gb)
    cfg.setdefault("max_size_gb", DEFAULT_SIZE_GB)
    save(cfg)
    base = ensure_dirs()
    # A library the user already ran 'link-steam' on has
    # steamapps/shadercache symlinked into the OLD folder — repoint those, or
    # they dangle the moment the old folder is cleaned/removed and Steam then
    # fails every shader write with "disk write error".
    _relink_steam_shadercache(base)
    return base


def _relink_steam_shadercache(base: Path) -> None:
    """Point every Steam library's `steamapps/shadercache` *symlink* at
    `base/steam-shadercache`. Only touches existing symlinks (libraries the
    user linked before) — never converts a real directory, never needs Steam
    closed (Steam resolves the path fresh on each open)."""
    dest = base / "steam-shadercache"
    root = _find_steam_root()
    if root is None:
        return
    dest.mkdir(parents=True, exist_ok=True)
    for lib in all_libraries(root):
        sc = lib / "steamapps" / "shadercache"
        if not sc.is_symlink():
            continue
        try:
            if sc.resolve(strict=True) == dest.resolve():
                continue                       # already correct
        except OSError:
            pass                               # broken link — fall through
        try:
            sc.unlink()
            sc.symlink_to(dest)
        except OSError:
            pass


def _du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        out = subprocess.run(["du", "-sb", str(path)], capture_output=True,
                             text=True, timeout=30)
        return int(out.stdout.split()[0]) if out.stdout.strip() else 0
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def status() -> dict:
    base = ensure_dirs()
    steam_b = _du_bytes(base / "steam-shadercache")
    rest_b = sum(_du_bytes(base / s) for s in SUBDIRS if s != "steam-shadercache")
    total_b = steam_b + rest_b
    return {
        "dir": str(base),
        "max_size_gb": max_size_gb(),
        "total": _human(total_b),
        "steam_shader_cache": _human(steam_b),
        "other_shader_caches": _human(rest_b),
        "bytes": {"total": total_b, "steam": steam_b, "other": rest_b},
    }


def _find_steam_root() -> Path | None:
    for c in _STEAM_ROOTS:
        p = Path(os.path.expanduser(c))
        if (p / "steamapps" / "libraryfolders.vdf").is_file():
            return p.resolve()
    return None


def link_steam_shadercache() -> tuple[bool, str]:
    """Move each Steam library's steamapps/shadercache into our folder and
    symlink it back, so Steam's own cache lives alongside the others."""
    if steam_running():
        return False, "close Steam first"
    root = _find_steam_root()
    if root is None:
        return False, "no Steam install found"
    base = ensure_dirs()
    dest = base / "steam-shadercache"
    dest.mkdir(parents=True, exist_ok=True)
    linked = []
    for lib in all_libraries(root):
        sc = lib / "steamapps" / "shadercache"
        if sc.is_symlink():
            try:
                if sc.resolve(strict=True) == dest.resolve():
                    continue                   # already linked correctly
            except OSError:
                pass                           # dangling — repair below
            try:
                sc.unlink()
                sc.symlink_to(dest)
                linked.append(str(sc) + " (repaired)")
            except OSError as exc:
                return False, f"could not repair {sc}: {exc}"
            continue
        if sc.is_dir():
            for item in sc.iterdir():
                target = dest / item.name
                if not target.exists():
                    shutil.move(str(item), str(target))
            shutil.rmtree(sc, ignore_errors=True)
        sc.symlink_to(dest)
        linked.append(str(sc))
    if not linked:
        return True, "nothing to do — already linked, or no library has a shadercache folder yet"
    return True, "linked: " + ", ".join(linked)


def steam_link_status() -> dict:
    """Health of each Steam library's steamapps/shadercache vs our folder.
    Per library: 'unlinked' (real dir / absent), 'ok' (symlink → current
    steam-shadercache), or 'broken' (dangling or points somewhere else — Steam
    will fail shader writes with 'disk write error'). `ok` overall = no broken.
    """
    root = _find_steam_root()
    if root is None:
        return {"ok": True, "libs": [], "summary": "no Steam install found"}
    dest = (cache_dir() / "steam-shadercache").resolve()
    libs = []
    for lib in all_libraries(root):
        sc = lib / "steamapps" / "shadercache"
        if sc.is_symlink():
            try:
                tgt = sc.resolve(strict=True)
                state = "ok" if tgt == dest else "broken"
                detail = str(tgt)
            except OSError:
                state, detail = "broken", f"dangling → {os.readlink(sc)}"
        elif sc.exists():
            state, detail = "unlinked", "plain directory"
        else:
            state, detail = "unlinked", "not created yet"
        libs.append({"lib": str(lib), "path": str(sc),
                     "state": state, "detail": detail})
    broken = [x for x in libs if x["state"] == "broken"]
    linked = [x for x in libs if x["state"] == "ok"]
    if broken:
        summary = (f"{len(broken)} broken shadercache link"
                   f"{'s' if len(broken) != 1 else ''} — Steam writes will fail; "
                   f"press “Link Steam cache” to repair")
    elif linked:
        summary = f"{len(linked)}/{len(libs)} librar" \
                  f"{'ies' if len(libs) != 1 else 'y'} linked, all healthy"
    else:
        summary = "not linked (each library keeps its own shadercache)"
    return {"ok": not broken, "libs": libs, "summary": summary,
            "broken": len(broken), "linked": len(linked)}


def unlink_steam_shadercache() -> tuple[bool, str]:
    """Undo link_steam_shadercache: copy the shared folder's content back out
    to a plain directory at each Steam library."""
    if steam_running():
        return False, "close Steam first"
    root = _find_steam_root()
    if root is None:
        return False, "no Steam install found"
    base = ensure_dirs()
    dest = base / "steam-shadercache"
    restored = []
    for lib in all_libraries(root):
        sc = lib / "steamapps" / "shadercache"
        if not sc.is_symlink():
            continue
        sc.unlink()
        sc.mkdir(parents=True, exist_ok=True)
        if dest.is_dir():
            for item in dest.iterdir():
                target = sc / item.name
                if not target.exists():
                    shutil.copy2(item, target) if item.is_file() else shutil.copytree(item, target)
        restored.append(str(sc))
    if not restored:
        return True, "nothing was linked"
    return True, "restored: " + ", ".join(restored)


def _all_steam_shadercache_dirs() -> list[Path]:
    """Every `steamapps/shadercache` across all libraries + our own
    steam-shadercache folder (the symlink target). De-duplicated by resolved
    path so a linked library isn't wiped twice."""
    dirs = [cache_dir() / "steam-shadercache"]
    root = _find_steam_root()
    if root is not None:
        for lib in all_libraries(root):
            sc = lib / "steamapps" / "shadercache"
            if sc.exists() or sc.is_symlink():
                dirs.append(sc)
    seen, out = set(), []
    for d in dirs:
        try:
            key = d.resolve()
        except OSError:
            key = d
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _empty_dir(d: Path) -> bool:
    if not d.is_dir():
        return False
    for child in d.iterdir():
        try:
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        except OSError:
            pass
    return True


def rebuild(target: str = "all") -> tuple[bool, str]:
    """Force a clean rebuild of **Steam's** Vulkan pipeline / shader cache by
    deleting it — the fossilize cache in `steamapps/shadercache` (and our
    `steam-shadercache` folder it's linked to) — so Steam regenerates it on
    the next launch. Leaves the Mesa / DXVK / NVIDIA caches alone.

    `target` is 'all' or a numeric Steam AppID. Refuses while Steam is
    running (it keeps its shader cache open). Only ever deletes inside a
    `steamapps/shadercache` / `steam-shadercache` folder — never a game
    install or a prefix.
    """
    if steam_running():
        return False, "close Steam first (it keeps its shader cache open)"

    ensure_dirs()

    if target and target != "all":
        if not str(target).isdigit():
            return False, f"target must be 'all' or a numeric AppID (got {target!r})"
        hit = 0
        for scdir in _all_steam_shadercache_dirs():
            appdir = scdir / str(target)
            if appdir.exists() or appdir.is_symlink():
                try:
                    shutil.rmtree(appdir) if appdir.is_dir() and not appdir.is_symlink() \
                        else appdir.unlink()
                    hit += 1
                except OSError:
                    pass
        if hit:
            return True, (f"cleared Steam's shader cache for AppID {target} "
                          f"({hit} location{'s' if hit != 1 else ''}); Steam rebuilds "
                          f"it on the next launch")
        return True, f"no Steam shader cache found for AppID {target} — nothing to do"

    cleared = []
    for scdir in _all_steam_shadercache_dirs():
        if _empty_dir(scdir):
            cleared.append(str(scdir))
    if not cleared:
        return True, "Steam's shader cache was already empty"
    return True, ("cleared " + ", ".join(cleared)
                  + " — Steam rebuilds its shader cache on the next launch")


# --- Steam "Allow background processing of Vulkan shaders" ---------------- #
# config.vdf: InstallConfigStore/Software/Valve/Steam/ShaderCacheManager/
#             EnableShaderBackgroundProcessing  ("1" = on, Steam's default)
_STEAM_BG_KEY = "EnableShaderBackgroundProcessing"


def _steam_config_vdf() -> Path | None:
    root = _find_steam_root()
    if root is None:
        return None
    p = root / "config" / "config.vdf"
    return p if p.is_file() else None


def steam_bg_shaders_status() -> str | None:
    """'on' / 'off' / None (no Steam config found)."""
    p = _steam_config_vdf()
    if p is None:
        return None
    try:
        m = re.search(rf'"{_STEAM_BG_KEY}"\s+"([01])"', p.read_text())
    except OSError:
        return None
    if not m:
        return "on"                        # key absent → Steam's default is on
    return "on" if m.group(1) == "1" else "off"


def set_steam_bg_shaders(enabled: bool) -> tuple[bool, str]:
    """Tick / untick Steam → Settings → Downloads → 'Allow background
    processing of Vulkan shaders'. Refuses while Steam runs (it rewrites
    config.vdf on exit and would clobber the change); backs the file up."""
    if steam_running():
        return False, "close Steam first (it rewrites config.vdf on exit)"
    p = _steam_config_vdf()
    if p is None:
        return False, "no Steam config.vdf found"
    try:
        txt = p.read_text()
    except OSError as exc:
        return False, f"cannot read {p}: {exc}"
    want = "1" if enabled else "0"
    pat = re.compile(rf'("{_STEAM_BG_KEY}"\s+")[01](")')
    if pat.search(txt):
        new = pat.sub(rf"\g<1>{want}\g<2>", txt, count=1)
    else:
        block = re.compile(r'("ShaderCacheManager"\s*\n\s*\{)')
        if not block.search(txt):
            return False, 'no "ShaderCacheManager" block in config.vdf to add the key to'
        new = block.sub(rf'\g<1>\n\t\t\t\t\t"{_STEAM_BG_KEY}"\t\t"{want}"', txt, count=1)
    if new == txt:
        return True, f"already {'on' if enabled else 'off'}"
    try:
        p.with_name(p.name + f".tuxthrottle-bak-{int(time.time())}").write_text(txt)
        tmp = p.with_name(p.name + ".tuxthrottle-tmp")
        tmp.write_text(new)
        os.replace(tmp, p)
    except OSError as exc:
        return False, f"write failed: {exc}"
    return True, (f"background processing of Vulkan shaders → {'ON' if enabled else 'OFF'} "
                  f"(restart Steam for it to take effect)")


def clean(which: str = "all") -> tuple[bool, str]:
    if steam_running() and which in ("steam-shadercache", "all"):
        return False, "close Steam first (it may be writing to its shader cache)"
    base = ensure_dirs()
    targets = SUBDIRS if which in ("all", None) else (which,)
    cleared = []
    for name in targets:
        d = base / name
        if not d.is_dir():
            continue
        for child in d.iterdir():
            try:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            except OSError:
                pass
        cleared.append(name)
    if not cleared:
        return True, "nothing to clean"
    return True, "cleared: " + ", ".join(cleared)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")

    st = sub.add_parser("set")
    st.add_argument("dir")
    st.add_argument("--size", type=int, help="max size in GB (Mesa/NVIDIA only)")

    ls = sub.add_parser("link-steam")
    ls.add_argument("--undo", action="store_true")

    lc = sub.add_parser("link-check")
    lc.add_argument("--json", action="store_true")

    rb = sub.add_parser("rebuild",
                        help="delete Steam's shader cache so Steam rebuilds it clean")
    rb.add_argument("target", nargs="?", default="all",
                    help="'all' (default) or a numeric Steam AppID")

    bg = sub.add_parser("steam-bg-shaders",
                        help="Steam 'Allow background processing of Vulkan shaders'")
    bg.add_argument("state", choices=["on", "off", "status"])

    cl = sub.add_parser("clean")
    cl.add_argument("what", nargs="?", default="all",
                    choices=[*SUBDIRS, "all"])

    args = ap.parse_args(argv)

    if args.cmd == "show":
        st_data = status()
        if args.json:
            print(json.dumps(st_data, indent=2))
        else:
            print(f"dir:                 {st_data['dir']}")
            print(f"max size (Mesa/NV):  {st_data['max_size_gb']} GB")
            print(f"total:               {st_data['total']}")
            print(f"  Steam shader cache: {st_data['steam_shader_cache']}")
            print(f"  other caches:       {st_data['other_shader_caches']}"
                  "   (Mesa + DXVK + NVIDIA)")
        return 0
    if args.cmd == "set":
        base = set_config(args.dir, args.size)
        print(f"cache directory -> {base}  (max {max_size_gb()} GB for Mesa/NVIDIA)")
        return 0
    if args.cmd == "link-steam":
        ok, msg = unlink_steam_shadercache() if args.undo else link_steam_shadercache()
        print(msg)
        return 0 if ok else 1
    if args.cmd == "link-check":
        stt = steam_link_status()
        if args.json:
            print(json.dumps(stt, indent=2))
        else:
            print(stt["summary"])
            for x in stt["libs"]:
                print(f"  [{x['state']:8}] {x['path']}  ({x['detail']})")
        return 0 if stt["ok"] else 1
    if args.cmd == "rebuild":
        ok, msg = rebuild(args.target)
        print(msg)
        return 0 if ok else 1
    if args.cmd == "steam-bg-shaders":
        if args.state == "status":
            s = steam_bg_shaders_status()
            print("unknown (no Steam config found)" if s is None else s)
            return 0 if s is not None else 1
        ok, msg = set_steam_bg_shaders(args.state == "on")
        print(msg)
        return 0 if ok else 1
    if args.cmd == "clean":
        ok, msg = clean(args.what)
        print(msg)
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
