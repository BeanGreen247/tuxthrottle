#!/usr/bin/env python3
"""Video-memory budget for a KDE Plasma 6 / Wayland laptop.

Model-agnostic: every tier is plain KWin/Plasma KConfig — no vendor calls —
so it degrades gracefully on AMD, NVIDIA or Intel graphics. The typical
target is a laptop whose integrated GPU shares a small slice of system RAM
as VRAM and whose KDE desktop fills it (spilling to slower GTT), while a
discrete GPU is kept free for video editing / games / 3D.

Three jobs:
  * report which GPU the desktop renders on, how full each GPU's video
    memory is, and which processes are holding it;
  * apply a KWin/Plasma "VRAM budget" tier — regular / medium / extreme —
    that trades desktop eye-candy for a smaller compositor VRAM footprint;
  * a "free VRAM now" action (driver-level buffer eviction where the GPU
    supports it + an optional compositor restart) and a desktop-GPU
    selector (KWIN_DRM_DEVICES) so the compositor can be pinned to the
    integrated GPU.

Config:   <user>/.config/tuxthrottle/vram.json   {"tier":…, "compositor_gpu":…}
Baseline: <user>/.config/tuxthrottle/vram-baseline.json — KConfig values
          captured before the first non-regular apply, so `regular` restores
          exactly what was there.

    tuxthrottle_vram.py status [--json]
    tuxthrottle_vram.py profile {regular|medium|extreme}
    tuxthrottle_vram.py free [--json] [--restart-compositor]
    tuxthrottle_vram.py compositor-gpu {auto|igpu|dgpu}

kread/kwriteconfig6 run in the real user's session (the module self-wraps
with `sudo -u` when it is root, same as the KDE tweaks) and all state files
resolve against the *real* user's home even when invoked via pkexec/sudo.
`free`'s driver eviction needs root; `compositor-gpu` only takes effect at
the next login.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pwd
import re
import subprocess
from pathlib import Path

import sensors

ENV_FILE_NAME = "09-tuxthrottle-gpu.sh"

TIERS = ("regular", "medium", "extreme")

# (file, [group chain], key, value) applied for each tier. `regular` is the
# absence of all of these — it restores the captured baseline instead.
_TIER_KEYS: dict[str, list[tuple[str, list[str], str, str]]] = {
    "medium": [
        ("kwinrc", ["Compositing"], "HiddenPreviews", "5"),
        ("kwinrc", ["Compositing"], "GLTextureFilter", "1"),
        ("kwinrc", ["Plugins"], "blurEnabled", "false"),
        ("kwinrc", ["Plugins"], "contrastEnabled", "false"),
        ("kdeglobals", ["KDE"], "AnimationDurationFactor", "0.25"),
    ],
    "extreme": [
        ("kwinrc", ["Compositing"], "HiddenPreviews", "4"),
        ("kwinrc", ["Compositing"], "GLTextureFilter", "0"),
        ("kwinrc", ["Compositing"], "LatencyPolicy", "Low"),
        ("kwinrc", ["Plugins"], "blurEnabled", "false"),
        ("kwinrc", ["Plugins"], "contrastEnabled", "false"),
        ("kwinrc", ["Plugins"], "overviewEnabled", "false"),
        ("kwinrc", ["Plugins"], "windowviewEnabled", "false"),
        ("kwinrc", ["Plugins"], "desktopgridEnabled", "false"),
        ("kwinrc", ["Plugins"], "slideEnabled", "false"),
        ("kwinrc", ["Plugins"], "slidingpopupsEnabled", "false"),
        ("kwinrc", ["Plugins"], "kwin4_effect_translucencyEnabled", "false"),
        ("kdeglobals", ["KDE"], "AnimationDurationFactor", "0"),
        # the Maliit on-screen keyboard (plasma-keyboard) keeps a live QML
        # scene in VRAM even unused — tens of MiB. Empty string = "None" in
        # System Settings → Virtual Keyboard. Fully applies next login.
        ("kwinrc", ["Wayland"], "InputMethod", ""),
    ],
}
# effects to live-unload (so the VRAM drops now, not just next login)
_EFFECTS_OFF: dict[str, list[str]] = {
    "medium": ["blur", "contrast"],
    "extreme": ["blur", "contrast", "overview", "windowview", "desktopgrid",
                "slide", "slidingpopups", "kwin4_effect_translucency"],
}
_SOLID_WALLPAPER_TIERS = ("extreme",)

_APPLETS = "plasma-org.kde.plasma.desktop-appletsrc"
_DESKTOP_PLUGINS = ("org.kde.desktopcontainment", "org.kde.plasma.folder")


# --------------------------------------------------------------------------- #
#  config / baseline — always under the REAL user's home, never /root when
#  we were launched via pkexec/sudo (the "state landed in /root" bug).
# --------------------------------------------------------------------------- #
def _real_pw() -> pwd.struct_passwd:
    if os.geteuid() == 0:
        for env in ("SUDO_USER", "PKEXEC_USER"):
            u = os.environ.get(env)
            if u and u != "root":
                try:
                    return pwd.getpwnam(u)
                except KeyError:
                    pass
        for env in ("PKEXEC_UID", "SUDO_UID"):
            v = os.environ.get(env)
            if v:
                try:
                    p = pwd.getpwuid(int(v))
                    if p.pw_uid != 0:
                        return p
                except (KeyError, ValueError):
                    pass
    return pwd.getpwuid(os.getuid())


def _real_home() -> Path:
    return Path(_real_pw().pw_dir)


def _chown_to_user(path: Path) -> None:
    if os.geteuid() != 0:
        return
    p = _real_pw()
    try:
        os.chown(path, p.pw_uid, p.pw_gid)
    except OSError:
        pass


def _cfg_path() -> Path:
    return _real_home() / ".config" / "tuxthrottle" / "vram.json"


def _baseline_path() -> Path:
    return _real_home() / ".config" / "tuxthrottle" / "vram-baseline.json"


def _load(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2, sort_keys=True))
    _chown_to_user(p.parent)
    _chown_to_user(p)


def current_tier() -> str:
    t = _load(_cfg_path()).get("tier")
    return t if t in TIERS else "regular"


def current_compositor_gpu() -> str:
    return _load(_cfg_path()).get("compositor_gpu") or _detect_compositor_gpu()


# --------------------------------------------------------------------------- #
#  session helpers — run the KDE tools in the real user's session. Everything
#  a tier does goes through ONE batched `bash -lc` per operation: a whole tier
#  is ~40 kreadconfig6/kwriteconfig6/qdbus calls and doing each as its own
#  `sudo -u <user>` fork is both slow and (on a box with slow sudo/NSS
#  lookups) enough to blow a per-call timeout — which is exactly how "profile
#  extreme" was failing with subprocess.TimeoutExpired.
# --------------------------------------------------------------------------- #
def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _run_session_script(script: str, timeout: int = 120) -> tuple[int, str]:
    """Run `script` under `bash -lc` in the real user's session (root ->
    `sudo -u <user> env …`, unchanged as that user). Never raises."""
    argv = sensors.session_cmd(["bash", "-lc", script])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        return -1, (out.decode(errors="replace") if isinstance(out, bytes)
                    else (out or ""))
    except OSError:
        return -1, ""


def _kwrite_cmd(file: str, groups: list[str], key: str, value: str | None) -> str:
    parts = ["kwriteconfig6", "--file", _shq(file)]
    for g in groups:
        parts += ["--group", _shq(g)]
    parts += ["--key", _shq(key)]
    parts += ["--delete"] if value is None else [_shq(value)]
    return " ".join(parts)


def _kread_batch(items: list[tuple[str, list[str], str]]) -> list[str]:
    """One shell round-trip: read every (file, groups, key). NUL-separates
    values so a value containing newlines can't desync the parse."""
    if not items:
        return []
    cmds = []
    for file, groups, key in items:
        c = ["kreadconfig6", "--file", _shq(file)]
        for g in groups:
            c += ["--group", _shq(g)]
        c += ["--key", _shq(key)]
        cmds.append(" ".join(c) + r' ; printf "\0"')
    _rc, out = _run_session_script(" ; ".join(cmds), timeout=60)
    parts = out.split("\0")
    return [p.strip("\n") for p in parts[:len(items)]] + \
           [""] * max(0, len(items) - len(parts))


