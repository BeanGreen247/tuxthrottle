#!/usr/bin/env python3
"""Keyboard-backlight control for the Dell G15 5515 (Alienware AW-ELC, USB
187c:0550) — a thin wrapper around the `openrgb` CLI.

Background: this BIOS has no SMBIOS keyboard tokens, `dell-laptop` makes no
LED, and hand-rolled HID writes are ACK'd but never light up. What *does* work
is **OpenRGB** driving the controller — verified on the real 5515.

The 5515's AW-ELC is a **single controllable zone**: OpenRGB advertises 4/16
zones, but every per-zone write path (CLI `-z`, the SDK per-LED buffer, a raw
HID user-animation with per-zone SELECT) lands on the whole keyboard — camera-
verified. So there is no per-zone colour and no gradient; the keyboard does one
solid colour, plus the firmware Spectrum Cycle. (Breathing / Flashing hold a
steady colour on fw 1.1.12; Rainbow Wave is washed-out — none are offered.)

Prerequisites:
  * OpenRGB installed (the `OpenRGB` app in the Toolkit's Software tab).
  * The keyboard backlight **enabled in BIOS setup** (F2 -> Keyboard
    Backlight). Nothing lights until that's on.

CLI:
    tuxthrottle_kbd.py on  [--color RRGGBB] [--brightness 0-100]
    tuxthrottle_kbd.py off
    tuxthrottle_kbd.py zone <0-3> --color RRGGBB [--brightness 0-100]   # == whole keyboard
    tuxthrottle_kbd.py effect spectrum [--speed 0-100] [--brightness 0-100]
    tuxthrottle_kbd.py apply-saved
    tuxthrottle_kbd.py reset
    tuxthrottle_kbd.py info

`spectrum` is the one working firmware mode (MCU-driven); *speed* is 0-100
(100 = fastest).
"""
from __future__ import annotations

import argparse
import configparser
import fcntl
import glob
import json
import os
import shutil
import subprocess
import sys
import time

# Keyboard specifics come from the model profile (models/<slug>.json →
# "keyboard"), defaulting to the 5515's AW-ELC values.
try:
    import sensors as _sensors
    _KBD_PROFILE = _sensors.model_profile().get("keyboard") or {}
except Exception:  # noqa: BLE001  — sensors import must never break the driver
    _KBD_PROFILE = {}

DEVICE = _KBD_PROFILE.get("openrgb_device") or "Dell G Series LED Controller"
_USB = (_KBD_PROFILE.get("usb") or "187c:0550").lower()
_USB_VID = _USB.split(":")[0]
_USB_PIDS = tuple(p for p in ({_USB.split(":", 1)[1] if ":" in _USB else "0550",
                               "0550", "0551"}))
ZONE_COUNT = 4                            # physical zones (label/schema only — the
                                         # 5515 firmware ignores zone-scoped writes)
GKEY_ZONE = 0                             # the G-key sits in the leftmost zone
ZONE_NAMES = ["Left", "Middle", "Right", "Numpad"]


class ElcError(RuntimeError):
    pass


def _hexify(c) -> str:
    return c.lstrip("#").upper() if isinstance(c, str) else "%02X%02X%02X" % tuple(c)


class Keyboard:
    """Compatibility shim: the GUI and sensors.py still call
    Keyboard()/_find()/set_zones()/set_brightness()/off(). Everything routes
    to the module-level openrgb helpers."""

    @staticmethod
    def _find():
        if not shutil.which("openrgb"):
            return None
        for vp in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            try:
                if open(vp).read().strip().lower() != _USB_VID:
                    continue
                pid = open(vp.rsplit("/", 1)[0] + "/idProduct").read().strip().lower()
                if pid in _USB_PIDS:
                    return "openrgb:" + DEVICE
            except OSError:
                pass
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def set_zones(self, colors: dict, brightness: int = 100) -> None:
        set_zones({z: _hexify(c) for z, c in colors.items()}, brightness)

    def set_all(self, color, brightness: int = 100) -> None:
        set_all(_hexify(color), brightness)

    def set_effect(self, name, speed=None, brightness: int = 100) -> None:
        set_effect(name, speed, brightness)

    def set_brightness(self, percent: int, zones=None) -> None:
        st = load_state()
        if st:
            set_zones(st[0], max(0, min(100, percent)))
        else:
            _run(["-b", str(max(0, min(100, percent)))])

    def off(self) -> None:
        off()

    def reset(self) -> None:
        reset()


