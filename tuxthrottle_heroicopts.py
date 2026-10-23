#!/usr/bin/env python3
"""Bulk-set Heroic Games Launcher per-game launch settings from one string.

Takes the same string the TuxThrottle launch-options builder produces for
Steam (`VAR=val ... gamemoderun mangohud %command%`) and writes it into every
Heroic game config (`~/.config/heroic/GamesConfig/<appName>.json`) and the
global defaults (`config.json` `defaultSettings`, used by new games):
`VAR=val` -> `enviromentOptions` (Heroic's own spelling), `gamemoderun` ->
`useGameMode`, `mangohud` -> `showMangohud`, any other wrapper -> `wrapperOptions`.
A gamescope prefix is ignored (Heroic has its own gamescope settings).

Usage:
    python3 tuxthrottle_heroicopts.py list
    python3 tuxthrottle_heroicopts.py set-all  '<string>' [--only-empty] [--dry-run]
    python3 tuxthrottle_heroicopts.py set-all  --b64 <base64-of-string> [--only-empty] [--dry-run]
    python3 tuxthrottle_heroicopts.py remove-token '<string>' [--dry-run]

`__NV_PRIME_RENDER_OFFLOAD` crashes gamescope's Xwayland, and the dGPU is wanted
on every game, so a game that gets it (via `set-all`) has gamescope switched off.

    python3 tuxthrottle_heroicopts.py fix [--dry-run]   # PRIME env on + gamescope off, every game

`--no-wayland` (either command) also sets `enableWineWayland` false on every
game (remove-token turns it back on): Heroic's Wine Wayland driver broke
Rayman 3 on this machine, it is a setting rather than an env var.
    python3 tuxthrottle_heroicopts.py remove-token --b64 <base64-of-string> [--dry-run]

Rules:
  * Heroic must be **closed** - it keeps settings in memory and rewrites them.
  * every game JSON is copied to `*.tuxthrottle-bak-<epoch>` first.
  * `set-all` merges: env keys in the string replace the same key, other env
    keys a game already has are kept. `--only-empty` skips games that already
    have any env/wrapper options.
  * run as your normal user, not root.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG_DIRS = (
    Path.home() / ".config" / "heroic" / "GamesConfig",
    Path.home() / ".var" / "app" / "com.heroicgameslauncher.hgl" / "config"
    / "heroic" / "GamesConfig",
)
BOOL_WRAPPERS = {"gamemoderun": "useGameMode", "mangohud": "showMangohud"}
SKIP_WRAPPERS = {"gamescope", "env", "--", "-f"}


def config_dirs() -> list[Path]:
    return [d for d in CONFIG_DIRS if d.is_dir()]


def game_files() -> list[Path]:
    out: list[Path] = []
    for d in config_dirs():
        out += sorted(p for p in d.glob("*.json") if not p.name.startswith("gog-"))
    return out


def heroic_running() -> bool:
    r = subprocess.run(["pgrep", "-x", "heroic"], capture_output=True)
    return r.returncode == 0


def parse(opts: str) -> tuple[dict[str, str], dict[str, bool], list[str]]:
    """Split a launch-options string into (env, bool switches, extra wrapper argv)."""
    env: dict[str, str] = {}
    flags: dict[str, bool] = {}
    wrap: list[str] = []
    toks = shlex.split(opts)
    if toks and toks[0] == "gamescope":
        # drop `gamescope ... --`; the game's own env/wrappers follow it
        toks = toks[toks.index("--") + 1:] if "--" in toks else []
    for t in toks:
        if t == "%command%":
            break
        if "=" in t and not t.startswith("-") and t.split("=", 1)[0].replace("_", "").isalnum():
            k, v = t.split("=", 1)
            env[k] = os.path.expandvars(v.replace("~", "$HOME", 1) if v.startswith("~") else v)
        elif t in BOOL_WRAPPERS:
            flags[BOOL_WRAPPERS[t]] = True
        elif t not in SKIP_WRAPPERS:
            wrap.append(t)
    return env, flags, wrap


# gamescope starts its own nested Xwayland; with __NV_PRIME_RENDER_OFFLOAD=1 in the
# environment that Xwayland aborts in xwl_glamor_init (seen on the G15, 2026-10-23).
# The dGPU is wanted on every game, so instead of dropping the var the tools keep
# it and switch gamescope off on any game that carries it.
PRIME_ENV = {
    "__NV_PRIME_RENDER_OFFLOAD": "1",
    "__VK_LAYER_NV_optimus": "NVIDIA_only",
    "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
}
GAMESCOPE_FLAGS = ("enableUpscaling", "enableLimiter", "enableForceGrabCursor")


def gamescope_on(g: dict) -> bool:
    gs = g.get("gamescope")
    return isinstance(gs, dict) and any(gs.get(k) for k in GAMESCOPE_FLAGS)


def gamescope_off(g: dict) -> None:
    for k in GAMESCOPE_FLAGS:
        if k in g.get("gamescope", {}):
            g["gamescope"][k] = False


def _load(path: Path) -> tuple[dict, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for k, v in data.items():
        if isinstance(v, dict) and "winePrefix" in v or isinstance(v, dict) and "enviromentOptions" in v:
            return data, k
    return None


def targets() -> list[tuple[str, Path, dict, dict]]:
    """(label, file, whole json, settings dict) for each game + Heroic's global defaults."""
    out = []
    for d in config_dirs():
        cfg = d.parent / "config.json"
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(data.get("defaultSettings"), dict):
                out.append(("GLOBAL", cfg, data, data["defaultSettings"]))
        except (OSError, ValueError):
            pass
    for p in game_files():
        got = _load(p)
        if got:
            out.append((got[1], p, got[0], got[0][got[1]]))
    return out


