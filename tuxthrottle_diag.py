#!/usr/bin/env python3
"""Diagnostics / debug-report / new-hardware-onboarding helpers — the
heavy read-only report builders, pulled out of tuxthrottle.py so the
Diagnostics tab mixin can import them without a circular dependency
(module-split pass, sixth slice). No Tk / GUI deps."""
import glob
import json
import os
import pwd
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sensors
from tuxthrottle_items import (
    evaluate_item,
    format_status_report,
    ledger_load,
    resolve_real_user,
    run_cmd3,
    toolkit_version,
)
from tuxthrottle_items import load_all_items as _load_all_items


def _diag_fans() -> str:
    lines = []
    try:
        lines.append(f"platform_profile: {sensors.get_platform_profile()}  "
                     f"choices={sensors.platform_profile_choices()}")
        fans = sensors.read_fans()
        for f in fans or []:
            lines.append(f"  {f['label']}: {f['rpm']} rpm  (max {f['max']}, boost {f['boost']})")
        if not fans:
            lines.append("  (no alienware_wmi / dell_smm fan interface found)")
        lines.append(f"dell_smm pwm state (enable,value): {sensors.get_pwm_state()}")
        lines.append(f"dGPU awake: {sensors.dgpu_is_awake()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(fan probe failed: {exc})")
    return "\n".join(lines)