def _openrgb() -> str:
    exe = shutil.which("openrgb")
    if not exe:
        raise ElcError("openrgb not found — install the 'OpenRGB' app (Software tab). "
                       "The G15 5515 keyboard backlight is only reachable through OpenRGB.")
    return exe


def _run_once(args: list[str], server: bool = True) -> str:
    # server=True: talk to a running `openrgb --server` (tuxthrottle-openrgb.service)
    # — ~1s, devices stay initialised. server=False: standalone scan (~4s), the
    # fallback when no server is up.
    cmd = [_openrgb()] + ([] if server else ["--noautoconnect"]) + ["-d", DEVICE, *args]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    # OpenRGB always exits 0 and prints an i2c/SMBus HTML warning to stdout;
    # a real failure shows up as "Device .* not found" / "Connection attempt".
    blob = (out.stdout + out.stderr)
    if "not found" in blob or "Connection attempt failed" in blob:
        raise ElcError(blob.strip().splitlines()[-1] if blob.strip() else "openrgb call failed")
    return blob


# --- single-flight locks -------------------------------------------------- #
# The keyboard "flickers between the colour I set and the desktop-accent
# colour" bug: three unrelated writers (`apply-saved` from the boot service,
# the systemd-sleep hook and the tray, plus the GUI's own apply thread) can
# all be pushing openrgb at the same time, so the last-write-wins race plays
# out visibly for several seconds. Two flocks fix it — a fixed /tmp path so a
# root boot service and the unprivileged tray share the same lock:
#   * WRITE_LOCK  — held for the duration of each openrgb call in `_run`, so
#     no two writes interleave.
#   * REASSERT_LOCK — `apply-saved` takes this non-blocking and bails if held,
#     so boot vs resume vs tray don't stack three re-assert storms, and a live
#     GUI change isn't stomped by a stale re-assert.
_WRITE_LOCK_PATH = "/tmp/tuxthrottle-kbd.write.lock"
_REASSERT_LOCK_PATH = "/tmp/tuxthrottle-kbd.reassert.lock"


def _take_lock(path: str, *, blocking: bool):
    fd = None
    for mode in ("a", "r"):
        try:
            fd = open(path, mode)  # noqa: SIM115 — held for the caller's lifetime
            break
        except OSError:
            continue
    if fd is None:
        return None
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fd.fileno(), flags)
    except OSError:
        fd.close()
        return None
    try:
        os.chmod(path, 0o666)  # let the other UID re-lock it later
    except OSError:
        pass
    return fd


def _run(args: list[str]) -> str:
    """Apply an openrgb command, fast path first.

    Tries the running OpenRGB SDK server (~1s); falls back to a standalone
    scan (~4s) if there's no server. Then writes the same command a second
    time immediately — on this AW-ELC controller a lone write often doesn't
    'take' (keys flash the colour then go dark a beat later); the repeat
    locks it in, with no artificial delay so the GUI stays snappy.

    The whole call holds WRITE_LOCK so a concurrent writer (a stale
    `apply-saved` re-assert, the tray, the GUI thread) can't interleave and
    make the keyboard strobe between two colours."""
    lk = _take_lock(_WRITE_LOCK_PATH, blocking=True)
    try:
        server = True
        try:
            blob = _run_once(args, server=True)
        except (ElcError, subprocess.TimeoutExpired):
            server = False
            blob = _run_once(args, server=False)
        try:
            _run_once(args, server=server)
        except (ElcError, subprocess.TimeoutExpired):
            pass
        return blob
    finally:
        if lk is not None:
            lk.close()


