#!/usr/bin/env python3
"""Bulk save-game backup vault for Steam/Proton prefixes.

A "vault" is a folder you keep on a SEPARATE drive (not the OS / Steam
drive) that holds a copy of each game's save data, laid out as:

    <vault>/<appid>/Documents/...
    <vault>/<appid>/Saved Games/...
    <vault>/<appid>/AppData/Roaming/...
    <vault>/<appid>/AppData/LocalLow/...
    <vault>/<appid>/.tuxthrottle-name        (the game's name, for listing)

Export copies those folders out of a prefix into the vault; import copies
them back in. Either can target one AppID or `all` prefixes. Nothing is
deleted — copies overwrite by newest-wins file copy (shutil.copy2).

Usage:
    python3 tuxthrottle_savevault.py list   <vault>
    python3 tuxthrottle_savevault.py export <vault> <appid>|all
    python3 tuxthrottle_savevault.py import <vault> <appid>|all

Run as your normal user (not root). Close Steam before `import`.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# shared Steam-library / prefix helpers
from tuxthrottle_prefix_relocate import (
    all_prefixes,
    appid_name,
    library_for_appid,
    steam_root,
    steam_running,
)

# folders under pfx/drive_c/users/steamuser/ worth backing up (save/config
# data; deliberately NOT AppData/Local — mostly shader/cache bulk)
SAVE_FOLDERS = ["Documents", "Saved Games", "AppData/Roaming", "AppData/LocalLow"]


def home_dev() -> int:
    return os.stat(os.path.expanduser("~")).st_dev


def check_vault(raw: str, *, create: bool) -> Path:
    p = Path(raw).expanduser()
    # check the drive BEFORE creating anything: walk up to the nearest path
    # that exists and test its filesystem
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        sys.exit(f"path does not exist: {p}")
    if probe.stat().st_dev == home_dev():
        sys.exit("the save vault must be on a SEPARATE drive — not the OS / Steam "
                 f"drive. {p} is on the same filesystem as your home directory.")
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            sys.exit(f"can't create vault folder {p}: {exc}")
    if not p.is_dir():
        sys.exit(f"vault folder does not exist: {p}")
    return p.resolve()


def steamuser(root: Path, appid: str) -> Path | None:
    lib = library_for_appid(root, appid)
    if lib is None:
        return None
    su = (lib / "steamapps" / "compatdata" / appid / "pfx"
          / "drive_c" / "users" / "steamuser")
    return su if su.is_dir() else None


def _stats(d: Path) -> tuple[int, int]:
    n = sz = 0
    for r, _dirs, files in os.walk(d):
        for f in files:
            try:
                sz += os.path.getsize(os.path.join(r, f))
                n += 1
            except OSError:
                pass
    return n, sz


def _human(b: int) -> str:
    x = float(b)
    for u in ("B", "K", "M", "G"):
        if x < 1024 or u == "G":
            return f"{x:.0f}{u}" if u == "B" else f"{x:.1f}{u}"
        x /= 1024
    return f"{x:.1f}G"


def export_one(root: Path, appid: str, vault: Path) -> int:
    name = appid_name(root, appid) or appid
    su = steamuser(root, appid)
    if su is None:
        print(f"  {name} ({appid}): no prefix on disk — launch it once first")
        return 0
    box = vault / appid
    copied = 0
    for rel in SAVE_FOLDERS:
        src = su / rel
        if not src.is_dir():
            continue
        n, sz = _stats(src)
        if n == 0:
            continue
        dst = box / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
        print(f"  {name}: {rel}  ({n} files, {_human(sz)})  ->  vault")
        copied += 1
    if copied:
        try:
            (box / ".tuxthrottle-name").write_text(name + "\n")
        except OSError:
            pass
        print(f"  {name} ({appid}): {copied} folder(s) exported")
    else:
        print(f"  {name} ({appid}): nothing to export")
    return 0


def import_one(root: Path, appid: str, vault: Path) -> int:
    name = appid_name(root, appid) or appid
    box = vault / appid
    if not box.is_dir():
        print(f"  {name} ({appid}): nothing in the vault")
        return 0
    su = steamuser(root, appid)
    if su is None:
        print(f"  {name} ({appid}): no prefix on disk — launch it once first")
        return 0
    got = 0
    for rel in SAVE_FOLDERS:
        src = box / rel
        if not src.is_dir():
            continue
        dst = su / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2)
        n, sz = _stats(src)
        print(f"  {name}: vault  ->  {rel}  ({n} files, {_human(sz)})")
        got += 1
    print(f"  {name} ({appid}): {got} folder(s) imported" if got
          else f"  {name} ({appid}): vault entry holds no known save folders")
    return 0


def do_list(vault: Path) -> int:
    boxes = sorted((p for p in vault.iterdir() if p.is_dir() and p.name.isdigit()),
                   key=lambda p: p.name)
    if not boxes:
        print(f"vault {vault} is empty.")
        return 0
    print(f"vault: {vault}\n")
    for b in boxes:
        nm = ""
        f = b / ".tuxthrottle-name"
        if f.is_file():
            nm = f.read_text(errors="replace").strip()
        folders = [r for r in SAVE_FOLDERS if (b / r).is_dir()]
        n, sz = _stats(b)
        print(f"  {b.name:<10} {nm[:38]:<38} {', '.join(folders) or '(empty)'}"
              f"   {n} files, {_human(sz)}")
    return 0


def main() -> int:
    if os.geteuid() == 0 and not os.environ.get("TUXTHROTTLE_ALLOW_ROOT"):
        sys.exit("run this as your normal user, not root")

    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("list", "export", "import"):
        sys.exit("usage: tuxthrottle_savevault.py list <vault> | "
                 "export <vault> <appid>|all | import <vault> <appid>|all")
    mode, raw_vault = a[0], a[1]
    who = a[2] if len(a) > 2 else "all"

    root = steam_root()

    if mode == "list":
        return do_list(check_vault(raw_vault, create=False))

    vault = check_vault(raw_vault, create=(mode == "export"))

    if mode == "import" and steam_running():
        sys.exit("Steam is running — close Steam (and every game) first, then retry.")

    if who == "all":
        if mode == "export":
            targets = [ap for ap, _n, _s, _l in all_prefixes(root)]
        else:  # import: whatever the vault holds
            targets = [p.name for p in sorted(vault.iterdir())
                       if p.is_dir() and p.name.isdigit()]
        if not targets:
            print("nothing to do.")
            return 0
        print(f"{mode}: {len(targets)} game(s)\n")
        for ap in targets:
            (export_one if mode == "export" else import_one)(root, ap, vault)
        print(f"\ndone — vault: {vault}")
        return 0

    if not who.isdigit():
        sys.exit(f"expected a numeric AppID or 'all', got: {who}")
    rc = (export_one if mode == "export" else import_one)(root, who, vault)
    print(f"vault: {vault}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
