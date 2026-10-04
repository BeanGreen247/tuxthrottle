#!/usr/bin/env python3
"""Per-game MangoHud - the overlay is opt-in, one Steam game at a time.

MangoHud is no longer enabled session-wide (no `MANGOHUD=1` in
environment.d). Per-game control works **live, with Steam running**, through
a gate: `/usr/local/bin/mangohud` (ahead of /usr/bin on PATH) is a small
script that every `mangohud %command%` launch-options wrapper lands on. It
reads Steam's own `SteamAppId` and only hands over to the real MangoHud when

    ~/.config/tuxthrottle/mangohud-games/<appid>.on

exists - otherwise it just runs the game. So switching a game on/off is
creating/removing one flag file; it applies at the game's next launch and
Steam never has to be closed. A game's own overlay settings live in

    ~/.config/MangoHud/tuxthrottle-<appid>.conf

- the game's own *profile*. When that file exists the gate exports it as
MANGOHUD_CONFIGFILE, so it takes priority over the global MangoHud.conf for
that game only. Switching a profile off parks it as `…conf.off` (kept, not
deleted) and the game falls back to the global one. The profile's layout and
placement are edited in the overlay editor on the MangoHud tab.

The only thing that still needs Steam closed is *adding* the `mangohud`
wrapper to a game whose Launch Options don't carry it yet (Steam rewrites
localconfig.vdf on exit). Without the gate installed, the module falls back
to editing Launch Options for every change.

Usage:
    tuxthrottle_mangohud_games.py list [--json]
    tuxthrottle_mangohud_games.py enable <appid> [--own-settings]
            [--position P] [--detail global|fps_only|full]
            [--font-size N] [--fps-limit N] [--dry-run]
    tuxthrottle_mangohud_games.py disable <appid> [--dry-run]
    tuxthrottle_mangohud_games.py disable-all [--dry-run]
    tuxthrottle_mangohud_games.py apply --plan <base64 json> [--dry-run]
            # one batch: {"on": {"<appid>": null | {"own": bool, ...settings}},
            #             "off": ["<appid>", ...]}  - at most ONE Steam config write
    tuxthrottle_mangohud_games.py profile <appid> on|off|delete|status
    tuxthrottle_mangohud_games.py global-status
    tuxthrottle_mangohud_games.py global-off
    tuxthrottle_mangohud_games.py hook-all [--dry-run]   # one-time, Steam closed
    tuxthrottle_mangohud_games.py gate-script     # print the gate (root installs it)
    tuxthrottle_mangohud_games.py gate-status

Run as the real user, not root.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import sys
import time
from pathlib import Path

import tuxthrottle_launchopts as lo
import tuxthrottle_mangohud_launchers as launchers
from tuxthrottle_prefix_relocate import all_libraries

POSITIONS = ("top-left", "top-center", "top-right", "middle-left", "middle-right",
             "bottom-left", "bottom-center", "bottom-right")
DETAILS = ("global", "fps_only", "full")

_TOOL = re.compile(r"(Proton( |$)|Steam Linux Runtime|Steamworks Common|SteamVR$)", re.I)
_WRAPPER = re.compile(r"(?<!\S)mangohud(?!\S)")
_CONFVAR = re.compile(r'(?<!\S)MANGOHUD_CONFIGFILE=("[^"]*"|\S+)')
_FORCE = re.compile(r"(?<!\S)MANGOHUD=[01](?!\S)")
# keys the per-game settings own inside a per-game conf
_OWNED = ("position", "offset_x", "offset_y", "font_size", "fps_limit",
          "fps_only", "full")


GATE_PATH = Path("/usr/local/bin/mangohud")
GATE_MARK = "tuxthrottle-mangohud-gate"
GATE_SCRIPT = r"""#!/usr/bin/env bash
# tuxthrottle-mangohud-gate - per-game MangoHud switch (TuxThrottle → MangoHud tab).
# Sits ahead of /usr/bin/mangohud on PATH: a Steam game only gets the overlay
# when ~/.config/tuxthrottle/mangohud-games/<appid>.on exists. Outside Steam
# (no AppID in the environment) it behaves exactly like the real mangohud.
real=/usr/bin/mangohud
appid="${SteamAppId:-${SteamGameId:-${STEAM_COMPAT_APP_ID:-}}}"
if [ -z "$appid" ] || [ "$#" -eq 0 ] || [ "${1#--}" != "$1" ]; then
    exec "$real" "$@"
