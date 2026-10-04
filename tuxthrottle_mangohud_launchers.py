#!/usr/bin/env python3
"""Games from other launchers + anti-cheat detection, for the MangoHud tab.

Heroic and Lutris both have their own per-game "show MangoHud" setting, so
there is no launch-options hook or gate involved - this module finds their
installed games and flips that setting:

  Heroic  ~/.config/heroic (or the Flatpak's ~/.var/app/…/config/heroic)
          Epic   legendaryConfig/legendary/installed.json
          GOG    gog_store/installed.json  (+ store_cache/gog_library.json titles)
          Amazon nile_config/nile/installed.json
          other  sideload_apps/library.json
          per-game switch: GamesConfig/<appName>.json → "showMangohud"
  Lutris  pga.db (sqlite, `games` table) + games/<configpath>.yml →
          system: {mangohud: true}

Anti-cheat detection is a local, offline scan of a game's install folder for
the files the common kernel/user-mode anti-cheats ship (EasyAntiCheat,
BattlEye, …). An overlay injected into such a game can get the account
flagged, so the tab greys the switch out unless the user forces it. Valve's
VAC has no files in the game folder and is not detected (MangoHud is fine
with it). Results are cached per folder mtime in ~/.cache/tuxthrottle/.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

# name → file/dir name patterns (lower-case; '*' suffix/prefix = startswith/endswith)
ANTICHEATS = (
    ("EasyAntiCheat", ("easyanticheat", "easyanticheat_eos", "easyanticheat*",
                       "start_protected_game.exe")),
    ("BattlEye", ("battleye", "beservice*", "beclient*", "*_be.exe")),
    ("Vanguard", ("vgk.sys", "vgc.exe", "riot vanguard")),
    ("nProtect GameGuard", ("gameguard", "gameguard.des")),
    ("XIGNCODE3", ("xigncode", "x3.xem", "xigncode*")),
    ("PunkBuster", ("pnkbstra.exe", "pnkbstrb.exe", "pb")),
    ("EQU8", ("equ8", "equ8_conf.json")),
    ("Denuvo Anti-Cheat", ("denuvo-anti-cheat*",)),
)
_SCAN_DEPTH = 3
_SCAN_LIMIT = 6000          # directory entries per game - keeps a huge install cheap


def _match(name: str, pat: str) -> bool:
    if pat.endswith("*"):
        return name.startswith(pat[:-1])
    if pat.startswith("*"):
        return name.endswith(pat[1:])
    return name == pat


def scan_anticheat(path) -> str:
    """Name of the anti-cheat found under `path` ('' = none / unreadable)."""
    root = Path(path)
    if not root.is_dir():
        return ""
    seen = 0
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > _SCAN_LIMIT:
                return ""
            name = e.name.lower()
            for ac, pats in ANTICHEATS:
                # the bare "pb" folder is only PunkBuster at the game's top level
                if any(_match(name, p) and (p != "pb" or (depth == 0 and e.is_dir()))
                       for p in pats):
                    return ac
            try:
                if depth + 1 < _SCAN_DEPTH and e.is_dir(follow_symlinks=False):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                pass
    return ""


def _cache_file() -> Path:
    return Path("~/.cache/tuxthrottle/anticheat.json").expanduser()


def anticheat_for(paths: dict) -> dict:
    """{key: anti-cheat name or ''} for {key: install dir}, using a cache keyed
    on the folder's mtime so the scan only reruns after the game changes."""
    cf = _cache_file()
    try:
        cache = json.loads(cf.read_text())
    except (OSError, ValueError):
        cache = {}
    out, dirty = {}, False
    for key, path in paths.items():
        p = str(path or "")
        try:
            mt = int(os.stat(p).st_mtime) if p else 0
        except OSError:
            out[key] = ""
            continue
        hit = cache.get(p)
        if not (isinstance(hit, dict) and hit.get("mtime") == mt):
            hit = {"mtime": mt, "ac": scan_anticheat(p)}
            cache[p] = hit
            dirty = True
        out[key] = hit.get("ac", "")
    if dirty:
        try:
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps(cache))
        except OSError:
            pass
    return out