_RESTART_STAMP = "/tmp/tuxthrottle-openrgb-restart.stamp"  # noqa: S108
_RESTART_MIN_GAP = 20.0   # seconds — never bounce the SDK server faster than this


def _restart_too_recent() -> bool:
    """True if the OpenRGB SDK server was restarted < _RESTART_MIN_GAP ago.
    Every restart triggers a full OpenRGB device rescan (SMBus + HID
    enumeration of *every* device); doing that in a tight loop — which a
    spectrum-effect re-assert storm used to — collides with other HID
    consumers (Steam's controller enumeration) hard enough to SIGSEGV them."""
    try:
        return (time.time() - os.path.getmtime(_RESTART_STAMP)) < _RESTART_MIN_GAP
    except OSError:
        return False


def restart_server(force: bool = False) -> bool:
    """Kick the OpenRGB SDK server. After a lot of mode changes this AW-ELC
    controller wedges — the CLI still exits 0 but the keyboard stops
    responding ('frozen'). Restarting the server (which re-opens the HID
    device) clears it. Tries the systemd unit first, then a plain pkill so it
    respawns / a later call falls back to the standalone --noautoconnect path.
    Rate-limited (see `_restart_too_recent`) unless `force`. Returns True if it
    did something."""
    if not force and _restart_too_recent():
        return False
    try:
        open(_RESTART_STAMP, "w").close()
    except OSError:
        pass
    for cmd in (["systemctl", "restart", "tuxthrottle-openrgb"],
                ["sudo", "-n", "systemctl", "restart", "tuxthrottle-openrgb"]):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0:
                time.sleep(2.5)
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        subprocess.run(["pkill", "-f", "openrgb --server"], capture_output=True, timeout=10)
        time.sleep(2.0)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def reset() -> None:
    """Unfreeze the backlight: restart the server, then re-assert saved state
    (or a plain white static fallback). `force` — this is the explicit
    user 'unfreeze' action, it should always bounce the server."""
    restart_server(force=True)
    st = load_state()
    if not st:
        _run_once(["-m", "Static", "-c", "FFFFFF", "-b", "100"], server=False)
        return
    meta = load_meta()
    zones, br = st
    if meta.get("mode") in ALL_EFFECTS:
        set_effect(meta["mode"], meta.get("speed", 50), br)
    else:
        set_zones(zones, br)


def _hexval(s: str) -> str:
    s = s.lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise argparse.ArgumentTypeError("colour must be RRGGBB hex")
    return s.upper()


# ---- operations ----------------------------------------------------------- #

def _leave_effect_kick(target_mode: str | None = None) -> None:
    """The AW-ELC will NOT switch out of a firmware effect (Spectrum Cycle,
    etc.) on a plain `-m Static` write — the keys flash the new colour for an
    instant, then the MCU effect just carries on. Verified live: the only
    thing that reliably clears it is restarting the OpenRGB SDK server (its
    HID connection state is what's stuck). So kick the server ONLY when we are
    actually leaving an effect for a *different* mode — re-asserting the same
    effect (the tray / boot / resume re-assert, saved mode = spectrum) must
    NOT restart the server, or overlapping re-asserts turn into a restart
    storm that crashes other HID consumers (Steam)."""
    try:
        saved = load_meta().get("mode")
        if saved in ALL_EFFECTS and saved != target_mode:
            restart_server()
    except Exception:  # noqa: BLE001
        pass


def set_all(color, brightness: int = 100) -> None:
    _leave_effect_kick()  # leave any firmware effect (Spectrum Cycle)
    _run(["-m", "Static", "-c", _hexify(color), "-b", str(max(0, min(100, brightness)))])