# Groups of (title, shell-command, max-lines). Kept read-only + quick; every
# command is best-effort. Inspired by the evtest / /proc/bus/input/devices /
# dmesg dumps used to bring this board up in the first place.
_DEBUG_CMDS = [
    ("── SYSTEM ──", None, 0),
    ("OS", "cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION|VARIANT|ID|BUILD)' ", 12),
    ("Kernel / cmdline", "uname -a; echo; cat /proc/cmdline", 6),
    ("Firmware / DMI", "for f in sys_vendor product_name product_sku board_name board_version "
     "bios_vendor bios_version bios_date chassis_type; do "
     "printf '%-16s %s\\n' \"$f\" \"$(cat /sys/class/dmi/id/$f 2>/dev/null)\"; done", 16),
    ("Desktop session", "for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do "
     "t=$(loginctl show-session \"$s\" -p Type --value 2>/dev/null); "
     "case \"$t\" in wayland|x11) loginctl show-session \"$s\" -p Name -p Type -p Desktop -p Active "
     "-p Remote 2>/dev/null; break;; esac; done; "
     "echo \"XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-} XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}\"", 10),
    ("Uptime / load", "uptime", 3),
    ("── CPU / MEMORY ──", None, 0),
    ("CPU", "lscpu 2>/dev/null | grep -E 'Model name|^CPU\\(s\\)|Thread|Core|Socket|CPU max|Vendor'; "
     "echo \"governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null) "
     "epp: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)\"", 16),
    ("Memory / zram", "free -h; echo; zramctl 2>/dev/null; swapon --show 2>/dev/null", 14),
    ("── GPU ──", None, 0),
    ("PCI display devices", "lspci -nnk 2>/dev/null | grep -iA3 -E 'vga compatible|3d controller|display controller'", 24),
    ("NVIDIA", "nvidia-smi 2>/dev/null | head -18 || echo '(nvidia-smi unavailable — driver missing or dGPU runtime-suspended)'", 20),
    ("NVIDIA runtime PM", "for d in /sys/bus/pci/devices/*; do [ \"$(cat $d/vendor 2>/dev/null)\" = 0x10de ] && "
     "echo \"$(basename $d)  class=$(cat $d/class 2>/dev/null)  power=$(cat $d/power/runtime_status 2>/dev/null)\"; done", 6),
    ("AMD iGPU", "for c in /sys/class/drm/card[0-9]*/device; do [ \"$(cat $c/vendor 2>/dev/null)\" = 0x1002 ] && { "
     "echo \"$c\"; echo \" dpm: $(cat $c/power_dpm_force_performance_level 2>/dev/null)\"; "
     "cat $c/pp_dpm_sclk 2>/dev/null; }; done", 20),
    ("Mesa / GL", "glxinfo -B 2>/dev/null | grep -E 'OpenGL renderer|OpenGL version|Device:|Video memory' "
     "|| echo '(glxinfo not installed)'", 10),
    ("── THERMAL / POWER ──", None, 0),
    ("platform_profile", "echo \"current: $(cat /sys/firmware/acpi/platform_profile 2>/dev/null)\"; "
     "echo \"choices: $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)\"", 4),
    ("power-profiles-daemon", "powerprofilesctl get 2>/dev/null; echo '---'; powerprofilesctl 2>/dev/null | head -24", 26),
    ("Fans / hwmon", _diag_fans, None),
    ("sensors", "sensors 2>/dev/null || echo '(lm_sensors not installed)'", 45),
    ("RAPL (CPU power)", "ls -l /sys/class/powercap/*/energy_uj 2>/dev/null; "
     "(head -c1 /sys/class/powercap/intel-rapl:0/energy_uj >/dev/null 2>&1 && echo 'RAPL readable') "
     "|| echo 'RAPL NOT readable without root (kernel side-channel mitigation)'", 10),
    ("── KEYBOARD / HOTKEYS / MEDIA KEYS ──", None, 0),
    ("Loaded modules", "lsmod | grep -E '^(dell|alienware|i8k|sparse_keymap|hid_|nvidia|amdgpu)' | sort", 30),
    ("Alienware USB LED controller", "lsusb 2>/dev/null | grep -iE '187c:|alienware' || echo '(187c:0550 AW-ELC not seen on USB)'", 4),
    ("HID devices", "for h in /sys/bus/hid/devices/*; do [ -e \"$h\" ] || continue; "
     "printf '%-24s %s\\n' \"$(basename $h)\" \"$(cat $h/input/input*/name 2>/dev/null | head -1)\"; done", 20),
    ("input devices (evdev + KEY capability bitmaps)", "cat /proc/bus/input/devices", 140),
    ("event device names", "for e in /dev/input/event*; do "
     "printf '%-22s %s\\n' \"$e\" \"$(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null)\"; done", 30),
    ("Dell WMI / hotkey / media-key devices", "for e in /dev/input/event*; do "
     "n=$(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null); "
     "case \"$n\" in *WMI*|*wireless\\ hotkey*|*Wireless\\ hotkey*|*Translated\\ Set\\ 2*|*Video\\ Bus*) "
     "echo \"$e  $n\";; esac; done", 15),
    ("Fn-Lock / G-key note", "echo 'G-key = KEY_PERFORMANCE(701) on \"AT Translated Set 2 keyboard\" when Fn-Lock OFF, "
     "KEY_F9 when ON. Media keys (vol/mute) come via \"Dell WMI hotkeys\". Fn is an EC key and never reaches evdev.'", 4),
    ("input group membership", "u=$(logname 2>/dev/null || echo \"${SUDO_USER:-}\"); "
     "echo \"desktop user: $u\"; id \"$u\" 2>/dev/null; getent group input; "
     "id -nG \"$u\" 2>/dev/null | tr ' ' '\\n' | grep -qx input "
     "&& echo 'OK: user is in the input group' || echo 'WARN: user NOT in input group "
     "(the G-key HotkeyListener needs it)'", 8),
    ("── RGB KEYBOARD (OpenRGB) ──", None, 0),
    ("OpenRGB", "openrgb --version 2>/dev/null | head -1 || echo '(openrgb not installed)'", 4),
    ("OpenRGB devices", "openrgb --noautoconnect -l 2>/dev/null | grep -vE '<[a-z]|i2c|SMBus|help.openrgb' | head -40", 45),
    ("kbd services", "for s in tuxthrottle-openrgb.service tuxthrottle-kbd.service; do "
     "printf '%-26s enabled=%-9s active=%s\\n' \"$s\" "
     "\"$(systemctl is-enabled $s 2>/dev/null)\" \"$(systemctl is-active $s 2>/dev/null)\"; done", 6),
    ("kbd saved state", "u=$(logname 2>/dev/null || echo \"${SUDO_USER:-$USER}\"); "
     "h=$(getent passwd \"$u\" | cut -d: -f6); cat \"$h/.config/tuxthrottle/kbd.json\" 2>/dev/null "
     "|| echo '(no kbd.json — colour not saved / KbdBacklightFix not used)'", 24),
    ("── TWEAK SERVICES / SUDOERS ──", None, 0),
    ("tuxthrottle units", "systemctl list-unit-files 2>/dev/null | grep -E 'tuxthrottle|hotkey' ; "
     "systemctl --user list-unit-files 2>/dev/null | grep -E 'tuxthrottle|hotkey'", 12),
    ("sudoers drop-ins", "ls -l /etc/sudoers.d/ 2>/dev/null | grep -E 'tuxthrottle|gamemode|claude' || echo '(none)'", 8),
    ("── PACKAGES ──", None, 0),
    ("Kernels installed", "rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\\n' 2>/dev/null | sort -V", 10),
    ("NVIDIA packages", "rpm -qa 2>/dev/null | grep -iE 'nvidia|akmod-nvidia|cuda' | sort "
     "|| echo '(no NVIDIA packages — driver may be from a -NV image or missing)'", 12),
    ("Relevant packages", "rpm -q openrgb gamemode mangohud goverlay gamescope vkbasalt lm_sensors "
     "nobara-updater tlp auto-cpufreq 2>&1 | sed 's/ is not installed/  — NOT installed/'", 14),
    ("Update tooling", "dnf --version 2>/dev/null | head -1; command -v nobara-sync >/dev/null && echo 'nobara-sync: present'; "
     "command -v flatpak >/dev/null && flatpak --version; command -v fwupdmgr >/dev/null && echo 'fwupd: present'", 6),
    ("── LOGS ──", None, 0),
    ("dmesg (filtered, deduped)", "dmesg 2>/dev/null "
     "| grep -iE 'dell_|dell-|alienware|aw-elc|187c:0550|hid-generic 0003:187C|i8042|"
     "firmware bug|thermal (throttl|event)|MCE|hardware error|"
     "(nvidia|amdgpu|nouveau).*(error|fail|warn|timed? ?out|reset|hang|fault|Xid)|"
     "platform.?profile|pstate' "
     "| grep -viE 'Mode Validation Warning|Unknown Status failed|Console: switching|fbcon' "
     "| sed -E 's/^\\[[0-9. ]+\\] //' | awk '!seen[$0]++' | tail -40 "
     "|| echo '(dmesg not readable — run the toolkit with sudo, or kernel.dmesg_restrict=1)'", 42),
    ("journal errors (this boot)", "journalctl -b -p err --no-pager 2>/dev/null "
     "| grep -viE 'Module lib.*from rpm|^ *Module |drkonqi|KCrash|Stack trace|"
     "^ *#[0-9]+ +0x|libQt6|libKF6|libc\\.so|__libc_start' "
     "| awk '!seen[$0]++' | tail -35 || echo '(journalctl unavailable)'", 37),
    ("journal — kbd / fan / gpu units (this boot)", "journalctl -b --no-pager "
     "-u 'tuxthrottle-*' -u 'tuxthrottle-*.service' 2>/dev/null | tail -25; "
     "journalctl -b --no-pager 2>/dev/null | grep -iE "
     "'openrgb\\[|dell_smm|alienware_wmi|nvidia-persistenced|(nvidia|amdgpu).*(Xid|GPU has fallen|ring .* timeout)' "
     "| grep -viE 'audit\\[|sudo\\[|Mode Validation' | awk '!seen[$0]++' | tail -20 || echo '(none)'", 40),
]


