#!/usr/bin/env python3
"""Per-game frame-rate limits for Heroic, Lutris and Steam games.

The limiter's environment variables go into each game's own settings (games do
not inherit a launcher's global environment): Heroic `enviromentOptions`, Lutris
`system.env`, Steam `LaunchOptions` (`VAR=val ... %command%`). Two limiters:

  strangle  libstrangle: STRANGLE_FPS=<n> plus the implicit Vulkan layer switch
            (DXVK / Vulkan games under Proton; install the `libstrangle` app in
            the Gaming category, it builds it from source)
  dxvk      DXVK's built-in limiter: DXVK_FRAME_RATE=<n> (D3D8/9/10/11 games,
            nothing to install)

Per game the suggested cap is your saved override
(~/.config/tuxthrottle/fpslimits.json) or else the recommended value in
config/fps_limits.json. Games with neither have no suggestion.

Usage:
    python3 tuxthrottle_fpslimit.py list [--json]
    python3 tuxthrottle_fpslimit.py env <fps> [--method strangle|dxvk]
    python3 tuxthrottle_fpslimit.py apply [--fps N] [--method M] [--dry-run]
    python3 tuxthrottle_fpslimit.py apply --sel <b64> [--method M] [--dry-run]
    python3 tuxthrottle_fpslimit.py remove [--sel <b64>] [--dry-run]
    python3 tuxthrottle_fpslimit.py set '<game name>' <fps>
    python3 tuxthrottle_fpslimit.py unset '<game name>'

`apply` without `--sel` hits every game that has a suggestion (`--fps N` forces
one value on every game). `--sel` is base64 JSON `[{"launcher","id","fps"}, ...]`
(the GUI's ticked rows); a cap that differs from the recommendation is saved as
that game's override. Close Heroic / Lutris / Steam first; every changed file is
backed up as *.tuxthrottle-bak-<epoch>.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECS = HERE / "config" / "fps_limits.json"
OVERRIDES = Path.home() / ".config" / "tuxthrottle" / "fpslimits.json"
METHODS = ("strangle", "dxvk")
LIMIT_KEYS = ("STRANGLE_FPS", "ENABLE_VK_LAYER_TORKEL104_libstrangle", "DXVK_FRAME_RATE")


def env_for(fps: int, method: str) -> dict[str, str]:
    if method == "strangle":
        return {"STRANGLE_FPS": str(fps), "ENABLE_VK_LAYER_TORKEL104_libstrangle": "1"}
    return {"DXVK_FRAME_RATE": str(fps)}


def env_string(fps: int, method: str) -> str:
    return " ".join(f"{k}={v}" for k, v in env_for(fps, method).items())


def _words(s: str) -> set[str]:
    return set("".join(c if c.isalnum() else " " for c in s.lower()).split())


def _norm(name: str) -> str:
    return " ".join(sorted(_words(name)))


def recommended(name: str) -> tuple[int | None, str]:
    data = json.loads(RECS.read_text(encoding="utf-8"))
    w = _words(name)
    for g in data["games"]:
        if all(m in w for m in g["match"]):
            return int(g["fps"]), g.get("note", "")
    return None, ""


def overrides() -> dict[str, int]:
    try:
        return {k: int(v) for k, v in json.loads(OVERRIDES.read_text()).items()}
    except (OSError, ValueError):
        return {}


def _save_overrides(d: dict[str, int]) -> None:
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES.write_text(json.dumps(d, indent=2, sort_keys=True), encoding="utf-8")


def suggested(name: str) -> int | None:
    return overrides().get(_norm(name)) or recommended(name)[0]


def _backup(p: Path) -> None:
    if p.exists():
        shutil.copy2(p, f"{p}.tuxthrottle-bak-{int(time.time())}")


def _running(proc: str) -> bool:
    return subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0


# --------------------------------------------------------------------------- #
#  launchers: each game is {launcher, id, name, ...}; get_env / set_env per launcher
# --------------------------------------------------------------------------- #
def _heroic_games() -> list[dict]:
    import tuxthrottle_mangohud_launchers as ml
    return [{"launcher": "heroic", "id": g["appid"], "name": g["name"], "base": g["base"],
             "store": g["source"]} for g in ml.heroic_games()]


def _heroic_path(g: dict) -> Path:
    return Path(g["base"]) / "GamesConfig" / f"{g['id']}.json"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _lutris_games() -> list[dict]:
    import tuxthrottle_mangohud_launchers as ml
    return [{"launcher": "lutris", "id": g["appid"], "name": g["name"], "yml": g["yml"],
             "store": g["source"]} for g in ml.lutris_games() if g.get("yml")]


_TOOL_PREFIXES = ("Proton", "Steam Linux Runtime", "Steamworks Common")


def _steam_games() -> list[dict]:
    import tuxthrottle_launchopts as lo
    import tuxthrottle_prefix_relocate as pr
    try:
        root = pr.steam_root()
    except SystemExit:
        return []
    out, seen = [], set()
    for lc in lo.find_localconfigs():
        apps = lo._apps_dict(lo._load(lc)) or {}
        for aid, _e in lo._iter_games(apps):
            name = pr.appid_name(root, aid)
            if name.startswith(_TOOL_PREFIXES):
                continue
            if name and aid not in seen:  # no name = not installed
                seen.add(aid)
                out.append({"launcher": "steam", "id": aid, "name": name, "store": "Steam"})
    return out


def _current_env(g: dict) -> dict[str, str]:
    kind = g["launcher"]
    if kind == "heroic":
        cfg = _read_json(_heroic_path(g)).get(g["id"], {})
        return {e["key"]: e["value"] for e in cfg.get("enviromentOptions", []) if "key" in e}
    if kind == "lutris":
        import yaml
        try:
            data = yaml.safe_load(Path(g["yml"]).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
        return {k: str(v) for k, v in ((data.get("system") or {}).get("env") or {}).items()}
    import tuxthrottle_launchopts as lo
    for lc in lo.find_localconfigs():
        apps = lo._apps_dict(lo._load(lc)) or {}
        e = apps.get(g["id"])
        if isinstance(e, dict):
            opts = lo._ci_get(e, "LaunchOptions") or ""
            return dict(t.split("=", 1) for t in opts.split() if "=" in t and not t.startswith("-"))
    return {}


def list_games() -> list[dict]:
    games = _heroic_games()
    try:
        games += _lutris_games()
    except ImportError:
        pass
    games += _steam_games()
    ov = overrides()
    for g in games:
        rec, note = recommended(g["name"])
        g.update(recommended=rec, note=note, override=ov.get(_norm(g["name"])))
        cur = _current_env(g)
        g["current"] = {k: cur[k] for k in LIMIT_KEYS if k in cur}
    return games


def _steam_opts(opts: str, env: dict[str, str] | None) -> str:
    toks = [t for t in opts.split() if t.split("=", 1)[0] not in LIMIT_KEYS]
    if env:
        add = [f"{k}={v}" for k, v in env.items()]
        toks = add + toks if "%command%" in toks else add + ["%command%"] + toks
    return " ".join(toks)


def _write(g: dict, env: dict[str, str] | None, dry: bool) -> bool:
    """Set (env) or strip (None) the limiter vars on one game. True if it changed."""
    kind = g["launcher"]
    if kind == "heroic":
        p = _heroic_path(g)
        data = _read_json(p) or {"version": "v0", "explicit": True}
        cfg = data.setdefault(g["id"], {})
        cur = {e["key"]: e["value"] for e in cfg.get("enviromentOptions", []) if "key" in e}
        new = {k: v for k, v in cur.items() if k not in LIMIT_KEYS}
        new.update(env or {})
        if new == cur:
            return False
        cfg["enviromentOptions"] = [{"key": k, "value": v} for k, v in sorted(new.items())]
        if not dry:
            p.parent.mkdir(parents=True, exist_ok=True)
            _backup(p)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    if kind == "lutris":
        import yaml
        p = Path(g["yml"])
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        s = data.setdefault("system", {})
        cur = {k: str(v) for k, v in (s.get("env") or {}).items()}
        new = {k: v for k, v in cur.items() if k not in LIMIT_KEYS}
        new.update(env or {})
        if new == cur:
            return False
        s["env"] = new
        if new == {}:
            s.pop("env")
        if not dry:
            _backup(p)
            p.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
                         encoding="utf-8")
        return True
    import tuxthrottle_launchopts as lo
    changed = False
    for lc in lo.find_localconfigs():
        cfg = lo._load(lc)
        apps = lo._apps_dict(cfg) or {}
        e = apps.get(g["id"])
        if not isinstance(e, dict):
            continue
        key = next((k for k in e if k.lower() == "launchoptions"), "LaunchOptions")
        old = e.get(key, "") or ""
        new = _steam_opts(old, env)
        if new != old:
            e[key] = new
            changed = True
            if not dry:
                _backup(lc)
                lo._dump(cfg, lc)
    return changed


def _blocked(games: list[dict]) -> str | None:
    for kind, proc in (("heroic", "heroic"), ("lutris", "lutris"), ("steam", "steam")):
        if any(g["launcher"] == kind for g in games) and _running(proc):
            return f"{proc} is running - close it first (it would overwrite the change)"
    return None


def do_apply(sel: list[dict] | None, forced: int | None, method: str, dry: bool,
             remove: bool = False) -> int:
    games = list_games()
    by = {(g["launcher"], g["id"]): g for g in games}
    if sel is None:
        todo = [(g, forced or g["override"] or g["recommended"]) for g in games
                if remove or forced or g["override"] or g["recommended"]]
    else:
        todo = [(by[(s["launcher"], str(s["id"]))], int(s.get("fps") or 0)) for s in sel
                if (s["launcher"], str(s["id"])) in by]
    if not dry and (msg := _blocked([g for g, _f in todo])):
        print(msg)
        return 1
    ov = overrides()
    n = 0
    for g, fps in todo:
        if not remove and fps <= 0:
            continue
        if _write(g, None if remove else env_for(fps, method), dry):
            what = "remove" if remove else f"{method} {fps}"
            print(f"{'would ' if dry else ''}{what}: {g['launcher']} {g['name']}")
            n += 1
        if sel is not None and not remove and not dry and fps != g["recommended"]:
            ov[_norm(g["name"])] = fps
    if sel is not None and not dry:
        _save_overrides(ov)
    print(f"{n} game(s) {'would be ' if dry else ''}changed")
    return 0


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "env", "apply", "remove", "set", "unset"):
        print(__doc__)
        return 2

    def opt(flag: str) -> str | None:
        if flag in a:
            i = a.index(flag)
            v = a[i + 1] if i + 1 < len(a) else ""
            del a[i:i + 2]
            return v
        return None

    method = opt("--method") or "strangle"
    sel_b64 = opt("--sel")
    fps_opt = opt("--fps")
    as_json = "--json" in a
    dry = "--dry-run" in a
    a = [x for x in a if x not in ("--json", "--dry-run")]
    if method not in METHODS:
        print("--method must be strangle or dxvk")
        return 2
    sel = json.loads(base64.b64decode(sel_b64)) if sel_b64 else None
    cmd = a[0]
    if cmd == "list":
        games = list_games()
        if as_json:
            print(json.dumps(games))
        for g in ([] if as_json else games):
            sug = g["override"] or g["recommended"]
            print(f"{g['launcher']}\t{g['name']}\t{'suggest ' + str(sug) if sug else 'no advice'}"
                  f"\tnow {g['current'] or 'no limit'}")
        return 0
    if cmd == "env":
        print(env_string(int(a[1]), method))
        return 0
    if cmd == "apply":
        return do_apply(sel, int(fps_opt) if fps_opt else None, method, dry)
    if cmd == "remove":
        return do_apply(sel, None, method, dry, remove=True)
    ov = overrides()
    if cmd == "set":
        ov[_norm(a[1])] = int(a[2])
        print(f"override saved: {a[1]} = {a[2]} FPS")
    else:
        ov.pop(_norm(a[1]), None)
        print(f"override removed: {a[1]}")
    _save_overrides(ov)
    return 0


if __name__ == "__main__":
    sys.exit(main())
