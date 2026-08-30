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
    tuxthrottle_kbd.py rainbow-test          # verify the wave maths, no hardware
    tuxthrottle_kbd.py apply-saved
    tuxthrottle_kbd.py reset
    tuxthrottle_kbd.py info

`spectrum` is the one working firmware mode (MCU-driven); *speed* is 0-100
(100 = fastest). The `rainbow-wave` / `gradient-wave` software daemons remain
only for the `*-test` self-checks and are not wired to anything.
"""
from __future__ import annotations

import argparse
import colorsys
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time

DEVICE = "Dell G Series LED Controller"   # OpenRGB's name for the AW-ELC on the G15
ZONE_COUNT = 4                            # physical zones
GKEY_ZONE = 0                             # the G-key sits in the leftmost zone
ZONE_NAMES = ["Left", "Middle", "Right", "Numpad"]
LOGICAL_ZONES = 16                       # OpenRGB drives it as 16; ~4 per physical zone
MIN_BRIGHTNESS = 0                        # OpenRGB's brightness-0 is recoverable here


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
        import glob
        for vp in glob.glob("/sys/bus/usb/devices/*/idVendor"):
            try:
                if open(vp).read().strip().lower() != "187c":
                    continue
                pid = open(vp.rsplit("/", 1)[0] + "/idProduct").read().strip().lower()
                if pid in ("0550", "0551"):
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


def _run(args: list[str]) -> str:
    """Apply an openrgb command, fast path first.

    Tries the running OpenRGB SDK server (~1s); falls back to a standalone
    scan (~4s) if there's no server. Then writes the same command a second
    time immediately — on this AW-ELC controller a lone write often doesn't
    'take' (keys flash the colour then go dark a beat later); the repeat
    locks it in, with no artificial delay so the GUI stays snappy."""
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


def restart_server() -> bool:
    """Kick the OpenRGB SDK server. After a lot of mode changes this AW-ELC
    controller wedges — the CLI still exits 0 but the keyboard stops
    responding ('frozen'). Restarting the server (which re-opens the HID
    device) clears it. Tries the systemd unit first, then a plain pkill so it
    respawns / a later call falls back to the standalone --noautoconnect path.
    Returns True if it did something."""
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
    (or a plain white static fallback)."""
    restart_server()
    st = load_state()
    if not st:
        _run_once(["-m", "Static", "-c", "FFFFFF", "-b", "100"], server=False)
        return
    meta = load_meta()
    zones, br = st
    if meta.get("mode") in ALL_EFFECTS:
        set_effect(meta["mode"], meta.get("speed", 50), br,
                   gradient=meta.get("gradient"))
    else:
        set_zones(zones, br)


# ======================================================================= #
#  Software rainbow wave — per-LED animated spectrum, streamed over the
#  OpenRGB SDK socket (the `openrgb` CLI is ~1s/call, far too slow for an
#  animation; a persistent socket does 30+ fps fine).
#
#  Why software: the controller's firmware "Rainbow Wave" mode takes no
#  colour and no direction and renders washed-out with dark gaps — nothing
#  we send can fix it. This draws the wave ourselves across the 16 logical
#  LEDs the controller exposes.
# ======================================================================= #

_SDK_HOST, _SDK_PORT = "127.0.0.1", 6742
_CMD_CONTROLLER_COUNT = 0
_CMD_CONTROLLER_DATA = 1
_CMD_PROTOCOL_VERSION = 40
_CMD_SET_CLIENT_NAME = 50
_CMD_UPDATE_LEDS = 1050

RAINBOW_DEFAULT_FPS = 4
RAINBOW_DEFAULT_CYCLE_S = 24.0     # seconds per full loop (slow — see _HW_MAX_FPS)