def collect_debug_report(items=None, wrap: bool = False) -> str:
    """Assemble a hardware + OS + toolkit-state report for bug reports. All
    commands are read-only, hard-timed-out and best-effort. Run as root for
    the complete picture (dmesg, RAPL, privileged checks). `wrap=True` returns
    it inside a GitHub `<details>` + fenced block, ready to paste."""
    hdr = [
        "TuxThrottle — debug report",
        f"generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')}   toolkit {toolkit_version()}   "
        f"euid={os.geteuid()}",
        "REVIEW BEFORE PASTING — this contains your username, hostname and hardware IDs.",
        "=" * 92, "",
    ]
    body = []
    for title, cmd, maxlines in _DEBUG_CMDS:
        if cmd is None:                       # section divider
            body.append(f"\n{title}")
            continue
        try:
            if callable(cmd):
                out = cmd()
            else:  # hard cap via coreutils `timeout` so nothing can wedge
                out = run_cmd3(f"timeout -k 2 12 bash -lc {shlex.quote(cmd)}", timeout=16)[2]
        except Exception as exc:              # noqa: BLE001
            out = f"(error: {exc})"
        out = out.strip() or "(no output)"
        if maxlines:
            ls = out.splitlines()
            if len(ls) > maxlines:
                out = "\n".join(ls[:maxlines]) + f"\n… ({len(ls) - maxlines} more lines trimmed)"
        body.append(f"\n### {title}\n{out}")

    body.append("\n\n── TOOLKIT: KEYBOARD DRIVER ──")
    try:
        info = __import__("tuxthrottle_kbd").info()
        body.append("\n### tuxthrottle_kbd info\n" +
                    "\n".join(f"{k:16}: {v}" for k, v in info.items()))
    except Exception as exc:  # noqa: BLE001
        body.append(f"\n### tuxthrottle_kbd info\n(error: {exc})")

    body.append("\n\n── TOOLKIT: APPLY STATUS ──")
    if items is None:
        items = _load_all_items()
        led = ledger_load()
        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(lambda it: evaluate_item(it, led), items))
    body.append("\n" + format_status_report(items))

    body.append("\n── TOOLKIT: APPLY LEDGER (state.json) ──\n" +
                json.dumps(ledger_load(), indent=2, sort_keys=True))
    report = "\n".join(hdr) + "\n".join(body) + "\n"
    return wrap_issue_block(report) if wrap else report


