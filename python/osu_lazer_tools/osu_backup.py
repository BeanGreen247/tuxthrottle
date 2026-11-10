#!/usr/bin/env python3
"""Back up and restore your osu!lazer data (beatmaps, skins, scores, replays, collections, key bindings, settings).

Usage:
    osu_backup.py backup [--dest DIR] [--settings-only] [--keep N]
    osu_backup.py list [--dest DIR]
    osu_backup.py restore FILE [--yes]

Close osu! first: its database (client.realm) is only consistent while the game is shut.
A full backup is the whole data folder minus logs and caches, as a .tar.zst (with zstd) or .tar.gz.
--settings-only saves just game.ini, framework.ini and input.json (a few KB); key bindings, collections
and everything else live in client.realm and need a full backup.
Restoring a full backup first moves the current data folder aside (osu.before-restore-DATE), so nothing
is deleted.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import osu_common as c

S = c.Style
DEFAULT_DEST = Path.home() / "osu-backups"
EXCLUDE = ("logs", "cache", "client.realm.lock", "client.realm.management", "client.realm.note", "*.bak-*",
           "client_*.realm")
SETTINGS_FILES = ("game.ini", "framework.ini", "input.json")


def folder_size(path: Path, skip: tuple[str, ...] = ("logs", "cache")) -> int:
    total = 0
    for p in path.iterdir():
        if p.name in skip:
            continue
        if p.is_file():
            total += p.stat().st_size
        elif p.is_dir():
            total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return total


def refuse_if_running(force: bool) -> None:
    if c.osu_running() and not force:
        sys.exit("osu! is running: close it first (its database is only safe to copy while the game is shut). "
                 "--force skips this check.")


def backups(dest: Path) -> list[Path]:
    return sorted((p for p in dest.glob("osu-backup-*.tar*") if p.is_file()), key=lambda p: p.name, reverse=True)


def cmd_backup(args) -> int:
    src = c.data_dir()
    if not (src / "client.realm").exists() and not args.settings_only:
        sys.exit(f"{src} doesn't look like an osu!lazer data folder (no client.realm)")
    refuse_if_running(args.force)
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if args.settings_only:
        out = dest / f"osu-backup-{stamp}-settings.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            for name in SETTINGS_FILES:
                if (src / name).is_file():
                    tar.add(src / name, arcname=name)
        print(f"saved {out} ({c.human_size(out.stat().st_size)})")
    else:
        size = folder_size(src)
        free = shutil.disk_usage(dest).free
        print(f"backing up {src} ({c.human_size(size)}) to {dest}")
        if free < size * 1.05:
            sys.exit(f"not enough space in {dest}: {c.human_size(free)} free")
        zstd = shutil.which("zstd") is not None
        out = dest / f"osu-backup-{stamp}.tar.{'zst' if zstd else 'gz'}"
        # beatmap audio and images barely compress, so a fast level is as good as a slow one here
        cmd = ["tar", "-C", str(src), *[f"--exclude=./{e}" for e in EXCLUDE], "-cf", str(out)]
        cmd[1:1] = ["-I", "zstd -T0 -3"] if zstd else ["-z"]
        t0 = time.time()
        res = subprocess.run([*cmd, "."], check=False)
        if res.returncode != 0:
            out.unlink(missing_ok=True)
            sys.exit("tar failed; nothing was saved")
        print(f"saved {out} ({c.human_size(out.stat().st_size)}, {time.time() - t0:.0f} s)")
    if args.keep:
        kind = "-settings" if args.settings_only else ""
        same = [p for p in backups(dest) if p.name.split(".tar")[0].endswith("-settings") == bool(kind)]
        for old in same[args.keep:]:
            old.unlink()
            print(f"removed old backup {old.name}")
    return 0


def cmd_list(args) -> int:
    dest = Path(args.dest).expanduser()
    found = backups(dest) if dest.is_dir() else []
    for p in found:
        print(f"{p.stat().st_size / 1e9:8.2f} GB  {p.name}" if p.stat().st_size > 1e8 else
              f"{c.human_size(p.stat().st_size):>11}  {p.name}")
    if not found:
        print(f"no backups in {dest}")
    return 0


def cmd_restore(args) -> int:
    archive = Path(args.file).expanduser()
    if not archive.is_file():
        sys.exit(f"{archive} not found")
    refuse_if_running(args.force)
    target = c.data_dir()
    settings_only = archive.name.split(".tar")[0].endswith("-settings")
    what = f"overwrite {', '.join(SETTINGS_FILES)} in {target}" if settings_only else \
        f"move {target} to {target.name}.before-restore-* and unpack {archive.name} in its place"
    if not args.yes:
        try:
            if input(f"This will {what}. Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except EOFError:
            return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if settings_only:
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                if m.name in SETTINGS_FILES:
                    if (target / m.name).exists():
                        shutil.copy2(target / m.name, target / f"{m.name}.bak-{stamp}")
                    tar.extract(m, target, filter="data")
                    print(f"restored {m.name}")
        return 0
    if target.exists():
        aside = target.with_name(f"{target.name}.before-restore-{stamp}")
        target.rename(aside)
        print(f"current data moved to {aside}")
    target.mkdir(parents=True)
    flag = ["-I", "zstd -d"] if archive.suffix == ".zst" else ["-z"]
    res = subprocess.run(["tar", *flag, "-xf", str(archive), "-C", str(target)], check=False)
    if res.returncode != 0:
        sys.exit(f"unpacking failed; your previous data is untouched in {target.name}.before-restore-{stamp}")
    print(f"restored into {target}. Once osu! starts fine you can delete the .before-restore folder.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools backup", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("backup", help="make a backup")
    p.add_argument("--dest", default=str(DEFAULT_DEST))
    p.add_argument("--settings-only", action="store_true")
    p.add_argument("--keep", type=int, default=0, help="keep only the newest N backups of this kind")
    p.add_argument("--force", action="store_true", help="don't check whether osu! is running")
    p = sub.add_parser("list", help="list backups")
    p.add_argument("--dest", default=str(DEFAULT_DEST))
    p = sub.add_parser("restore", help="restore a backup")
    p.add_argument("file")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("--force", action="store_true")
    argv = sys.argv[1:] if argv is None else argv
    args = ap.parse_args(argv or ["list"])
    return {"backup": cmd_backup, "list": cmd_list, "restore": cmd_restore}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