def _effects_cmds(action: str, names: list[str]) -> list[str]:
    out = []
    for n in names:
        out.append(
            f'{{ qdbus-qt6 org.kde.KWin /Effects '
            f'org.kde.kwin.Effects.{action}Effect {_shq(n)} '
            f'|| qdbus org.kde.KWin /Effects '
            f'org.kde.kwin.Effects.{action}Effect {_shq(n)} '
            f'|| dbus-send --session --type=method_call --dest=org.kde.KWin '
            f'/Effects org.kde.kwin.Effects.{action}Effect string:{_shq(n)}; '
            f'}} >/dev/null 2>&1 || true')
    return out


def _reload_kwin_cmd() -> str:
    return ('{ qdbus-qt6 org.kde.KWin /KWin reconfigure '
            '|| qdbus org.kde.KWin /KWin reconfigure '
            '|| dbus-send --session --type=method_call --dest=org.kde.KWin '
            '/KWin org.kde.KWin.reconfigure; } >/dev/null 2>&1 || true')


def _restart_plasmashell() -> None:
    # --no-block: `systemctl restart` otherwise blocks until plasmashell has
    # fully re-initialised every applet, which is 20-40 s — far too long to
    # sit inside a tier apply. The wallpaper change is already on disk; the
    # panel picks it up when it comes back.
    _run_session_script(
        "systemctl --user restart --no-block plasma-plasmashell.service "
        "|| (setsid kstart plasmashell >/dev/null 2>&1 &)", timeout=20)