fi
# per-game "force the dedicated GPU" switch (<appid>.dgpu): PRIME render offload
if [ -e "$HOME/.config/tuxthrottle/mangohud-games/$appid.dgpu" ]; then
    if [ -e /proc/driver/nvidia/version ]; then
        export __NV_PRIME_RENDER_OFFLOAD=1 __VK_LAYER_NV_optimus=NVIDIA_only \
               __GLX_VENDOR_LIBRARY_NAME=nvidia
    else
        export DRI_PRIME=1
    fi
fi
if [ -e "$HOME/.config/tuxthrottle/mangohud-games/$appid.on" ]; then
    own="$HOME/.config/MangoHud/tuxthrottle-$appid.conf"
    [ -f "$own" ] && export MANGOHUD_CONFIGFILE="$own"
    exec "$real" "$@"
fi
exec "$@"
"""


def gate_installed() -> bool:
    try:
        return GATE_MARK in GATE_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def gate_current() -> bool:
    """The installed gate is this version's script (an older one lacks the
    dedicated-GPU switch) - press Install on the MangoHud tab to refresh it."""
    try:
        return GATE_PATH.read_text(encoding="utf-8", errors="replace") == GATE_SCRIPT
    except OSError:
        return False


def flags_dir() -> Path:
    return Path("~/.config/tuxthrottle/mangohud-games").expanduser()


def flag_on(appid: str) -> bool:
    return (flags_dir() / f"{appid}.on").exists()


def set_flag(appid: str, on: bool) -> None:
    f = flags_dir() / f"{appid}.on"
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    else:
        f.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
#  launch-options string surgery (pure)
# --------------------------------------------------------------------------- #
def _squeeze(s: str) -> str:
    return re.sub(r" {2,}", " ", s).strip()


def state(opts: str) -> dict:
    """{'enabled': wrapper present, 'conf': per-game config path or ''}."""
    m = _CONFVAR.search(opts or "")
    return {"enabled": bool(_WRAPPER.search(opts or "")),
            "conf": m.group(1).strip('"') if m else ""}


def transform(opts: str, enabled: bool, conffile: str = "") -> str:
    """Return `opts` with the MangoHud wrapper switched on/off.

    Off: drops the `mangohud` wrapper and any MANGOHUD_CONFIGFILE; everything
    else (incl. an explicit MANGOHUD=0) is left alone. On: also drops a
    MANGOHUD=0/1 override, puts `mangohud` right before %command% and, with
    `conffile`, adds MANGOHUD_CONFIGFILE - after gamescope's `--` when the
    game is wrapped in gamescope, since env there must reach the game.
    """
    s = _CONFVAR.sub("", _WRAPPER.sub("", opts or ""))
    if not enabled:
        return _squeeze(s)
    s = _squeeze(_FORCE.sub("", s))
    if "%command%" not in s:
        s = _squeeze(s + " %command%")
    s = s.replace("%command%", "mangohud %command%", 1)
    if conffile:
        var = f'MANGOHUD_CONFIGFILE="{conffile}"' if " " in conffile \
            else f"MANGOHUD_CONFIGFILE={conffile}"
        s = _put_env(s, var)
    return _squeeze(s)


# --------------------------------------------------------------------------- #
#  per-game dedicated GPU
# --------------------------------------------------------------------------- #
_DGPU_NV = ("__NV_PRIME_RENDER_OFFLOAD=1", "__VK_LAYER_NV_optimus=NVIDIA_only",
            "__GLX_VENDOR_LIBRARY_NAME=nvidia")
_DGPU_RE = re.compile(r"(?<!\S)(__NV_PRIME_RENDER_OFFLOAD=\S+|__VK_LAYER_NV_optimus=\S+"
                      r"|__GLX_VENDOR_LIBRARY_NAME=nvidia|DRI_PRIME=\S+)(?!\S)")


def has_nvidia() -> bool:
    return Path("/proc/driver/nvidia/version").exists()


def dgpu_vars(nvidia: bool | None = None) -> str:
    """The environment that sends a game to the dedicated GPU: NVIDIA PRIME
    render offload, or DRI_PRIME=1 for a Mesa-driven dGPU."""
    nvidia = has_nvidia() if nvidia is None else nvidia
    return " ".join(_DGPU_NV) if nvidia else "DRI_PRIME=1"


def has_dgpu(opts: str) -> bool:
    return bool(re.search(r"(?<!\S)(__NV_PRIME_RENDER_OFFLOAD=1|DRI_PRIME=1)(?!\S)", opts or ""))


def _put_env(s: str, var: str) -> str:
    """Add `VAR=value ...` so it reaches the game: after gamescope's `--`
    when the game is wrapped in gamescope, else at the front."""
    if " -- env " in f" {s} ":
        return s.replace("-- env ", f"-- env {var} ", 1)
    if " -- " in f" {s} ":
        return s.replace("-- ", f"-- env {var} ", 1)
    return f"{var} {s}"


def transform_dgpu(opts: str, on: bool, nvidia: bool | None = None) -> str:
    """Launch options with the dedicated-GPU variables added or removed;
    everything else in the string is kept."""
    s = _squeeze(_DGPU_RE.sub("", opts or ""))
    s = _squeeze(s.replace("-- env %command%", "-- %command%"))
    if not on:
        return "" if s == "%command%" else s
    if "%command%" not in s:
        s = _squeeze(s + " %command%")
    return _squeeze(_put_env(s, dgpu_vars(nvidia)))


def _mark(appid: str, kind: str, on: bool) -> None:
    f = flags_dir() / f"{appid}.{kind}"
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    else:
        f.unlink(missing_ok=True)


def _marked(kind: str) -> set:
    d = flags_dir()
    return {f.stem for f in d.glob(f"*.{kind}")} if d.is_dir() else set()


def dgpu_state(appid: str, opts: str, gate: bool) -> dict:
    """wanted = the switch position; active = what the game gets right now.
    Live through the gate when the game has the `mangohud` hook, otherwise
    through the variables in its Launch Options."""
    flag = (flags_dir() / f"{appid}.dgpu").exists()
    off_pending = (flags_dir() / f"{appid}.nodgpu").exists()
    in_opts = has_dgpu(opts)
    hooked = state(opts)["enabled"]
    return {"wanted": flag or (in_opts and not off_pending),
            "active": in_opts or (flag and gate and hooked)}


# --------------------------------------------------------------------------- #
#  per-game MangoHud config
# --------------------------------------------------------------------------- #
def conf_dir() -> Path:
    return Path("~/.config/MangoHud").expanduser()


def conf_path(appid: str) -> Path:
    return conf_dir() / f"tuxthrottle-{appid}.conf"


def _key(line: str) -> str:
    return line.split("=", 1)[0].strip()


def render_conf(appid: str, settings: dict, base_text: str = "") -> str:
    """The per-game config: the global one minus the keys the per-game
    settings own, plus those settings."""
    own_pos = bool(settings.get("position"))
    drop = set(_OWNED) if own_pos else set(_OWNED) - {"position", "offset_x", "offset_y"}
    out = [f"# MangoHud config for Steam AppID {appid} (managed by TuxThrottle)"]
    for ln in base_text.splitlines():
        t = ln.strip()
        if not t or t.startswith("#") or _key(t) in drop:
            continue
        out.append(t)
    if own_pos:
        out.append(f"position={settings['position']}")
    for k in ("font_size", "fps_limit"):
        v = str(settings.get(k) or "").strip()
        if v.isdigit() and int(v) > 0:
            out.append(f"{k}={v}")
    if settings.get("detail") in ("fps_only", "full"):
        out.append(settings["detail"])
    return "\n".join(out) + "\n"


def read_settings(appid: str) -> dict:
    """What the per-game conf currently says (empty values when absent)."""
    res = {"position": "", "detail": "global", "font_size": "", "fps_limit": ""}
    try:
        lines = conf_path(appid).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return res
    for ln in lines:
        t = ln.strip()
        k = _key(t)
        if k in ("position", "font_size", "fps_limit") and "=" in t:
            res[k] = t.split("=", 1)[1].strip()
        elif t in ("fps_only", "full"):
            res["detail"] = t
    return res


def profile_off_path(appid: str) -> Path:
    """Where a game's profile is parked while it is switched off."""
    return conf_path(appid).with_name(conf_path(appid).name + ".off")


