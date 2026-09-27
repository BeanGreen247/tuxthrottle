#!/usr/bin/env python3
"""Apply competitive osu!lazer settings by editing game.ini and framework.ini.

Close osu! before running: lazer rewrites both files on exit and would undo the changes.
Every file is backed up next to itself (e.g. game.ini.bak-20260926-201500) before writing.

Usage:
    python3 osu_lazer_settings.py            # apply
    python3 osu_lazer_settings.py --dry-run  # show changes only
    python3 osu_lazer_settings.py --check    # exit 0 if already applied, 1 if not
    python3 osu_lazer_settings.py --data-dir /path/to/osu
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Key names/values match OsuConfigManager.cs / FrameworkConfigManager.cs in ppy/osu and ppy/osu-framework.
GAME_INI: dict[str, str] = {
    "DimLevel": "1.0",                    # background dim 100 %
    "BlurLevel": "0.0",                   # background blur off
    "HitLighting": "False",
    "StarFountains": "False",
    "BeatmapSkins": "False",
    "BeatmapColours": "False",
    "BeatmapHitsounds": "False",
    "AutoCursorSize": "False",            # cursor size independent of circle size
    "KeyOverlay": "True",
    "HUDVisibilityMode": "Always",
    "GameplayLeaderboard": "False",
    "AlwaysPlayFirstComboBreak": "False",
    "MouseDisableButtons": "True",
    "MouseDisableWheel": "True",
}
FRAMEWORK_INI: dict[str, str] = {
    "FrameSync": "Unlimited",             # or "Limit8x" (8x refresh rate)
    "WindowMode": "Fullscreen",
    # Renderer is left as-is on purpose (OpenGL / Vulkan / Automatic is a per-machine choice).
    "ExecutionMode": "MultiThreaded",
}


def default_data_dir() -> Path:
    base = Path.home() / ".local" / "share" / "osu"
    # A custom storage location (Settings > Maintenance) is recorded in storage.ini as FullPath.
    storage = base / "storage.ini"
    if storage.is_file():
        for line in storage.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "FullPath" and value.strip():
                return Path(value.strip())
    return base


def osu_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-af", "osu"], capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        return False
    return any(("osu!" in ln or "osu.AppImage" in ln or "osu.Desktop" in ln) and "osu_lazer_settings" not in ln
               for ln in out.splitlines())


def same(a: str, b: str) -> bool:
    """Compare ini values numerically when possible, so 0 == 0.0."""
    try:
        return float(a) == float(b)
    except ValueError:
        return a.strip().lower() == b.strip().lower()


def pending(path: Path, wanted: dict[str, str]) -> list[str]:
    """Keys in `wanted` that the ini file doesn't already hold with that value."""
    have: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                have[key.strip()] = value.strip()
    return [k for k, v in wanted.items() if k not in have or not same(have[k], v)]


def apply(path: Path, wanted: dict[str, str], dry_run: bool) -> None:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    remaining = dict(wanted)
    changed = False
    for i, line in enumerate(lines):
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in remaining:
            new = remaining.pop(key)
            if not same(value.strip(), new):
                print(f"  {path.name}: {key}: {value.strip()} -> {new}")
                lines[i] = f"{key} = {new}"
                changed = True
    for key, new in remaining.items():
        print(f"  {path.name}: {key}: (unset) -> {new}")
        lines.append(f"{key} = {new}")
        changed = True
    if not changed:
        print(f"  {path.name}: already up to date")
        return
    if dry_run:
        return
    if path.is_file():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"  backup: {backup}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=None, help="osu! data folder (default: ~/.local/share/osu)")
    ap.add_argument("--dry-run", action="store_true", help="print changes without writing")
    ap.add_argument("--check", action="store_true",
                    help="exit 0 when every setting is already applied, 1 otherwise")
    args = ap.parse_args()

    data_dir = args.data_dir or default_data_dir()
    if not data_dir.is_dir():
        print(f"osu! data folder not found: {data_dir}. Start lazer once first, or pass --data-dir.", file=sys.stderr)
        return 1
    if args.check:
        todo = (pending(data_dir / "game.ini", GAME_INI)
                + pending(data_dir / "framework.ini", FRAMEWORK_INI))
        print("pending: " + ", ".join(todo) if todo else "all applied")
        return 1 if todo else 0
    if not args.dry_run and osu_running():
        print("osu! is running. Close it first; it overwrites its settings on exit.", file=sys.stderr)
        return 1

    print(f"osu! data folder: {data_dir}")
    apply(data_dir / "game.ini", GAME_INI, args.dry_run)
    apply(data_dir / "framework.ini", FRAMEWORK_INI, args.dry_run)
    print("Dry run, nothing written." if args.dry_run else "Done. Start osu! to use the new settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