# --------------------------------------------------------------------------- #
#  wallpaper (extreme tier -> solid colour, frees a full-screen texture/screen)
# --------------------------------------------------------------------------- #
def _appletsrc_path() -> Path:
    return _real_home() / ".config" / _APPLETS


_CONT_HDR = re.compile(r"^\[Containments\]\[(\d+)\](\[.+\])?$")


def _desktop_containments() -> list[str]:
    p = _appletsrc_path()
    if not p.is_file():
        return []
    out, cur, is_desktop = [], None, False
    for line in p.read_text(errors="ignore").splitlines():
        m = _CONT_HDR.match(line)
        if m and not m.group(2):
            if cur is not None and is_desktop:
                out.append(cur)
            cur, is_desktop = m.group(1), False
        elif cur is not None and line.strip().startswith("plugin="):
            if line.strip().split("=", 1)[1] in _DESKTOP_PLUGINS:
                is_desktop = True
    if cur is not None and is_desktop:
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
#  tier apply — one batched session script for the whole tier
# --------------------------------------------------------------------------- #
def _all_tier_keys() -> list[tuple[str, list[str], str]]:
    seen, out = set(), []
    for rows in _TIER_KEYS.values():
        for file, groups, key, _ in rows:
            sig = (file, tuple(groups), key)
            if sig not in seen:
                seen.add(sig)
                out.append((file, groups, key))
    return out


def _capture_baseline() -> None:
    if _baseline_path().is_file():
        return
    keys = _all_tier_keys()
    vals = _kread_batch(keys)
    conts = _desktop_containments()
    wp = _kread_batch([(_APPLETS, ["Containments", c], "wallpaperplugin")
                       for c in conts])
    wallpaper = {cid: (v or "org.kde.image") for cid, v in zip(conts, wp)}
    data = {"keys": [[f, g, k, v] for (f, g, k), v in zip(keys, vals)],
            "wallpaper": wallpaper}
    _save(_baseline_path(), data)


def _solid_wallpaper_cmds(cid: str) -> list[str]:
    return [
        _kwrite_cmd(_APPLETS, ["Containments", cid], "wallpaperplugin",
                    "org.kde.color"),
        _kwrite_cmd(_APPLETS, ["Containments", cid, "Wallpaper",
                               "org.kde.color", "General"], "Color", "0,0,0"),
    ]