def profile_state(appid: str) -> str:
    """'on'  - the game has its own profile and it is in force (it then takes
              priority over the global MangoHud.conf),
       'off' - a profile is saved but parked; the game uses the global one,
       'none' - no profile."""
    if conf_path(appid).is_file():
        return "on"
    return "off" if profile_off_path(appid).is_file() else "none"


def _set_keys(path: Path, keys: dict) -> None:
    """Replace `key=value` lines in a config ('' / 0 removes the key)."""
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
             if _key(ln.strip()) not in keys]
    for k, v in keys.items():
        v = str(v or "").strip()
        if v.isdigit() and int(v) > 0:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def ensure_profile(appid: str, settings: dict | None = None) -> Path:
    """Make the game's own profile exist and be in force. A parked profile is
    brought back as it was; a brand-new one starts as a copy of the global
    config. Only font size / FPS limit are touched here - the layout and
    placement are edited in the overlay editor and must survive this."""
    p, off = conf_path(appid), profile_off_path(appid)
    settings = settings or {}
    if not p.is_file():
        if off.is_file():
            off.rename(p)
        else:
            return write_conf(appid, settings)
    _set_keys(p, {k: settings[k] for k in ("font_size", "fps_limit") if k in settings})
    return p


def disable_profile(appid: str) -> None:
    """Park the game's profile (kept, not deleted) - the global one applies."""
    p = conf_path(appid)
    if p.is_file():
        p.replace(profile_off_path(appid))


