#!/usr/bin/env python3
"""Move a Steam/Proton compatdata prefix off a filesystem that can't host it.

NTFS (ntfs3, mounted with `windows_names`) and exFAT/FAT reject ':' in a
filename, so Proton can't create the `pfx/dosdevices/c:` … drive-letter
symlinks and the prefix build dies with:

    OSError: [Errno 22] Invalid argument:
        '<lib>' -> '<lib>/steamapps/compatdata/<appid>/pfx/dosdevices/s:'

…and the game never starts. Steam has no "prefix elsewhere" option, so the
fix is to keep the game where it is, move just its `compatdata/<appid>`
directory onto a Linux-native drive, and drop a symlink back in its place
(the symlink name is the numeric appid — no ':' — so NTFS accepts it).

Usage:
    python3 tuxthrottle_prefix_relocate.py <appid>            # do the move
    python3 tuxthrottle_prefix_relocate.py <appid> --check    # exit 0 if OK,
                                                              # 1 if it needs moving
    python3 tuxthrottle_prefix_relocate.py --scan             # list every prefix
                                                              # and its status
    python3 tuxthrottle_prefix_relocate.py --all              # relocate every
                                                              # at-risk prefix

It also handles save files that ended up on another drive: a Windows
"known folder" inside a prefix (Documents / Saved Games / AppData / …) that
is a symlink onto a *different* filesystem (a common dual-boot save-sharing
setup, or left behind when a game was moved between drives). Those are
pulled back into the prefix, with the off-drive copy left untouched.

    python3 tuxthrottle_prefix_relocate.py --saves-scan          # list stranded saves
    python3 tuxthrottle_prefix_relocate.py --saves <appid>       # pull one game's in
    python3 tuxthrottle_prefix_relocate.py --saves-all           # pull all in
    python3 tuxthrottle_prefix_relocate.py --saves-import <appid> # copy a drive's
                                                                 # loose Documents
                                                                 # into <appid>'s prefix

Run it as the real user (not root); close Steam / the game first.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

STEAM_ROOTS = ["~/.local/share/Steam", "~/.steam/steam", "~/.steam/root"]


def steam_root() -> Path:
    for c in STEAM_ROOTS:
        p = Path(os.path.expanduser(c))
        if (p / "steamapps" / "libraryfolders.vdf").is_file():
            return p.resolve()
    sys.exit("no Steam install found (looked in %s)" % ", ".join(STEAM_ROOTS))


def all_libraries(root: Path) -> list[Path]:
    """Every library-folder path in libraryfolders.vdf (root's own included)."""
    vdf = (root / "steamapps" / "libraryfolders.vdf").read_text(
        encoding="utf-8", errors="replace")
    libs = [Path(p) for p in re.findall(r'"path"\s*"([^"]+)"', vdf)]
    if root not in libs:
        libs.insert(0, root)
    return [p for p in libs if p.is_dir()]


def library_for_appid(root: Path, appid: str) -> Path | None:
    """The library-folder whose libraryfolders.vdf block lists <appid>, or
    (fallback) whichever library actually has its appmanifest on disk."""
    vdf = (root / "steamapps" / "libraryfolders.vdf").read_text(
        encoding="utf-8", errors="replace")
    best: str | None = None
    for m in re.finditer(r'"path"\s*"([^"]+)"(.*?)(?=\n\t*"\d+"\s*\n\t*{|\Z)',
                         vdf, re.S):
        path, body = m.group(1), m.group(2)
        if re.search(r'"%s"\s' % re.escape(appid), body):
            best = path
    if best:
        return Path(best)
    for lib in all_libraries(root):
        if (lib / "steamapps" / f"appmanifest_{appid}.acf").is_file() or \
           (lib / "steamapps" / "compatdata" / appid).exists():
            return lib
    return None


def appid_name(root: Path, appid: str) -> str:
    for lib in all_libraries(root):
        acf = lib / "steamapps" / f"appmanifest_{appid}.acf"
        try:
            m = re.search(r'"name"\s*"([^"]+)"', acf.read_text(errors="replace"))
            if m:
                return m.group(1)
        except OSError:
            pass
    return ""


def colon_ok(directory: Path) -> bool:
    """True if `directory`'s filesystem allows ':' in a filename."""
    probe = directory / "tuxthrottle_colon_probe:tmp"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def classify(lib: Path, appid: str) -> tuple[str, Path]:
    """Return (status, prefix_path). status ∈ {symlink, absent, ok, needs-fix}."""
    src = lib / "steamapps" / "compatdata" / appid
    if src.is_symlink():
        return "symlink", src
    probe = src if src.is_dir() else (lib / "steamapps" / "compatdata")
    if not probe.exists():
        return "absent", src
    return ("ok" if colon_ok(probe) else "needs-fix"), src


def steam_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "steam"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def all_prefixes(root: Path) -> list[tuple[str, str, str, Path]]:
    """(appid, name, status, library) for every compatdata prefix, deduped."""
    seen: set[str] = set()
    rows: list[tuple[str, str, str, Path]] = []
    for lib in all_libraries(root):
        cd = lib / "steamapps" / "compatdata"
        if not cd.is_dir():
            continue
        for entry in sorted(cd.iterdir(), key=lambda p: p.name):
            if not entry.name.isdigit() or entry.name in seen or entry.name == "0":
                continue
            seen.add(entry.name)
            status, _ = classify(lib, entry.name)
            rows.append((entry.name, appid_name(root, entry.name) or "?",
                         status, lib))
    return rows


def do_scan(root: Path) -> int:
    rows = all_prefixes(root)
    if not rows:
        print("no Proton prefixes found.")
        return 0
    w = max(len(n) for n, *_ in rows)
    need = 0
    for appid, name, status, lib in rows:
        flag = {"needs-fix": "NEEDS FIX", "symlink": "relocated",
                "ok": "ok", "absent": "not built"}[status]
        if status == "needs-fix":
            need += 1
        print(f"  {appid:<{w}}  {flag:<10}  {name[:44]:<44}  {lib}")
    print()
    if need:
        print(f"{need} prefix(es) NEED FIX — run:  "
              f"python3 tuxthrottle_prefix_relocate.py <appid>   (or --all)   "
              f"(Steam closed)")
    else:
        print("all prefixes are fine.")
    return 0


class RelocateError(RuntimeError):
    pass


def relocate_one(root: Path, appid: str, lib: Path | None = None) -> None:
    """Move compatdata/<appid> off a colon-hostile filesystem and symlink it
    back. Raises RelocateError on any blocker."""
    if lib is None:
        lib = library_for_appid(root, appid)
    if lib is None:
        raise RelocateError(f"appid {appid} not found in any Steam library")
    status, src = classify(lib, appid)
    dst = root / "steamapps" / "compatdata" / appid
    if status != "needs-fix":
        raise RelocateError(f"{appid}: status is '{status}', not moving")
    if src.resolve() == dst.resolve():
        raise RelocateError(
            f"{appid}: its library ({lib}) is the bad filesystem AND the main "
            f"Steam library — move the game to a Linux drive instead")
    if not src.exists():
        raise RelocateError(f"{appid}: {src} vanished")
    if dst.exists() or dst.is_symlink():
        raise RelocateError(f"{appid}: {dst} already exists — clear it first")
    name = appid_name(root, appid) or appid
    print(f"moving prefix for {name}:\n  {src}\n    ->  {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    os.symlink(str(dst), str(src))
    print(f"  done — {src} is now a symlink to {dst}")


def do_one(root: Path, appid: str, check: bool) -> int:
    lib = library_for_appid(root, appid)
    if lib is None:
        print(f"appid {appid} not found in any Steam library — nothing to do "
              f"(is it installed? launched once?)")
        return 0

    status, src = classify(lib, appid)
    if status == "symlink":
        print(f"OK — prefix already relocated: {src} -> {os.readlink(src)}")
        return 0
    if status == "absent":
        print(f"OK — no prefix on disk yet ({src} absent); it'll build in place.")
        return 0
    if status == "ok":
        print(f"OK — {src} is on a filesystem that allows ':' in names; "
              f"no relocation needed.")
        return 0

    if check:
        print(f"NEEDS FIX — {src} is on a filesystem that rejects ':' in "
              f"filenames; Proton can't build drive-letter links there.")
        return 1

    if steam_running():
        sys.exit("Steam is running — close Steam (and the game) first, then retry.")
    try:
        relocate_one(root, appid, lib)
    except RelocateError as exc:
        sys.exit(str(exc))
    print("re-launch the game from Steam; the prefix will finish building on "
          "the Linux drive.")
    return 0


def do_all(root: Path) -> int:
    targets = [(a, n, lib) for a, n, st, lib in all_prefixes(root)
               if st == "needs-fix"]
    if not targets:
        print("no prefixes need relocating — nothing to do.")
        return 0
    print(f"{len(targets)} prefix(es) to relocate:")
    for a, n, _ in targets:
        print(f"  {a}  {n}")
    print()
    if steam_running():
        sys.exit("Steam is running — close Steam (and every game) first, then retry.")
    ok = 0
    fails: list[str] = []
    for appid, _name, lib in targets:
        try:
            relocate_one(root, appid, lib)
            ok += 1
        except (RelocateError, OSError) as exc:
            print(f"  SKIP {appid}: {exc}")
            fails.append(str(exc))
    print(f"\nrelocated {ok}/{len(targets)}."
          + (f"  {len(fails)} skipped." if fails else ""))
    print("re-launch each game from Steam; prefixes rebuild on the Linux drive.")
    return 1 if fails else 0


# --------------------------------------------------------------------------- #
#  Save files stranded on another drive
# --------------------------------------------------------------------------- #

# Windows "known folders" under pfx/drive_c/users/steamuser/ where games keep
# saves/config. If one of these is a symlink onto a *different* filesystem,
# the saves live off the prefix's drive.
KNOWN_FOLDERS = [
    "Documents", "My Documents", "Saved Games", "Desktop",
    "AppData/Local", "AppData/LocalLow", "AppData/Roaming",
]


def _dev(p: Path) -> int:
    try:
        return p.stat().st_dev
    except OSError:
        return -1


def stray_saves(root: Path, appid: str) -> list[tuple[str, Path, Path]]:
    """(known-folder, symlink path, resolved off-drive target) for this game."""
    lib = library_for_appid(root, appid)
    if lib is None:
        return []
    pfx = (lib / "steamapps" / "compatdata" / appid / "pfx").resolve()
    su = pfx / "drive_c" / "users" / "steamuser"
    if not su.is_dir():
        return []
    home_dev = _dev(pfx)
    out = []
    for kf in KNOWN_FOLDERS:
        link = su / kf
        if not link.is_symlink():
            continue
        target = link.resolve()
        if not target.exists() or not target.is_dir():
            continue
        # only care about links that leave the prefix onto another filesystem
        if _dev(target) != home_dev and su not in target.parents:
            out.append((kf, link, target))
    return out


def pull_saves_one(root: Path, appid: str) -> int:
    strays = stray_saves(root, appid)
    name = appid_name(root, appid) or appid
    if not strays:
        print(f"{name} ({appid}): no stray save folders — nothing to do.")
        return 0
    moved = 0
    for kf, link, target in strays:
        staging = link.with_name(link.name + ".tuxthrottle-pull")
        print(f"{name}: {kf}  <-  {target}")
        if staging.exists():
            print(f"  SKIP — {staging} already exists"); continue
        try:
            shutil.copytree(target, staging, symlinks=True,
                            ignore_dangling_symlinks=True)
            link.unlink()
            staging.rename(link)
        except OSError as exc:
            print(f"  FAILED: {exc}")
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            continue
        print(f"  pulled into the prefix; old copy left at {target}")
        moved += 1
    if moved:
        print(f"{name}: {moved} folder(s) pulled in. Verify saves in-game, then "
              f"the old off-drive copies can be deleted.")
    return 0


LOOSE_DIR_NAMES = ["Documents", "My Documents", "My Games", "Saved Games"]


def mount_point(path: Path) -> Path:
    p = path.resolve()
    try:
        dev = p.stat().st_dev
        while p != p.parent and p.parent.stat().st_dev == dev:
            p = p.parent
    except OSError:
        pass
    return p


def _has_files(d: Path) -> bool:
    try:
        for _r, _dirs, files in os.walk(d):
            if files:
                return True
    except OSError:
        pass
    return False


def loose_save_dirs(root: Path) -> list[Path]:
    """Drive-root Documents/My Games/… folders (outside any steamapps tree and
    outside home) — left behind when a prefix used to redirect its Documents
    onto another drive. e.g. /mnt/Data/Documents ."""
    home = Path.home().resolve()
    seen: set[Path] = set()
    out: list[Path] = []
    for lib in all_libraries(root):
        mp = mount_point(lib)
        if mp in seen or mp == Path("/") or mp == home or home in mp.parents \
           or mp in home.parents:
            seen.add(mp)
            continue
        seen.add(mp)
        for nm in LOOSE_DIR_NAMES:
            d = mp / nm
            if d.is_dir() and not d.is_symlink() and _has_files(d):
                out.append(d)
    return out


def do_saves_scan(root: Path) -> int:
    hits = 0
    for appid, name, _st, _lib in all_prefixes(root):
        strays = stray_saves(root, appid)
        if not strays:
            continue
        hits += 1
        print(f"  {appid}  {name}")
        for kf, _link, target in strays:
            print(f"      {kf:<18} -> {target}")
    if hits:
        print(f"\n{hits} game(s) have a save folder symlinked onto another drive"
              f" — run:  tuxthrottle_prefix_relocate.py --saves <appid>  (or --saves-all)")

    loose = loose_save_dirs(root)
    if loose:
        print("\nLoose save folders sitting on a Steam drive (a prefix used to "
              "redirect Documents here; the redirect is gone but the files "
              "remain):")
        for d in loose:
            subs = sorted(p.name for p in d.iterdir() if p.is_dir())
            print(f"  {d}")
            for s in subs:
                print(f"      {s}")
        print("  These can't be auto-matched to a game. Launch the game, note "
              "which subfolder is its saves, then run:\n"
              "      tuxthrottle_prefix_relocate.py --saves-import <appid>\n"
              "  which copies that drive's loose Documents into <appid>'s prefix.")

    if not hits and not loose:
        print("no save folders found on another drive.")
    return 0


def import_loose_one(root: Path, appid: str) -> int:
    lib = library_for_appid(root, appid)
    if lib is None:
        sys.exit(f"appid {appid} not found in any Steam library")
    name = appid_name(root, appid) or appid
    pfx = (lib / "steamapps" / "compatdata" / appid / "pfx")
    docs = pfx / "drive_c" / "users" / "steamuser" / "Documents"
    if not docs.is_dir():
        sys.exit(f"{name}: no prefix Documents folder at {docs} — launch the "
                 f"game once first")
    mp = mount_point(lib)
    srcs = [mp / nm for nm in LOOSE_DIR_NAMES
            if (mp / nm).is_dir() and not (mp / nm).is_symlink() and _has_files(mp / nm)]
    if not srcs:
        print(f"{name}: no loose save folders on {mp} — nothing to import.")
        return 0
    total = 0
    for src in srcs:
        print(f"{name}: importing {src}  ->  {docs}")
        for item in src.iterdir():
            dst = docs / item.name
            try:
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True,
                                    copy_function=shutil.copy2)
                elif not dst.exists():
                    shutil.copy2(item, dst)
                total += 1
                print(f"    + {item.name}")
            except OSError as exc:
                print(f"    ! {item.name}: {exc}")
    print(f"{name}: imported {total} item(s); the originals on {mp} are "
          f"untouched. Verify saves in-game before deleting them.")
    return 0


def do_saves_all(root: Path) -> int:
    targets = [a for a, _n, _s, _l in all_prefixes(root) if stray_saves(root, a)]
    if not targets:
        print("no save folders on another drive — nothing to do.")
        return 0
    if steam_running():
        sys.exit("Steam is running — close Steam (and every game) first, then retry.")
    for appid in targets:
        pull_saves_one(root, appid)
    print(f"\ndone — {len(targets)} game(s) processed.")
    return 0


def main() -> int:
    if os.geteuid() == 0 and not os.environ.get("TUXTHROTTLE_ALLOW_ROOT"):
        sys.exit("run this as your normal user, not root (it edits ~/.local/share/Steam)")

    root = steam_root()
    if "--scan" in sys.argv:
        return do_scan(root)
    if "--all" in sys.argv:
        return do_all(root)
    if "--saves-scan" in sys.argv:
        return do_saves_scan(root)
    if "--saves-all" in sys.argv:
        return do_saves_all(root)

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--saves-import" in sys.argv:
        if not args or not args[0].isdigit():
            sys.exit("usage: tuxthrottle_prefix_relocate.py --saves-import <appid>")
        if steam_running():
            sys.exit("Steam is running — close Steam and the game first, then retry.")
        return import_loose_one(root, args[0])
    if "--saves" in sys.argv:
        if not args or not args[0].isdigit():
            sys.exit("usage: tuxthrottle_prefix_relocate.py --saves <appid>")
        if steam_running():
            sys.exit("Steam is running — close Steam and the game first, then retry.")
        return pull_saves_one(root, args[0])
    if not args or not args[0].isdigit():
        sys.exit("usage: tuxthrottle_prefix_relocate.py <appid> [--check] | --scan "
                 "| --all | --saves-scan | --saves <appid> | --saves-all "
                 "| --saves-import <appid>")
    return do_one(root, args[0], "--check" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
