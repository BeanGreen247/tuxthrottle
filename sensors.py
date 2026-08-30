#!/usr/bin/env python3
"""Sensor reads + Game Mode control — shared by tuxthrottle.py (GUI,
stdlib-only) and tray_monitor.py (PySide6). Deliberately has NO GUI
dependency of its own so the checkbox Toolkit doesn't need PySide6 just to
show live numbers.

Tested platform: Dell G15 5515 Ryzen Edition (Ryzen 7 5800H, RTX 3050 Ti
Mobile). CPU temp lookup assumes k10temp (AMD); GPU lookups auto-detect by
PCI vendor ID (0x1002 AMD / nvidia-smi for NVIDIA) so they should degrade
gracefully on a different Dell model rather than crash, but haven't been
verified on one.
"""
import glob
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache


@lru_cache(maxsize=None)
def which(cmd: str):
    # PATH doesn't change over a session; cache so the hot dashboard/status
    # paths stop stat-walking PATH on every poll.
    return shutil.which(cmd)


TARGET_MODEL = "Dell G15 5515"
TARGET_BOARD = "0R3CDX"


def _dmi(name: str) -> str:
    try:
        with open(f"/sys/class/dmi/id/{name}") as f:
            return f.read().strip()
    except OSError:
        return ""


def detect_model() -> dict:
    """Read this machine's DMI identity and say whether it's the platform the
    Toolkit was written for (Dell G15 5515). Every model-specific path here —
    the alienware-wmi fan boost, the AW-ELC keyboard, the 5800H C-state fix,
    k10temp, … — assumes that board."""
    vendor = _dmi("sys_vendor")
    product = _dmi("product_name")
    board = _dmi("board_name")
    bios = _dmi("bios_version")
    is_target = (product == TARGET_MODEL) or (board == TARGET_BOARD)
    close = (not is_target) and ("G15 5515" in product or product.startswith("Dell G15"))
    return {
        "vendor": vendor,
        "product": product or "unknown",
        "board": board,
        "bios": bios,
        "is_target": is_target,
        "is_close": close,
    }


def read_cpu_freq_ghz() -> str:
    try:
        freqs = []
        for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
            with open(path) as f:
                freqs.append(int(f.read().strip()))
        if freqs:
            return f"{max(freqs) / 1_000_000:.2f} GHz (peak core)"
    except OSError:
        pass
    return "n/a"


def read_cpu_freq_ghz_value() -> float:
    try:
        freqs = []
        for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
            with open(path) as f:
                freqs.append(int(f.read().strip()))
        if freqs:
            return max(freqs) / 1_000_000
    except OSError:
        pass
    return 0.0


def read_cpu_temp_c() -> str:
    val = read_cpu_temp_c_value()
    return f"{val:.0f} C" if val is not None else "n/a"


def read_cpu_temp_c_value():
    for name_path in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name_path) as f:
                if f.read().strip() != "k10temp":
                    continue
            hwmon_dir = name_path.rsplit("/", 1)[0]
            with open(f"{hwmon_dir}/temp1_input") as f:
                return int(f.read().strip()) / 1000
        except OSError:
            continue
    return None


def _amdgpu_card_dir():
    for card in glob.glob("/sys/class/drm/card[0-9]*/device"):
        vendor_path = f"{card}/vendor"
        try:
            with open(vendor_path) as f:
                if f.read().strip() == "0x1002":  # AMD
                    return card
        except OSError:
            continue
    return None


def has_amd_gpu() -> bool:
    return _amdgpu_card_dir() is not None


def has_nvidia_gpu() -> bool:
    if which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return True
        except Exception:  # noqa: BLE001
            pass
    for vendor_path in glob.glob("/sys/bus/pci/devices/*/vendor"):
        try:
            with open(vendor_path) as f:
                if f.read().strip() == "0x10de":  # NVIDIA
                    return True
        except OSError:
            continue
    return False