class _Sdk:
    """Minimal OpenRGB SDK-server client: negotiate, find the device, stream
    per-LED colours. Just enough for the rainbow-wave daemon."""

    def __init__(self, timeout: float = 4.0):
        self.sock = socket.create_connection((_SDK_HOST, _SDK_PORT), timeout)
        self.sock.settimeout(timeout)
        self._send(0, _CMD_SET_CLIENT_NAME, b"tuxthrottle-fx\0")
        self._send(0, _CMD_PROTOCOL_VERSION, struct.pack("<I", 5))
        try:
            _, _, body = self._recv()
            self.version = min(5, struct.unpack("<I", body[:4])[0]) if len(body) >= 4 else 1
        except (OSError, struct.error, ElcError):
            self.version = 1

    def _send(self, dev, pid, data=b""):
        self.sock.sendall(b"ORGB" + struct.pack("<III", dev, pid, len(data)) + data)

    def _recvn(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ElcError("OpenRGB server closed the connection")
            buf += chunk
        return buf

    def _recv(self):
        hdr = self._recvn(16)
        if hdr[:4] != b"ORGB":
            raise ElcError("bad reply from OpenRGB server")
        dev, pid, size = struct.unpack("<III", hdr[4:16])
        return dev, pid, (self._recvn(size) if size else b"")

    def count(self):
        self._send(0, _CMD_CONTROLLER_COUNT)
        return struct.unpack("<I", self._recv()[2][:4])[0]

    def _raw(self, idx):
        self._send(idx, _CMD_CONTROLLER_DATA, struct.pack("<I", self.version))
        return self._recv()[2]

    def find(self, name_sub: str) -> int:
        target = name_sub.encode()
        for i in range(self.count()):
            if target in self._raw(i):
                return i
        raise ElcError(f"OpenRGB server has no device matching {name_sub!r}")

    def update_leds(self, idx, rgb_list):
        payload = struct.pack("<H", len(rgb_list))
        for r, g, b in rgb_list:
            payload += struct.pack("<BBBB", r & 255, g & 255, b & 255, 0)
        self._send(idx, _CMD_UPDATE_LEDS, struct.pack("<I", len(payload) + 4) + payload)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _wrap360(deg: float) -> float:
    return deg % 360.0


def hsv_to_rgb255(hue_deg: float, sat: float, val: float, gamma: float = 1.0):
    """Circular hue (degrees) → clamped 0-255 RGB. Hue is taken on the HSV
    wheel and only converted here, so 360°→0° is seamless and transitions
    never go muddy."""
    r, g, b = colorsys.hsv_to_rgb(_wrap360(hue_deg) / 360.0,
                                  max(0.0, min(1.0, sat)),
                                  max(0.0, min(1.0, val)))
    if gamma and gamma != 1.0:
        r, g, b = (max(0.0, c) ** gamma for c in (r, g, b))
    return (max(0, min(255, round(r * 255))),
            max(0, min(255, round(g * 255))),
            max(0, min(255, round(b * 255))))


# ----- perceptual colour (OKLab / OKLCH), stdlib only -------------------- #
#
# The rainbow/gradient engines blend in OKLab so hue sweeps keep an even
# perceived brightness (HSV yellow/cyan glare, blue muddies) and gradients
# between distant colours stay clean instead of going grey. Conversion:
# sRGB -> linear -> OKLab, blend, OKLab -> linear -> sRGB. Matrices are the
# reference Björn Ottosson OKLab constants.


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    c = 0.0 if c < 0.0 else (1.0 if c > 1.0 else c)
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1.0 / 3.0), x)


def _linsrgb_to_oklab(r: float, g: float, b: float):
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = _cbrt(l), _cbrt(m), _cbrt(s)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _oklab_to_linsrgb(L: float, a: float, b: float):
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (+4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def _oklab_to_oklch(L: float, a: float, b: float):
    return (L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360.0)


def _oklch_to_oklab(L: float, C: float, h_deg: float):
    h = math.radians(h_deg)
    return (L, C * math.cos(h), C * math.sin(h))


def hex_to_srgb01(hx: str):
    hx = hx.lstrip("#")
    return (int(hx[0:2], 16) / 255.0, int(hx[2:4], 16) / 255.0, int(hx[4:6], 16) / 255.0)


def oklab_of_hex(hx: str):
    r, g, b = (_srgb_to_linear(c) for c in hex_to_srgb01(hx))
    return _linsrgb_to_oklab(r, g, b)


def _oklab_to_lin01(L: float, a: float, b: float):
    """OKLab -> linear-sRGB clamped to [0,1]. If the colour is outside the
    sRGB gamut, walk chroma down (toward the neutral axis) until it fits, so
    bright hues clip gracefully instead of turning into a channel spike."""
    r, g, bl = _oklab_to_linsrgb(L, a, b)
    if min(r, g, bl) < -5e-4 or max(r, g, bl) > 1.0005:
        lo, hi = 0.0, 1.0
        for _ in range(18):
            mid = 0.5 * (lo + hi)
            r2, g2, b2 = _oklab_to_linsrgb(L, a * mid, b * mid)
            if min(r2, g2, b2) < -5e-4 or max(r2, g2, b2) > 1.0005:
                hi = mid
            else:
                lo = mid
        r, g, bl = _oklab_to_linsrgb(L, a * lo, b * lo)
    return (max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, bl)))


# 8×8 ordered Bayer matrix (0..63), for optional low-bit temporal dithering
_BAYER8 = (
    (0, 32, 8, 40, 2, 34, 10, 42), (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38), (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41), (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 7, 39, 13, 45, 5, 37), (63, 31, 55, 23, 61, 29, 53, 21),
)


def _fx_pidfile() -> str:
    return os.path.join(os.path.dirname(_state_path()), "fx.pid")


def _chown_to_invoker(path: str) -> None:
    pw = _invoking_pw()
    if pw and os.geteuid() == 0:
        try:
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def _fx_running_pid():
    try:
        pid = int(open(_fx_pidfile()).read().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, ProcessLookupError):
        return None
    except PermissionError:
        return -1   # exists but owned by another uid


