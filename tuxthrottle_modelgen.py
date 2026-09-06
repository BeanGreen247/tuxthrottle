#!/usr/bin/env python3
"""Generate a `models/<slug>.json` scaffold from the machine it runs on.

Fills in every field TuxThrottle can auto-detect (DMI match, CPU/fan hwmon
names, fan count, platform_profile path + choices, GPU PCI ids, OpenRGB
device, battery method) and leaves the rest as `null`, listing them under
`_todo` so a human can finish the file from the `--collect` bundle.

stdlib only. Reads sysfs + a couple of optional CLIs (`openrgb`,
`smbios-battery-ctl`); never writes anything unless `--out` is given.

  python3 tuxthrottle_modelgen.py                 # print scaffold to stdout
  python3 tuxthrottle_modelgen.py --out models/foo.json
  tuxthrottlectl collect-model [--slug NAME] [--out PATH]
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402

_CPU_HWMONS = ("k10temp", "zenpower", "coretemp")
_FAN_HWMONS = ("alienware_wmi", "dell_smm", "asus_nb_wmi", "asus_wmi_sensors",
               "hp_wmi", "thinkpad")
_PP_PATH = "/sys/firmware/acpi/platform_profile"


def _hwmon_names() -> list[str]:
    out = []
    for p in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            out.append(open(p).read().strip())
        except OSError:
            pass
    return out


def _pci_id(sysfs_dir: str | None) -> str | None:
    if not sysfs_dir:
        return None
    try:
        v = open(f"{sysfs_dir}/vendor").read().strip()
        d = open(f"{sysfs_dir}/device").read().strip()
        return f"{v[2:]}:{d[2:]}"  # strip the 0x
    except OSError:
        return None


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "unknown-model"


def _openrgb_device() -> str | None:
    if not shutil.which("openrgb"):
        return None
    try:
        out = subprocess.run(["openrgb", "--noautoconnect", "-l"],
                             capture_output=True, text=True, timeout=40).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for ln in out.splitlines():
        m = re.match(r"\s*\d+:\s+(.*\S)", ln)
        if m and any(k in m.group(1).lower()
                     for k in ("led", "rgb", "keyboard", "aura", "g series")):
            return m.group(1)
    return None


def build_scaffold(slug: str | None = None) -> dict:
    hw = _hwmon_names()
    vendor = sensors._dmi("sys_vendor")
    product = sensors._dmi("product_name")
    board = sensors._dmi("board_name")
    todo: list[str] = []

    cpu_hwmon = next((n for n in _CPU_HWMONS if n in hw), None)
    if not cpu_hwmon:
        todo.append("cpu.hwmon")

    fan_hwmon = next((n for n in _FAN_HWMONS if n in hw), None)
    fan_count = None
    pwm_hwmon = "dell_smm" if "dell_smm" in hw else None
    if fan_hwmon:
        d = sensors._hwmon_by_name(fan_hwmon)
        if d:
            fan_count = len(glob.glob(f"{d}/fan[0-9]_input")) or None
            if not pwm_hwmon and glob.glob(f"{d}/pwm[0-9]"):
                pwm_hwmon = fan_hwmon
    else:
        todo.append("fans.hwmon")
    if not fan_count:
        todo.append("fans.count")
    if not pwm_hwmon:
        todo.append("fans.pwm_hwmon")

    pp_path: str | None = _PP_PATH
    pp_choices: list[str] = []
    if os.path.exists(_PP_PATH):
        try:
            pp_choices = open(_PP_PATH + "_choices").read().split()
        except OSError:
            pp_choices = []
    else:
        pp_path = None
        todo.append("fans.platform_profile_path")

    igpu_pci = _pci_id(sensors._amdgpu_card_dir())
    dgpu_pci = _pci_id(sensors._nvidia_pci_dir())

    orgb = _openrgb_device()
    if not orgb:
        todo.append("keyboard.openrgb_device")
    todo.append("keyboard.usb")

    bat_sysfs = bool(sensors._battery_dir())
    bat_method = "sysfs" if bat_sysfs else (
        "libsmbios" if shutil.which("smbios-battery-ctl") else None)
    if not bat_method:
        todo.append("battery.method")

    todo += ["gkey.device", "gkey.evdev", "gkey.keycode_fnlock_off",
             "cpu.stock_ppt_w"]

    return {
        "id": slug or _slugify(product),
        "name": product or (slug or "unknown model"),
        "match": {
            "product_name": [product] if product else [],
            "board_name": [board] if board else [],
        },
        "cpu": {
            "vendor": "amd" if sensors._cpu_is_amd() else "intel",
            "hwmon": cpu_hwmon,
            "ryzenadj": sensors.ryzenadj_available(),
            "stock_ppt_w": None,
        },
        "igpu": {"pci": igpu_pci},
        "dgpu": {"pci": dgpu_pci, "vendor": "nvidia" if dgpu_pci else None},
        "fans": {
            "hwmon": fan_hwmon,
            "pwm_hwmon": pwm_hwmon,
            "count": fan_count,
            "pwm_floor": 77,
            "platform_profile_path": pp_path,
            "platform_profiles": pp_choices,
        },
        "game_mode": {"mechanism": "platform_profile", "value": "performance"},
        "keyboard": {
            "rgb": "openrgb" if orgb else None,
            "openrgb_device": orgb,
            "usb": None,
            "zones": 1,
            "effects": ["spectrum"],
            "brightness_on": 100,
        },
        "gkey": {
            "device": None, "evdev": None,
            "keycode_fnlock_off": None, "keycode_fnlock_on": None,
        },
        "battery": {"sysfs_threshold": bat_sysfs, "method": bat_method},
        "tweaks_skip": [],
        "_todo": sorted(set(todo)),
        "_generated": {
            "by": "tuxthrottle_modelgen",
            "host_dmi": {"sys_vendor": vendor, "product_name": product,
                         "board_name": board},
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="tuxthrottle_modelgen", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slug", help="model id / filename stem (default: from DMI)")
    ap.add_argument("--out", help="write to this path instead of stdout")
    a = ap.parse_args(argv)
    prof = build_scaffold(a.slug)
    text = json.dumps(prof, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n")
        n = len(prof["_todo"])
        print(f"wrote {a.out}  — {n} field(s) still need manual completion "
              f'(see the "_todo" list); delete "_todo"/"_generated" when done.')
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
