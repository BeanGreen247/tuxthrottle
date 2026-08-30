#!/usr/bin/env python3
"""Dell G15 dedicated G-key listener — toggles Game Mode.

On the G15 5515 the dedicated key emits evdev keycode KEY_PERFORMANCE (701)
on the "AT Translated Set 2 keyboard" device — a distinct, purpose-built
Linux keycode, not aliased to anything else. Which physical action produces
it depends on Fn Lock:

    Fn Lock OFF : a bare tap of the key            -> KEY_PERFORMANCE
    Fn Lock ON  : a bare tap                       -> KEY_F9 (plain F9)
                  Fn + F9                          -> KEY_PERFORMANCE

This listener only ever acts on KEY_PERFORMANCE, so it can never hijack a
real F9 press. Default behaviour mirrors Windows: **one press toggles
G-Mode** (the "performance" platform_profile — on the G15 the firmware ramps
the fan curve and raises the CPU/GPU power limits; balanced turns it back
off). If you find a bare tap too easy to hit by accident, set
TUXTHROTTLE_HOTKEY_MODE=double for a double-tap trigger (two KEY_PERFORMANCE
within TUXTHROTTLE_HOTKEY_DOUBLE_MS — a long-press can't be used because
KEY_PERFORMANCE fires as an instantaneous down+up).

Runs as a systemd --user service (installed by the HotkeyListener tweak).
Needs python3-evdev and 'input' group membership. The toggle re-uses
sensors.toggle_game_mode_external(), which tries passwordless sudo first
(PasswordlessGameModeToggle tweak) and falls back to a GUI pkexec prompt.
"""
import glob
import os
import sys
import threading
import time
from pathlib import Path

try:
    import evdev
    from evdev import ecodes
except ImportError:
    print("python3-evdev not found. Install with: dnf install python3-evdev")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sensors import toggle_game_mode_external  # noqa: E402

TARGET_KEYCODE = ecodes.KEY_PERFORMANCE  # 701
DEVICE_NAME_HINTS = ("AT Translated Set 2 keyboard",)

MODE = os.environ.get("TUXTHROTTLE_HOTKEY_MODE", "single").strip().lower()
try:
    DOUBLE_MS = int(os.environ.get("TUXTHROTTLE_HOTKEY_DOUBLE_MS", "600"))
except ValueError:
    DOUBLE_MS = 600


def find_device():
    readable = evdev.list_devices()
    for path in readable:
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if dev.name in DEVICE_NAME_HINTS:
            return dev
    # evdev.list_devices() silently drops nodes we can't read, so a missing
    # keyboard here usually means a permissions gap, not a missing device.
    hidden = [p for p in glob.glob("/dev/input/event*") if p not in readable
              and not os.access(p, os.R_OK)]
    if hidden:
        print(f"Can't read {len(hidden)} input device(s) incl. the keyboard — "
              "your user is not in the 'input' group. Apply the HotkeyListener "
              "tweak (it runs 'usermod -aG input'), then log out and back in.")
    return None


_toggle_busy = threading.Event()


def _run_toggle():
    try:
        ok, err = toggle_game_mode_external()
        if not ok:
            print(f"Toggle failed: {err}", flush=True)
    finally:
        _toggle_busy.clear()


def _fire_toggle():
    if _toggle_busy.is_set():
        print("Toggle already in progress — ignoring", flush=True)
        return
    _toggle_busy.set()
    threading.Thread(target=_run_toggle, daemon=True).start()


def main():
    single = MODE == "single"
    window = DOUBLE_MS / 1000.0
    while True:
        dev = find_device()
        if dev is None:
            print("Keyboard device not found, retrying in 5s...", flush=True)
            time.sleep(5)
            continue

        trigger = "single press" if single else f"double-tap (<{DOUBLE_MS}ms)"
        print(f"Listening on {dev.path} ({dev.name}) for KEY_PERFORMANCE — trigger: {trigger}",
              flush=True)
        last_press = 0.0
        try:
            for event in dev.read_loop():
                if event.type != ecodes.EV_KEY:
                    continue
                if event.code != TARGET_KEYCODE:
                    continue
                if event.value != 1:  # key-down only; ignore repeat/release
                    continue

                # Use the event's own hardware timestamp, NOT wall-clock at
                # processing time — the toggle runs in a thread now, but even
                # so this keeps the double-tap gap accurate under any latency.
                ts = event.timestamp()
                if single:
                    _fire_toggle()
                    continue

                gap = ts - last_press
                if 0 < gap <= window:
                    last_press = 0.0        # consume the pair
                    _fire_toggle()
                else:
                    last_press = ts         # first tap — wait for the second
        except OSError:
            print("Device disconnected, retrying...", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