def apply_tier(tier: str) -> tuple[bool, str]:
    if tier not in TIERS:
        return False, f"unknown tier {tier!r} (regular|medium|extreme)"

    cmds: list[str] = []
    wallpaper_changed = False

    if tier != "regular":
        _capture_baseline()
        cmds += [_kwrite_cmd(*row) for row in _TIER_KEYS[tier]]
        cmds += _effects_cmds("unload", _EFFECTS_OFF.get(tier, []))
        if tier in _SOLID_WALLPAPER_TIERS:
            for cid in _desktop_containments():
                cmds += _solid_wallpaper_cmds(cid)
                wallpaper_changed = True
    else:
        base = _load(_baseline_path())
        known = {(f, tuple(g), k) for f, g, k, _ in base.get("keys", [])}
        for file, groups, key, old in base.get("keys", []):
            cmds.append(_kwrite_cmd(file, groups, key, old or None))
        for file, groups, key in _all_tier_keys():        # not in baseline -> del
            if (file, tuple(groups), key) not in known:
                cmds.append(_kwrite_cmd(file, groups, key, None))
        cmds += _effects_cmds(
            "load", sorted({n for v in _EFFECTS_OFF.values() for n in v}))
        wp_base = list(base.get("wallpaper", {}).items())
        wp_now = _kread_batch([(_APPLETS, ["Containments", c], "wallpaperplugin")
                               for c, _ in wp_base])
        for (cid, plugin), now in zip(wp_base, wp_now):
            if now == "org.kde.color":                    # we had set it solid
                cmds.append(_kwrite_cmd(_APPLETS, ["Containments", cid],
                                        "wallpaperplugin", plugin or "org.kde.image"))
                wallpaper_changed = True

    cmds.append(_reload_kwin_cmd())
    _run_session_script(" ; ".join(cmds), timeout=120)
    if wallpaper_changed:
        _restart_plasmashell()

    cfg = _load(_cfg_path())
    cfg["tier"] = tier
    _save(_cfg_path(), cfg)
    extra = (" — the panel restarts in the background for the wallpaper change"
             if wallpaper_changed else "")
    return True, f"VRAM budget -> {tier}{extra}"


# --------------------------------------------------------------------------- #
#  free VRAM now
# --------------------------------------------------------------------------- #
def _debugfs_dirs(driver: str) -> list[str]:
    """One /sys/kernel/debug/dri dir per device with this driver. The kernel
    exposes several minors for the same card (0000:BB:DD.F, a small int, a
    128+ render minor) — dedupe by the device each points at."""
    seen, out = set(), []
    for name in sorted(glob.glob("/sys/kernel/debug/dri/*/name")):
        d = os.path.dirname(name)
        try:
            txt = open(name).read()
        except OSError:
            continue
        if driver not in txt:
            continue
        m = re.search(r"dev=(\S+)", txt)
        dev = m.group(1) if m else d
        if dev in seen:
            continue
        seen.add(dev)
        out.append(d)
    return out


def free_vram(restart_compositor: bool = False) -> dict:
    before = sensors.vram_info()
    evicted, errors = [], []
    # AMD: on current kernels (6.x+) amdgpu_evict_{vram,gtt} are read-only
    # trigger files — *reading* one evicts that pool to system RAM (buffers
    # page back in on next use). Older kernels took a write of "1".
    for d in _debugfs_dirs("amdgpu"):
        for node in ("amdgpu_evict_vram", "amdgpu_evict_gtt"):
            path = os.path.join(d, node)
            if not os.path.exists(path):
                continue
            done = False
            try:
                open(path).read()
                done = True
            except OSError:
                try:
                    fd = os.open(path, os.O_WRONLY)
                    os.write(fd, b"1")
                    os.close(fd)
                    done = True
                except OSError as exc:
                    errors.append(f"{node}: {exc}")
            if done:
                evicted.append(f"{os.path.basename(d)}/{node}")
    # Intel: i915/xe expose a drop_caches bitmask (0x4 = bound, 0x8 = unbound
    # objects, 0xf = everything idle). No dedicated VRAM to speak of on an
    # iGPU, but it releases shrinkable GEM pages.
    for drv, node in (("i915", "i915_gem_drop_caches"), ("xe", "xe_drop_caches")):
        for d in _debugfs_dirs(drv):
            path = os.path.join(d, node)
            if not os.path.exists(path):
                continue
            try:
                fd = os.open(path, os.O_WRONLY)
                os.write(fd, b"0xf")
                os.close(fd)
                evicted.append(f"{os.path.basename(d)}/{node}")
            except OSError as exc:
                errors.append(f"{node}: {exc}")
    if not evicted and not restart_compositor:
        errors.append("no driver-level eviction available on this GPU — try "
                      "Restart compositor to release KWin's own allocations")
    if restart_compositor:
        rc, _out = _run_session_script(
            "systemctl --user restart plasma-kwin_wayland.service", timeout=45)
        if rc != 0:
            errors.append("kwin restart failed (rc %s)" % rc)
        else:
            evicted.append("compositor restarted")
    after = sensors.vram_info()
    return {"before": before, "after": after, "evicted": evicted,
            "errors": errors, "consumers": sensors.vram_consumers()}