def read_igpu_clock_temp() -> str:
    clock, temp = read_igpu_clock_temp_values()
    c = f"{clock} MHz" if clock is not None else "?"
    t = f"{temp:.0f} C" if temp is not None else "?"
    return f"{c}, {t}"


def read_igpu_clock_temp_values():
    card = _amdgpu_card_dir()
    if not card:
        return None, None
    clock = None
    try:
        with open(f"{card}/pp_dpm_sclk") as f:
            for line in f:
                if "*" in line:
                    m = re.search(r"(\d+)Mhz", line)
                    if m:
                        clock = int(m.group(1))
    except OSError:
        pass
    temp = None
    for hwmon in glob.glob(f"{card}/hwmon/hwmon*/temp1_input"):
        try:
            with open(hwmon) as f:
                temp = int(f.read().strip()) / 1000
        except OSError:
            pass
    return clock, temp


def read_dgpu_clock_temp_util() -> str:
    clock, temp, util, power = read_dgpu_values()
    if clock is None:
        return "n/a (no nvidia-smi / asleep)"
    pw = f", {power:.0f} W" if power is not None else ""
    return f"{clock} MHz, {temp} C, {util}% util{pw}"


def _nvidia_pci_dir():
    for dev in glob.glob("/sys/bus/pci/devices/*"):
        try:
            with open(f"{dev}/vendor") as f:
                if f.read().strip() != "0x10de":
                    continue
            with open(f"{dev}/class") as f:
                if f.read().strip().startswith("0x03"):  # display controller
                    return dev
        except OSError:
            continue
    return None


def dgpu_is_awake() -> bool:
    """True unless the NVIDIA dGPU is runtime-suspended. Lets callers skip
    `nvidia-smi` while the GPU is parked — polling it would spin it back up
    (battery + heat) for nothing."""
    dev = _nvidia_pci_dir()
    if not dev:
        return False
    try:
        with open(f"{dev}/power/runtime_status") as f:
            return f.read().strip() == "active"
    except OSError:
        return True


def read_dgpu_values():
    """Returns (clock_mhz, temp_c, util_pct, power_w) — any may be None."""
    if not which("nvidia-smi") or not dgpu_is_awake():
        return None, None, None, None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=clocks.gr,temperature.gpu,utilization.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None, None, None, None
        clk, temp, util, power = [x.strip() for x in out.stdout.strip().split(",")]
        return int(clk), int(temp), int(util), float(power)
    except Exception:  # noqa: BLE001
        return None, None, None, None


def _powercap_energy_paths():
    """Cumulative-energy sysfs files for CPU package power (RAPL, both
    Intel's intel-rapl and the AMD equivalent share this powercap class).
    Reading these needs no root — but the kernel's RAPL side-channel
    mitigation (post-2020) makes them root-only by default on stock
    permissions; see the RaplPowerPermissions tweak."""
    paths = []
    for name_path in glob.glob("/sys/class/powercap/*/name"):
        try:
            with open(name_path) as f:
                name = f.read().strip()
            if "package" in name.lower() or "core" in name.lower():
                energy_path = name_path.rsplit("/", 1)[0] + "/energy_uj"
                if glob.glob(energy_path):
                    paths.append(energy_path)
        except OSError:
            continue
    return paths


def read_cpu_power_watts():
    """Delta-measures CPU package power over a short window via RAPL
    powercap energy counters. Returns None if unreadable (permissions) or
    unsupported (no RAPL zone — some AMD kernels/BIOS combos)."""
    paths = _powercap_energy_paths()
    if not paths:
        return None
    try:
        def read_total():
            total = 0
            for p in paths:
                with open(p) as f:
                    total += int(f.read().strip())
            return total

        e1 = read_total()
        t1 = time.monotonic()
        time.sleep(0.1)
        e2 = read_total()
        t2 = time.monotonic()
        delta_uj = e2 - e1
        if delta_uj < 0:  # counter wrapped
            return None
        return (delta_uj / 1_000_000) / (t2 - t1)
    except (OSError, PermissionError):
        return None