def wrap_issue_block(report: str) -> str:
    """Wrap a raw report in a GitHub-ready collapsible fenced block."""
    return ("<details><summary>debug report — TuxThrottle</summary>\n\n"
            "```\n" + report.replace("```", "``​`").rstrip() + "\n```\n\n</details>\n")


GITHUB_ISSUE_TEMPLATE = """\
### What happened


### What you expected instead


### Where in the toolkit (which page / button / tweak)


### Steps to reproduce
1.
2.
3.

### Is your hardware the Dell G15 5515 Ryzen Edition on Nobara?
<!-- This tool is written for exactly that one machine. On anything else most
     checks/tweaks won't apply — say what you're on. -->
- [ ] yes, G15 5515 Ryzen + Nobara
- [ ] close (other G15 / other Dell hybrid) — details:
- [ ] no — details:

### Debug report
<!-- Toolkit → Report a Bug page → "Generate report" → "Copy report",
     or a terminal:  sudo python3 /opt/tuxthrottle/tuxthrottle.py --debug
     Review it for your username/hostname, then paste between the ``` fences. -->
<details><summary>debug report</summary>

```
PASTE THE DEBUG REPORT HERE
```

</details>

### Screenshot / log console output (if relevant)

"""


# ── new-hardware onboarding: a raw dump bundle to attach to a support issue ──

# linux/input-event-codes.h — the codes that matter for a laptop's function /
# media / hardware keys. Unknowns print as KEY_<n>.
_KEY_CODE_NAMES = {
    59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6", 65: "F7",
    66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
    99: "SYSRQ", 110: "INSERT", 111: "DELETE", 119: "PAUSE", 127: "MENU",
    113: "MUTE", 114: "VOLUMEDOWN", 115: "VOLUMEUP", 116: "POWER",
    128: "STOP", 140: "CALC", 142: "SLEEP", 143: "WAKEUP",
    148: "PROG1", 149: "PROG2", 150: "WWW", 152: "SCREENLOCK",
    158: "BACK", 159: "FORWARD", 161: "EJECTCD",
    163: "NEXTSONG", 164: "PLAYPAUSE", 165: "PREVIOUSSONG", 166: "STOPCD",
    172: "HOMEPAGE", 173: "REFRESH", 190: "PROG3", 191: "PROG4",
    202: "PAUSECD", 217: "SEARCH",
    224: "BRIGHTNESSDOWN", 225: "BRIGHTNESSUP", 226: "MEDIA",
    227: "SWITCHVIDEOMODE", 228: "KBDILLUMTOGGLE", 229: "KBDILLUMDOWN",
    230: "KBDILLUMUP", 236: "BATTERY", 238: "WLAN", 239: "UWB",
    240: "UNKNOWN", 241: "VIDEO_NEXT", 244: "BRIGHTNESS_AUTO",
    245: "DISPLAY_OFF", 246: "WWAN", 247: "RFKILL", 248: "MICMUTE",
    418: "SCALE", 431: "ASSISTANT", 464: "FN", 484: "FN_RIGHT_SHIFT",
    582: "MICMUTE", 701: "PERFORMANCE (the G-key / G-Mode)",
}
for _i in range(183, 195):                       # 183-194 -> F13..F24
    _KEY_CODE_NAMES[_i] = f"F{_i - 170}"