def delete_profile(appid: str) -> None:
    conf_path(appid).unlink(missing_ok=True)
    profile_off_path(appid).unlink(missing_ok=True)


def write_conf(appid: str, settings: dict) -> Path:
    p = conf_path(appid)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        base = (conf_dir() / "MangoHud.conf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        base = ""
    p.write_text(render_conf(appid, settings, base), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
#  Steam side
# --------------------------------------------------------------------------- #
def installed_games() -> dict[str, str]:
    """{appid: name} for every game installed in any Steam library."""
    names: dict[str, str] = {}
    for root in lo._steam_roots():
        try:
            libs = all_libraries(root)
        except OSError:
            continue
        for lib in libs:
            for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
                try:
                    t = acf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                n = re.search(r'"name"\s+"([^"]*)"', t)
                if a:
                    names.setdefault(a.group(1), n.group(1) if n else a.group(1))
    return names


def steam_install_dirs() -> dict[str, str]:
    """{appid: steamapps/common/<installdir>} for every installed Steam game."""
    dirs: dict[str, str] = {}
    for root in lo._steam_roots():
        try:
            libs = all_libraries(root)
        except OSError:
            continue
        for lib in libs:
            for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
                try:
                    t = acf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                a = re.search(r'"appid"\s+"(\d+)"', t)
                d = re.search(r'"installdir"\s+"([^"]*)"', t)
                if a and d:
                    dirs.setdefault(a.group(1), str(lib / "steamapps" / "common" / d.group(1)))
    return dirs


def _force_file() -> Path:
    return Path("~/.config/tuxthrottle/mangohud-force.json").expanduser()


def forced() -> set:
    """Game ids the user forced MangoHud on for despite an anti-cheat."""
    try:
        d = json.loads(_force_file().read_text())
        return set(d) if isinstance(d, list) else set()
    except (OSError, ValueError):
        return set()


def _save_forced(ids: set) -> None:
    f = _force_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(sorted(ids)))


def _gid(raw: str) -> str:
    """Normalise a plan id: a bare AppID is a Steam game."""
    raw = str(raw)
    return f"steam:{raw}" if raw.isdigit() else raw


def _anticheat_map(ids) -> dict:
    """{game id: anti-cheat name} for the given ids (only games that have one)."""
    want = set(ids)
    paths = {f"steam:{a}": p for a, p in steam_install_dirs().items() if f"steam:{a}" in want}
    paths.update({g["id"]: g["path"] for g in launchers.other_games() if g["id"] in want})
    return {k: v for k, v in launchers.anticheat_for(paths).items() if v}


def _launch_options() -> dict[str, str]:
    opts: dict[str, str] = {}
    for lc in lo.find_localconfigs():
        apps = lo._apps_dict(lo._load(lc)) or {}
        for aid, e in lo._iter_games(apps):
            opts.setdefault(aid, lo._ci_get(e, "LaunchOptions") or "")
    return opts


def list_games() -> list[dict]:
    """One row per installed game - Steam first-class, plus whatever Heroic
    and Lutris have installed. `hook` = the `mangohud` wrapper is in a Steam
    game's Launch Options (always True for the other launchers, which have
    their own switch); `wanted` = the switch position; `enabled` = the overlay
    will actually show; `anticheat` = name of a detected anti-cheat ('' =
    none); `forced` = the user overrode the anti-cheat lock."""
    opts = _launch_options()
    gate = gate_installed()
    rows = []
    for aid, name in installed_games().items():
        if _TOOL.match(name):
            continue                    # Proton builds / runtimes aren't games
        st = state(opts.get(aid, ""))
        own = conf_path(aid).is_file() if gate else bool(st["conf"])
        prof = profile_state(aid)
        dg = dgpu_state(aid, opts.get(aid, ""), gate)
        rows.append({"id": f"steam:{aid}", "source": "Steam",
                     "appid": aid, "name": name, "hook": st["enabled"],
                     "wanted": flag_on(aid) if gate else st["enabled"],
                     "enabled": st["enabled"] and (flag_on(aid) or not gate),
                     "own_settings": own, "profile": prof, "options": opts.get(aid, ""),
                     "dgpu": dg["wanted"], "dgpu_active": dg["active"],
                     "settings": read_settings(aid)})
    for g in launchers.other_games():
        rows.append({"id": g["id"], "source": g["source"], "appid": g["appid"],
                     "name": g["name"], "hook": True, "wanted": g["wanted"],
                     "enabled": g["enabled"], "own_settings": False, "profile": "none",
                     "options": "",
                     "settings": {}, "path": g.get("path", "")})
    paths = {r["id"]: r["path"] for r in rows if r.get("path")}
    paths.update({f"steam:{a}": p for a, p in steam_install_dirs().items()})
    ac = launchers.anticheat_for(paths)
    force = forced()
    for r in rows:
        r.pop("path", None)
        r["anticheat"] = ac.get(r["id"], "")
        r["forced"] = r["id"] in force
    return sorted(rows, key=lambda r: r["name"].lower())


def _rewrite(change, dry: bool) -> int:
    """Run `change(appid, current) -> new` over every game in every
    localconfig.vdf; write (with a backup) where something differs."""
    if lo.steam_running():
        print("Steam is running - quit Steam completely first (it overwrites "
              "localconfig.vdf on exit).")
        return 2
    files = lo.find_localconfigs()
    if not files:
        print("no localconfig.vdf found - is Steam installed for this user?")
        return 1
    total = 0
    for lc in files:
        cfg = lo._load(lc)
        apps = lo._apps_dict(cfg)
        if apps is None:
            continue
        changed = 0
        for aid in change.appids(apps):
            entry = apps.setdefault(aid, {})
            cur = lo._ci_get(entry, "LaunchOptions") or ""
            new = change(aid, cur)
            if new == cur:
                continue
            for k in list(entry):
                if k.lower() == "launchoptions" and k != "LaunchOptions":
                    del entry[k]
            entry["LaunchOptions"] = new
            changed += 1
        if changed and not dry:
            bak = lc.with_name(lc.name + f".tuxthrottle-bak-{int(time.time())}")
            shutil.copy2(lc, bak)
            lo._dump(cfg, lc)
        total += changed
    verb = "would change" if dry else "changed"
    print(f"{verb} LaunchOptions on {total} game(s)"
          + ("" if dry or not total else " - restart Steam to pick it up"))
    return 0


class _One:
    def __init__(self, appid: str, enabled: bool, conffile: str = ""):
        self.appid, self.enabled, self.conffile = appid, enabled, conffile

    def appids(self, _apps):
        return [self.appid]

    def __call__(self, _aid, cur):
        return transform(cur, self.enabled, self.conffile)


class _AllOff:
    def appids(self, apps):
        return [aid for aid, _e in lo._iter_games(apps)]

    def __call__(self, _aid, cur):
        return transform(cur, False)


def _set_game_gated(appid: str, enabled: bool, settings: dict | None, dry: bool) -> int:
    """Gate installed: flag file + per-game conf, live. Only a game with no
    `mangohud` wrapper in its Launch Options needs the (Steam-closed) edit."""
    if not enabled:
        if not dry:
            set_flag(appid, False)
        print(f"MangoHud off for {appid} - applies at its next launch")
        return 0
    hooked = state(_launch_options().get(appid, ""))["enabled"]
    if not hooked:
        rc = _rewrite(_One(appid, True), dry)
        if rc:
            print(f"AppID {appid} has no `mangohud` wrapper in its Launch Options "
                  f"yet - adding it is the one step that needs Steam closed.")
            return rc
    if not dry:
        if settings is not None:
            ensure_profile(appid, settings)
        else:
            disable_profile(appid)
        set_flag(appid, True)
    print(f"MangoHud on for {appid} - applies at its next launch")
    return 0


def set_game(appid: str, enabled: bool, settings: dict | None = None,
             dry: bool = False) -> int:
    if gate_installed():
        return _set_game_gated(appid, enabled, settings, dry)
    conffile = ""
    if enabled and settings is not None:
        conffile = str(conf_path(appid))
        if not dry and not lo.steam_running():
            ensure_profile(appid, settings)
    return _rewrite(_One(appid, enabled, conffile), dry)


class _Edits:
    """Batch: {appid: [fn(cur) -> new, ...]} applied in one pass over
    localconfig.vdf, so MangoHud and dedicated-GPU changes share one write."""

    def __init__(self):
        self.edits: dict = {}

    def add(self, aid: str, fn) -> None:
        self.edits.setdefault(aid, []).append(fn)

    def appids(self, _apps):
        return list(self.edits)

    def __call__(self, aid, cur):
        for fn in self.edits[aid]:
            cur = fn(cur)
        return cur

    def __bool__(self):
        return bool(self.edits)


def _plan_dgpu(plan: dict, edits: _Edits, opts: dict, gate: bool, dry: bool) -> list:
    """Record this batch's dedicated-GPU switches and queue the Launch Options
    edits still needed - for this batch and for anything left waiting from an
    earlier one. Returns the appids that need the (Steam-closed) write."""
    on = [str(a) for a in (plan.get("dgpu_on") or []) if str(a).isdigit()]
    off = [str(a) for a in (plan.get("dgpu_off") or []) if str(a).isdigit()]
    if not dry:
        for aid in on:
            _mark(aid, "dgpu", True)
            _mark(aid, "nodgpu", False)
        for aid in off:
            _mark(aid, "dgpu", False)
            _mark(aid, "nodgpu", True)
    want_on = (_marked("dgpu") | set(on)) - set(off)
    want_off = (_marked("nodgpu") | set(off)) - set(on)
    need = []
    for aid in sorted(want_on):
        cur = opts.get(aid, "")
        live = gate and state(cur)["enabled"]        # the gate does it, no edit
        if not live and not has_dgpu(cur):
            edits.add(aid, lambda c: transform_dgpu(c, True))
            need.append(aid)
    for aid in sorted(want_off):
        if has_dgpu(opts.get(aid, "")):
            edits.add(aid, lambda c: transform_dgpu(c, False))
            need.append(aid)
        elif not dry:
            _mark(aid, "nodgpu", False)              # nothing left to strip
    if on or off:
        print(f"Dedicated GPU: {len(on)} game(s) on, {len(off)} off")
    return need


def apply_plan(plan: dict, dry: bool = False) -> int:
    """Apply a whole batch of per-game switches at once. With the gate the
    flags/configs go live immediately and only games lacking the `mangohud`
    hook need Steam's config edited - all of them in ONE write. Returns 0 when
    everything is done, 3 when hook additions are waiting for Steam to close
    (they stay recorded: run apply again later), else the write's error."""
    all_on = {_gid(a): v for a, v in (plan.get("on") or {}).items()}
    all_off = [_gid(a) for a in (plan.get("off") or [])]
    force = forced() | {_gid(a) for a in (plan.get("force") or [])}
    # anti-cheat lock: refuse to switch the overlay on unless it was forced
    blocked = {g: ac for g, ac in _anticheat_map(all_on).items()
               if g in all_on and g not in force}
    for g, ac in blocked.items():
        print(f"skipped {g}: {ac} detected - an injected overlay can get the "
              f"account flagged. Tick “force” for that game to override.")
        all_on.pop(g, None)
    if not dry:
        plan_force = {_gid(a) for a in (plan.get("force") or [])}
        _save_forced((forced() - set(all_off)) | (plan_force & set(all_on)))
        for g in all_off:
            if not g.startswith("steam:"):
                launchers.set_enabled(g, False)
        for g in all_on:
            if not g.startswith("steam:") and not launchers.set_enabled(g, True):
                print(f"could not switch {g} on (launcher config not writable)")
    others = sum(1 for g in list(all_on) + all_off if not g.startswith("steam:"))
    if others:
        print(f"{others} Heroic/Lutris game(s) updated in the launcher's own "
              f"per-game MangoHud setting")
    on = {g[6:]: v for g, v in all_on.items() if g.startswith("steam:")}
    off = [g[6:] for g in all_off if g.startswith("steam:")]
    gate = gate_installed()
    opts = _launch_options()
    edits = _Edits()
    dgpu_need = _plan_dgpu(plan, edits, opts, gate, dry)
    if not dry:
        for aid, v in on.items():
            if isinstance(v, dict):
                if v.get("own"):
                    ensure_profile(aid, v)
                else:
                    disable_profile(aid)
    hook_need = []
    if not gate:
        for aid in off:
            edits.add(aid, lambda c: transform(c, False))
        for aid, v in on.items():
            own = conf_path(aid).is_file() if not isinstance(v, dict) else bool(v.get("own"))
            conf = str(conf_path(aid)) if own else ""
            edits.add(aid, lambda c, conf=conf: transform(c, True, conf))
    else:
        if not dry:
            for aid in off:
                set_flag(aid, False)
            for aid in on:
                set_flag(aid, True)
        if on or off:
            print(f"MangoHud: {len(on)} game(s) on, {len(off)} off - applies at each "
                  f"game's next launch")
        # every game that is switched on but has no hook yet, not just this batch
        hook_need = sorted(a for a in _marked("on") | set(on)
                           if a not in off and not state(opts.get(a, ""))["enabled"])
        for aid in hook_need:
            edits.add(aid, lambda c: transform(c, True, ""))
    if not edits:
        return 0
    rc = _rewrite(edits, dry)
    if rc == 0 and not dry:
        for aid in _marked("nodgpu"):
            _mark(aid, "nodgpu", False)
    if rc == 2:
        names = installed_games()
        if gate and hook_need:
            print(f"{len(hook_need)} game(s) don't have the `mangohud` hook in their "
                  f"Launch Options yet: " + ", ".join(names.get(a, a) for a in hook_need))
        if dgpu_need:
            print(f"{len(dgpu_need)} game(s) need their Launch Options edited for the "
                  f"dedicated-GPU switch: " + ", ".join(names.get(a, a) for a in dgpu_need))
        print("They are recorded - close Steam once, press Apply again, and all "
              "of them are done in a single write.")
        return 3
    return rc


class _HookAll:
    """Add the (gated, inert) `mangohud` hook to every game that has Launch
    Options or is installed - so no later switch ever needs Steam closed."""

    def __init__(self, installed):
        self.installed = set(installed)

    def appids(self, apps):
        return sorted({aid for aid, _e in lo._iter_games(apps)} | self.installed)

    def __call__(self, _aid, cur):
        return cur if state(cur)["enabled"] else transform(cur, True)


def hook_all(dry: bool = False) -> int:
    if not gate_installed():
        print("install the live per-game switch first - without it the hook "
              "would turn the overlay ON in every game")
        return 1
    return _rewrite(_HookAll(installed_games()), dry)


def disable_all(dry: bool = False) -> int:
    if gate_installed():
        flags = list(flags_dir().glob("*.on")) if flags_dir().is_dir() else []
        if not dry:
            for f in flags:
                f.unlink(missing_ok=True)
        print(f"MangoHud switched off for {len(flags)} game(s) - no game shows "
              f"the overlay until you enable it")
        return 0
    return _rewrite(_AllOff(), dry)


# --------------------------------------------------------------------------- #
#  session-wide enable (the thing this module replaces)
# --------------------------------------------------------------------------- #
def global_confs() -> list[Path]:
    """environment.d files that switch MangoHud on for the whole session."""
    hits = []
    dirs = [Path("~/.config/environment.d").expanduser(), Path("/etc/environment.d")]
    for d in dirs:
        for f in sorted(d.glob("*.conf")) if d.is_dir() else []:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"^\s*MANGOHUD=1\s*$", text, re.M) or "libMangoHud" in text:
                hits.append(f)
    return hits


