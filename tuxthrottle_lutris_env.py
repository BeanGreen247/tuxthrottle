#!/usr/bin/env python3
"""Put the shader-cache env vars into every installed Lutris Wine game.

Lutris keeps per-game environment variables in `games/<configpath>.yml`
under `system: env:` (the "Environment variables" table in Configure →
System options). This writes the set below into every installed Wine game,
with the cache paths pointing at that game's own Wine prefix, so DXVK and
NVIDIA shader caches survive between launches and stay next to the game:

    DXVK_HUD                            compiler
    DXVK_STATE_CACHE_PATH               <prefix>
    STAGING_SHARED_MEMORY               1
    __GL_SHADER_DISK_CACHE              1
    __GL_SHADER_DISK_CACHE_PATH         <prefix>
    __GL_SHADER_DISK_CACHE_SKIP_CLEANUP 1

Usage:
    python3 tuxthrottle_lutris_env.py list
    python3 tuxthrottle_lutris_env.py apply  [--overwrite] [--dry-run]
    python3 tuxthrottle_lutris_env.py remove [--dry-run]

Rules:
  * `apply` only adds keys a game doesn't have yet; `--overwrite` also
    replaces values the game already sets.
  * `remove` drops only keys whose value is still the one `apply` writes,
    so anything you changed by hand stays.
  * every yml is copied to `*.tuxthrottle-bak-<epoch>` before it's changed.
  * close Lutris first if a game's Configure dialog is open - saving it
    would write the old values back.
  * run as your normal user, not root.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from tuxthrottle_mangohud_launchers import _lutris_yml, _yaml, lutris_dirs


def wanted_env(cache_dir: str) -> dict[str, str]:
    return {
        "DXVK_HUD": "compiler",
        "DXVK_STATE_CACHE_PATH": cache_dir,
        "STAGING_SHARED_MEMORY": "1",
        "__GL_SHADER_DISK_CACHE": "1",
        "__GL_SHADER_DISK_CACHE_PATH": cache_dir,
        "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP": "1",
    }


def wine_games() -> list[dict]:
    """[{name, yml, dir}] for every installed Lutris Wine game with a config."""
    out = []
    for db, dirs in lutris_dirs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("SELECT name, runner, directory, configpath FROM games "
                               "WHERE installed = 1").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for name, runner, directory, configpath in rows:
            if runner != "wine" or not configpath:
                continue
            yml = _lutris_yml(dirs, configpath)
            if yml is not None:
                out.append({"name": name or configpath, "yml": yml, "dir": directory or ""})
    return out


def cache_dir_for(data: dict, directory: str) -> str:
    """The game's Wine prefix, else its Lutris install directory."""
    game = data.get("game")
    prefix = game.get("prefix") if isinstance(game, dict) else None
    return str(prefix or directory or "")


def merge(env: dict, want: dict, overwrite: bool) -> list[str]:
    """Add `want` into `env` in place; return the keys that changed."""
    changed = []
    for k, v in want.items():
        if k in env and (not overwrite or str(env[k]) == v):
            continue
        env[k] = v
        changed.append(k)
    return changed


def unmerge(env: dict, want: dict) -> list[str]:
    """Drop keys from `env` that still hold the value `apply` writes."""
    gone = [k for k, v in want.items() if k in env and str(env[k]) == v]
    for k in gone:
        del env[k]
    return gone


def _process(action: str, overwrite: bool, dry: bool) -> int:
    yaml = _yaml()
    if yaml is None:
        print("PyYAML is not installed (python3-pyyaml) - can't edit Lutris configs.",
              file=sys.stderr)
        return 1
    games = wine_games()
    if not games:
        print("No installed Lutris Wine games found.")
        return 0
    stamp = int(time.time())
    touched = 0
    for g in games:
        p: Path = g["yml"]
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"  ! {g['name']}: can't read {p}: {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        cache = cache_dir_for(data, g["dir"])
        if not cache:
            print(f"  - {g['name']}: no prefix or install directory, skipped")
            continue
        if data.get("system") is None:
            data["system"] = {}
        system = data["system"]
        if not isinstance(system, dict):
            print(f"  ! {g['name']}: unexpected 'system' section, skipped", file=sys.stderr)
            continue
        if system.get("env") is None:
            system["env"] = {}
        env = system["env"]
        if not isinstance(env, dict):
            print(f"  ! {g['name']}: unexpected 'env' section, skipped", file=sys.stderr)
            continue
        want = wanted_env(cache)
        keys = merge(env, want, overwrite) if action == "apply" else unmerge(env, want)
        if action == "remove" and not env:
            del system["env"]
        if not keys:
            print(f"  = {g['name']}: nothing to change")
            continue
        sign = "+" if action == "apply" else "-"
        print(f"  {sign} {g['name']}: {', '.join(keys)}  ({cache})")
        touched += 1
        if dry:
            continue
        shutil.copy2(p, p.with_name(f"{p.name}.tuxthrottle-bak-{stamp}"))
        p.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
                     encoding="utf-8")
    verb = "would change" if dry else "changed"
    print(f"\nDone. {verb} {touched} of {len(games)} Lutris Wine game(s).")
    return 0


def do_list() -> int:
    yaml = _yaml()
    games = wine_games()
    if not games:
        print("No installed Lutris Wine games found.")
        return 0
    for g in games:
        state = "?"
        if yaml is not None:
            try:
                data = yaml.safe_load(g["yml"].read_text(encoding="utf-8")) or {}
                env = ((data.get("system") or {}).get("env") or {})
                want = wanted_env(cache_dir_for(data, g["dir"]))
                have = sum(1 for k, v in want.items() if str(env.get(k)) == v)
                state = f"{have}/{len(want)}"
            except (OSError, yaml.YAMLError, AttributeError):
                pass
        print(f"  {state:>4}  {g['name']}  ({g['yml']})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("list", "apply", "remove"))
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.action == "list":
        return do_list()
    return _process(a.action, a.overwrite, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