def _model_scaffold_json() -> str:
    """Run the model-profile scaffold generator (probes DMI / hwmon / PCI /
    OpenRGB / battery method — no writes) and return its JSON. This is the
    starting point for a new `models/<slug>.json`; the maintainer fills the
    `_todo` fields from the other bundle files."""
    try:
        import tuxthrottle_modelgen
        return json.dumps(tuxthrottle_modelgen.build_scaffold(), indent=2)
    except Exception as exc:  # noqa: BLE001
        return f"(model scaffold generation failed: {exc})"


def _decode_key_caps() -> str:
    """For each evdev device in /proc/bus/input/devices, decode its `B: KEY=`
    capability bitmap into KEY_ names — the fastest way to see what a new
    laptop's Fn / media / vendor keys can emit, without live evtest."""
    ok, _rc, blob = run_cmd3("cat /proc/bus/input/devices", timeout=8)
    if not ok:
        return "(could not read /proc/bus/input/devices)"
    out, name, keyline = [], "?", ""
    def flush():
        if not keyline:
            return
        words = keyline.split()
        codes = []
        for wi, w in enumerate(reversed(words)):
            try:
                val = int(w, 16)
            except ValueError:
                continue
            for bit in range(64):
                if val >> bit & 1:
                    codes.append(wi * 64 + bit)
        pretty = ", ".join(
            f"{c}:{_KEY_CODE_NAMES.get(c, 'KEY_' + str(c))}" for c in sorted(codes)
            if c >= 55 or c in _KEY_CODE_NAMES)          # skip the boring alnum block
        out.append(f"[{name}]\n  {pretty or '(only standard keys)'}\n")
    for ln in blob.splitlines():
        if ln.startswith("N: Name="):
            flush(); name = ln.split('"', 2)[1] if '"' in ln else ln[8:]; keyline = ""
        elif ln.startswith("B: KEY="):
            keyline = ln[7:].strip()
    flush()
    return "\n".join(out) or "(no KEY-capable devices found)"


def _collect_display_txt() -> str:
    """kscreen-doctor needs the real user's Wayland/D-Bus session — run bare
    as root (which every other bundle command here does, via plain shell) it
    SIGABRTs instead of erring cleanly, leaving a coredump behind every single
    time the bundle is collected. sensors._session_cmd() hops back to the
    real user's session the same way the Display tab's refresh-rate switcher
    already does."""
    out = ["# kscreen-doctor"]
    for args in (["kscreen-doctor", "-o"], ["kscreen-doctor", "-j"]):
        try:
            r = subprocess.run(sensors._session_cmd(args), capture_output=True,
                               text=True, timeout=6)
            out.append(r.stdout.strip())
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(f"(kscreen-doctor failed: {exc})")
        out.append("")
    out.append("# xrandr")
    try:
        r = subprocess.run(sensors._session_cmd(["xrandr", "--verbose"]),
                           capture_output=True, text=True, timeout=6)
        out.append("\n".join(r.stdout.splitlines()[:120]))
    except (OSError, subprocess.SubprocessError) as exc:
        out.append(f"(xrandr failed: {exc})")
    out.append("")
    out.append("# drm modes")
    for m in sorted(glob.glob("/sys/class/drm/*/modes")):
        out.append(f"{m}:")
        try:
            out.append(Path(m).read_text().strip())
        except OSError:
            pass
    out.append("")
    out.append("# vrr_capable")
    for m in sorted(glob.glob("/sys/class/drm/*/vrr_capable")):
        try:
            out.append(f"{m}:{Path(m).read_text().strip()}")
        except OSError:
            pass
    return "\n".join(out)


