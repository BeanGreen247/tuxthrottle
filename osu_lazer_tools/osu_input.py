#!/usr/bin/env python3
"""Tablet and mouse helper for osu!lazer on Linux: checks your tablet setup and converts aim settings.

Usage:
    osu_input.py status                          tablet detection + permissions, driver conflicts, osu!'s
                                                 current tablet area / mouse sensitivity
    osu_input.py mouse --dpi 1600 [--sens 1.0]   cm to cross the screen, eDPI, and the matching tablet area
    osu_input.py tablet WIDTH HEIGHT             check a tablet area (mm): aspect vs screen, mouse equivalent
    osu_input.py dpi-change --from 800 --to 1600 [--sens 1.0]
                                                 osu! sensitivity that keeps your aim the same after a DPI change

The screen size comes from the display's preferred mode (/sys/class/drm); override with --screen 2560x1440.
osu!lazer drives tablets itself (OpenTabletDriver built in), so it needs read/write access to the tablet's
/dev/hidraw node; that is what "status" checks first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import osu_common as c

S = c.Style
OK, WARN, BAD = f"{S.GRN}ok{S.OFF}", f"{S.YEL}!!{S.OFF}", f"{S.RED}XX{S.OFF}"

# USB vendor IDs of drawing-tablet makers (vendor-wide; the rest of the device list is OpenTabletDriver's)
TABLET_VENDORS = {
    0x056A: "Wacom", 0x256C: "Huion/Gaomon", 0x28BD: "XP-Pen", 0x5543: "UC-Logic", 0x2FEB: "Veikk",
    0x172F: "Waltop", 0x08CA: "Aiptek", 0x0B57: "Parblo", 0x2179: "Genius/UC-Logic", 0x0531: "Wacom (old)",
    0x1B96: "N-Trig",
}
UDEV_DIRS = ("/etc/udev/rules.d", "/usr/lib/udev/rules.d", "/lib/udev/rules.d", "/run/udev/rules.d")


def screen_size(override: str | None) -> tuple[int, int]:
    if override:
        w, _, h = override.lower().partition("x")
        return int(w), int(h)
    for conn in sorted(Path("/sys/class/drm").glob("card*-*")):
        try:
            if (conn / "status").read_text().strip() != "connected":
                continue
            mode = (conn / "modes").read_text().split("\n", 1)[0].strip()
        except OSError:
            continue
        w, _, h = mode.partition("x")
        if w.isdigit() and h.rstrip("i").isdigit():
            return int(w), int(h.rstrip("i"))
    return 1920, 1080


def input_config() -> dict[str, dict]:
    """Handler settings from osu!'s input.json keyed by short name (Mouse, Tablet, ...)."""
    try:
        data = json.loads((c.data_dir() / "input.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for h in data.get("InputHandlers", []):
        name = h.get("$type", "").split(",")[0].rsplit(".", 1)[-1].replace("Handler", "")
        out["Tablet" if "OpenTabletDriver" in name else name] = h
    return out


def hid_tablets() -> list[dict]:
    found: dict[str, dict] = {}
    for dev in Path("/sys/bus/hid/devices").glob("*"):
        try:
            uevent = dict(line.split("=", 1) for line in (dev / "uevent").read_text().splitlines() if "=" in line)
        except OSError:
            continue
        _, vendor, product = (uevent.get("HID_ID", "0:0:0").split(":") + ["0", "0"])[:3]
        vid, pid = int(vendor, 16), int(product, 16)
        if vid not in TABLET_VENDORS:
            continue
        key = f"{vid:04x}:{pid:04x}"
        t = found.setdefault(key, {"id": key, "maker": TABLET_VENDORS[vid], "name": uevent.get("HID_NAME", "?"),
                                   "hidraw": [], "driver": set()})
        t["driver"].add(uevent.get("DRIVER", "none"))
        for raw in (dev / "hidraw").glob("hidraw*") if (dev / "hidraw").is_dir() else []:
            t["hidraw"].append(f"/dev/{raw.name}")
    return list(found.values())


def running(names: tuple[str, ...]) -> bool:
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                comm = (d / "comm").read_text().strip()
            except OSError:
                continue
            if any(comm.startswith(n) for n in names):
                return True
    return False


def udev_rules_for(vid: int) -> list[str]:
    hits = []
    needle = f"{vid:04x}"
    for d in UDEV_DIRS:
        for f in Path(d).glob("*.rules") if Path(d).is_dir() else []:
            try:
                text = f.read_text(errors="replace").lower()
            except OSError:
                continue
            if "opentabletdriver" in f.name.lower() or ("hidraw" in text and needle in text):
                hits.append(str(f))
    return hits


def cmd_status(args) -> int:
    cfg = input_config()
    w, h = screen_size(args.screen)
    print(f"{S.BOLD}Tablet{S.OFF}")
    tablets = hid_tablets()
    if not tablets:
        print(f"  {S.DIM}no drawing tablet connected (known makers: Wacom, Huion, Gaomon, XP-Pen, Veikk, ...){S.OFF}")
    for t in tablets:
        print(f"  {t['maker']} {t['name']} [{t['id']}]  kernel driver: {', '.join(sorted(t['driver']))}")
        for node in t["hidraw"]:
            rw = os.access(node, os.R_OK | os.W_OK)
            print(f"  {OK if rw else BAD} {node} {'is' if rw else 'is NOT'} readable+writable by you")
        if t["hidraw"] and not all(os.access(n, os.R_OK | os.W_OK) for n in t["hidraw"]):
            rules = udev_rules_for(int(t["id"][:4], 16))
            print("     osu! can't read the tablet. Install OpenTabletDriver's udev rules "
                  "(package 'opentabletdriver', or its generated 70-opentabletdriver.rules), then replug the tablet."
                  + (f" Rules found but not applied: {', '.join(rules)} (replug / sudo udevadm trigger)" if rules else ""))
        mods = [m for m in ("wacom", "hid_uclogic") if Path(f"/sys/module/{m}").exists()]
        if mods:
            print(f"  {WARN} kernel tablet driver loaded ({', '.join(mods)}): if the cursor fights between two "
                  f"positions in osu!, turn off \"Pen\" under Settings > Input, or blacklist the module")
    if running(("OpenTabletDrive", "otd-daemon")):
        print(f"  {WARN} the OpenTabletDriver daemon is running: osu!lazer has its own copy built in and both "
              f"reading the tablet causes jumps. Stop it while playing (systemctl --user stop opentabletdriver)")
    tab = cfg.get("Tablet")
    if tab:
        size, off = tab.get("AreaSize", {}), tab.get("AreaOffset", {})
        aw, ah = size.get("x", 0), size.get("y", 0)
        state = "on" if tab.get("Enabled", True) else "off"
        if aw and ah:
            print(f"  osu! tablet area: {aw:.1f} x {ah:.1f} mm at ({off.get('x', 0):.1f}, {off.get('y', 0):.1f}), "
                  f"rotation {tab.get('Rotation', 0):g}°, handler {state}")
            aspect_note(aw, ah, w, h)
        else:
            print(f"  osu! tablet area: not set yet (full area), handler {state}")

    print(f"\n{S.BOLD}Mouse{S.OFF}")
    mouse = cfg.get("Mouse", {})
    sens = mouse.get("Sensitivity", 1.0)
    raw = mouse.get("UseRelativeMode", False)
    print(f"  osu! sensitivity {sens:g}, high precision (raw) mode {'on' if raw else 'off'}")
    if not raw and abs(sens - 1) > 1e-6:
        print(f"  {WARN} sensitivity only applies with \"High precision mouse\" on (Settings > Input > Mouse)")
    if not raw:
        print(f"  {S.DIM}with it off, the desktop pointer speed/acceleration applies "
              f"(the launcher turns acceleration off while playing){S.OFF}")
    print(f"\n  screen {w}x{h}.  For cm/360-style numbers: osu-tools input mouse --dpi YOUR_DPI "
          f"(don't know it? osu-lazer-launcher measure-dpi)")
    return 0


def aspect_note(aw: float, ah: float, sw: int, sh: int) -> None:
    ratio, screen = aw / ah, sw / sh
    if abs(ratio - screen) / screen > 0.02:
        print(f"  {WARN} area aspect {ratio:.3f} vs screen {screen:.3f}: movement is stretched "
              f"{'horizontally' if ratio < screen else 'vertically'}. Same-aspect height: {aw / screen:.1f} mm")
    else:
        print(f"  {OK} area matches the screen aspect ({screen:.3f})")


def cm_per_screen(dpi: float, sens: float, width_px: int) -> float:
    """Mouse travel (cm) to move the cursor across the screen in osu! (1 count = sens pixels in raw mode)."""
    return width_px / (dpi * sens) * 2.54


def cmd_mouse(args) -> int:
    w, h = screen_size(args.screen)
    cm = cm_per_screen(args.dpi, args.sens, w)
    print(f"{args.dpi:g} DPI x {args.sens:g} = {args.dpi * args.sens:g} eDPI on a {w}x{h} screen")
    print(f"  {cm:.1f} cm of mouse movement crosses the screen ({cm / 2.54:.1f} in)")
    print(f"  the same aim on a tablet: area {cm * 10:.1f} x {cm * 10 * h / w:.1f} mm")
    return 0


def cmd_tablet(args) -> int:
    w, h = screen_size(args.screen)
    print(f"tablet area {args.width:g} x {args.height:g} mm on a {w}x{h} screen")
    aspect_note(args.width, args.height, w, h)
    edpi = w / (args.width / 25.4)
    print(f"  mouse equivalent: {edpi:.0f} eDPI (e.g. {edpi:.0f} DPI at sensitivity 1.0, "
          f"{edpi / 2:.0f} DPI at 2.0)")
    return 0


def cmd_dpi_change(args) -> int:
    new = args.sens * args.from_dpi / args.to_dpi
    print(f"{args.from_dpi:g} DPI x {args.sens:g}  ->  {args.to_dpi:g} DPI x {new:.3f}  (same aim)")
    if new < 0.1 or new > 10:
        print(f"  {WARN} osu! sensitivity goes from 0.1 to 10; pick a DPI closer to the old one")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools input", description=__doc__.split("\n\n")[0])
    scr = argparse.ArgumentParser(add_help=False)
    scr.add_argument("--screen", help="WIDTHxHEIGHT (default: detected)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", parents=[scr], help="tablet/mouse setup check")
    p = sub.add_parser("mouse", parents=[scr], help="cm per screen and tablet equivalent")
    p.add_argument("--dpi", type=float, required=True)
    p.add_argument("--sens", type=float, default=None, help="osu! sensitivity (default: from input.json)")
    p = sub.add_parser("tablet", parents=[scr], help="check a tablet area")
    p.add_argument("width", type=float)
    p.add_argument("height", type=float)
    p = sub.add_parser("dpi-change", parents=[scr], help="keep the same aim after changing DPI")
    p.add_argument("--from", dest="from_dpi", type=float, required=True)
    p.add_argument("--to", dest="to_dpi", type=float, required=True)
    p.add_argument("--sens", type=float, default=None)
    argv = sys.argv[1:] if argv is None else argv
    args = ap.parse_args(argv or ["status"])
    if getattr(args, "sens", 0) is None:
        args.sens = input_config().get("Mouse", {}).get("Sensitivity", 1.0)
    return {"status": cmd_status, "mouse": cmd_mouse, "tablet": cmd_tablet,
            "dpi-change": cmd_dpi_change, None: cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