# --------------------------------------------------------------------------- #
#  Heroic
# --------------------------------------------------------------------------- #
def heroic_dirs() -> list[Path]:
    cands = [Path("~/.config/heroic").expanduser(),
             Path("~/.var/app/com.heroicgameslauncher.hgl/config/heroic").expanduser()]
    return [d for d in cands if d.is_dir()]


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _heroic_installed(base: Path) -> list[tuple[str, str, str, str]]:
    """[(store, appName, title, install_path)] for one Heroic config dir."""
    out = []
    d = _json(base / "legendaryConfig" / "legendary" / "installed.json")
    if isinstance(d, dict):
        for app, e in d.items():
            if isinstance(e, dict):
                out.append(("Epic", app, e.get("title") or app, e.get("install_path", "")))
    titles = {}
    lib = _json(base / "store_cache" / "gog_library.json")
    for g in (lib or {}).get("games", []) if isinstance(lib, dict) else []:
        if isinstance(g, dict):
            titles[str(g.get("app_name"))] = g.get("title", "")
    d = _json(base / "gog_store" / "installed.json")
    for e in (d or {}).get("installed", []) if isinstance(d, dict) else []:
        if isinstance(e, dict) and e.get("appName"):
            app = str(e["appName"])
            path = e.get("install_path", "")
            out.append(("GOG", app, titles.get(app) or Path(path).name or app, path))
    d = _json(base / "nile_config" / "nile" / "installed.json")
    for e in d if isinstance(d, list) else []:
        if isinstance(e, dict) and e.get("id"):
            path = e.get("path", "")
            out.append(("Amazon", str(e["id"]), Path(path).name or str(e["id"]), path))
    d = _json(base / "sideload_apps" / "library.json")
    for e in (d or {}).get("games", []) if isinstance(d, dict) else []:
        if isinstance(e, dict) and e.get("app_name") and e.get("is_installed", True):
            exe = (e.get("install") or {}).get("executable", "")
            out.append(("Sideload", str(e["app_name"]), e.get("title") or str(e["app_name"]),
                        str(Path(exe).parent) if exe else ""))
    return out


def _heroic_conf(base: Path, app: str) -> Path:
    return base / "GamesConfig" / f"{app}.json"


def heroic_games() -> list[dict]:
    rows = []
    for base in heroic_dirs():
        for store, app, title, path in _heroic_installed(base):
            cfg = _json(_heroic_conf(base, app)) or {}
            on = bool((cfg.get(app) or {}).get("showMangohud")) if isinstance(cfg, dict) else False
            rows.append({"id": f"heroic:{app}", "source": f"Heroic · {store}", "appid": app,
                         "name": title, "wanted": on, "enabled": on, "hook": True,
                         "path": path, "base": str(base)})
    return rows


def heroic_set(app: str, on: bool) -> bool:
    """Flip Heroic's own per-game showMangohud. True if a config was written."""
    done = False
    for base in heroic_dirs():
        if not any(a == app for _s, a, _t, _p in _heroic_installed(base)):
            continue
        cf = _heroic_conf(base, app)
        cfg = _json(cf)
        if not isinstance(cfg, dict):
            cfg = {"version": "v0"}
        cfg.setdefault(app, {})["showMangohud"] = bool(on)
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        done = True
    return done


# --------------------------------------------------------------------------- #
#  Lutris
# --------------------------------------------------------------------------- #
def lutris_dirs() -> list[tuple[Path, list[Path]]]:
    """[(pga.db, [game-config dirs])] for the native and Flatpak installs."""
    out = []
    for data, conf in ((Path("~/.local/share/lutris"), Path("~/.config/lutris")),
                       (Path("~/.var/app/net.lutris.Lutris/data/lutris"),
                        Path("~/.var/app/net.lutris.Lutris/config/lutris"))):
        db = data.expanduser() / "pga.db"
        if db.is_file():
            out.append((db, [conf.expanduser() / "games", data.expanduser() / "games"]))
    return out


def _lutris_yml(dirs: list[Path], configpath: str) -> Path | None:
    for d in dirs:
        p = d / f"{configpath}.yml"
        if p.is_file():
            return p
    return None


def _yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:
        return None


def lutris_games() -> list[dict]:
    yaml = _yaml()
    rows = []
    for db, dirs in lutris_dirs():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            found = con.execute("SELECT id, name, runner, directory, configpath FROM games "
                                "WHERE installed = 1").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for gid, name, runner, directory, configpath in found:
            if runner == "steam" or not configpath:
                continue                      # Steam games are listed from Steam itself
            yml = _lutris_yml(dirs, configpath)
            on = False
            if yaml is not None and yml is not None:
                try:
                    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
                    on = bool((data.get("system") or {}).get("mangohud"))
                except (OSError, yaml.YAMLError, AttributeError):
                    pass
            rows.append({"id": f"lutris:{gid}", "source": f"Lutris · {runner or '?'}",
                         "appid": str(gid), "name": name or configpath, "wanted": on,
                         "enabled": on, "hook": True, "path": directory or "",
                         "yml": str(yml) if yml else ""})
    return rows


def lutris_set(gid: str, on: bool) -> bool:
    yaml = _yaml()
    if yaml is None:
        return False
    for g in lutris_games():
        if g["appid"] != str(gid) or not g["yml"]:
            continue
        p = Path(g["yml"])
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return False
            if not isinstance(data.get("system"), dict):
                data["system"] = {}
            data["system"]["mangohud"] = bool(on)
            p.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False),
                         encoding="utf-8")
            return True
        except (OSError, yaml.YAMLError):
            return False
    return False


def other_games() -> list[dict]:
    return heroic_games() + lutris_games()


def set_enabled(game_id: str, on: bool) -> bool:
    src, _, rest = game_id.partition(":")
    if src == "heroic":
        return heroic_set(rest, on)
    if src == "lutris":
        return lutris_set(rest, on)
    return False