_HW_BUNDLE_FILES = [
    ("model-scaffold.json", _model_scaffold_json),
    ("dmi.txt", "grep -r . /sys/class/dmi/id/ 2>/dev/null | sed 's#/sys/class/dmi/id/##' "
     "| grep -viE 'uevid|modalias'; echo; echo '# dmidecode (root)'; "
     "dmidecode -t 0 -t 1 -t 2 -t 3 -t 11 2>/dev/null || echo '(dmidecode needs root)'"),
    ("kernel.txt", "uname -a; echo; echo '# cmdline'; cat /proc/cmdline; echo; "
     "echo '# os-release'; cat /etc/os-release; echo; echo '# virt'; systemd-detect-virt 2>/dev/null"),
    ("cpu.txt", "lscpu 2>/dev/null; echo; echo '# /proc/cpuinfo (cpu0)'; "
     "awk '/^$/{exit} {print}' /proc/cpuinfo; echo; echo '# amd_pstate'; "
     "grep -rH . /sys/devices/system/cpu/amd_pstate/ 2>/dev/null; echo; "
     "echo '# ryzenadj -i'; ryzenadj -i 2>/dev/null || echo '(ryzenadj not installed / not AMD)'"),
    ("lspci.txt", "lspci -nnvvv 2>/dev/null || lspci -nnk 2>/dev/null || echo '(lspci missing)'"),
    ("lsusb.txt", "lsusb -t 2>/dev/null; echo; lsusb 2>/dev/null; echo '=== verbose ==='; "
     "lsusb -v 2>/dev/null"),
    ("modules.txt", "lsmod; echo; for m in dell_laptop dell_wmi dell_smbios dell_smm_hwmon "
     "alienware_wmi hid_generic i8k sparse_keymap; do echo \"=== modinfo $m ===\"; "
     "modinfo $m 2>/dev/null | grep -E '^(filename|description|parm|alias):'; done"),
    ("input-devices.txt", "cat /proc/bus/input/devices"),
    ("key-capabilities.txt", _decode_key_caps),
    ("evdev-udev.txt", "for e in /dev/input/event*; do echo \"=== $e ===\"; "
     "udevadm info -q all -n $e 2>/dev/null; echo; done"),
    ("hwmon.txt", "for h in /sys/class/hwmon/hwmon*; do echo \"### $h  name=$(cat $h/name 2>/dev/null)\"; "
     "for f in $h/*; do [ -f \"$f\" ] || continue; printf '  %-26s %s\\n' \"$(basename $f)\" "
     "\"$(head -c 160 \"$f\" 2>/dev/null | tr -d '\\n')\"; done; echo; done"),
    ("thermal-power.txt", "echo '# platform_profile'; for f in /sys/firmware/acpi/platform_profile*; do "
     "echo \"$f = $(cat $f 2>/dev/null)\"; done; echo; echo '# powercap'; "
     "grep -rH . /sys/class/powercap/*/name /sys/class/powercap/*/*_range_uj 2>/dev/null; echo; "
     "echo '# power-profiles-daemon'; powerprofilesctl 2>/dev/null; echo; "
     "echo '# lm_sensors'; sensors 2>/dev/null; echo; sensors -j 2>/dev/null"),
    ("battery.txt", "echo '# power_supply sysfs'; "
     "grep -rH . /sys/class/power_supply/*/ 2>/dev/null | grep -viE 'uevent|modalias'; echo; "
     "echo '# upower'; timeout 4 upower -d 2>/dev/null; echo; "
     "echo '# smbios-battery-ctl'; timeout 4 smbios-battery-ctl --get-charging-cfg 2>/dev/null "
     "|| echo '(libsmbios not installed / not a Dell)'"),
    ("vendor-platform.txt", "echo '# /sys/devices/platform vendor interfaces'; "
     "for d in /sys/devices/platform/*wmi* /sys/devices/platform/*-laptop /sys/devices/platform/*_laptop "
     "/sys/devices/platform/alienware-wmi* /sys/devices/platform/dell-laptop; do "
     "[ -d \"$d\" ] || continue; echo \"### $d\"; grep -rH . \"$d\" 2>/dev/null "
     "| grep -viE 'uevent|modalias|power/' | head -80; echo; done; "
     "echo '# /sys/class/leds'; for l in /sys/class/leds/*; do echo \"$(basename $l): "
     "brightness=$(cat $l/brightness 2>/dev/null) max=$(cat $l/max_brightness 2>/dev/null)\"; done; echo; "
     "echo '# module parameters'; for m in dell_laptop dell_smm_hwmon alienware_wmi asus_nb_wmi "
     "hp_wmi ideapad_laptop thinkpad_acpi; do [ -d /sys/module/$m/parameters ] || continue; "
     "echo \"=== $m ===\"; grep -rH . /sys/module/$m/parameters/ 2>/dev/null; done"),
    ("firmware.txt", "echo '# fwupd devices'; timeout 6 fwupdmgr get-devices "
     "--no-authenticate-modules 2>/dev/null || timeout 6 fwupdmgr get-devices 2>/dev/null "
     "|| echo '(fwupd not installed / timed out reaching the daemon)'"),
    ("acpi.txt", "ls -l /sys/firmware/acpi/tables/ 2>/dev/null; echo; "
     "command -v acpidump >/dev/null && echo 'acpidump present — run: sudo acpidump -b (attach the DSDT.dat)'; "
     "command -v acpi_listen >/dev/null && echo 'acpi_listen present — run it and press Fn/media keys to capture ACPI events'"),
    ("dsdt.b64", "echo '# base64 of the ACPI DSDT + SSDTs — decode with:  base64 -d dsdt.b64 > acpi.bin ; "
     "iasl -d acpi.bin'; for t in /sys/firmware/acpi/tables/DSDT /sys/firmware/acpi/tables/SSDT*; do "
     "[ -r \"$t\" ] || continue; echo \"=== $(basename $t) ===\"; base64 \"$t\" 2>/dev/null; echo; done "
     "|| echo '(ACPI tables need root to read)'"),
    ("display.txt", _collect_display_txt),
    ("drm-gpu.txt", "for c in /sys/class/drm/card[0-9]*; do echo \"### $c\"; "
     "cat $c/device/uevent 2>/dev/null; echo \" runtime_status=$(cat $c/device/power/runtime_status 2>/dev/null)\"; "
     "echo; done; echo '=== nvidia-smi -q ==='; timeout 8 nvidia-smi -q 2>/dev/null; echo; "
     "echo '=== nvidia-smi -q -d SUPPORTED_CLOCKS ==='; "
     "timeout 8 nvidia-smi -q -d SUPPORTED_CLOCKS 2>/dev/null | head -50; echo; "
     "echo '=== glxinfo / vulkaninfo ==='; "
     "timeout 4 glxinfo -B 2>/dev/null | grep -E 'renderer|OpenGL version|Device'; "
     "timeout 4 vulkaninfo --summary 2>/dev/null | head -40"),
    ("openrgb.txt", "openrgb --version 2>/dev/null; echo; "
     "openrgb --noautoconnect -l --verbose 2>/dev/null | grep -vE 'i2c|SMBus|help.openrgb' "
     "|| openrgb --noautoconnect -l 2>/dev/null"),
    ("smbios-tokens.txt", "timeout 6 smbios-token-ctl 2>/dev/null | head -400 "
     "|| echo '(libsmbios / smbios-token-ctl not installed — needed for Dell battery / USB / thermal tokens)'"),
    ("dmesg-full.txt", "dmesg 2>/dev/null || echo '(dmesg needs root / kernel.dmesg_restrict=1)'"),
    ("journal-boot-tail.txt", "journalctl -b --no-pager 2>/dev/null | tail -3000 || echo '(journalctl unavailable)'"),
]