# --------------------------------------------------------------------------- #
#  compositor GPU selector
# --------------------------------------------------------------------------- #
def _card_node(pci: str) -> str:
    """Real /dev/dri/cardN for a PCI id (KWin's device matching is happier
    with the real node than the by-path symlink); '' if not found."""
    link = f"/dev/dri/by-path/pci-{pci}-card"
    try:
        return os.path.realpath(link) if os.path.exists(link) else ""
    except OSError:
        return ""


def _is_muxless() -> bool:
    """True when the panel is wired to the integrated GPU (no hardware MUX) —
    then KWin *must* use the iGPU for scanout and cannot put the whole
    desktop on the dGPU. True for most Optimus / hybrid gaming laptops."""
    return any(g["kind"] == "integrated" and g["boot_vga"]
               for g in sensors.drm_gpus())


def compositor_gpu_modes() -> list[str]:
    """Which selector modes make sense on this box."""
    return ["auto", "igpu"] if _is_muxless() else ["auto", "igpu", "dgpu"]


def _detect_compositor_gpu() -> str:
    """'igpu' / 'dgpu' / 'auto' from the env file we wrote, else 'auto'."""
    f = _real_home() / ".config" / "plasma-workspace" / "env" / ENV_FILE_NAME
    try:
        txt = f.read_text()
    except OSError:
        return "auto"
    m = re.search(r'KWIN_DRM_DEVICES="?([^"\n]+)', txt)
    if not m:
        return "auto"
    first = m.group(1).split(":")[0]
    for g in sensors.drm_gpus():
        if first == _card_node(g["pci"]):
            return "igpu" if g["kind"] == "integrated" else "dgpu"
    return "auto"


def set_compositor_gpu(mode: str) -> tuple[bool, str]:
    if mode not in ("auto", "igpu", "dgpu"):
        return False, "mode must be auto|igpu|dgpu"
    env_dir = _real_home() / ".config" / "plasma-workspace" / "env"
    env_file = env_dir / ENV_FILE_NAME
    cfg = _load(_cfg_path())

    if mode == "auto":
        try:
            env_file.unlink()
        except OSError:
            pass
        cfg["compositor_gpu"] = "auto"
        _save(_cfg_path(), cfg)
        return True, "desktop GPU -> auto (KWin picks; log out to apply)"

    gpus = sensors.drm_gpus()
    if mode == "dgpu" and _is_muxless():
        return False, ("this laptop's panel is wired to the integrated GPU "
                       "(muxless) — KWin cannot render the whole desktop on "
                       "the dGPU. Use per-app PRIME offload for games / "
                       "DaVinci Resolve instead.")

    if mode == "igpu":
        # ONLY the integrated node. Listing the NVIDIA DRM node here makes KWin
        # try to init it as a usable GPU and the proprietary driver's GBM/EGL
        # aborts the session (login loop) — learned the hard way on the g15.
        cards = [_card_node(g["pci"]) for g in gpus if g["kind"] == "integrated"]
    else:  # dgpu, non-muxless: discrete first, integrated kept for scanout
        cards = ([_card_node(g["pci"]) for g in gpus if g["kind"] == "discrete"]
                 + [_card_node(g["pci"]) for g in gpus if g["kind"] == "integrated"])
    cards = [c for c in cards if c]
    if not cards:
        return False, "no matching /dev/dri/card node found"

    # A shell guard so a node that vanishes (driver reload, reorder) is simply
    # not exported — KWin then auto-picks, instead of failing to start.
    lines = "\n".join(f'[ -e {c} ] && _d="${{_d:+$_d:}}{c}"' for c in cards)
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "# written by tuxthrottle_vram — pin the KDE compositor's GPU.\n"
        "# If the desktop fails to start: switch to a TTY (Ctrl+Alt+F3),\n"
        f"# log in, and run:  rm {env_file}\n"
        "_d=\"\"\n"
        f"{lines}\n"
        '[ -n "$_d" ] && export KWIN_DRM_DEVICES="$_d"\n'
        "unset _d\n")
    if os.geteuid() == 0:
        _chown_to_user(env_file)
        _chown_to_user(env_dir)
    cfg["compositor_gpu"] = mode
    _save(_cfg_path(), cfg)
    return True, (f"desktop GPU -> {mode}  ({':'.join(cards)}); "
                  "log out and back in to apply")