def rapl_permissions_ok() -> bool:
    """Checks the world-readable bit on the RAPL energy file directly,
    rather than 'can I open it' — the Toolkit GUI runs fully elevated
    (root), which can always read these regardless of the actual
    permission bits, so a naive open()-succeeds check would never catch
    the problem that unprivileged readers (tray icon, hotkey listener)
    actually hit."""
    import stat
    paths = _powercap_energy_paths()
    if not paths:
        return True  # nothing to check — not a permissions problem
    try:
        mode = os.stat(paths[0]).st_mode
        return bool(mode & stat.S_IROTH)
    except OSError:
        return False


def get_game_mode_state() -> bool:
    if not which("powerprofilesctl"):
        return False
    try:
        out = subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "performance"
    except Exception:  # noqa: BLE001
        return False


def notify(summary: str, body: str = "") -> None:
    """Best-effort desktop notification. No-op if notify-send is missing or
    there's no session bus to reach (e.g. the fully-elevated GUI process)."""
    exe = which("notify-send")
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "-a", "TuxThrottle", "-i", "input-keyboard", "-t", "10000",
             summary, body],
            capture_output=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass


def _notify_game_mode(enable: bool) -> None:
    if enable:
        notify("Game Mode: ON", "G-Mode / performance profile — fans + power limits up")
    else:
        notify("Game Mode: OFF", "Back to balanced profile")


def _gmode_kbd_indicator(enable: bool) -> None:
    """Disabled. The plan was to tint the G-key zone red while G-Mode is
    active (as AWCC does on Windows), but this AW-ELC controller drops the
    entire backlight whenever the four zones are given different colours —
    the same reason the Keyboard tab is whole-keyboard only. Kept as a no-op
    so callers don't need to change."""
    return