def set_zone(zone: int, color, brightness: int = 100) -> None:
    # The 5515's AW-ELC is a SINGLE controllable zone. OpenRGB advertises 4/16
    # zones, but every write path — CLI `-z`, the SDK per-LED buffer (4/8/16
    # entries), and a raw HID user-animation with per-zone SELECT — lands on
    # the whole keyboard (camera-verified on hardware). So "per zone" == whole
    # keyboard; the last colour wins.
    set_all(color, brightness)


def set_zones(colors: dict, brightness: int = 100) -> None:
    set_all(next(iter(colors.values())) if colors else "FFFFFF", brightness)


def off() -> None:
    _leave_effect_kick()
    _run(["-b", "0"])


# Hardware effect modes exposed through OpenRGB. Only Spectrum Cycle actually
# animates on this AW-ELC (fw 1.1.12) — camera-verified: Breathing and Flashing
# just hold a steady colour, and Rainbow Wave is washed-out with dark gaps, so
# none of those are offered.
EFFECT_MODES = {
    "spectrum": "Spectrum Cycle",
}
# every mode key the GUI / saved state may use, for effect-vs-static dispatch.
ALL_EFFECTS = set(EFFECT_MODES)


def set_effect(name: str, speed: int | None = None, brightness: int = 100) -> None:
    """Apply a firmware lighting effect (`spectrum` — the only one this AW-ELC
    animates). `speed` is 0-100 where 100 = fastest. Every mode on this
    controller reports a degenerate brightness range (min=100/max=0) and
    empirically `-b 100` is what lights it, so the caller's brightness is
    passed straight through (`-b 0` leaves it dark)."""
    _leave_effect_kick(name)  # clear a stuck *prior different* effect — but not
                              # when we're just re-asserting the same one
    mode = EFFECT_MODES.get(name, name)
    args = ["-m", mode, "-b", str(max(0, min(100, brightness)))]
    if speed is not None:
        args += ["-s", str(100 - max(0, min(100, int(speed))))]
    _run(args)


def info() -> dict:
    txt = subprocess.run([_openrgb(), "--noautoconnect", "-l"],
                         capture_output=True, text=True, timeout=40).stdout
    seen = DEVICE in txt or "AW-ELC" in txt or "AlienWare" in txt
    plat = ""
    for line in txt.splitlines():
        if "platform:" in line and ("Unknown" in line or "Known" in line):
            plat = line.split("platform:")[1].strip().split(",")[0].split()[0]
    return {"backend": "openrgb", "device": DEVICE, "detected": seen,
            "platform_id": plat or "?", "physical_zones": ZONE_COUNT}


# ---- persisted state (so a login/resume service can re-assert it) -------- #

def _invoking_pw():
    """The real desktop user's pwd entry when running elevated. pkexec sets
    PKEXEC_UID (no PKEXEC_USER!), sudo sets SUDO_USER/SUDO_UID — check them
    all, or the state file lands in /root and the boot service never sees it."""
    import pwd as _pwd
    for var in ("SUDO_USER", "PKEXEC_USER"):
        name = os.environ.get(var)
        if name:
            try:
                return _pwd.getpwnam(name)
            except KeyError:
                pass
    for var in ("PKEXEC_UID", "SUDO_UID"):
        val = os.environ.get(var)
        if val:
            try:
                return _pwd.getpwuid(int(val))
            except (KeyError, ValueError):
                pass
    return None


def _state_path() -> str:
    override = os.environ.get("TUXTHROTTLE_KBD_STATE")
    if override:
        return override
    pw = _invoking_pw()
    home = pw.pw_dir if pw else os.path.expanduser("~")
    return os.path.join(home, ".config", "tuxthrottle", "kbd.json")