def _save(path: Path, data: dict) -> None:
    shutil.copy2(path, f"{path}.tuxthrottle-bak-{int(time.time())}")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def do_list() -> int:
    for p in game_files():
        got = _load(p)
        if not got:
            continue
        data, key = got
        g = data[key]
        env = ",".join(e.get("key", "") for e in g.get("enviromentOptions", []))
        print(f"{key}\tgamemode={g.get('useGameMode')}\tmangohud={g.get('showMangohud')}"
              f"\twrappers={len(g.get('wrapperOptions', []))}\tenv=[{env}]")
    return 0


def set_all(opts: str, only_empty: bool, dry: bool, no_wayland: bool = False) -> int:
    env, flags, wrap = parse(opts)
    n = 0
    for key, p, data, g in targets():
        if only_empty and (g.get("enviromentOptions") or g.get("wrapperOptions")):
            print(f"skip {key} (already has options)")
            continue
        cur = {e["key"]: e["value"] for e in g.get("enviromentOptions", []) if "key" in e}
        cur.update(env)
        if "__NV_PRIME_RENDER_OFFLOAD" in cur and gamescope_on(g):
            gamescope_off(g)
            print(f"  {key}: PRIME offload is set, gamescope switched off")
        g["enviromentOptions"] = [{"key": k, "value": v} for k, v in sorted(cur.items())]
        g.update(flags)
        if no_wayland:
            g["enableWineWayland"] = False
        known = {w.get("exe") for w in g.get("wrapperOptions", [])}
        for w in wrap:
            if w not in known:
                g.setdefault("wrapperOptions", []).append({"exe": w, "args": ""})
        print(f"{'would set' if dry else 'set'} {key}")
        if not dry:
            _save(p, data)
        n += 1
    print(f"{n} game(s) {'would be ' if dry else ''}updated")
    return 0


def remove_token(opts: str, dry: bool, no_wayland: bool = False) -> int:
    env, flags, wrap = parse(opts)
    n = 0
    for key, p, data, g in targets():
        before = json.dumps(g, sort_keys=True)
        g["enviromentOptions"] = [e for e in g.get("enviromentOptions", [])
                                  if env.get(e.get("key")) != e.get("value")]
        for f in flags:
            g[f] = False
        if no_wayland:
            g["enableWineWayland"] = True
        g["wrapperOptions"] = [w for w in g.get("wrapperOptions", []) if w.get("exe") not in wrap]
        if json.dumps(g, sort_keys=True) != before:
            print(f"{'would strip' if dry else 'stripped'} {key}")
            if not dry:
                _save(p, data)
            n += 1
    print(f"{n} game(s) {'would be ' if dry else ''}changed")
    return 0


def fix_gamescope(dry: bool) -> int:
    """Put the dGPU (PRIME offload) env back on every game and switch gamescope off."""
    if heroic_running() and not dry:
        print("Heroic is running - close it first (it would overwrite the change)")
        return 1
    n = 0
    for key, p, data, g in targets():
        cur = {e["key"]: e["value"] for e in g.get("enviromentOptions", []) if "key" in e}
        missing = [k for k, v in PRIME_ENV.items() if cur.get(k) != v]
        gs = gamescope_on(g)
        if not missing and not gs:
            continue
        cur.update(PRIME_ENV)
        g["enviromentOptions"] = [{"key": k, "value": v} for k, v in sorted(cur.items())]
        gamescope_off(g)
        print(f"{'would fix' if dry else 'fixed'} {key}"
              f"{' (gamescope off)' if gs else ''}{' (+PRIME env)' if missing else ''}")
        if not dry:
            _save(p, data)
        n += 1
    print(f"{n} target(s) {'would be ' if dry else ''}fixed")
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "set-all", "remove-token", "fix"):
        print(__doc__)
        return 2
    if not config_dirs():
        print("no Heroic GamesConfig directory found")
        return 1
    if a[0] == "list":
        return do_list()
    if a[0] == "fix":
        return fix_gamescope("--dry-run" in a)
    dry = "--dry-run" in a
    rest = [x for x in a[1:] if x not in ("--dry-run", "--only-empty", "--no-wayland")]
    if rest[:1] == ["--b64"] and len(rest) > 1:
        opts = base64.b64decode(rest[1]).decode()
    elif rest:
        opts = rest[0]
    else:
        print("missing launch-options string")
        return 2
    if heroic_running() and not dry:
        print("Heroic is running - close it first (it would overwrite the change)")
        return 1
    if a[0] == "set-all":
        return set_all(opts, "--only-empty" in a, dry, "--no-wayland" in a)
    return remove_token(opts, dry, "--no-wayland" in a)


if __name__ == "__main__":
    sys.exit(main())