def set_game_mode(enable: bool) -> tuple[bool, str]:
    names = (("gaming-performance", "amdgpu-perf-high", "nvidia-max-perf") if enable
             else ("gaming-balanced", "amdgpu-perf-auto"))
    paths = [p for p in (which(n) for n in names) if p]
    if not paths:
        return False, "None of the Game Mode helper scripts are installed — run the Toolkit's Presets first."

    def _ok_now() -> bool:
        _notify_game_mode(enable)
        _gmode_kbd_indicator(enable)
        return True

    try:
        # Passwordless sudo, one script at a time — this is what the narrow
        # PasswordlessGameModeToggle sudoers rule whitelists (exact script
        # paths, not an `sh -c` wrapper). Needed for the hotkey path, which
        # has no way to answer a GUI prompt.
        last_err = ""
        any_fail = False
        for p in paths:
            r = subprocess.run(["sudo", "-n", p], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                any_fail = True
                last_err = (r.stderr or r.stdout or f"{p} failed").strip()
        # A helper can exit non-zero on harmless noise (e.g. nvidia-settings
        # over a headless display) while still doing the real work — trust
        # the end state.
        if not any_fail or get_game_mode_state() == enable:
            return _ok_now(), ""

        # Fall back to a single interactive GUI prompt for the whole batch
        # (fine for tray/Dashboard clicks; a hotkey press with no sudoers
        # rule just gets an unanswered prompt).
        r = subprocess.run(["pkexec", "sh", "-c", " ; ".join(paths)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 and get_game_mode_state() != enable:
            return False, (r.stderr or r.stdout or last_err or "pkexec failed").strip()
        return _ok_now(), ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def toggle_game_mode_external():
    """Entry point for external callers (tray icon click, hotkey listener)
    to flip Game Mode without going through any particular GUI."""
    enable = not get_game_mode_state()
    return set_game_mode(enable)


# --------------------------------------------------------------------------- #
#  Fan control — Dell G15 5515
#
#  Two hwmon devices carry the fans:
#   * alienware_wmi : fan{1,2}_input (RPM, ro), fan{1,2}_boost (0-255, RW) —
#     the AWCC-style additive boost. Boost only *adds* airflow on top of the
#     firmware curve, so it can never stop a fan → the safe lever.
#   * dell_smm      : pwm{1,2} + pwm{1,2}_enable (0=full, 1=manual, 2=auto).
#     Real manual control, but a low pwm can slow/stop the fan → risky, gated
#     behind a warning in the GUI and floored at PWM_FLOOR here.
#  Thermal profile is /sys/firmware/acpi/platform_profile
#  (balanced / performance / custom on this board).
# --------------------------------------------------------------------------- #

_PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
PWM_FLOOR = 77   # ~30 % of 255 — a manual curve never drops below this


def _hwmon_by_name(name: str):
    for name_path in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name_path) as f:
                if f.read().strip() == name:
                    return name_path.rsplit("/", 1)[0]
        except OSError:
            continue
    return None


def _read_int(path: str):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_fans() -> list:
    """[{'index', 'label', 'rpm', 'max', 'boost'}] per fan. RPM comes from
    alienware_wmi, falling back to dell_smm; 'boost' is the current
    alienware_wmi fan boost (0-255) or None."""
    aw = _hwmon_by_name("alienware_wmi")
    smm = _hwmon_by_name("dell_smm")
    base = aw or smm
    if not base:
        return []
    fans = []
    for i in (1, 2):
        rpm = _read_int(f"{base}/fan{i}_input")
        if rpm is None and smm and smm != base:
            rpm = _read_int(f"{smm}/fan{i}_input")
        if rpm is None:
            continue
        try:
            with open(f"{base}/fan{i}_label") as f:
                label = f.read().strip()
        except OSError:
            label = f"Fan {i}"
        fans.append({
            "index": i,
            "label": label,
            "rpm": rpm,
            "max": _read_int(f"{base}/fan{i}_max") or 4700,
            "boost": _read_int(f"{aw}/fan{i}_boost") if aw else None,
        })
    return fans


def get_fan_boost() -> list:
    aw = _hwmon_by_name("alienware_wmi")
    if not aw:
        return []
    return [(_read_int(f"{aw}/fan{i}_boost") or 0) for i in (1, 2)]


def set_fan_boost(index: int, value_0_255: int) -> tuple[bool, str]:
    aw = _hwmon_by_name("alienware_wmi")
    if not aw:
        return False, "alienware_wmi hwmon not present"
    v = max(0, min(255, int(value_0_255)))
    try:
        with open(f"{aw}/fan{index}_boost", "w") as f:
            f.write(str(v))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def platform_profile_choices() -> list:
    try:
        with open(f"{_PLATFORM_PROFILE}_choices") as f:
            return f.read().split()
    except OSError:
        return []


def get_platform_profile() -> str:
    try:
        with open(_PLATFORM_PROFILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def set_platform_profile(name: str) -> tuple[bool, str]:
    try:
        with open(_PLATFORM_PROFILE, "w") as f:
            f.write(name)
        return True, ""
    except OSError as exc:
        return False, str(exc)


def get_pwm_state() -> list:
    """[(enable_mode, pwm_value)] for dell_smm pwm1/pwm2. enable: 0 full /
    1 manual / 2 automatic. pwm_value is None while in automatic mode."""
    smm = _hwmon_by_name("dell_smm")
    if not smm:
        return []
    return [(_read_int(f"{smm}/pwm{i}_enable"), _read_int(f"{smm}/pwm{i}"))
            for i in (1, 2)]


def set_pwm_manual(index: int, value_0_255: int) -> tuple[bool, str]:
    """Put dell_smm fan `index` into manual mode at the given PWM (floored at
    PWM_FLOOR so the fan can't be stopped)."""
    smm = _hwmon_by_name("dell_smm")
    if not smm:
        return False, "dell_smm hwmon not present"
    v = max(PWM_FLOOR, min(255, int(value_0_255)))
    try:
        with open(f"{smm}/pwm{index}_enable", "w") as f:
            f.write("1")
        with open(f"{smm}/pwm{index}", "w") as f:
            f.write(str(v))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def restore_fan_auto() -> tuple[bool, str]:
    """Full reset: dell_smm pwm back to automatic, alienware_wmi boost to 0."""
    errs = []
    smm = _hwmon_by_name("dell_smm")
    if smm:
        for i in (1, 2):
            try:
                with open(f"{smm}/pwm{i}_enable", "w") as f:
                    f.write("2")
            except OSError as exc:
                errs.append(str(exc))
    for i in (1, 2):
        ok, err = set_fan_boost(i, 0)
        if not ok and "not present" not in err:
            errs.append(err)
    return (not errs), "; ".join(errs)


# --------------------------------------------------------------------------- #
#  CPU power limits — ryzenadj (Ryzen 7 5800H / Cezanne)
#
#  ryzenadj talks to the SMU over the ACPI mailbox; every call (reads too)
#  needs root. The GUI runs elevated so this works directly; the tray/hotkey
#  (unprivileged) will just get "n/a", which is fine — they only display.
#  Limits are Watts. STAPM = sustained (long window), fast = short burst,
#  slow = the medium PPT window.
# --------------------------------------------------------------------------- #

# name shown by `ryzenadj -i` → key we expose
_RYZENADJ_LIMIT_ROWS = {
    "STAPM LIMIT": "stapm_limit",
    "PPT LIMIT FAST": "fast_limit",
    "PPT LIMIT SLOW": "slow_limit",
    "THM LIMIT CORE": "tctl_limit",
}
_RYZENADJ_VALUE_ROWS = {
    "STAPM VALUE": "stapm_value",
    "PPT VALUE FAST": "fast_value",
    "PPT VALUE SLOW": "slow_value",
    "THM VALUE CORE": "tctl_value",
}


def ryzenadj_available() -> bool:
    return which("ryzenadj") is not None


def read_ryzenadj_info() -> dict | None:
    """Parse `ryzenadj -i` into {stapm_limit, fast_limit, slow_limit,
    tctl_limit, *_value, ...} (Watts / °C). None if ryzenadj is missing or
    couldn't run (not root, unsupported SMU)."""
    exe = which("ryzenadj")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-i"], capture_output=True, text=True, timeout=8)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0 and not out.stdout:
        return None
    info: dict = {}
    for line in out.stdout.splitlines():
        if line.count("|") < 3:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].upper()
        try:
            val = float(cells[1])
        except ValueError:
            continue
        for label, key in _RYZENADJ_LIMIT_ROWS.items():
            if name == label:
                info[key] = round(val, 1)
        for label, key in _RYZENADJ_VALUE_ROWS.items():
            if name == label:
                info[key] = round(val, 1)
    return info or None


def set_ryzenadj_limits(fast_w=None, slow_w=None, stapm_w=None) -> tuple[bool, str]:
    """Apply any of the three PPT limits (Watts). Clamped to a sane
    5800H envelope (10–90 W). At least one value must be given."""
    exe = which("ryzenadj")
    if not exe:
        return False, "ryzenadj is not installed (Power & Limits tab tweak)"
    args = [exe]
    for flag, val in (("--stapm-limit", stapm_w), ("--fast-limit", fast_w),
                      ("--slow-limit", slow_w)):
        if val is None:
            continue
        w = max(10, min(90, int(round(float(val)))))
        args.append(f"{flag}={w * 1000}")  # ryzenadj wants milliwatts
    if len(args) == 1:
        return False, "no limit given"
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        # ryzenadj prints "Setting ... to N ... : OK" per arg; a non-zero exit
        # with all-OK lines still means it worked on this SMU.
        if r.returncode == 0 or "OK" in (r.stdout or ""):
            return True, ""
        return False, (r.stderr or r.stdout or "ryzenadj failed").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------- #
#  Battery charge threshold — stop charging at N % to spare the cell on a
#  laptop that lives on AC. Kernel exposes this on Dell via the
#  `charge_control_end_threshold` sysfs attr when the platform supports it.
# --------------------------------------------------------------------------- #

def _battery_dir() -> str | None:
    for bat in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        if glob.glob(f"{bat}/charge_control_end_threshold"):
            return bat
    return None


def _smbios_battery_ctl():
    return which("smbios-battery-ctl")


def _smbios_battery_end() -> int | None:
    """End % of the Dell 'custom' charge interval via libsmbios. Needs root;
    returns None if libsmbios is absent, not custom mode, or unreadable."""
    exe = _smbios_battery_ctl()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--get-charging-cfg"],
                             capture_output=True, text=True, timeout=8)
        m = re.search(r"[Ee]nd\s*[:=]\s*(\d+)", out.stdout or "")
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def battery_charge_limit_info() -> dict:
    """{'supported', 'method', 'current', 'capacity', 'ac_online',
    'dell_libsmbios_possible'}. method is 'sysfs' | 'libsmbios' | None."""
    bat = _battery_dir()
    info: dict = {"supported": False, "method": None, "current": None,
                  "capacity": None, "ac_online": None,
                  "dell_libsmbios_possible": False}
    if bat:
        info.update(supported=True, method="sysfs",
                    current=_read_int(f"{bat}/charge_control_end_threshold"),
                    capacity=_read_int(f"{bat}/capacity"))
    else:
        is_dell = "dell" in _dmi("sys_vendor").lower()
        info["dell_libsmbios_possible"] = is_dell
        if _smbios_battery_ctl():
            end = _smbios_battery_end()
            info.update(supported=True, method="libsmbios", current=end)
        for cap in glob.glob("/sys/class/power_supply/BAT*/capacity"):
            info["capacity"] = _read_int(cap)
            break
    for ac in glob.glob("/sys/class/power_supply/A[CD]*/online"):
        v = _read_int(ac)
        if v is not None:
            info["ac_online"] = bool(v)
            break
    return info


def set_battery_charge_limit(percent: int) -> tuple[bool, str]:
    p = max(50, min(100, int(percent)))
    bat = _battery_dir()
    if bat:
        try:
            with open(f"{bat}/charge_control_end_threshold", "w") as f:
                f.write(str(p))
            return True, ""
        except OSError as exc:
            return False, str(exc)
    exe = _smbios_battery_ctl()
    if exe:
        # Dell 'custom' interval: start < end, both in 50..100, gap >= 5.
        start = max(50, min(p - 5, 95))
        if p >= 100:
            try:
                r = subprocess.run([exe, "--set-charging-mode=standard"],
                                   capture_output=True, text=True, timeout=15)
                return (r.returncode == 0), (r.stderr or r.stdout or "").strip()
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
        try:
            r = subprocess.run([exe, f"--set-custom-charge-interval={start} {p}"],
                               capture_output=True, text=True, timeout=15)
            return (r.returncode == 0), (r.stderr or r.stdout or "").strip()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
    return False, ("no charge_control_end_threshold in sysfs; on Dell install "
                   "libsmbios (dnf install libsmbios) for firmware-level control")


# --------------------------------------------------------------------------- #
#  NVIDIA board power limit — nvidia-smi -pl. The single most useful GPU
#  lever on this chassis for heat / battery. Needs root to set.
# --------------------------------------------------------------------------- #

def _f(x: str):
    try:
        return float(x)
    except ValueError:
        return None  # nvidia-smi prints "[N/A]" for fields the GPU doesn't expose


def nvidia_power_limit_info() -> dict | None:
    """{'supported', 'min', 'max', 'default', 'current'} in Watts.
    None if the dGPU is asleep / nvidia-smi is missing (don't wake it to poll).
    supported=False when the query works but the GPU's power limit is
    firmware-locked — the Dell G15 5515's RTX 3050 Ti Mobile is one of these
    (Dynamic Boost; `power.limit` reads [N/A], `-pl` is rejected)."""
    if not which("nvidia-smi") or not dgpu_is_awake():
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=power.limit,power.min_limit,power.max_limit,power.default_limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        cur, lo, hi, dft = [_f(x.strip()) for x in out.stdout.strip().split(",")]
        return {
            "supported": cur is not None,
            "current": round(cur) if cur is not None else None,
            "min": round(lo) if lo is not None else 1,
            "max": round(hi) if hi is not None else 100,
            "default": round(dft) if dft is not None else None,
        }
    except Exception:  # noqa: BLE001
        return None


def set_nvidia_power_limit(watts: int) -> tuple[bool, str]:
    if not which("nvidia-smi"):
        return False, "nvidia-smi not installed"
    info = nvidia_power_limit_info()
    if info and not info["supported"]:
        return False, ("this GPU's power limit is firmware-locked (laptop Dynamic "
                       "Boost) — nvidia-smi -pl is not supported on it")
    w = int(watts)
    if info:
        w = max(int(info["min"]), min(int(info["max"]), w))
    try:
        subprocess.run(["nvidia-smi", "-pm", "1"], capture_output=True, timeout=8)
        r = subprocess.run(["nvidia-smi", "-pl", str(w)],
                           capture_output=True, text=True, timeout=10)
        blob = (r.stdout or "") + (r.stderr or "")
        # nvidia-smi exits 0 even when it prints "not supported in current scope"
        if "not supported" in blob.lower():
            return False, ("this GPU's power limit is firmware-locked (laptop "
                           "Dynamic Boost) — nvidia-smi -pl is not supported on it")
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "nvidia-smi -pl failed").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# --------------------------------------------------------------------------- #
#  Feral GameMode bridge status (informational — the GameModeBridge tweak
#  wires gamemoded's start/end hooks to gaming-performance/-balanced).
# --------------------------------------------------------------------------- #

