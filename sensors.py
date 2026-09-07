#!/usr/bin/env python3
"""Sensor reads + Game Mode control — shared by tuxthrottle.py (GUI,
stdlib-only) and tray_monitor.py (PySide6). Deliberately has NO GUI
dependency of its own so the checkbox Toolkit doesn't need PySide6 just to
show live numbers.

Reference platform: Dell G15 5515 Ryzen Edition (Ryzen 7 5800H, RTX 3050 Ti
Mobile). Board specifics — CPU/fan hwmon names, the platform_profile path, the
PWM floor, fan count, the per-fan boost attribute, the max RPM, and which
platform_profile value equals Game Mode — come from the model profile
(`models/<slug>.json` via `model_profile()`), defaulting to the 5515 values
when no profile matches.
GPU lookups auto-detect by PCI vendor ID (0x1002 AMD / nvidia-smi for NVIDIA).
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from functools import cache


@cache
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


_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_MODEL_CACHE: dict | None = None


def _model_files() -> list[str]:
    """All models/*.json except `_`-prefixed ones — those are work-in-progress
    scaffolds (`collect-model --out models/_foo.json`) and test fixtures, and
    must never be auto-matched against a real machine."""
    try:
        return sorted(p for p in glob.glob(os.path.join(_MODELS_DIR, "*.json"))
                      if not os.path.basename(p).startswith("_"))
    except OSError:
        return []


def model_profile() -> dict:
    """The per-board hardware profile for this machine (models/<slug>.json),
    matched on DMI. Falls back to g15-5515 (the reference board), or a minimal
    stub if that file is missing. Cached — DMI doesn't change at runtime.

    Set ``TUXTHROTTLE_MODEL=<slug>`` to force a specific profile regardless of
    DMI — a dev/testing lever for bringing up a new board, printed loudly to
    stderr so it can't be left on by accident."""
    global _MODEL_CACHE
    if _MODEL_CACHE is not None:
        return _MODEL_CACHE
    forced = os.environ.get("TUXTHROTTLE_MODEL", "").strip()
    product, board = _dmi("product_name"), _dmi("board_name")
    fallback: dict = {"id": "g15-5515", "name": TARGET_MODEL, "match": {}}
    chosen = None
    # the override may name a `_`-prefixed WIP/fixture file, so scan those too
    paths = (sorted(glob.glob(os.path.join(_MODELS_DIR, "*.json")))
             if forced else _model_files())
    for path in paths:
        try:
            with open(path) as f:
                prof = json.load(f)
        except (OSError, ValueError):
            continue
        if forced and prof.get("id") == forced:
            print(f"sensors: TUXTHROTTLE_MODEL override active — using "
                  f"'{forced}' profile, not this machine's DMI", file=sys.stderr)
            _MODEL_CACHE = prof
            return _MODEL_CACHE
        m = prof.get("match", {})
        if not forced and (
                (product and product in m.get("product_name", [])) or
                (board and board in m.get("board_name", []))):
            chosen = prof
            break
        if prof.get("id") == "g15-5515":
            fallback = prof
    if forced:
        print(f"sensors: TUXTHROTTLE_MODEL='{forced}' set but no such "
              f"models/*.json — falling back", file=sys.stderr)
    _MODEL_CACHE = chosen or fallback
    return _MODEL_CACHE


def model_id() -> str:
    return str(model_profile().get("id", "g15-5515"))


# --------------------------------------------------------------------------- #
#  Model-profile accessors. Every hardware specific that used to be a literal
#  in this file now comes from models/<slug>.json via model_profile(), with
#  the reference 5515 value as the fallback — so a machine with no matching
#  profile (or an old profile missing a field) behaves exactly as before.
# --------------------------------------------------------------------------- #

def _prof_section(name: str) -> dict:
    v = model_profile().get(name)
    return v if isinstance(v, dict) else {}


def _cpu_temp_hwmon() -> str:
    """hwmon `name` that carries the CPU package temperature (`k10temp` on
    AMD, `coretemp`/`k10temp` elsewhere)."""
    return _prof_section("cpu").get("hwmon") or "k10temp"


def _fan_hwmon() -> str:
    """hwmon that exposes fan RPM + the additive `fanN_boost` lever."""
    return _prof_section("fans").get("hwmon") or "alienware_wmi"


def _fan_pwm_hwmon() -> str:
    """hwmon that exposes real `pwmN` / `pwmN_enable` manual fan control."""
    return _prof_section("fans").get("pwm_hwmon") or "dell_smm"


def _fan_indices() -> tuple[int, ...]:
    """1-based fan indices this board has (2 on the 5515: CPU + GPU)."""
    fans = _prof_section("fans")
    n = fans.get("count") or len(fans.get("rpm_inputs") or []) or 2
    return tuple(range(1, int(n) + 1))


def _platform_profile_path() -> str:
    return (_prof_section("fans").get("platform_profile_path")
            or "/sys/firmware/acpi/platform_profile")


def _pwm_floor() -> int:
    """Lowest PWM a manual curve may command, so a fan is never stopped."""
    return int(_prof_section("fans").get("pwm_floor") or PWM_FLOOR)


def _fan_boost_attr(i: int) -> str:
    """hwmon attribute for fan `i`'s additive AWCC-style boost."""
    names = _prof_section("fans").get("additive_boost") or []
    if 1 <= i <= len(names) and names[i - 1]:
        return str(names[i - 1])
    return f"fan{i}_boost"


def _fan_rpm_max() -> int:
    """Best-guess top fan RPM, for the Fans-tab gauge when `fanN_max` is absent."""
    return int(_prof_section("fans").get("rpm_max") or 4700)


def _game_mode_value() -> str:
    """The platform_profile value that = Game Mode / G-Mode on this board."""
    return str(_prof_section("game_mode").get("value") or "performance")


def model_allows(models) -> bool:
    """Whether a config entry carrying this `models` list applies to the
    current board. None / empty = applies everywhere (the default)."""
    if not models:
        return True
    return model_id() in [str(m) for m in models]


def model_skips_tweak(tweak_id: str) -> bool:
    """Whether the current model profile's `tweaks_skip` names this tweak id."""
    return tweak_id in (model_profile().get("tweaks_skip") or [])


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
    want = _cpu_temp_hwmon()
    for name_path in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name_path) as f:
                if f.read().strip() != want:
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


def cpu_model_name() -> str:
    """Marketing CPU name, e.g. 'AMD Ryzen 7 5800H'. '' if unreadable."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


_GPU_TRIM = re.compile(
    r"\s*\((?:rev|prog-if)[^)]*\)|"                       # (rev a1) / (prog-if 00)
    r"\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]|"              # [10de:25a0]
    r"\s*\bCorporation\b|\s*\bTechnology\b|\s*\bInc\.?", re.I)


def gpu_names() -> list:
    """Ordered list of GPU marketing names for MangoHud labels — dGPU-ish
    entries first. Uses nvidia-smi for the NVIDIA name, lspci for the rest;
    de-duplicated, best-effort. e.g. ['GeForce RTX 3050 Ti', 'Radeon Vega']."""
    names: list = []
    nvidia_seen = False
    if which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi",
                                "--query-gpu=name", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=5)
            for ln in (r.stdout or "").splitlines():
                ln = ln.strip()
                if ln and ln not in names:
                    names.append(ln)
                    nvidia_seen = True
        except (OSError, subprocess.SubprocessError):
            pass
    if which("lspci"):
        try:
            r = subprocess.run(["lspci", "-mm"], capture_output=True,
                               text=True, timeout=6)
            for ln in (r.stdout or "").splitlines():
                parts = re.findall(r'"([^"]*)"', ln)
                if len(parts) < 3:
                    continue
                cls = parts[0].lower()
                if not ("vga compatible controller" in cls
                        or "3d controller" in cls
                        or "display controller" in cls):
                    continue
                if nvidia_seen and parts[1].lower().startswith("nvidia"):
                    continue                       # already named by nvidia-smi
                dev = _GPU_TRIM.sub("", parts[2]).strip(" ,-")
                # prefer the pretty name inside brackets: 'GA107 [GeForce RTX ...]'
                m = re.search(r"\[([^\]]+)\]", dev)
                if m:
                    dev = m.group(1).strip()
                dev = dev.split(" / ")[0].strip()  # 'Radeon Vega Series / ... Mobile'
                if dev and dev not in names:
                    names.append(dev)
        except (OSError, subprocess.SubprocessError):
            pass
    return names


def _norm_pci(addr: str) -> str:
    """'00000000:01:00.0' | '01:00.0' -> '0000:01:00.0' (lspci -D style)."""
    a = (addr or "").strip().lower()
    if not a:
        return ""
    parts = a.split(":")
    if len(parts) == 2:                       # bus:slot.func, no domain
        return f"0000:{a}"
    try:
        return f"{int(parts[0], 16):04x}:{':'.join(parts[1:])}"
    except ValueError:
        return a


def gpu_devices() -> list:
    """[{'name', 'pci'}] for every real GPU, dGPU-ish first — like gpu_names()
    but each entry also carries its PCI address so two identical cards can be
    told apart. Best-effort; 'pci' may be '' if only nvidia-smi named it and it
    gave no bus id."""
    devs: list = []
    seen_names: set = set()
    nvidia_seen = False
    # Only ask nvidia-smi when the dGPU is already powered — a bare query wakes
    # a runtime-suspended card, and a burst of those at startup can wedge the
    # driver. lspci below names and locates every GPU without touching it.
    if which("nvidia-smi") and dgpu_is_awake():
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,pci.bus_id",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            for ln in (r.stdout or "").splitlines():
                if "," not in ln:
                    continue
                name, _, pci = ln.partition(",")
                name = name.strip()
                if name and name not in seen_names:
                    devs.append({"name": name, "pci": _norm_pci(pci)})
                    seen_names.add(name)
                    nvidia_seen = True
        except (OSError, subprocess.SubprocessError):
            pass
    if which("lspci"):
        try:
            r = subprocess.run(["lspci", "-Dmm"], capture_output=True,
                               text=True, timeout=6)
            for ln in (r.stdout or "").splitlines():
                slot = ln.split(None, 1)[0] if ln.strip() else ""
                parts = re.findall(r'"([^"]*)"', ln)
                if len(parts) < 3:
                    continue
                cls = parts[0].lower()
                if not ("vga compatible controller" in cls
                        or "3d controller" in cls
                        or "display controller" in cls):
                    continue
                if nvidia_seen and parts[1].lower().startswith("nvidia"):
                    for d in devs:                 # backfill the pci we lacked
                        if not d["pci"] and "nvidia" in d["name"].lower():
                            d["pci"] = _norm_pci(slot)
                    continue
                dev = _GPU_TRIM.sub("", parts[2]).strip(" ,-")
                m = re.search(r"\[([^\]]+)\]", dev)
                if m:
                    dev = m.group(1).strip()
                dev = dev.split(" / ")[0].strip()
                if dev and dev not in seen_names:
                    devs.append({"name": dev, "pci": _norm_pci(slot)})
                    seen_names.add(dev)
        except (OSError, subprocess.SubprocessError):
            pass
    return devs


def _gpu_lspci_blobs() -> dict:
    """{normalised PCI addr: lowercased 'vendor device subsystem' text} for
    every GPU lspci can see. Richer than gpu_devices()' cleaned name and, being
    pure lspci, it never wakes a runtime-suspended card. Names come straight
    from the live pci.ids database — nothing GPU-specific is hard-coded here."""
    out: dict = {}
    if not which("lspci"):
        return out
    try:
        r = subprocess.run(["lspci", "-Dmm"], capture_output=True,
                           text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return out
    for ln in (r.stdout or "").splitlines():
        if not ln.strip():
            continue
        slot = ln.split(None, 1)[0]
        parts = re.findall(r'"([^"]*)"', ln)
        if len(parts) < 3:
            continue
        cls = parts[0].lower()
        if not ("vga compatible controller" in cls or "3d controller" in cls
                or "display controller" in cls):
            continue
        out[_norm_pci(slot)] = " ".join(parts[1:]).lower()
    return out


def _tok(text: str) -> set:
    """Word/number tokens (≥2 chars) of a GPU name or label, lowercased."""
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) > 1}


def _gpu_name_candidates(devices=None) -> list:
    """[{'pci', 'tokens'}] for every GPU actually present, built live from the
    hardware: the card list is sysfs DRM (drm_gpus) ∪ lspci, and each card's
    identifying tokens are whatever lspci's pci.ids lookup — plus any nvidia-smi
    / caller-supplied marketing name — says for that exact PCI address. No
    static vendor tables or keyword lists; add a GPU nobody's heard of and it
    still resolves."""
    blobs = _gpu_lspci_blobs()
    order: list = list(blobs)
    try:
        for g in drm_gpus():
            p = _norm_pci(g.get("pci", ""))
            if p and p not in order:
                order.append(p)
    except Exception:  # noqa: BLE001
        pass
    named: dict = {}
    for d in (devices or []):
        p = _norm_pci(d.get("pci", ""))
        if p:
            named[p] = (named.get(p, "") + " " + d.get("name", "")).strip()
            if p not in order:
                order.append(p)
    return [{"pci": p, "tokens": _tok(blobs.get(p, "") + " " + named.get(p, ""))}
            for p in order]


def gpu_label_pci_map(labels: list, devices=None) -> list:
    """Best-effort PCI address for each MangoHud `gpu_text` label.

    A label typed by the user — or read back from a MangoHud.conf whose
    `gpu_text` order no longer matches detection order — must stay glued to the
    card it actually names, or `gpu_list` pins the wrong stats line under it
    (the "GPU labels are swapped" bug on hybrid laptops).

    Matching is fully data-driven: each label is scored against every present
    card by shared tokens, weighting each shared token by how *rare* it is
    across the cards on this machine (a token unique to one card discriminates;
    boilerplate like the vendor name or "mobile"/"series" that every card
    shares barely moves the score). Highest score wins, each PCI used at most
    once, '' when nothing overlaps (caller keeps its positional guess)."""
    cand = _gpu_name_candidates(devices)
    n = len(cand)
    if not n:
        return ["" for _ in labels]
    df: dict = {}
    for c in cand:
        for t in c["tokens"]:
            df[t] = df.get(t, 0) + 1

    used: set = set()
    out: list = []
    for label in labels:
        ltoks = _tok(label)
        best, best_score = "", 0.0
        for c in cand:
            if not c["pci"] or c["pci"] in used:
                continue
            # 1 point per shared token + a bonus for tokens few cards carry
            score = sum(1.0 + (n - df[t]) for t in (ltoks & c["tokens"]))
            if score > best_score:
                best, best_score = c["pci"], score
        if best and best_score > 0:
            used.add(best)
            out.append(best)
        else:
            out.append("")
    return out


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


# --------------------------------------------------------------------------- #
#  VRAM accounting — which GPU the desktop renders on, how full each GPU's
#  video memory is, and which processes are holding it. The G15 5515's Ryzen
#  iGPU has a tiny (512 MiB) UMA carveout that the KDE/Wayland stack routinely
#  fills, spilling to slower GTT; the RTX 3050 Ti has 4 GiB we want to keep
#  free for DaVinci Resolve / games / 3D. Pure sysfs + one optional nvidia-smi.
# --------------------------------------------------------------------------- #

def mangohud_gpu_order() -> list:
    """PCI addresses in MangoHud's `gpu_list` index order. MangoHud (0.7+)
    enumerates GPUs by ascending DRM **render node** (`/sys/class/drm/renderD*`,
    128, 129, …) — NOT `cardN`, and on hybrid laptops the two orders differ
    (e.g. G15 5515: card0=NVIDIA/card1=AMD but renderD128=AMD/renderD129=NVIDIA).
    Index `i` of the returned list is the GPU that `gpu_list=i` selects. This is
    usually iGPU-first, the reverse of `gpu_devices()` (discrete-first). Returns
    [] when it can't be read (callers then keep positional 0,1,…)."""
    nodes = []
    for p in glob.glob("/sys/class/drm/renderD[0-9]*"):
        m = re.search(r"/renderD(\d+)$", p)
        if not m:
            continue
        try:
            pci = os.path.basename(os.path.realpath(os.path.join(p, "device")))
        except OSError:
            continue
        if re.match(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$", pci):
            nodes.append((int(m.group(1)), _norm_pci(pci)))
    out: list = []
    for _, pci in sorted(nodes):
        if pci not in out:
            out.append(pci)
    return out


def drm_gpus() -> list:
    """[{card, render, pci, vendor, driver, kind, boot_vga}] for every real
    render GPU. `kind` is 'integrated' (amdgpu/i915 + boot_vga) or 'discrete'
    (NVIDIA, or a non-boot_vga card). Discrete first, like gpu_devices()."""
    out = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = os.path.join(card, "device")
        try:
            vendor = open(f"{dev}/vendor").read().strip()
        except OSError:
            continue
        driver = os.path.basename(os.path.realpath(f"{dev}/driver")) \
            if os.path.exists(f"{dev}/driver") else ""
        pci = os.path.basename(os.path.realpath(dev))
        try:
            boot_vga = open(f"{dev}/boot_vga").read().strip() == "1"
        except OSError:
            boot_vga = False
        rnode = ""
        for rd in glob.glob("/sys/class/drm/renderD*"):
            try:
                if os.path.realpath(f"{rd}/device") == os.path.realpath(dev):
                    rnode = "/dev/dri/" + os.path.basename(rd)
                    break
            except OSError:
                pass
        nvidia = vendor == "0x10de" or driver in ("nvidia", "nouveau")
        kind = "discrete" if (nvidia or not boot_vga) else "integrated"
        out.append({
            "card": "/dev/dri/" + os.path.basename(card),
            "render": rnode, "pci": pci, "vendor": vendor,
            "driver": driver, "kind": kind, "boot_vga": boot_vga,
        })
    out.sort(key=lambda g: 0 if g["kind"] == "discrete" else 1)
    return out


def _amdgpu_vram(dev: str) -> dict:
    def _mb(name):
        try:
            return int(open(f"{dev}/{name}").read().strip()) // (1024 * 1024)
        except (OSError, ValueError):
            return None
    used, total = _mb("mem_info_vram_used"), _mb("mem_info_vram_total")
    gtt = _mb("mem_info_gtt_used")
    return {"used_mb": used, "total_mb": total, "gtt_used_mb": gtt}


def vram_info() -> list:
    """Per-GPU VRAM usage: [{name, pci, driver, kind, used_mb, total_mb,
    pct, gtt_used_mb}]. AMD from sysfs (no root, no wakeup); NVIDIA from
    nvidia-smi only when the dGPU is already awake."""
    names = {}
    for d in gpu_devices():
        if d.get("pci"):
            names[d["pci"].lower()] = d["name"]
    rows = []
    for g in drm_gpus():
        dev = f"/sys/bus/pci/devices/{g['pci']}"
        name = names.get(g["pci"].lower()) or g["driver"] or g["pci"]
        row = {"name": name, "pci": g["pci"], "driver": g["driver"],
               "kind": g["kind"], "used_mb": None, "total_mb": None,
               "pct": None, "gtt_used_mb": None, "asleep": False}
        if g["driver"] == "amdgpu":
            row.update(_amdgpu_vram(dev))
        elif g["driver"] in ("nvidia", "nouveau"):
            if not (which("nvidia-smi") and dgpu_is_awake()):
                row["asleep"] = True
            else:
                try:
                    r = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5)
                    u, t = (x.strip() for x in r.stdout.strip().split(","))
                    row["used_mb"], row["total_mb"] = int(u), int(t)
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
        if row["used_mb"] is not None and row["total_mb"]:
            row["pct"] = round(100 * row["used_mb"] / row["total_mb"])
        rows.append(row)
    return rows


_FDINFO_VRAM = re.compile(r"drm-(?:total|resident|memory)-vram:\s*(\d+)\s*KiB")
_FDINFO_DRV = re.compile(r"drm-driver:\s*(\S+)")


def vram_consumers(limit: int = 12) -> list:
    """Best-effort [{pid, comm, driver, vram_mb}] sorted desc — who is holding
    video memory right now. AMD/Intel via /proc/<pid>/fdinfo drm-*-vram lines
    (one entry per pid, deduped); NVIDIA via nvidia-smi compute+graphics apps
    when the card is awake."""
    by_pid = {}
    for fdinfo in glob.glob("/proc/[0-9]*/fdinfo/*"):
        pid = fdinfo.split("/")[2]
        if pid in by_pid:
            continue
        try:
            txt = open(fdinfo, errors="ignore").read()
        except OSError:
            continue
        if "drm-driver" not in txt:
            continue
        mv = _FDINFO_VRAM.search(txt)
        if not mv:
            continue
        kb = int(mv.group(1))
        if kb <= 0:
            continue
        dm = _FDINFO_DRV.search(txt)
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            comm = "?"
        by_pid[pid] = {"pid": int(pid), "comm": comm,
                       "driver": dm.group(1) if dm else "drm",
                       "vram_mb": round(kb / 1024, 1)}
    if which("nvidia-smi") and dgpu_is_awake():
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-compute-apps=pid,used_memory,process_name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            for ln in (r.stdout or "").splitlines():
                parts = [x.strip() for x in ln.split(",")]
                if len(parts) < 3:
                    continue
                pid, mem, pname = parts[0], parts[1], parts[2]
                by_pid[f"nv{pid}"] = {
                    "pid": int(pid), "comm": os.path.basename(pname),
                    "driver": "nvidia", "vram_mb": float(mem or 0)}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    rows = sorted(by_pid.values(), key=lambda x: x["vram_mb"], reverse=True)
    return rows[:limit]


def nvidia_runtime_pm() -> "dict | None":
    """{'control': 'auto'|'on', 'status': 'active'|'suspended'|...} for the
    dGPU's PCI power/control knob. `auto` lets the driver D3-cold the card
    when nothing uses it (frees its VRAM + a few watts); `on` pins it awake.
    None when there is no NVIDIA GPU."""
    dev = _nvidia_pci_dir()
    if not dev:
        return None
    try:
        control = open(f"{dev}/power/control").read().strip()
    except OSError:
        control = "?"
    try:
        status = open(f"{dev}/power/runtime_status").read().strip()
    except OSError:
        status = "?"
    return {"control": control, "status": status}


def set_nvidia_runtime_pm(auto: bool) -> "tuple[bool, str]":
    """Flip the dGPU PCI power/control to 'auto' (allow suspend) or 'on'
    (pin awake). Live only — not persistent; the NvidiaRuntimePM tweak makes
    it stick. Needs root for the sysfs write."""
    dev = _nvidia_pci_dir()
    if not dev:
        return False, "no NVIDIA GPU"
    want = "auto" if auto else "on"
    try:
        with open(f"{dev}/power/control", "w") as f:
            f.write(want)
        return True, f"dGPU runtime PM -> {want}"
    except OSError as exc:
        return False, f"{exc} (need root)"


def session_cmd(argv: list) -> list:
    """Public wrapper of _session_cmd — run argv in the real user's graphical
    session when we're root, unchanged otherwise. Used by tuxthrottle_vram."""
    return _session_cmd(argv)


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
        return out.stdout.strip() == _game_mode_value()
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
#  Fan control. The hwmon names + platform_profile path + PWM floor come from
#  the model profile (models/<slug>.json → _fan_hwmon() / _fan_pwm_hwmon() /
#  _platform_profile_path() / _pwm_floor() / _fan_indices()); the 5515 values
#  are the fallback.
#
#  On the reference 5515 two hwmon devices carry the fans:
#   * alienware_wmi : fanN_input (RPM, ro), fanN_boost (0-255, RW) — the
#     AWCC-style additive boost. Boost only *adds* airflow on top of the
#     firmware curve, so it can never stop a fan → the safe lever.
#   * dell_smm      : pwmN + pwmN_enable (0=full, 1=manual, 2=auto). Real
#     manual control, but a low pwm can slow/stop the fan → risky, gated
#     behind a warning in the GUI and floored at _pwm_floor() here.
# --------------------------------------------------------------------------- #

PWM_FLOOR = 77   # 5515 default; a manual curve never drops below _pwm_floor()


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
    aw = _hwmon_by_name(_fan_hwmon())
    smm = _hwmon_by_name(_fan_pwm_hwmon())
    base = aw or smm
    if not base:
        return []
    fans = []
    for i in _fan_indices():
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
            "max": _read_int(f"{base}/fan{i}_max") or _fan_rpm_max(),
            "boost": _read_int(f"{aw}/{_fan_boost_attr(i)}") if aw else None,
        })
    return fans


def get_fan_boost() -> list:
    aw = _hwmon_by_name(_fan_hwmon())
    if not aw:
        return []
    return [(_read_int(f"{aw}/{_fan_boost_attr(i)}") or 0) for i in _fan_indices()]


def set_fan_boost(index: int, value_0_255: int) -> tuple[bool, str]:
    aw = _hwmon_by_name(_fan_hwmon())
    if not aw:
        return False, f"{_fan_hwmon()} hwmon not present"
    v = max(0, min(255, int(value_0_255)))
    try:
        with open(f"{aw}/{_fan_boost_attr(index)}", "w") as f:
            f.write(str(v))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def platform_profile_choices() -> list:
    try:
        with open(f"{_platform_profile_path()}_choices") as f:
            return f.read().split()
    except OSError:
        return []


def get_platform_profile() -> str:
    try:
        with open(_platform_profile_path()) as f:
            return f.read().strip()
    except OSError:
        return ""


def set_platform_profile(name: str) -> tuple[bool, str]:
    try:
        with open(_platform_profile_path(), "w") as f:
            f.write(name)
        return True, ""
    except OSError as exc:
        return False, str(exc)


def get_pwm_state() -> list:
    """[(enable_mode, pwm_value)] per fan for the pwm hwmon. enable: 0 full /
    1 manual / 2 automatic. pwm_value is None while in automatic mode."""
    smm = _hwmon_by_name(_fan_pwm_hwmon())
    if not smm:
        return []
    return [(_read_int(f"{smm}/pwm{i}_enable"), _read_int(f"{smm}/pwm{i}"))
            for i in _fan_indices()]


def set_pwm_manual(index: int, value_0_255: int) -> tuple[bool, str]:
    """Put fan `index` into manual mode at the given PWM (floored at
    _pwm_floor() so the fan can't be stopped)."""
    smm = _hwmon_by_name(_fan_pwm_hwmon())
    if not smm:
        return False, f"{_fan_pwm_hwmon()} hwmon not present"
    v = max(_pwm_floor(), min(255, int(value_0_255)))
    try:
        with open(f"{smm}/pwm{index}_enable", "w") as f:
            f.write("1")
        with open(f"{smm}/pwm{index}", "w") as f:
            f.write(str(v))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def restore_fan_auto() -> tuple[bool, str]:
    """Full reset: pwm hwmon back to automatic, fan boost to 0."""
    errs = []
    smm = _hwmon_by_name(_fan_pwm_hwmon())
    if smm:
        for i in _fan_indices():
            try:
                with open(f"{smm}/pwm{i}_enable", "w") as f:
                    f.write("2")
            except OSError as exc:
                errs.append(str(exc))
    for i in _fan_indices():
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
#  Ryzen Curve Optimizer (per-all-core undervolt) — Cezanne
#
#  `ryzenadj --set-coall=<n>` sets an all-core CO offset (negative = undervolt,
#  0..-30 is the usual sane range). ryzenadj can't read the CO back, so the
#  desired value is only ever tracked in a file (co.json). This is a genuinely
#  risky knob: too aggressive an offset causes silent calculation errors, a
#  segfault storm, or a hard hang — ALWAYS drive it through
#  tuxthrottle_co_stress.py, which stress-tests and auto-reverts.
# --------------------------------------------------------------------------- #

def _cpu_is_amd() -> bool:
    try:
        with open("/proc/cpuinfo") as f:
            return "AuthenticAMD" in f.read(4096)
    except OSError:
        return False


def ryzenadj_co_supported() -> bool:
    """CO is a Zen2+/Cezanne feature and needs ryzenadj. We can't verify the
    SMU accepts it without writing, so this is 'ryzenadj present on an AMD CPU'."""
    return ryzenadj_available() and _cpu_is_amd()


def set_co_offset(all_core: int) -> tuple[bool, str]:
    """Apply an all-core Curve Optimizer offset. `all_core` is the signed CO
    count; clamped to -50..0 (negative = undervolt, 0 = stock). ryzenadj does
    the negative -> SMU two's-complement conversion itself, so pass the plain
    signed int."""
    exe = which("ryzenadj")
    if not exe:
        return False, "ryzenadj is not installed (RyzenCurveOptimizer tweak)"
    v = max(-50, min(0, int(round(float(all_core)))))
    try:
        r = subprocess.run([exe, f"--set-coall={v}"],
                           capture_output=True, text=True, timeout=10)
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


def battery_charge_mode() -> str | None:
    """Dell firmware charging mode via libsmbios: 'standard' | 'express' |
    'adaptive' | 'primarily_ac' | 'custom' | None. 'express' = fast charge."""
    exe = _smbios_battery_ctl()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--get-charging-cfg"],
                             capture_output=True, text=True, timeout=8).stdout or ""
        m = re.search(r"[Cc]harging mode\s*[:=]\s*([A-Za-z_]+)", out)
        return m.group(1).lower() if m else None
    except Exception:  # noqa: BLE001
        return None


def set_battery_charge_mode(mode: str) -> tuple[bool, str]:
    """Set the Dell firmware charging mode. 'standard' or 'express' (fast).
    Setting a non-custom mode clears any custom charge interval."""
    exe = _smbios_battery_ctl()
    if not exe:
        return False, "libsmbios not installed (Dell battery threshold tweak)"
    if mode not in ("standard", "express", "adaptive", "primarily_ac"):
        return False, f"unknown charging mode {mode!r}"
    try:
        r = subprocess.run([exe, f"--set-charging-mode={mode}"],
                           capture_output=True, text=True, timeout=15)
        return (r.returncode == 0), (r.stderr or r.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def battery_health_info() -> dict:
    """Static + slow-changing battery facts from
    /sys/class/power_supply/BAT*: design vs full-charge capacity (→ wear %),
    charge cycles, chemistry, plus the live charge / draw. `{}` if there's no
    battery. Model-agnostic — this is generic ACPI/`power_supply` sysfs."""
    bat = next(iter(sorted(glob.glob("/sys/class/power_supply/BAT*"))), None)
    if not bat:
        return {}

    def _s(name: str):
        try:
            with open(f"{bat}/{name}") as f:
                return f.read().strip()
        except OSError:
            return None

    # energy_* (µWh, Wh-based gauges) or charge_* (µAh, Ah-based gauges)
    full = _read_int(f"{bat}/energy_full")
    design = _read_int(f"{bat}/energy_full_design")
    unit, scale = "Wh", 1_000_000
    if full is None:
        full = _read_int(f"{bat}/charge_full")
        design = _read_int(f"{bat}/charge_full_design")
        unit = "Ah"
    wear = round(100 * (1 - full / design), 1) if (full and design and design > 0) else None

    power_uw = _read_int(f"{bat}/power_now")
    volt_uv = _read_int(f"{bat}/voltage_now")
    cur_ua = _read_int(f"{bat}/current_now")
    if power_uw is None and cur_ua is not None and volt_uv:
        power_uw = abs(cur_ua) * volt_uv // 1_000_000

    # time-to-full / -to-empty at the current rate. Keep the reservoir and the
    # rate in matching units: energy gauges (µWh) with power (µW), charge gauges
    # (µAh) with current (µA).
    status = (_s("status") or "").strip()
    if unit == "Wh":
        now_raw, rate = _read_int(f"{bat}/energy_now"), power_uw
    else:
        now_raw = _read_int(f"{bat}/charge_now")
        rate = abs(cur_ua) if cur_ua else None
    eta_min = eta_kind = None
    if rate and now_raw is not None and full:
        if status.lower() == "discharging":
            eta_min, eta_kind = round(now_raw / rate * 60), "to empty"
        elif status.lower() == "charging" and full > now_raw:
            eta_min, eta_kind = round((full - now_raw) / rate * 60), "to full"

    return {
        "present": True,
        "manufacturer": _s("manufacturer"),
        "model": _s("model_name"),
        "technology": _s("technology"),
        "status": status or None,
        "capacity_pct": _read_int(f"{bat}/capacity"),
        "cycle_count": _read_int(f"{bat}/cycle_count"),
        "full": round(full / scale, 1) if full else None,
        "design": round(design / scale, 1) if design else None,
        "unit": unit,
        "wear_pct": wear,
        "power_w": round(power_uw / 1_000_000, 1) if power_uw else None,
        "voltage_v": round(volt_uv / 1_000_000, 2) if volt_uv else None,
        "eta_min": eta_min,
        "eta_kind": eta_kind,
    }


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


def nvidia_powerd_status() -> dict:
    """`nvidia-powerd` arbitrates the shared CPU/GPU power budget (Dynamic
    Boost) on Ryzen+RTX laptops — if it isn't running the dGPU is stuck near
    its base clock. {'installed', 'active'}."""
    st = {"installed": False, "active": False}
    try:
        r = subprocess.run(["systemctl", "is-active", "nvidia-powerd.service"],
                           capture_output=True, text=True, timeout=5)
        st["active"] = r.stdout.strip() == "active"
        r2 = subprocess.run(["systemctl", "list-unit-files", "nvidia-powerd.service"],
                            capture_output=True, text=True, timeout=5)
        st["installed"] = "nvidia-powerd.service" in (r2.stdout or "")
    except (OSError, subprocess.SubprocessError):
        pass
    return st


def amd_pstate_mode() -> str | None:
    """'active' | 'guided' | 'passive' — how the amd_pstate driver runs.
    'active' (EPP) is what gives Cezanne its proper boost behaviour. None if
    the driver isn't amd_pstate (old acpi-cpufreq) or unreadable."""
    try:
        with open("/sys/devices/system/cpu/amd_pstate/status") as f:
            return f.read().strip() or None
    except OSError:
        return None


def vrr_status() -> dict:
    """Variable-refresh-rate capability of the connected panels.
    {'capable': [conn...], 'panels': N}. Purely informational — reads
    /sys/class/drm/*/vrr_capable."""
    capable = []
    for p in glob.glob("/sys/class/drm/card*-*/vrr_capable"):
        try:
            if open(p).read().strip() == "1":
                capable.append(p.split("/")[-2].split("-", 1)[-1])
        except OSError:
            continue
    return {"capable": capable,
            "panels": len(glob.glob("/sys/class/drm/card*-*/status"))}


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


# --------------------------------------------------------------------------- #
#  Run a command inside the real user's graphical session. kscreen-doctor /
#  kwriteconfig6-style tools need XDG_RUNTIME_DIR + the session bus; the GUI
#  runs elevated (pkexec/sudo) so we have to hop back to the user for those.
# --------------------------------------------------------------------------- #

_SESSION_USER: "str | None" = None


def set_session_user(name: "str | None") -> None:
    """Tell the module which login user's graphical session to target for
    `_session_cmd()` (kscreen-doctor etc.). The GUI/CLI inherit SUDO_USER /
    PKEXEC_UID and don't need this; `tuxthrottled` runs straight from systemd
    with none of those set, so it calls this with its `--user` value."""
    global _SESSION_USER
    _SESSION_USER = name or None


def _real_user_uid() -> "tuple[str, int] | None":
    import pwd
    if _SESSION_USER:
        try:
            p = pwd.getpwnam(_SESSION_USER)
            if p.pw_uid != 0:
                return p.pw_name, p.pw_uid
        except KeyError:
            pass
    for env in ("SUDO_USER", "PKEXEC_USER"):
        val = os.environ.get(env)
        if val and val != "root":
            try:
                p = pwd.getpwnam(val)
                return p.pw_name, p.pw_uid
            except KeyError:
                pass
    for env in ("PKEXEC_UID", "SUDO_UID"):
        val = os.environ.get(env)
        if val:
            try:
                p = pwd.getpwuid(int(val))
                if p.pw_uid != 0:
                    return p.pw_name, p.pw_uid
            except (KeyError, ValueError):
                pass
    try:
        p = pwd.getpwuid(os.getuid())
        if p.pw_uid != 0:
            return p.pw_name, p.pw_uid
    except KeyError:
        pass
    return None


def _session_ready() -> bool:
    """True only when the target user's KWin/Wayland session is actually up.
    `kscreen-doctor` SIGABRTs (core-dumps, and DrKonqi then pops a crash
    notification) if it's run before KWin — which is exactly what the boot /
    resume `reassert` path was tripping every time. Callers that shell out to
    kscreen-doctor gate on this."""
    try:
        if os.geteuid() != 0:
            return True                       # we already are the session user
    except AttributeError:
        return True
    ru = _real_user_uid()
    if not ru:
        return False
    _name, uid = ru
    if not any(os.path.exists(f"/run/user/{uid}/{s}")
               for s in ("wayland-0", "wayland-1")) and \
       not os.path.exists("/tmp/.X11-unix/X0"):
        return False
    for proc in ("kwin_wayland", "kwin_x11"):
        try:
            if subprocess.run(["pgrep", "-u", str(uid), "-x", proc],
                              capture_output=True).returncode == 0:
                return True
        except OSError:
            return False
    return False


def _session_cmd(argv: list) -> list:
    """Wrap argv so it runs in the real user's Wayland/D-Bus session when we
    are root; return argv unchanged when we already are that user."""
    try:
        if os.geteuid() != 0:
            return argv
    except AttributeError:      # non-POSIX; shouldn't happen on this tool
        return argv
    ru = _real_user_uid()
    if not ru:
        return argv
    name, uid = ru
    rundir = f"/run/user/{uid}"
    return ["sudo", "-u", name, "-H", "env",
            f"XDG_RUNTIME_DIR={rundir}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={rundir}/bus",
            "WAYLAND_DISPLAY=wayland-0", "DISPLAY=:0", *argv]


# --------------------------------------------------------------------------- #
#  Panel refresh rate (KDE / KScreen). The G15 5515 panel is 120 Hz; dropping
#  to 60 on battery is a real power lever. kscreen-doctor persists the choice
#  in kscreen's own config, so no boot service is needed.
# --------------------------------------------------------------------------- #

def panel_modes() -> "dict | None":
    """{'output', 'current_id', 'current_hz', 'modes':[{id,w,h,hz}], 'rates':[hz]}.
    None if kscreen-doctor is missing or KScreen/KWin isn't reachable."""
    if not which("kscreen-doctor") or not _session_ready():
        return None                          # don't let kscreen-doctor abort
    try:
        r = subprocess.run(_session_cmd(["kscreen-doctor", "-j"]),
                           capture_output=True, text=True, timeout=6)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    outs = data.get("outputs", []) or []
    if not outs:
        return None

    def _key(o):
        n = (o.get("name") or "").upper()
        return (0 if n.startswith(("EDP", "LVDS")) else 1,
                0 if o.get("enabled") else 1)

    o = sorted(outs, key=_key)[0]
    cur_id = str(o.get("currentModeId", ""))
    modes, rates, cur_hz = [], set(), None
    for m in o.get("modes", []) or []:
        size = m.get("size", {}) or {}
        w, h, hz = size.get("width"), size.get("height"), m.get("refreshRate")
        if not (w and h and hz):
            continue
        hz = round(float(hz), 3)
        mid = str(m.get("id", ""))
        modes.append({"id": mid, "w": int(w), "h": int(h), "hz": hz})
        rates.add(round(hz))
        if mid == cur_id:
            cur_hz = hz
    return {"output": o.get("name"), "current_id": cur_id, "current_hz": cur_hz,
            "modes": modes, "rates": sorted(rates)}