def global_off() -> int:
    """Remove the user-level session-wide enable. Files under /etc (or ones
    this user can't write) are reported, not touched."""
    rc = 0
    for f in global_confs():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            keep = [ln for ln in text.splitlines()
                    if not re.match(r"\s*MANGOHUD=1\s*$", ln) and "libMangoHud" not in ln]
            if any(ln.strip() and not ln.strip().startswith("#") for ln in keep):
                f.write_text("\n".join(keep) + "\n", encoding="utf-8")
            else:
                f.unlink()
            print(f"removed session-wide MangoHud from {f}")
        except OSError as exc:
            print(f"could not edit {f}: {exc}")
            rc = 1
    if rc == 0:
        print("session-wide MangoHud is off - takes full effect at next login")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")
    en = sub.add_parser("enable")
    en.add_argument("appid")
    en.add_argument("--own-settings", action="store_true")
    en.add_argument("--position", default="", choices=("",) + POSITIONS)
    en.add_argument("--detail", default="global", choices=DETAILS)
    en.add_argument("--font-size", default="")
    en.add_argument("--fps-limit", default="")
    en.add_argument("--dry-run", action="store_true")
    di = sub.add_parser("disable")
    di.add_argument("appid")
    di.add_argument("--dry-run", action="store_true")
    da = sub.add_parser("disable-all")
    da.add_argument("--dry-run", action="store_true")
    pl = sub.add_parser("apply")
    pl.add_argument("--plan", required=True)
    pl.add_argument("--dry-run", action="store_true")
    ha = sub.add_parser("hook-all")
    ha.add_argument("--dry-run", action="store_true")
    sub.add_parser("global-status")
    sub.add_parser("global-off")
    pr = sub.add_parser("profile")
    pr.add_argument("appid")
    pr.add_argument("action", choices=("on", "off", "delete", "status"))
    sub.add_parser("gate-script")
    sub.add_parser("gate-status")
    args = ap.parse_args()

    if args.cmd == "profile":
        if not args.appid.isdigit():
            sys.exit("appid must be numeric")
        {"on": ensure_profile, "off": disable_profile, "delete": delete_profile,
         "status": lambda a: None}[args.action](args.appid)
        print(f"profile for {args.appid}: {profile_state(args.appid)}")
        return 0
    if args.cmd == "gate-script":
        sys.stdout.write(GATE_SCRIPT)
        return 0
    if args.cmd == "apply":
        try:
            plan = json.loads(base64.b64decode(args.plan).decode("utf-8"))
        except ValueError as exc:
            sys.exit(f"bad --plan: {exc}")
        return apply_plan(plan, args.dry_run)
    if args.cmd == "hook-all":
        return hook_all(args.dry_run)
    if args.cmd == "gate-status":
        print("installed" if gate_installed() else "not installed")
        return 0 if gate_installed() else 1

    if args.cmd == "list":
        rows = list_games()
        if args.json:
            print(json.dumps(rows))
        else:
            for r in rows:
                mark = "on " if r["enabled"] else "off"
                own = "  (own settings)" if r["own_settings"] else ""
                print(f"  [{mark}] {r['appid']:<8} {r['name']}{own}")
        return 0
    if args.cmd in ("enable", "disable") and not args.appid.isdigit():
        sys.exit("appid must be numeric")
    if args.cmd == "enable":
        settings = None
        if args.own_settings:
            settings = {"position": args.position, "detail": args.detail,
                        "font_size": args.font_size, "fps_limit": args.fps_limit}
        return set_game(args.appid, True, settings, args.dry_run)
    if args.cmd == "disable":
        return set_game(args.appid, False, None, args.dry_run)
    if args.cmd == "disable-all":
        return disable_all(args.dry_run)
    if args.cmd == "global-status":
        hits = global_confs()
        print("\n".join(str(h) for h in hits) if hits else "off")
        return 1 if hits else 0
    return global_off()


if __name__ == "__main__":
    raise SystemExit(main())