# --------------------------------------------------------------------------- #
#  status
# --------------------------------------------------------------------------- #
def status() -> dict:
    return {
        "tier": current_tier(),
        "compositor_gpu": current_compositor_gpu(),
        "gpus": sensors.vram_info(),
        "consumers": sensors.vram_consumers(),
        "nvidia_runtime_pm": sensors.nvidia_runtime_pm(),
    }


def _fmt_status(s: dict) -> str:
    lines = [f"VRAM budget tier : {s['tier']}",
             f"desktop GPU      : {s['compositor_gpu']}"]
    pm = s.get("nvidia_runtime_pm")
    if pm:
        lines.append(f"dGPU runtime PM  : {pm['control']} ({pm['status']})")
    lines.append("")
    for g in s["gpus"]:
        if g.get("asleep"):
            lines.append(f"  {g['name']:<28} {g['kind']:<10} (dGPU asleep)")
            continue
        used = g["used_mb"]
        total = g["total_mb"]
        if used is None or not total:
            lines.append(f"  {g['name']:<28} {g['kind']:<10} n/a")
            continue
        bar_n = round(20 * used / total)
        bar = "#" * bar_n + "-" * (20 - bar_n)
        gtt = f"  +GTT {g['gtt_used_mb']} MiB" if g.get("gtt_used_mb") else ""
        lines.append(f"  {g['name']:<28} {g['kind']:<10} "
                     f"[{bar}] {used}/{total} MiB ({g['pct']}%){gtt}")
    lines.append("")
    lines.append("  top VRAM consumers:")
    for c in s["consumers"][:10]:
        lines.append(f"    {c['vram_mb']:>8.1f} MiB  {c['comm']:<20} "
                     f"pid {c['pid']} [{c['driver']}]")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")

    pr = sub.add_parser("profile")
    pr.add_argument("tier", choices=TIERS)

    fr = sub.add_parser("free")
    fr.add_argument("--json", action="store_true")
    fr.add_argument("--restart-compositor", action="store_true",
                    help="also restart plasma-kwin_wayland (disruptive; "
                         "windows survive, brief black flash)")

    cg = sub.add_parser("compositor-gpu")
    cg.add_argument("mode", choices=["auto", "igpu", "dgpu"])

    args = ap.parse_args(argv)

    if args.cmd == "status":
        s = status()
        print(json.dumps(s, indent=2) if args.json else _fmt_status(s))
        return 0

    if args.cmd == "profile":
        ok, msg = apply_tier(args.tier)
        print(msg)
        return 0 if ok else 1

    if args.cmd == "free":
        res = free_vram(restart_compositor=args.restart_compositor)
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        for g_b, g_a in zip(res["before"], res["after"]):
            if g_b.get("used_mb") is None:
                continue
            print(f"  {g_b['name']:<28} {g_b['used_mb']} -> {g_a['used_mb']} MiB"
                  + (f"  (GTT {g_b.get('gtt_used_mb')} -> {g_a.get('gtt_used_mb')})"
                     if g_b.get("gtt_used_mb") is not None else ""))
        if res["evicted"]:
            print("  evicted: " + ", ".join(res["evicted"]))
        for e in res["errors"]:
            print("  ! " + e)
        return 0

    if args.cmd == "compositor-gpu":
        ok, msg = set_compositor_gpu(args.mode)
        print(msg)
        return 0 if ok else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