def accent_hex(fallback: str = "FF8800") -> str:
    """Current Plasma accent colour as 'RRGGBB', from the invoking user's
    ~/.config/kdeglobals [General] AccentColor ('r,g,b' or '#rrggbb').
    `fallback` (used when it can't be read) may be passed with or without '#'.
    """
    fb = fallback.lstrip("#") or "FF8800"
    pw = _invoking_pw()
    home = pw.pw_dir if pw else os.path.expanduser("~")
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(os.path.join(home, ".config", "kdeglobals"))
    except (configparser.Error, OSError):
        return fb
    for sec, key in (("General", "AccentColor"),
                     ("Colors:Selection", "DecorationFocus")):
        if not cp.has_option(sec, key):
            continue
        v = cp.get(sec, key).strip()
        if "," in v:
            try:
                r, g, b = (int(x) & 255 for x in v.split(",")[:3])
                return f"{r:02X}{g:02X}{b:02X}"
            except ValueError:
                continue
        h = v.lstrip("#")
        if len(h) >= 6:
            return h[:6].upper()
    return fb


def save_state(zone_colors: dict, brightness: int, mode: str = "zones",
               speed: int = 50, push_accent: bool | None = None) -> None:
    """Persist the lighting so the boot/resume service can re-assert it.
    `mode` is 'zones' (static whole-keyboard colour), 'spectrum' (the firmware
    effect), or 'accent' (follow the live Plasma accent on each re-assert).
    `push_accent` (GUI-only convenience flag: the desktop accent tracks this
    colour) is kept as given, or carried over from the existing file when
    None."""
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if push_accent is None:
        try:
            push_accent = bool(json.load(open(path)).get("push_accent", False))
        except (OSError, ValueError):
            push_accent = False
    doc = {"brightness": int(brightness),
           "mode": mode,
           "speed": int(speed),
           "push_accent": bool(push_accent),
           "zones": {str(z): _hexify(c) for z, c in zone_colors.items()}}
    json.dump(doc, open(path, "w"), indent=2)
    pw = _invoking_pw()
    if pw and os.geteuid() == 0:
        os.chown(path, pw.pw_uid, pw.pw_gid)
        os.chown(os.path.dirname(path), pw.pw_uid, pw.pw_gid)
        try:
            os.chown(os.path.dirname(os.path.dirname(path)), pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def load_state() -> tuple[dict[int, tuple[int, int, int]], int] | None:
    """Returns ({physical_zone: (r,g,b)}, brightness). RGB tuples for the
    GUI's benefit; the set_* helpers accept tuples or hex."""
    try:
        d = json.load(open(_state_path()))
    except (OSError, ValueError):
        return None
    zones = {}
    for z, c in d.get("zones", {}).items():
        if isinstance(c, str):
            c = c.lstrip("#")
            c = (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
        zones[int(z)] = tuple(c)
    return (zones, int(d.get("brightness", 100))) if zones else None


def load_meta() -> dict:
    """{'mode', 'speed'} from the saved state — separate from load_state() so
    its 2-tuple callers don't change."""
    try:
        d = json.load(open(_state_path()))
    except (OSError, ValueError):
        return {"mode": "zones", "speed": 50, "push_accent": False}
    return {"mode": d.get("mode", "zones"), "speed": int(d.get("speed", 50)),
            "push_accent": bool(d.get("push_accent", False))}


# ---- CLI ---------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_on = sub.add_parser("on")
    p_on.add_argument("--color", type=_hexval, default="FFFFFF")
    p_on.add_argument("--brightness", type=int, default=100)

    sub.add_parser("off")

    p_z = sub.add_parser("zone")
    p_z.add_argument("index", type=int, choices=range(ZONE_COUNT))
    p_z.add_argument("--color", type=_hexval, required=True)
    p_z.add_argument("--brightness", type=int, default=None)

    p_e = sub.add_parser("effect")
    p_e.add_argument("name", choices=sorted(ALL_EFFECTS))
    p_e.add_argument("--speed", type=int, default=50)
    p_e.add_argument("--brightness", type=int, default=100)

    sub.add_parser("apply-saved")
    sub.add_parser("reset")
    sub.add_parser("info")

    a = p.parse_args(argv)
    try:
        if a.cmd == "info":
            for k, v in info().items():
                print(f"{k:15}: {v}")
        elif a.cmd == "on":
            set_all(a.color, a.brightness)
            save_state(dict.fromkeys(range(ZONE_COUNT), a.color), a.brightness)
            print(f"backlight on: #{a.color.lower()} @ {a.brightness}%")
        elif a.cmd == "off":
            off()
            print("backlight off")
        elif a.cmd == "reset":
            reset()
            print("backlight reset (server restarted, saved state re-applied)")
        elif a.cmd == "zone":
            st = load_state()
            colors = st[0] if st else dict.fromkeys(range(ZONE_COUNT), "FFFFFF")
            br = a.brightness if a.brightness is not None else (st[1] if st else 100)
            colors[a.index] = a.color
            set_zones(colors, br)
            save_state(colors, br)
            print(f"zone {a.index} ({ZONE_NAMES[a.index]}) -> #{a.color.lower()} @ {br}%")
        elif a.cmd == "effect":
            st = load_state()
            colors = st[0] if st else dict.fromkeys(range(ZONE_COUNT), "FFFFFF")
            set_effect(a.name, a.speed, a.brightness)
            save_state(colors, a.brightness, mode=a.name, speed=a.speed)
            print(f"effect {a.name} @ speed {a.speed}, {a.brightness}%")
        elif a.cmd == "apply-saved":
            st = load_state()
            if not st:
                print("no saved lighting state")
                return 0
            meta = load_meta()
            zones, br = st

            # If another re-assert (boot vs resume vs tray) or a live GUI apply
            # is already running, don't pile a second colour storm on top — the
            # newer writer wins, this stale one yields. (WRITE_LOCK in _run only
            # stops mid-write interleave; this stops the overlap entirely.)
            _guard = _take_lock(_REASSERT_LOCK_PATH, blocking=False)
            if _guard is None:
                print("another keyboard write is in progress — skipping re-assert")
                return 0

            # Resolve the accent ONCE up front — re-reading kdeglobals on every
            # loop pass could catch Plasma mid-accent-change and assert two
            # different colours in the same run.
            _accent_target = None
            if meta["mode"] == "accent":
                _fallback = next(iter(zones.values()), (255, 255, 255))
                _accent_target = accent_hex(_hexify(_fallback))

            def _assert_saved():
                if meta["mode"] in ALL_EFFECTS:
                    set_effect(meta["mode"], meta.get("speed", 50), br)
                elif meta["mode"] == "accent":
                    set_all(_accent_target, br)
                else:
                    set_zones(zones, br)

            # Cold boot / resume: the USB HID device *and* the OpenRGB SDK
            # server can take a while to be ready — the single fixed sleep the
            # systemd unit used before wasn't enough, so the colour "didn't
            # persist". Wait for the controller to actually appear, then assert
            # it several times spaced out (this controller sometimes takes the
            # first write then blanks a beat later).
            deadline = time.time() + 45
            seen = False
            while time.time() < deadline:
                try:
                    txt = subprocess.run([_openrgb(), "--noautoconnect", "-l"],
                                         capture_output=True, text=True,
                                         timeout=25).stdout
                    if DEVICE in txt or "AW-ELC" in txt.upper():
                        seen = True
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
                time.sleep(2)
            if not seen:
                print("warning: keyboard controller not detected yet — trying anyway",
                      file=sys.stderr)

            # Two passes (was six): _run already double-writes each command, so
            # 2×2 = 4 writes is plenty to make it stick, and a shorter window
            # means far less chance of overlapping anything else.
            last = None
            for i in range(2):
                if i:
                    time.sleep(1.5)
                try:
                    _assert_saved()
                    last = None
                except ElcError as exc:
                    last = exc
            if last:
                raise last
            print(f"re-applied saved lighting ({meta['mode']}) @ {br}%")
    except ElcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