def set_panel_refresh(hz: int) -> "tuple[bool, str]":
    """Switch the internal panel to ~`hz`, keeping the current resolution when
    a matching mode exists there. Runs in the user's session."""
    info = panel_modes()
    if not info:
        return False, "kscreen-doctor not available (KDE / KScreen only)"
    if not info["modes"]:
        return False, "no modes reported for the panel"
    want = float(hz)
    cur = next((m for m in info["modes"] if m["id"] == info["current_id"]), None)
    pool = info["modes"]
    if cur:
        same_res = [m for m in pool if m["w"] == cur["w"] and m["h"] == cur["h"]]
        if same_res:
            pool = same_res
    best = min(pool, key=lambda m: (abs(m["hz"] - want), -(m["w"] * m["h"])))
    tag = f"{best['w']}x{best['h']}@{round(best['hz'])}"
    for spec in (f"output.{info['output']}.mode.{best['id']}",
                 f"output.{info['output']}.mode.{tag}"):
        try:
            r = subprocess.run(_session_cmd(["kscreen-doctor", spec]),
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True, tag
            last = (r.stderr or r.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            last = str(exc)
    return False, last or "kscreen-doctor failed"


# --------------------------------------------------------------------------- #
#  Touchpad (KWin's own D-Bus InputDevice interface — Wayland-native, no
#  xinput). Session-only by design: these are live KWin property writes, not
#  a boot-persisted config, so "disable" only lasts until the next KWin
#  session (logout/reboot) — a stuck-off touchpad can never survive a reboot
#  on its own, which is the safety margin that matters most here.
# --------------------------------------------------------------------------- #

_TOUCHPAD_PATH_RE = re.compile(r"(/org/kde/KWin/InputDevice/\S+)")


def _touchpad_device_path() -> "str | None":
    """The KWin D-Bus object path for the touchpad, or None if KWin isn't
    reachable or no device reports `touchpad=true`. Re-resolved every call —
    the eventN path isn't guaranteed stable across reconnects."""
    if not which("busctl"):
        return None
    try:
        r = subprocess.run(_session_cmd(["busctl", "--user", "tree", "org.kde.KWin"]),
                           capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for path in _TOUCHPAD_PATH_RE.findall(r.stdout):
        try:
            rr = subprocess.run(_session_cmd(
                ["busctl", "--user", "get-property", "org.kde.KWin", path,
                 "org.kde.KWin.InputDevice", "touchpad"]),
                capture_output=True, text=True, timeout=6)
        except (OSError, subprocess.SubprocessError):
            continue
        if rr.returncode == 0 and rr.stdout.split()[:2] == ["b", "true"]:
            return path
    return None


def _touchpad_get_bool(path: str, prop: str) -> "bool | None":
    try:
        r = subprocess.run(_session_cmd(
            ["busctl", "--user", "get-property", "org.kde.KWin", path,
             "org.kde.KWin.InputDevice", prop]),
            capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) < 2 or parts[0] != "b":
        return None
    return parts[1] == "true"


def touchpad_info() -> dict:
    """{'available', 'name', 'enabled', 'tap_to_click', 'natural_scroll',
    'disable_while_typing'}. `available=False` when no touchpad is reachable
    via KWin's D-Bus (not Plasma/Wayland, or no touchpad on this machine)."""
    path = _touchpad_device_path()
    if not path:
        return {"available": False, "name": None, "enabled": None,
                "tap_to_click": None, "natural_scroll": None,
                "disable_while_typing": None}
    name = None
    try:
        r = subprocess.run(_session_cmd(
            ["busctl", "--user", "get-property", "org.kde.KWin", path,
             "org.kde.KWin.InputDevice", "name"]),
            capture_output=True, text=True, timeout=6)
        if r.returncode == 0:
            name = r.stdout.split(" ", 1)[-1].strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "available": True, "name": name,
        "enabled": _touchpad_get_bool(path, "enabled"),
        "tap_to_click": _touchpad_get_bool(path, "tapToClick"),
        "natural_scroll": _touchpad_get_bool(path, "naturalScroll"),
        "disable_while_typing": _touchpad_get_bool(path, "disableWhileTyping"),
    }


def _touchpad_set_bool(prop: str, value: bool) -> "tuple[bool, str]":
    path = _touchpad_device_path()
    if not path:
        return False, "no touchpad reachable via KWin D-Bus (KDE Plasma / Wayland only)"
    try:
        r = subprocess.run(_session_cmd(
            ["busctl", "--user", "set-property", "org.kde.KWin", path,
             "org.kde.KWin.InputDevice", prop, "b", "true" if value else "false"]),
            capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "busctl set-property failed").strip()
    return True, f"{prop} -> {value}"


def set_touchpad_enabled(on: bool) -> "tuple[bool, str]":
    return _touchpad_set_bool("enabled", on)


def set_touchpad_tap_to_click(on: bool) -> "tuple[bool, str]":
    return _touchpad_set_bool("tapToClick", on)


def set_touchpad_natural_scroll(on: bool) -> "tuple[bool, str]":
    return _touchpad_set_bool("naturalScroll", on)


def set_touchpad_disable_while_typing(on: bool) -> "tuple[bool, str]":
    return _touchpad_set_bool("disableWhileTyping", on)


# --------------------------------------------------------------------------- #
#  NVIDIA graphics-clock lock. Unlike -pl (firmware-locked on the 3050 Ti),
#  `nvidia-smi --lock-gpu-clocks` works in both directions — underclocking for
#  battery / heat is the useful one on this chassis.
# --------------------------------------------------------------------------- #

def nvidia_clock_info() -> "dict | None":
    """{'supported', 'gr_min', 'gr_max', 'gr_cur', 'mem_max', 'mem_cur'} MHz.
    None if the dGPU is asleep / nvidia-smi missing (don't wake it to poll)."""
    if not which("nvidia-smi") or not dgpu_is_awake():
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=clocks.max.graphics,clocks.max.memory,"
             "clocks.graphics,clocks.memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=6)
        if out.returncode != 0:
            return None
        gmax, mmax, gcur, mcur = [_f(x.strip()) for x in out.stdout.strip().split(",")]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    gr_min = None
    try:
        q = subprocess.run(["nvidia-smi", "-q", "-d", "SUPPORTED_CLOCKS"],
                           capture_output=True, text=True, timeout=8)
        vals = [int(v) for v in re.findall(r"Graphics\s*:\s*(\d+)\s*MHz", q.stdout or "")]
        if vals:
            gr_min = min(vals)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {"supported": gmax is not None,
            "gr_min": gr_min or 210,
            "gr_max": round(gmax) if gmax else None,
            "gr_cur": round(gcur) if gcur else None,
            "mem_max": round(mmax) if mmax else None,
            "mem_cur": round(mcur) if mcur else None}


def set_nvidia_clock_lock(gr_min: int, gr_max: int) -> "tuple[bool, str]":
    """Clamp the dGPU graphics clock to [gr_min, gr_max] MHz. Needs root."""
    if not which("nvidia-smi"):
        return False, "nvidia-smi not installed"
    info = nvidia_clock_info()
    if info and info.get("gr_max"):
        floor, ceil = int(info.get("gr_min") or 210), int(info["gr_max"])
        gr_min = max(floor, min(ceil, int(gr_min)))
        gr_max = max(gr_min, min(ceil, int(gr_max)))
    else:
        gr_min, gr_max = int(gr_min), max(int(gr_min), int(gr_max))
    try:
        subprocess.run(["nvidia-smi", "-pm", "1"], capture_output=True, timeout=8)
        r = subprocess.run(["nvidia-smi", f"--lock-gpu-clocks={gr_min},{gr_max}"],
                           capture_output=True, text=True, timeout=12)
        blob = ((r.stdout or "") + (r.stderr or "")).lower()
        if r.returncode == 0 and "not supported" not in blob:
            return True, f"{gr_min}-{gr_max} MHz"
        return False, (r.stderr or r.stdout or "lock-gpu-clocks failed").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def reset_nvidia_clocks() -> "tuple[bool, str]":
    if not which("nvidia-smi"):
        return False, "nvidia-smi not installed"
    try:
        r = subprocess.run(["nvidia-smi", "--reset-gpu-clocks"],
                           capture_output=True, text=True, timeout=12)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "reset failed").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def nvidia_clock_offset_info() -> dict:
    """Whether a core/mem *clock offset* (overclock) can be applied on this
    box, plus the current offset if readable. Offsets go through
    `nvidia-settings`, which needs Coolbits enabled AND a reachable X display
    — usually absent on a Wayland-only session, so `available` is commonly
    False here. Never raises."""
    out = {"available": False, "reason": "", "core": None, "mem": None,
           "core_range": None}
    ns = which("nvidia-settings")
    if not ns:
        out["reason"] = "nvidia-settings not installed"
        return out
    disp = os.environ.get("DISPLAY")
    if not disp:
        out["reason"] = "no X DISPLAY (nvidia-settings can't run under pure Wayland)"
        return out
    try:
        r = subprocess.run(
            [ns, "-c", disp, "-q", "[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels",
             "-q", "[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels"],
            capture_output=True, text=True, timeout=8)
        blob = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "ERROR" in blob or "Unable" in blob:
            out["reason"] = "GPUGraphicsClockOffset not exposed (Coolbits not set?)"
            return out
        m = re.search(r"GraphicsClockOffsetAllPerformanceLevels'.*?\):\s*(-?\d+)", blob)
        mm = re.search(r"MemoryTransferRateOffsetAllPerformanceLevels'.*?\):\s*(-?\d+)", blob)
        rng = re.search(r"range (-?\d+) - (-?\d+)", blob)
        out.update(available=True,
                   core=int(m.group(1)) if m else 0,
                   mem=int(mm.group(1)) if mm else 0,
                   core_range=(int(rng.group(1)), int(rng.group(2))) if rng else (-200, 1000))
        return out
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        out["reason"] = str(exc)
        return out


def set_nvidia_clock_offset(core_mhz: int, mem_mhz: int) -> "tuple[bool, str]":
    """Apply a core / memory clock offset via nvidia-settings. Only call when
    nvidia_clock_offset_info()['available'] is True. Needs root + the user's
    X session."""
    ns = which("nvidia-settings")
    disp = os.environ.get("DISPLAY")
    if not ns or not disp:
        return False, "nvidia-settings / X display unavailable"
    try:
        r = subprocess.run(
            [ns, "-c", disp,
             "-a", f"[gpu:0]/GPUGraphicsClockOffsetAllPerformanceLevels={int(core_mhz)}",
             "-a", f"[gpu:0]/GPUMemoryTransferRateOffsetAllPerformanceLevels={int(mem_mhz)}"],
            capture_output=True, text=True, timeout=12)
        blob = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 and "ERROR" not in blob:
            return True, f"core {core_mhz:+d} / mem {mem_mhz:+d} MHz"
        return False, (r.stderr or r.stdout or "offset apply failed").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def snapshot_light() -> dict:
    """A cheap point-in-time reading for before/after deltas (preset apply,
    profile apply). Every field may be None. No sudo, ~one syscall each plus
    an optional nvidia-smi / ryzenadj hop."""
    cpu_t = read_cpu_temp_c_value()
    dclk, dtemp, dutil, dpow = read_dgpu_values()
    ra = read_ryzenadj_info() or {}
    fans = read_fans() or []
    return {
        "cpu_temp_c": cpu_t,
        "cpu_freq_ghz": read_cpu_freq_ghz_value(),
        "stapm_w": ra.get("stapm_limit_value") or ra.get("stapm_limit"),
        "dgpu_temp_c": dtemp,
        "dgpu_clock_mhz": dclk,
        "dgpu_power_w": dpow,
        "fan_rpm": [f.get("rpm") for f in fans],
    }
