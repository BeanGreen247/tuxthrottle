#!/usr/bin/env python3
"""Bulk-set Lutris per-game system options from one launch-options string.

Same string as the Steam/Heroic builder (`VAR=val ... gamemoderun mangohud %command%`).
Written into every game yml (`~/.local/share/lutris/games/*.yml`, `system:` block) and
the global defaults (`~/.config/lutris/system.yml`): `VAR=val` -> `system.env`,
`gamemoderun` -> `system.gamemode`, `mangohud` -> `system.mangohud`, other
wrappers -> `system.prefix_command`. A gamescope prefix is ignored.

Usage:
    python3 tuxthrottle_lutrisopts.py list
    python3 tuxthrottle_lutrisopts.py set-all  --b64 <base64-of-string> [--only-empty] [--dry-run]
    python3 tuxthrottle_lutrisopts.py remove-token --b64 <base64-of-string> [--dry-run]

Lutris must be closed. Each file is copied to `*.tuxthrottle-bak-<epoch>` first.
set-all merges env keys; the installer `script:` block of a game is never touched.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from tuxthrottle_heroicopts import GAMESCOPE_UNSAFE_ENV, parse

GAMES = Path.home() / ".local" / "share" / "lutris" / "games"
GLOBAL = Path.home() / ".config" / "lutris" / "system.yml"


def targets() -> list[tuple[str, Path]]:
    out = [("GLOBAL", GLOBAL)] if GLOBAL.parent.is_dir() else []
    out += [(p.stem, p) for p in sorted(GAMES.glob("*.yml"))] if GAMES.is_dir() else []
    return out


def _load(p: Path) -> dict:
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _save(p: Path, data: dict) -> None:
    if p.exists():
        shutil.copy2(p, f"{p}.tuxthrottle-bak-{int(time.time())}")
    p.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False), encoding="utf-8")


def do_list() -> int:
    for label, p in targets():
        s = _load(p).get("system") or {}
        print(f"{label}\tgamemode={s.get('gamemode')}\tmangohud={s.get('mangohud')}"
              f"\tprefix_command={s.get('prefix_command')!r}\tenv={sorted(s.get('env') or {})}")
    return 0


def set_all(opts: str, only_empty: bool, dry: bool) -> int:
    env, flags, wrap = parse(opts)
    n = 0
    for label, p in targets():
        data = _load(p)
        s = data.setdefault("system", {})
        if only_empty and (s.get("env") or s.get("prefix_command")):
            print(f"skip {label} (already has options)")
            continue
        s.setdefault("env", {}).update(env)
        if s.get("gamescope"):  # gamescope's Xwayland aborts with PRIME offload set
            for k in GAMESCOPE_UNSAFE_ENV:
                s["env"].pop(k, None)
        if flags.get("useGameMode"):
            s["gamemode"] = True
        if flags.get("showMangohud"):
            s["mangohud"] = True
        cur = (s.get("prefix_command") or "").split()
        for w in wrap:
            if w not in cur:
                cur.append(w)
        if cur:
            s["prefix_command"] = " ".join(cur)
        print(f"{'would set' if dry else 'set'} {label}")
        if not dry:
            _save(p, data)
        n += 1
    print(f"{n} target(s) {'would be ' if dry else ''}updated")
    return 0


def remove_token(opts: str, dry: bool) -> int:
    env, flags, wrap = parse(opts)
    n = 0
    for label, p in targets():
        data = _load(p)
        s = data.get("system")
        if not isinstance(s, dict):
            continue
        before = yaml.safe_dump(s)
        e = s.get("env") or {}
        for k, v in env.items():
            if str(e.get(k)) == v:
                e.pop(k)
        if flags.get("useGameMode"):
            s.pop("gamemode", None)
        if flags.get("showMangohud"):
            s.pop("mangohud", None)
        pc = [w for w in (s.get("prefix_command") or "").split() if w not in wrap]
        if pc:
            s["prefix_command"] = " ".join(pc)
        else:
            s.pop("prefix_command", None)
        if yaml.safe_dump(s) != before:
            print(f"{'would strip' if dry else 'stripped'} {label}")
            if not dry:
                _save(p, data)
            n += 1
    print(f"{n} target(s) {'would be ' if dry else ''}changed")
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "set-all", "remove-token"):
        print(__doc__)
        return 2
    if a[0] == "list":
        return do_list()
    dry = "--dry-run" in a
    rest = [x for x in a[1:] if x not in ("--dry-run", "--only-empty")]
    if rest[:1] == ["--b64"] and len(rest) > 1:
        opts = base64.b64decode(rest[1]).decode()
    elif rest:
        opts = rest[0]
    else:
        print("missing launch-options string")
        return 2
    if not dry and subprocess.run(["pgrep", "-x", "lutris"], capture_output=True).returncode == 0:
        print("Lutris is running - close it first")
        return 1
    return set_all(opts, "--only-empty" in a, dry) if a[0] == "set-all" else remove_token(opts, dry)


if __name__ == "__main__":
    sys.exit(main())