def envycontrol_available() -> bool:
    return which("envycontrol") is not None


def gpu_mode_get() -> str | None:
    """Current hybrid-graphics mode via EnvyControl: 'integrated' | 'hybrid'
    | 'nvidia', or None if envycontrol isn't installed / couldn't be read."""
    exe = which("envycontrol")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query"], capture_output=True, text=True, timeout=8)
        text = (out.stdout or out.stderr or "").strip().lower()
        for mode in ("integrated", "hybrid", "nvidia"):
            if mode in text:
                return mode
    except Exception:  # noqa: BLE001
        pass
    return None


def gpu_mode_set(mode: str) -> tuple[bool, str]:
    """Switch hybrid-graphics mode (needs root; takes effect after logout /
    reboot). mode is 'integrated' | 'hybrid' | 'nvidia'."""
    exe = which("envycontrol")
    if not exe:
        return False, "envycontrol not installed (install the EnvyControl app first)"
    if mode not in ("integrated", "hybrid", "nvidia"):
        return False, f"unknown mode {mode!r}"
    try:
        r = subprocess.run([exe, "-s", mode, "--dm", "sddm"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, ""
        # retry without the display-manager hint (older envycontrol)
        r2 = subprocess.run([exe, "-s", mode], capture_output=True, text=True, timeout=60)
        if r2.returncode == 0:
            return True, ""
        return False, (r2.stderr or r2.stdout or r.stderr or "envycontrol failed").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def gamemode_status() -> dict:
    """{'installed': bool, 'active': bool, 'clients': int}."""
    exe = which("gamemoded")
    st = {"installed": exe is not None, "active": False, "clients": 0}
    if not exe:
        return st
    try:
        r = subprocess.run([exe, "-s"], capture_output=True, text=True, timeout=5)
        text = (r.stdout or "") + (r.stderr or "")
        st["active"] = "is active" in text
        m = re.search(r"(\d+)\s+client", text)
        if m:
            st["clients"] = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return st