def collect_hw_bundle(dest_dir: str | None = None) -> str:
    """Write a folder of raw hardware dumps (+ the human report + a README) and
    tar it. Return the .tar.gz path. Everything needed to add a new laptop
    model to config/*.json and the sysfs paths — attach it to a
    'new hardware support' issue."""
    prod = run_cmd3("cat /sys/class/dmi/id/product_name 2>/dev/null")[2].strip() or "unknown"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", prod).strip("-").lower() or "laptop"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dirname = f"tuxthrottle-hwdump-{slug}-{stamp}"

    try:
        home = pwd.getpwnam(resolve_real_user()).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    dest_dir = dest_dir or home
    work = os.path.join(dest_dir, dirname)
    os.makedirs(work, exist_ok=True)

    for fname, cmd in _HW_BUNDLE_FILES:
        try:
            data = cmd() if callable(cmd) else run_cmd3(
                f"timeout -k 2 25 bash -lc {shlex.quote(str(cmd))}", timeout=30)[2]
        except Exception as exc:  # noqa: BLE001
            data = f"(error: {exc})"
        with open(os.path.join(work, fname), "w") as fh:
            fh.write((data or "(no output)").rstrip() + "\n")

    with open(os.path.join(work, "report.md"), "w") as fh:
        fh.write(collect_debug_report())
    with open(os.path.join(work, "README-attach-this.txt"), "w") as fh:
        fh.write(
            "TuxThrottle — hardware dump bundle\n"
            f"machine: {prod}   collected: {stamp}   euid={os.geteuid()}\n\n"
            "WHAT THIS IS\n"
            "  Raw sysfs / DMI / evdev / hwmon / PCI / OpenRGB / ACPI dumps + the\n"
            "  readable debug report, plus model-scaffold.json — an auto-generated\n"
            "  starting point for models/<slug>.json (probed fields filled, the\n"
            "  rest left under \"_todo\"). Together these are enough to add support\n"
            "  for this laptop: DMI strings to gate on, hwmon fan/pwm paths, the\n"
            "  platform_profile path + choices, evdev key codes for the Fn/media/\n"
            "  vendor keys, OpenRGB controller layout, battery method, GPU PCI ids,\n"
            "  panel modes, Dell/ASUS/Lenovo firmware tokens, and the decompilable\n"
            "  DSDT for reverse-engineering vendor WMI.\n\n"
            "HOW TO USE\n"
            "  1. Skim the files for anything private (hostname, serials in dmi.txt /\n"
            "     lsusb.txt / nvidia-smi). Redact if you care.\n"
            "  2. Open a 'new hardware support' issue and ATTACH this whole .tar.gz\n"
            "     (drag it onto the GitHub comment box).\n"
            "  3. Run this collector as root (sudo) if you can — dmidecode, the DSDT\n"
            "     and smbios-token-ctl need it. Re-run and re-attach if the first was\n"
            "     unprivileged.\n"
            "  4. If a Fn/media key doesn't work: run  sudo evtest  , pick the\n"
            "     keyboard / hotkey device, press the key, and paste those lines too.\n\n"
            "NEXT (maintainer): decode dsdt.b64 with  base64 -d dsdt.b64 > acpi.bin ;\n"
            "  iasl -d acpi.bin   ; finish model-scaffold.json's _todo fields; add\n"
            "  \"models\": [<slug>] gates to config/*.json entries that differ.\n\n"
            "FILES\n" + "".join(f"  {n}\n" for n, _ in _HW_BUNDLE_FILES) +
            "  report.md\n")

    tgz = os.path.join(dest_dir, dirname + ".tar.gz")
    run_cmd3(f"tar czf {shlex.quote(tgz)} -C {shlex.quote(dest_dir)} {shlex.quote(dirname)}",
             timeout=60)
    run_cmd3(f"rm -rf {shlex.quote(work)}", timeout=10)
    if os.geteuid() == 0:
        try:
            pw = pwd.getpwnam(resolve_real_user())
            os.chown(tgz, pw.pw_uid, pw.pw_gid)
        except (KeyError, OSError):
            pass
    return tgz