def _pkill_fx_daemons() -> None:
    """Belt-and-suspenders: kill any lingering software-wave daemon whose PID
    never made it into fx.pid (the SDK connect can outlast the spawning call).
    Never kills the current process (a daemon calls stop_fx() at startup)."""
    pat = r"tuxthrottle_kbd(\.py)? (rainbow|gradient)-wave"
    me = os.getpid()
    try:
        out = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for tok in out.split():
        try:
            victim = int(tok)
        except ValueError:
            continue
        if victim == me:
            continue
        try:
            os.kill(victim, signal.SIGTERM)
            time.sleep(0.1)
            os.kill(victim, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            try:                       # daemon owned by another uid (pkexec)
                subprocess.run(["sudo", "-n", "kill", "-9", str(victim)],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass


def stop_fx() -> bool:
    """Stop a running software-effect daemon (the gradient wave). Cheap no-op
    when none is running — every other keyboard write calls this, so switching
    away from the wave is automatic. Returns True if one was signalled."""
    pid = _fx_running_pid()
    if pid is None:
        try:
            os.unlink(_fx_pidfile())
        except OSError:
            pass
        _pkill_fx_daemons()
        return False
    if pid == -1:
        _pkill_fx_daemons()
        return True
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            time.sleep(0.05)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.unlink(_fx_pidfile())
    except OSError:
        pass
    _pkill_fx_daemons()      # sweep any sibling daemon not in fx.pid
    return True


# The AW-ELC only repaints a few times/sec over USB, so streaming faster just
# builds a backlog that plays back as visible "stepping". The software waves
# are therefore slow ambient effects: capped send rate, long cycles.
_HW_MAX_FPS = 5


def _speed_to_cycle(speed_pct: float) -> float:
    """GUI speed slider (0-100, 100 = fastest) → seconds per full cycle for the
    software waves. Deliberately slow (40s..12s) — anything quicker steps
    visibly on this controller. Slider 50 ≈ 26s."""
    return 40.0 - max(0.0, min(100.0, speed_pct)) / 100.0 * 28.0


def _spawn_fx(subcmd: str, extra: list[str]) -> None:
    stop_fx()
    args = [sys.executable, os.path.abspath(__file__), subcmd, *extra]
    subprocess.Popen(args, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------------- #
#  Shared animation engine — used by rainbow_wave() and gradient_wave().
#
#  Per frame: one phase from the monotonic clock (reused for every LED so the
#  board stays coherent), then per-LED colour from a wrapped spatial position
#  u ∈ [0,1).  `color_at(u)` returns LINEAR-sRGB (0..1, gamut-mapped). The
#  engine then encodes to sRGB, applies brightness, optional adjacent-LED
#  smoothing, optional ordered dithering, and clamps to 0..255 — in that
#  order (no double gamma).
# ------------------------------------------------------------------------- #

def _finish_frame(lin_rows, bright01: float, smooth: float, dither: bool,
                  frame: int, gamma: float):
    enc = []
    for (r, g, b) in lin_rows:
        e = [_linear_to_srgb(r), _linear_to_srgb(g), _linear_to_srgb(b)]
        if gamma and gamma != 1.0:
            e = [max(0.0, x) ** (1.0 / gamma) for x in e]
        enc.append([x * bright01 for x in e])
    n = len(enc)
    if smooth > 0.0 and n >= 3:
        sm = []
        for i in range(n):
            lft = enc[i - 1] if i > 0 else enc[i]
            rgt = enc[i + 1] if i < n - 1 else enc[i]
            cur = enc[i]
            sm.append([(1.0 - smooth) * cur[j]
                       + smooth * (0.25 * lft[j] + 0.5 * cur[j] + 0.25 * rgt[j])
                       for j in range(3)])
        enc = sm
    out = []
    for i, c in enumerate(enc):
        px = []
        for j in range(3):
            val = c[j] * 255.0
            if dither:
                val += _BAYER8[(i + j * 2) & 7][(frame + i * 3 + j) & 7] / 64.0 - 0.5
            px.append(0 if val < 0 else (255 if val > 255 else int(val + 0.5)))
        out.append((px[0], px[1], px[2]))
    return out


def _stream_wave(color_at, *, cycle_seconds: float, direction: int,
                 wavelength: float, fps: int, leds: int, brightness: int,
                 smooth: float, dither: bool, ease: str, gamma: float,
                 run_seconds: float | None, label: str) -> None:
    bright01 = max(0.0, min(1.0, brightness / 100.0))
    dir_ = 1 if direction >= 0 else -1
    denom = max(1, leds - 1)
    period = 1.0 / max(1, min(_HW_MAX_FPS, int(fps)))   # capped — see _HW_MAX_FPS
    cyc = max(0.1, cycle_seconds)

    stop = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: stop.__setitem__("now", True))

    # be the only running software effect — kill any predecessor (a bare CLI
    # invocation doesn't go through _spawn_fx, so do it here too)
    stop_fx()

    for srv in (True, False):
        try:
            _run_once(["-m", "Static", "-b", "100"], server=srv)
            break
        except (ElcError, subprocess.TimeoutExpired):
            continue

    try:
        sdk = _Sdk()
        idx = sdk.find(DEVICE)
    except (OSError, ElcError, struct.error) as exc:
        raise ElcError(f"{label} needs the OpenRGB SDK server ({exc})")

    pf = _fx_pidfile()
    mypid = str(os.getpid())
    try:
        os.makedirs(os.path.dirname(pf), exist_ok=True)
        with open(pf, "w") as f:
            f.write(mypid)
        _chown_to_invoker(pf)
    except OSError:
        pass

    t0 = time.monotonic()
    lin = [(0.0, 0.0, 0.0)] * leds
    frame = 0
    last_buf = None
    try:
        while not stop["now"]:
            fs = time.monotonic()
            elapsed = fs - t0
            if run_seconds is not None and elapsed >= run_seconds:
                break
            p01 = (dir_ * elapsed / cyc) % 1.0
            if ease == "sine":
                p01 = (1.0 - math.cos(2.0 * math.pi * p01)) / 2.0
            for i in range(leds):
                u = ((i / denom) * wavelength + p01) % 1.0
                lin[i] = color_at(u)
            buf = _finish_frame(lin, bright01, smooth, dither, frame, gamma)
            try:
                if buf != last_buf:          # don't re-push an identical frame
                    sdk.update_leds(idx, buf)
                    last_buf = buf
            except (OSError, ElcError, struct.error):
                try:
                    sdk.close()
                    sdk = _Sdk()
                    idx = sdk.find(DEVICE)
                except (OSError, ElcError, struct.error):
                    break
            frame += 1
            slack = period - (time.monotonic() - fs)
            if slack > 0:
                time.sleep(slack)
    finally:
        try:
            sdk.close()
        except Exception:  # noqa: BLE001
            pass
        try:                       # only remove the pidfile if it's still ours
            if open(pf).read().strip() == mypid:
                os.unlink(pf)
        except OSError:
            pass


# fixed OKLCH lightness / chroma for the perceptual rainbow — picked to stay
# (mostly) in sRGB gamut all the way round the hue circle while still reading
# as a vivid rainbow. `_oklab_to_lin01` maps the few residual out-of-gamut
# hues gracefully.
_RAINBOW_OKLCH_L = 0.72
_RAINBOW_OKLCH_C = 0.15


def rainbow_wave(cycle_seconds: float = RAINBOW_DEFAULT_CYCLE_S,
                 brightness: int = 100, saturation: float = 1.0,
                 direction: int = 1, wavelength: float = 1.0,
                 fps: int = 4, gamma: float = 1.0,
                 leds: int = LOGICAL_ZONES, run_seconds: float | None = None,
                 base_hue: float = 0.0, blend: str = "oklch",
                 smooth: float = 0.12, dither: bool = True,
                 ease: str = "linear") -> None:
    """Smooth horizontal spectrum wave. Default path sweeps hue on the OKLCH
    wheel at constant perceptual lightness/chroma, so the whole keyboard
    keeps an even brightness as the colours travel (HSV makes yellow/cyan
    glare and blue go muddy). `blend="hsv"` restores the old HSV sweep.

        u       = wrap01(led/(leds-1) * wavelength + dir*elapsed/cycle_seconds)
        hue_deg = wrap360(360*u + base_hue)
        colour  = OKLCH(L0, C0, hue_deg)  ->  linear sRGB  ->  sRGB  ->  ...

    Phase comes from the monotonic clock (frame-rate independent) and one
    phase is used for the whole frame. Runs until SIGINT/SIGTERM or
    `run_seconds`."""
    _stream_wave(rainbow_color_at(blend, base_hue, saturation),
                 cycle_seconds=cycle_seconds, direction=direction,
                 wavelength=wavelength, fps=fps, leds=leds, brightness=brightness,
                 smooth=smooth, dither=dither, ease=ease, gamma=gamma,
                 run_seconds=run_seconds, label="rainbow wave")


def gradient_wave(colors, cycle_seconds: float = 24.0, brightness: int = 100,
                  direction: int = 1, wavelength: float = 1.0,
                  fps: int = 4, gamma: float = 1.0, blend: str = "oklab",
                  min_value: float = 0.15, max_value: float = 1.0,
                  smooth: float = 0.12, dither: bool = True,
                  ease: str = "linear", leds: int = LOGICAL_ZONES,
                  run_seconds: float | None = None) -> None:
    """Animated travelling gradient built from 1-6 anchor colours.

    * 1 anchor  → a looping brightness envelope of that hue sweeps across
      (a moving 'comet' on a dim base of the same colour); `min_value` /
      `max_value` set the dim/bright ends.
    * 2-6 anchors → a closed loop c1→c2→…→cN→c1 sampled per LED at a wrapped
      position and interpolated (OKLab + smoothstep by default; `linear` =
      linear-sRGB lerp, `hsv` = hue lerp fallback).

    Same monotonic-time phase / seamless wrap / direction as the rainbow."""
    _stream_wave(gradient_color_at(colors, blend, min_value, max_value),
                 cycle_seconds=cycle_seconds, direction=direction,
                 wavelength=wavelength, fps=fps, leds=leds, brightness=brightness,
                 smooth=smooth, dither=dither, ease=ease, gamma=gamma,
                 run_seconds=run_seconds, label="gradient wave")


def rainbow_color_at(blend: str = "oklch", base_hue: float = 0.0,
                     saturation: float = 1.0):
    """Build the per-position colour function for the rainbow: u∈[0,1) →
    linear-sRGB (0..1). Shared by `rainbow_wave` and the self-test."""
    if blend == "hsv":
        sat = max(0.0, min(1.0, saturation))

        def color_at(u):
            r, g, b = colorsys.hsv_to_rgb(_wrap360(360.0 * u + base_hue) / 360.0, sat, 1.0)
            return (_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b))
        return color_at

    def color_at(u):
        L, a, b = _oklch_to_oklab(_RAINBOW_OKLCH_L, _RAINBOW_OKLCH_C,
                                  _wrap360(360.0 * u + base_hue))
        return _oklab_to_lin01(L, a, b)
    return color_at


def gradient_color_at(colors, blend: str = "oklab", min_value: float = 0.15,
                      max_value: float = 1.0):
    """Build the per-position colour function for the gradient: u∈[0,1) →
    linear-sRGB (0..1). Shared by `gradient_wave` and the self-test."""
    hexes = [c.lstrip("#").upper() for c in colors]
    if not 1 <= len(hexes) <= 6:
        raise ElcError("gradient needs 1 to 6 anchor colours")
    mn = max(0.0, min(1.0, min_value))
    mx = max(mn, min(1.0, max_value))
    M = len(hexes)

    if M == 1:
        base_lab = oklab_of_hex(hexes[0])

        def color_at(u):
            env = mn + (mx - mn) * (0.5 - 0.5 * math.cos(2.0 * math.pi * u))
            return _oklab_to_lin01(base_lab[0] * env, base_lab[1] * env, base_lab[2] * env)
        return color_at

    def _seg(u):
        s = u * M
        k = int(s) % M
        f = s - math.floor(s)
        return k, (f * f * (3.0 - 2.0 * f))            # smoothstep

    if blend == "linear":
        lin = [tuple(_srgb_to_linear(x) for x in hex_to_srgb01(h)) for h in hexes]

        def color_at(u):
            k, fs = _seg(u)
            a, b = lin[k], lin[(k + 1) % M]
            return tuple(max(0.0, min(1.0, a[j] + (b[j] - a[j]) * fs)) for j in range(3))
        return color_at

    if blend == "hsv":
        hsv = [colorsys.rgb_to_hsv(*hex_to_srgb01(h)) for h in hexes]

        def color_at(u):
            k, fs = _seg(u)
            (h0, s0, v0), (h1, s1, v1) = hsv[k], hsv[(k + 1) % M]
            dh = ((h1 - h0 + 0.5) % 1.0) - 0.5          # shortest way round
            r, g, b = colorsys.hsv_to_rgb((h0 + dh * fs) % 1.0,
                                          s0 + (s1 - s0) * fs, v0 + (v1 - v0) * fs)
            return (_srgb_to_linear(r), _srgb_to_linear(g), _srgb_to_linear(b))
        return color_at

    labs = [oklab_of_hex(h) for h in hexes]                # oklab (default)

    def color_at(u):
        k, fs = _seg(u)
        a, b = labs[k], labs[(k + 1) % M]
        return _oklab_to_lin01(a[0] + (b[0] - a[0]) * fs,
                               a[1] + (b[1] - a[1]) * fs,
                               a[2] + (b[2] - a[2]) * fs)
    return color_at


def _render_frame(color_at, *, leds=16, wavelength=1.0, direction=1,
                  cycle_seconds=4.0, elapsed=0.0, brightness=100, smooth=0.12,
                  dither=False, ease="linear", gamma=1.0, frame=0):
    """No-hardware: what one frame's 0..255 buffer would be. Mirrors
    _stream_wave's per-frame maths exactly."""
    denom = max(1, leds - 1)
    p01 = (direction * elapsed / max(0.1, cycle_seconds)) % 1.0
    if ease == "sine":
        p01 = (1.0 - math.cos(2.0 * math.pi * p01)) / 2.0
    lin = [color_at((((i / denom) * wavelength + p01) % 1.0)) for i in range(leds)]
    return _finish_frame(lin, max(0.0, min(1.0, brightness / 100.0)),
                         smooth, dither, frame, gamma)


def _lightness(rgb255):
    r, g, b = (_srgb_to_linear(c / 255.0) for c in rgb255)
    return _linsrgb_to_oklab(r, g, b)[0]


def rainbow_selftest() -> tuple[bool, str]:
    """No hardware: verify the improved rainbow maths."""
    out, ok = [], True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        out.append(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    rc = rainbow_color_at("oklch")

    # wrap 359°→0° (u≈0.997 vs u≈0.003) has no flash / black frame
    a = _finish_frame([rc(0.997)], 1.0, 0.0, False, 0, 1.0)[0]
    b = _finish_frame([rc(0.003)], 1.0, 0.0, False, 0, 1.0)[0]
    check("hue wrap 359°→0° — no flash",
          max(abs(x - y) for x, y in zip(a, b)) <= 14
          and min(sum(a), sum(b)) > 30)

    row = _render_frame(rc, elapsed=0.7)
    check("adjacent LEDs differ (wave visible)",
          all(row[i] != row[i + 1] for i in range(len(row) - 1)))

    # phase advances smoothly: small dt → small per-LED change
    r1 = _render_frame(rc, elapsed=1.00)
    r2 = _render_frame(rc, elapsed=1.03)
    check("phase advance is smooth",
          max(abs(x - y) for p, q in zip(r1, r2) for x, y in zip(p, q)) <= 25)

    # all channels in-gamut after OKLCH→sRGB→clamp, across a full sweep + dither
    allc = [c for uu in (i / 200 for i in range(200))
            for c in _finish_frame([rc(uu)], 0.8, 0.12, True, 3, 1.0)[0]]
    check("channels within 0..255 (in-gamut)", all(0 <= c <= 255 for c in allc))

    # OKLCH keeps perceived lightness roughly constant round the wheel
    Ls = [_lightness(_finish_frame([rc(i / 60)], 1.0, 0.0, False, 0, 1.0)[0])
          for i in range(60)]
    check(f"perceptual lightness stable (Δ={max(Ls) - min(Ls):.3f} ≤ 0.16)",
          max(Ls) - min(Ls) <= 0.16)

    # direction reversal → opposite motion
    fwd = _render_frame(rc, elapsed=0.5, direction=1)
    rev = _render_frame(rc, elapsed=0.5, direction=-1)
    check("direction reversal is opposite", fwd != rev)

    # brightness + saturation (hsv path) knobs work
    check("brightness param works",
          _render_frame(rc, elapsed=0.3, brightness=25)
          != _render_frame(rc, elapsed=0.3, brightness=100))
    hc_lo = rainbow_color_at("hsv", saturation=0.2)
    hc_hi = rainbow_color_at("hsv", saturation=1.0)
    check("saturation param works (hsv blend)",
          _render_frame(hc_lo, elapsed=0.3) != _render_frame(hc_hi, elapsed=0.3))

    # dithering stays in bounds
    dc = [c for c in _finish_frame([rc(0.0), rc(0.02)], 0.05, 0.0, True, 7, 1.0)[0]]
    check("dither never exceeds 0..255", all(0 <= c <= 255 for c in dc))

    # smoothing is a mild blur: it barely perturbs an already-smooth gradient
    seg = [rc(0.02 + 0.9 * i / 15) for i in range(16)]
    plain = _finish_frame(seg, 1.0, 0.0, False, 0, 1.0)
    blur = _finish_frame(seg, 1.0, 0.4, False, 0, 1.0)
    check("smoothing preserves the gradient (small delta)",
          max(abs(x - y) for p, q in zip(plain, blur) for x, y in zip(p, q)) <= 18)

    # easing options stay bounded
    check("ease sine stays bounded",
          all(0 <= c <= 255 for c in _render_frame(rc, elapsed=0.9, ease="sine")[0]))

    return ok, "\n".join(out)


def gradient_selftest() -> tuple[bool, str]:
    """No hardware: verify the gradient maths."""
    out, ok = [], True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        out.append(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # circular / seamless wrap for a 3-colour loop
    gc = gradient_color_at(["FF0000", "00FF00", "0000FF"], "oklab")
    a = _finish_frame([gc(0.999)], 1.0, 0.0, False, 0, 1.0)[0]
    b = _finish_frame([gc(0.001)], 1.0, 0.0, False, 0, 1.0)[0]
    check("closed loop wraps seamlessly (no flash)",
          max(abs(x - y) for x, y in zip(a, b)) <= 12)
    check("no black / white frame at wrap",
          30 < sum(a) < 3 * 255 - 20 and 30 < sum(b) < 3 * 255 - 20)

    # single anchor → a MOVING value envelope, not a flat board
    g1 = gradient_color_at(["0088FF"], "oklab", 0.15, 1.0)
    f0 = _render_frame(g1, elapsed=0.0)
    fq = _render_frame(g1, elapsed=1.0, cycle_seconds=4.0)   # +¼ cycle
    br0 = [sum(px) for px in f0]
    check("single anchor: spatial brightness envelope", max(br0) - min(br0) > 60)
    check("single anchor: envelope travels over time", f0 != fq)

    # ≥2 anchors: each anchor colour appears somewhere on the board
    gc2 = gradient_color_at(["FF0000", "00FF00", "0000FF", "FFFFFF"], "oklab")
    row = _render_frame(gc2, elapsed=0.0, leds=64)           # dense sample
    def near(px, tgt, tol=60):
        return any(sum(abs(a - b) for a, b in zip(p, tgt)) < tol for p in row)
    check("multi-anchor: red present", near(row, (255, 0, 0)))
    check("multi-anchor: green present", near(row, (0, 255, 0)))
    check("multi-anchor: blue present", near(row, (0, 0, 255)))

    check("adjacent LEDs differ", all(r != s for r, s in zip(
        _render_frame(gc2, elapsed=0.4)[:-1], _render_frame(gc2, elapsed=0.4)[1:])))

    allc = [c for uu in (i / 150 for i in range(150))
            for c in _finish_frame([gc2(uu)], 0.9, 0.12, True, 2, 1.0)[0]]
    check("channels within 0..255", all(0 <= c <= 255 for c in allc))

    fwd = _render_frame(gc2, elapsed=0.5, direction=1)
    rev = _render_frame(gc2, elapsed=0.5, direction=-1)
    check("direction reversal is opposite", fwd != rev)

    # blend space actually changes interpolation (sample mid-segment, not on a
    # segment boundary where every blend returns the anchor exactly)
    b_ok = gradient_color_at(["FF0000", "0000FF"], "oklab")
    b_lin = gradient_color_at(["FF0000", "0000FF"], "linear")
    check("blend space selectable (oklab ≠ linear)",
          _finish_frame([b_ok(0.3)], 1.0, 0.0, False, 0, 1.0)
          != _finish_frame([b_lin(0.3)], 1.0, 0.0, False, 0, 1.0))

    # wavelength affects spatial repetition
    w1 = _render_frame(gc2, elapsed=0.0, wavelength=1.0)
    w2 = _render_frame(gc2, elapsed=0.0, wavelength=2.0)
    check("wavelength changes spatial repetition", w1 != w2)

    return ok, "\n".join(out)


def _hexval(s: str) -> str:
    s = s.lstrip("#")
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise argparse.ArgumentTypeError("colour must be RRGGBB hex")
    return s.upper()


# ---- operations ----------------------------------------------------------- #

def _leave_effect_kick() -> None:
    """The AW-ELC will NOT switch out of a firmware effect (Spectrum Cycle,
    etc.) on a plain `-m Static` write — the keys flash the new colour for an
    instant, then the MCU effect just carries on. Verified live: the only
    thing that reliably clears it is restarting the OpenRGB SDK server (its
    HID connection state is what's stuck). So when the saved mode is an
    effect, kick the server before writing static colour."""
    try:
        if load_meta().get("mode") in ALL_EFFECTS:
            restart_server()
    except Exception:  # noqa: BLE001
        pass


def set_all(color, brightness: int = 100) -> None:
    stop_fx()             # leave any running software wave
    _leave_effect_kick()  # ...and any firmware effect
    _run(["-m", "Static", "-c", _hexify(color), "-b", str(max(0, min(100, brightness)))])


def set_zone(zone: int, color, brightness: int = 100) -> None:
    # The 5515's AW-ELC is a SINGLE controllable zone. OpenRGB advertises 4/16
    # zones, but every write path — CLI `-z`, the SDK per-LED buffer (4/8/16
    # entries), and a raw HID user-animation with per-zone SELECT — lands on
    # the whole keyboard (camera-verified on hardware). So "per zone" == whole
    # keyboard; the last colour wins.
    set_all(color, brightness)


def set_zones(colors: dict, brightness: int = 100) -> None:
    stop_fx()
    set_all(next(iter(colors.values())) if colors else "FFFFFF", brightness)


def off() -> None:
    stop_fx()
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
SOFTWARE_EFFECTS = set()          # no software animations any more
ALL_EFFECTS = set(EFFECT_MODES) | SOFTWARE_EFFECTS


def set_effect(name: str, speed: int | None = None, brightness: int = 100,
               gradient: dict | None = None) -> None:
    """Apply a firmware lighting effect (`spectrum` / `breathing` / `flashing`).
    `speed` is 0-100 where 100 = fastest. Every mode on this AW-ELC reports a
    degenerate brightness range (min=100/max=0) and empirically `-b 100` is
    what lights it, so the caller's brightness is passed straight through
    (`-b 0` leaves it dark). `gradient` is accepted but unused (legacy)."""
    stop_fx()                 # kill any lingering software-wave daemon
    _leave_effect_kick()      # ...and clear a stuck prior effect — this
                              # controller won't switch effect->effect
                              # without an OpenRGB server restart
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


def save_state(zone_colors: dict, brightness: int, mode: str = "zones",
               speed: int = 50, gradient: dict | None = None) -> None:
    """Persist the lighting so the boot/resume service can re-assert it.
    `mode` is 'zones' (static per-zone colours) or an effect key
    (rainbow / gradient / spectrum / breathing / flashing). For 'gradient',
    pass the `gradient` block ({'colors': [...], 'blend': ..., ...})."""
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"brightness": int(brightness),
           "mode": mode,
           "speed": int(speed),
           "zones": {str(z): _hexify(c) for z, c in zone_colors.items()}}
    if mode == "gradient" and gradient:
        doc["gradient"] = {**gradient,
                           "colors": [c.lstrip("#").upper()
                                      for c in gradient.get("colors", [])]}
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
    """{'mode', 'speed', 'gradient'} from the saved state — separate from
    load_state() so its 2-tuple callers don't change."""
    try:
        d = json.load(open(_state_path()))
    except (OSError, ValueError):
        return {"mode": "zones", "speed": 50, "gradient": None}
    return {"mode": d.get("mode", "zones"), "speed": int(d.get("speed", 50)),
            "gradient": d.get("gradient")}


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

    def _fps(v):
        v = int(v)
        if not 2 <= v <= 120:
            raise argparse.ArgumentTypeError("fps must be 2..120")
        return v

    def _hexlist(s):
        cols = [_hexval(x) for x in s.split(",") if x.strip()]
        if not 1 <= len(cols) <= 6:
            raise argparse.ArgumentTypeError("give 1 to 6 comma-separated RRGGBB colours")
        return cols

    p_rw = sub.add_parser("rainbow-wave",
                          help="software per-LED spectrum wave (blocks until killed)")
    p_rw.add_argument("--cycle", type=float, default=24.0,
                      help="seconds per full loop (default 24; faster steps visibly)")
    p_rw.add_argument("--brightness", type=int, default=100)
    p_rw.add_argument("--saturation", type=float, default=1.0, help="hsv blend only")
    p_rw.add_argument("--direction", choices=("ltr", "rtl"), default="ltr")
    p_rw.add_argument("--wavelength", type=float, default=1.0,
                      help="number of rainbow cycles visible across the board")
    p_rw.add_argument("--fps", type=_fps, default=4)
    p_rw.add_argument("--gamma", type=float, default=1.0,
                      help="extra post-sRGB power curve; 1.0 = standard output")
    p_rw.add_argument("--blend", choices=("oklch", "hsv"), default="oklch")
    p_rw.add_argument("--smooth", type=float, default=0.12,
                      help="0..1 adjacent-LED smoothing")
    p_rw.add_argument("--dither", action=argparse.BooleanOptionalAction, default=True)
    p_rw.add_argument("--ease", choices=("linear", "sine"), default="linear")
    p_rw.add_argument("--leds", type=int, default=LOGICAL_ZONES)
    p_rw.add_argument("--seconds", type=float, default=None,
                      help="stop after N seconds instead of running until killed")

    p_gw = sub.add_parser("gradient-wave",
                          help="software travelling gradient from 1-6 anchor colours")
    p_gw.add_argument("--colors", type=_hexlist, required=True,
                      help="comma-separated RRGGBB, 1 to 6 (e.g. ff0000,0000ff)")
    p_gw.add_argument("--cycle", type=float, default=24.0)
    p_gw.add_argument("--brightness", type=int, default=100)
    p_gw.add_argument("--direction", choices=("ltr", "rtl"), default="ltr")
    p_gw.add_argument("--wavelength", type=float, default=1.0)
    p_gw.add_argument("--fps", type=_fps, default=4)
    p_gw.add_argument("--gamma", type=float, default=1.0)
    p_gw.add_argument("--blend", choices=("oklab", "linear", "hsv"), default="oklab")
    p_gw.add_argument("--min-value", type=float, default=0.15,
                      help="single-anchor: dim end of the moving envelope")
    p_gw.add_argument("--max-value", type=float, default=1.0,
                      help="single-anchor: bright end of the moving envelope")
    p_gw.add_argument("--smooth", type=float, default=0.12)
    p_gw.add_argument("--dither", action=argparse.BooleanOptionalAction, default=True)
    p_gw.add_argument("--ease", choices=("linear", "sine"), default="linear")
    p_gw.add_argument("--leds", type=int, default=LOGICAL_ZONES)
    p_gw.add_argument("--seconds", type=float, default=None)

    sub.add_parser("rainbow-test", help="verify the rainbow maths, no hardware")
    sub.add_parser("gradient-test", help="verify the gradient maths, no hardware")
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
            save_state({z: a.color for z in range(ZONE_COUNT)}, a.brightness)
            print(f"backlight on: #{a.color.lower()} @ {a.brightness}%")
        elif a.cmd == "off":
            off()
            print("backlight off")
        elif a.cmd == "reset":
            reset()
            print("backlight reset (server restarted, saved state re-applied)")
        elif a.cmd == "zone":
            st = load_state()
            colors = st[0] if st else {z: "FFFFFF" for z in range(ZONE_COUNT)}
            br = a.brightness if a.brightness is not None else (st[1] if st else 100)
            colors[a.index] = a.color
            set_zones(colors, br)
            save_state(colors, br)
            print(f"zone {a.index} ({ZONE_NAMES[a.index]}) -> #{a.color.lower()} @ {br}%")
        elif a.cmd == "effect":
            st = load_state()
            colors = st[0] if st else {z: "FFFFFF" for z in range(ZONE_COUNT)}
            grad = load_meta().get("gradient") if a.name == "gradient" else None
            set_effect(a.name, a.speed, a.brightness, gradient=grad)
            save_state(colors, a.brightness, mode=a.name, speed=a.speed,
                       gradient=grad)
            print(f"effect {a.name} @ speed {a.speed}, {a.brightness}%")
        elif a.cmd == "rainbow-wave":
            rainbow_wave(cycle_seconds=a.cycle, brightness=a.brightness,
                         saturation=a.saturation,
                         direction=1 if a.direction == "ltr" else -1,
                         wavelength=a.wavelength, fps=a.fps, gamma=a.gamma,
                         blend=a.blend, smooth=a.smooth, dither=a.dither,
                         ease=a.ease, leds=a.leds, run_seconds=a.seconds)
        elif a.cmd == "gradient-wave":
            gradient_wave(a.colors, cycle_seconds=a.cycle, brightness=a.brightness,
                          direction=1 if a.direction == "ltr" else -1,
                          wavelength=a.wavelength, fps=a.fps, gamma=a.gamma,
                          blend=a.blend, min_value=a.min_value, max_value=a.max_value,
                          smooth=a.smooth, dither=a.dither, ease=a.ease,
                          leds=a.leds, run_seconds=a.seconds)
        elif a.cmd == "rainbow-test":
            ok, report = rainbow_selftest()
            print(report)
            print("OK" if ok else "FAILED")
            return 0 if ok else 1
        elif a.cmd == "gradient-test":
            ok, report = gradient_selftest()
            print(report)
            print("OK" if ok else "FAILED")
            return 0 if ok else 1
        elif a.cmd == "apply-saved":
            st = load_state()
            if not st:
                print("no saved lighting state")
                return 0
            meta = load_meta()
            zones, br = st

            if meta["mode"] in SOFTWARE_EFFECTS:
                # software wave: spawn the detached daemon and we're done —
                # it does its own device wait / reconnect.
                set_effect(meta["mode"], meta.get("speed", 50), br,
                           gradient=meta.get("gradient"))
                print(f"re-applied saved lighting ({meta['mode']} wave)")
                return 0

            def _assert_saved():
                if meta["mode"] in ALL_EFFECTS:
                    set_effect(meta["mode"], meta.get("speed", 50), br)
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

            last = None
            for i in range(6):
                if i:
                    time.sleep(2)
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
