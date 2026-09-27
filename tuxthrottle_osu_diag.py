#!/usr/bin/env python3
"""osu!lazer latency / performance diagnostics — a live TUI (Textual) plus a plain-text mode.

Reads everything unprivileged: the running osu! process (threads, scheduling, its environment),
PipeWire (graph quantum, osu!'s stream, xruns), osu!'s framework.ini / game.ini, the launcher
config, CPU / GPU / power state, display + KWin, kernel command line, realtime limits, IRQ
threads and USB mouse polling. Every row gets ok / warn / bad plus what to change.

    tuxthrottle_osu_diag.py            live TUI (r refresh, s save report, q quit)
    tuxthrottle_osu_diag.py --once     print one report and exit
    tuxthrottle_osu_diag.py --json     same, as JSON
    osu-lazer-launcher diag            same as the first line
"""
from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

HOME = Path.home()
OSU_DIR = HOME / ".local" / "share" / "osu"
LAUNCHER_CONF = HOME / ".config" / "osu-lazer-launcher" / "config"
REPORT_DIR = HOME / ".cache" / "osu-lazer-launcher"

OK, WARN, BAD, INFO = "ok", "warn", "bad", "info"


@dataclass
class Check:
    section: str
    name: str
    value: str
    status: str = INFO
    hint: str = ""


# ---------------------------------------------------------------- helpers
def run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


def parse_ini(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith(("#", ";", "[")):
            out[key.strip()] = value.split("#", 1)[0].strip().strip('"')
    return out


def osu_data_dir() -> Path:
    storage = parse_ini(read(OSU_DIR / "storage.ini"))
    return Path(storage["FullPath"]) if storage.get("FullPath") else OSU_DIR


def find_osu_pid() -> int | None:
    for d in Path("/proc").iterdir():
        if d.name.isdigit() and read(d / "comm") == "osu!":
            return int(d.name)
    return None


def proc_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.decode(errors="replace").partition("=")
        if sep:
            env[k] = v
    return env


def parse_pw_top(text: str) -> dict[str, dict]:
    """Last snapshot of `pw-top -b` → {node name: {quant, rate, err, busy_ratio, role}}."""
    rows: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or parts[0] not in ("R", "S", "C") or not parts[1].isdigit():
            continue
        name = parts[-1]
        try:
            quant, rate, err = int(parts[2]), int(parts[3]), int(parts[8])
        except ValueError:
            continue
        bq = parts[7].replace(",", ".")
        rows[name] = {"quant": quant, "rate": rate, "err": err, "state": parts[0],
                      "busy_ratio": float(bq) if re.fullmatch(r"[0-9.]+", bq) else None,
                      "follower": "+" in parts[-2:-1] or " + " in line}
    return rows


def parse_pipewire_alsa(value: str) -> dict[str, int]:
    return {k: int(v) for k, v in re.findall(r"alsa\.([a-z-]+)=(\d+)", value or "")}


def cmdline_flags(cmdline: str) -> dict[str, str]:
    flags = {}
    for tok in cmdline.split():
        k, _, v = tok.partition("=")
        flags[k] = v
    return flags


def ms(frames: float, rate: float) -> float:
    return frames * 1000.0 / rate if rate else 0.0


def thread_load(pid: int, window: float = 1.0) -> dict[str, tuple[str, float, float, float]]:
    """osu!'s main threads over `window` s: (policy, CPU ms/s, involuntary switches/s, voluntary/s).
    Threads sharing a name (osu! has several "Audio" threads) are summed."""
    def snap() -> dict[str, tuple[str, int, int, int]]:
        d = {}
        for t in Path(f"/proc/{pid}/task").iterdir():
            try:
                comm = read(t / "comm")
                st = read(t / "status")
                vol = int(re.search(r"^voluntary_ctxt_switches:\s+(\d+)", st, re.M).group(1))
                inv = int(re.search(r"^nonvoluntary_ctxt_switches:\s+(\d+)", st, re.M).group(1))
                d[t.name] = (comm.split(" (")[0].strip(), vol, inv, int(read(t / "schedstat").split()[0]))
            except (OSError, AttributeError, ValueError, IndexError):
                pass
        return d
    try:
        a = snap()
        t0 = time.monotonic()
        time.sleep(window)
        b = snap()
    except OSError:
        return {}
    dt = time.monotonic() - t0
    pols = {}
    for line in run(["ps", "-L", "-o", "tid=,cls=,rtprio=,ni=", "-p", str(pid)]).splitlines():
        f = line.split()
        if len(f) == 4:
            pols[f[0]] = f"{f[1]}{'' if f[2] == '-' else ' ' + f[2]}, nice {f[3]}"
    agg: dict[str, list] = {}
    for tid, (name, vol, inv, ns) in b.items():
        if name not in ("Draw", "Update", "Input", "Audio") or tid not in a:
            continue
        g = agg.setdefault(name, [pols.get(tid, "?"), 0.0, 0.0, 0.0])
        g[1] += (ns - a[tid][3]) / 1e6 / dt
        g[2] += (inv - a[tid][2]) / dt
        g[3] += (vol - a[tid][1]) / dt
    order = ("Input", "Audio", "Update", "Draw")
    return {k: tuple(agg[k]) for k in order if k in agg}


# ---------------------------------------------------------------- collectors
def check_osu(pid: int | None, env: dict[str, str]) -> list[Check]:
    s = "osu! process"
    if pid is None:
        return [Check(s, "running", "no", WARN,
                      "Start osu! (Games menu / osu-lazer-launcher) — live checks need it running.")]
    out = [Check(s, "running", f"pid {pid}", OK)]
    via = "PIPEWIRE_LATENCY" in env or "DISABLE_MANGOHUD" in env or "__GL_MaxFramesAllowed" in env
    out.append(Check(s, "started by osu-lazer-launcher", "yes" if via else "no",
                     OK if via else BAD,
                     "" if via else "Launch through osu-lazer-launcher so the latency env is applied."))
    pre = env.get("LD_PRELOAD", "")
    mh = "mangohud" in pre.lower() or env.get("MANGOHUD") == "1" and env.get("DISABLE_MANGOHUD") != "1"
    out.append(Check(s, "MangoHud injected", "yes" if mh else "no", BAD if mh else OK,
                     "SHOW_MANGOHUD=no in the launcher config." if mh else ""))
    out.append(Check(s, "GameMode", "yes" if "gamemode" in pre else "no",
                     OK if "gamemode" in pre else WARN, "" if "gamemode" in pre else "Run via gamemoderun."))
    for name, (pol, cpu_ms, preempt, sleeps) in thread_load(pid).items():
        busy = cpu_ms / 10  # ms of CPU per second -> % of one core
        out.append(Check(s, f"{name} thread", f"{busy:.0f}% of a core, {preempt:.0f} preemptions/s, "
                         f"{sleeps:.0f} sleeps/s, {pol}",
                         WARN if preempt > 500 else INFO,
                         "Often kicked off its CPU: close background load or raise osu!'s priority."
                         if preempt > 500 else ""))
    out.append(Check(s, "exact Input/Audio/Update/Draw fps", "in-game only (Ctrl+F11)", INFO,
                     "osu! doesn't export its frame counters; the thread rows above are measured from /proc."))
    return out


def check_gpu(pid: int | None, env: dict[str, str]) -> list[Check]:
    s = "GPU"
    out: list[Check] = []
    prime = env.get("__NV_PRIME_RENDER_OFFLOAD") == "1"
    if shutil.which("nvidia-smi"):
        on_nv = pid is not None and str(pid) in run(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"]) + run(["nvidia-smi"])
        where = "NVIDIA (PRIME offload)" if on_nv else ("AMD iGPU" if pid else "-")
        out.append(Check(s, "osu! renders on", where, INFO,
                         "The laptop panel is wired to the iGPU: NVIDIA frames are copied across (PRIME). "
                         "USE_DGPU=no skips that copy." if on_nv else ""))
        q = run(["nvidia-smi", "--query-gpu=pstate,utilization.gpu,clocks.gr,clocks.max.gr,power.draw,"
                 "temperature.gpu,clocks_throttle_reasons.active", "--format=csv,noheader,nounits"])
        f = [x.strip() for x in q.split(",")]
        if len(f) >= 7:
            ps_, util, clk, mx, pw, temp, thr = f[:7]
            out.append(Check(s, "NVIDIA state", f"{ps_}, {util}% busy, {clk}/{mx} MHz, {pw} W, {temp} °C",
                             WARN if on_nv and ps_ not in ("P0", "P1", "P2") else INFO))
            reasons = int(thr, 16) if thr.startswith("0x") else 0
            # 0x1 = idle, 0x4 = app clocks; anything else while playing = throttled
            bad = reasons & ~0x1 & ~0x4 if on_nv else 0
            out.append(Check(s, "NVIDIA clock throttling", thr, WARN if bad else OK,
                             "Power/thermal limit active: check cooling / Game Mode fan boost." if bad else ""))
    elif prime:
        out.append(Check(s, "osu! renders on", "PRIME requested, nvidia-smi missing", WARN))
    return out


def check_osu_config() -> list[Check]:
    s = "osu! settings"
    d = osu_data_dir()
    fw = parse_ini(read(d / "framework.ini"))
    game = parse_ini(read(d / "game.ini"))
    if not fw:
        return [Check(s, "framework.ini", f"not found in {d}", WARN, "Start osu! once.")]
    out = []
    fs = fw.get("FrameSync", "?")
    out.append(Check(s, "Frame limiter", fs, OK if fs in ("Unlimited", "Limit8x", "Limit4x") else BAD,
                     "" if fs != "VSync" else "VSync adds 1-2 frames of latency: use Unlimited (1000 fps cap)."))
    wm = fw.get("WindowMode", "?")
    out.append(Check(s, "Window mode", wm, OK if wm == "Fullscreen" else WARN,
                     "" if wm == "Fullscreen" else "Fullscreen lets KWin tear / scan out directly."))
    em = fw.get("ExecutionMode", "?")
    out.append(Check(s, "Execution mode", em, OK if em == "MultiThreaded" else WARN,
                     "" if em == "MultiThreaded" else "MultiThreaded keeps input/audio off the draw thread."))
    out.append(Check(s, "Renderer", fw.get("Renderer", "?"), INFO))
    out.append(Check(s, "Audio device", fw.get("AudioDevice") or "Default", INFO,
                     "Default and \"PipeWire\" both go through pipewire-alsa (PIPEWIRE_ALSA applies)."))
    off = game.get("AudioOffset")
    if off is not None:
        out.append(Check(s, "Audio offset", f"{off} ms", INFO,
                         "Re-run the offset calibration after any audio latency change."))
    for key, want in (("HitLighting", "False"), ("BlurLevel", "0")):
        if key in game:
            v = game[key]
            good = v.lower() == want.lower() or (want == "0" and v in ("0", "0.0"))
            out.append(Check(s, key, v, OK if good else WARN, "" if good else f"{want} saves draw time."))
    return out


def check_audio(pid: int | None, env: dict[str, str], pwtop: dict[str, dict]) -> list[Check]:
    s = "Audio"
    out: list[Check] = []
    meta = run(["pw-metadata", "-n", "settings"])
    get = lambda k: (re.search(rf"key:'{re.escape(k)}' value:'([^']*)'", meta) or [None, ""])[1]  # noqa: E731
    rate = int(get("clock.rate") or 48000)
    out.append(Check(s, "Graph rate / default quantum",
                     f"{rate} Hz / {get('clock.quantum') or '?'} (min {get('clock.min-quantum') or '?'}, "
                     f"force {get('clock.force-quantum') or '0'})", INFO))
    osu_node = next((v for k, v in pwtop.items() if "osu" in k.lower()), None)
    driver = next((v for v in pwtop.values() if v["state"] == "R" and not v["follower"]), None)
    if osu_node:
        q = osu_node["quant"] or (driver or {}).get("quant", 0)
        r = osu_node["rate"] or rate
        out.append(Check(s, "Live quantum (osu! stream)", f"{q} @ {r} Hz = {ms(q, r):.1f} ms",
                         OK if 0 < q <= 256 else (WARN if q <= 512 else BAD),
                         "" if 0 < q <= 256 else "Launch via the launcher (PIPEWIRE_LATENCY) or lower AUDIO_QUANTUM."))
        e = osu_node["err"]  # counted since the stream opened; one at start-up is normal
        out.append(Check(s, "xruns (osu! stream)", str(e), OK if e <= 1 else WARN,
                         "" if e <= 1 else "Dropouts: raise AUDIO_QUANTUM / ALSA_PERIOD_FRAMES."))
    elif pid:
        out.append(Check(s, "osu! stream", "not playing (menu silent?)", INFO))
    if driver:
        bq = driver.get("busy_ratio")
        out.append(Check(s, "xruns (output device)", str(driver["err"]), OK if driver["err"] <= 1 else WARN))
        if bq is not None:
            out.append(Check(s, "Driver busy ratio", f"{bq:.2f}", OK if bq < 0.6 else WARN,
                             "" if bq < 0.6 else "Little headroom: a smaller quantum will crackle."))
    pa = parse_pipewire_alsa(env.get("PIPEWIRE_ALSA", ""))
    if pa:
        frames = pa.get("buffer-bytes", 0) // 8
        out.append(Check(s, "pipewire-alsa buffer (osu!)",
                         f"{frames} frames = {ms(frames, rate):.1f} ms (period {pa.get('period-bytes', 0) // 8})", OK))
    elif pid:
        out.append(Check(s, "pipewire-alsa buffer (osu!)", "default (up to ~20 ms)", WARN,
                         "PW_ALSA_TUNE=yes in the launcher config."))
    if osu_node:
        q = osu_node["quant"] or (driver or {}).get("quant", 0)
        est = ms(q, rate) * 2 + (ms(pa.get("buffer-bytes", 0) // 8, rate) if pa else 20)
        out.append(Check(s, "Estimated output latency", f"~{est:.1f} ms (stream + device quantum + ALSA buffer)",
                         OK if est < 20 else WARN,
                         "" if est < 20 else "Lower AUDIO_QUANTUM / ALSA_PERIOD_FRAMES until it crackles, then step back."))
    loops = run(["ps", "-eLo", "cls=,rtprio=,comm=", "-u", str(os.getuid())])
    prios = sorted({ln.split()[1] for ln in loops.splitlines() if ln.split()[-1].startswith("data-loop")})
    out.append(Check(s, "PipeWire data-loop priority", "RR " + ",".join(prios) if prios else "not RT",
                     OK if prios else BAD, "" if prios else "rtkit-daemon isn't granting realtime."))
    return out


def check_cpu_power() -> list[Check]:
    s = "CPU / power"
    out = []
    prof = run(["powerprofilesctl", "get"]).strip()
    out.append(Check(s, "Power profile", prof or "?", OK if prof == "performance" else WARN,
                     "" if prof == "performance" else "The launcher / GameMode switch this while playing."))
    cpu0 = "/sys/devices/system/cpu/cpu0/cpufreq"
    out.append(Check(s, "Driver / governor / EPP",
                     f"{read(cpu0 + '/scaling_driver')} / {read(cpu0 + '/scaling_governor')} / "
                     f"{read(cpu0 + '/energy_performance_preference', '-')}", INFO))
    freqs = [int(read(p, "0")) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq")]
    if freqs:
        out.append(Check(s, "Core clocks", f"avg {sum(freqs) / len(freqs) / 1000:.0f} MHz, "
                         f"max {max(freqs) / 1000:.0f} MHz", INFO))
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        if read(hw / "name") == "k10temp":
            t = int(read(hw / "temp1_input", "0")) / 1000
            out.append(Check(s, "CPU temperature (Tctl)", f"{t:.0f} °C", OK if t < 90 else WARN,
                             "" if t < 90 else "Near the 95 °C limit: boost clocks will drop."))
    gm = run(["gamemoded", "-s"]).strip()
    if gm:
        out.append(Check(s, "GameMode", gm, OK if "active" in gm and "inactive" not in gm else INFO))
    return out


def check_display() -> list[Check]:
    s = "Display"
    out = []
    raw = run(["kscreen-doctor", "-j"])
    try:
        outputs = json.loads(raw).get("outputs", [])
    except (json.JSONDecodeError, AttributeError):
        outputs = []
    for o in outputs:
        if not o.get("enabled"):
            continue
        mode = next((m for m in o.get("modes", []) if m.get("id") == o.get("currentModeId")), {})
        hz = float(mode.get("refreshRate", 0))
        out.append(Check(s, f"{o.get('name')} refresh", f"{hz:.0f} Hz",
                         OK if hz >= 120 else WARN, "" if hz >= 120 else "Pick the highest refresh mode."))
        vrr = {0: "never", 1: "always", 2: "automatic"}.get(o.get("vrrPolicy"), str(o.get("vrrPolicy")))
        out.append(Check(s, f"{o.get('name')} VRR", vrr, INFO))
    tear = run(["kreadconfig6", "--file", "kwinrc", "--group", "Compositing", "--key", "AllowTearing"]).strip()
    out.append(Check(s, "KWin AllowTearing", tear or "unset", OK if tear == "true" else WARN,
                     "" if tear == "true" else "Enable it (TuxThrottle KDE tweak / osu! card step 4)."))
    return out


def check_kernel() -> list[Check]:
    s = "Kernel / scheduling"
    f = cmdline_flags(read("/proc/cmdline"))
    out = [
        Check(s, "threadirqs", "on" if "threadirqs" in f else "off", OK if "threadirqs" in f else WARN,
              "" if "threadirqs" in f else "Lets IRQ threads be prioritised."),
        Check(s, "preempt", f.get("preempt", "kernel default"), OK if f.get("preempt") == "full" else INFO),
        Check(s, "mitigations", f.get("mitigations", "on"), OK if f.get("mitigations") == "off" else INFO),
        Check(s, "C-state limit", f.get("processor.max_cstate", "none"), INFO),
        Check(s, "clocksource", read("/sys/devices/system/clocksource/clocksource0/current_clocksource"),
              OK if read("/sys/devices/system/clocksource/clocksource0/current_clocksource") == "tsc" else WARN),
    ]
    rt_rt = int(read("/proc/sys/kernel/sched_rt_runtime_us", "0") or 0)
    rt_p = int(read("/proc/sys/kernel/sched_rt_period_us", "1") or 1)
    uncapped = rt_rt < 0 or rt_rt >= rt_p
    out.append(Check(s, "RT throttling", "off (RT can take 100 % of a CPU)" if uncapped else f"{rt_rt / rt_p:.0%} cap",
                     WARN if uncapped else OK,
                     "Keep 950000 if you ever run osu! realtime." if uncapped else ""))
    soft, _hard = resource.getrlimit(resource.RLIMIT_RTPRIO)
    out.append(Check(s, "Your realtime priority limit", str(soft), INFO if soft == 0 else OK,
                     "0 = osu! can't be made SCHED_RR (limits.d rtprio needed)." if soft == 0 else ""))
    irqs = run(["ps", "-eLo", "cls=,rtprio=,comm="])
    for kind in ("snd", "xhci"):
        pr = sorted({ln.split()[1] for ln in irqs.splitlines()
                     if re.search(rf"irq/\d+-{kind}", ln.split()[-1] if ln.split() else "")})
        if pr:
            out.append(Check(s, f"{kind} IRQ thread priority", "FIFO " + ",".join(pr), INFO,
                             "Equal to every other IRQ (50): rtirq-style boost would favour audio/mouse."
                             if pr == ["50"] else ""))
    return out


def check_input() -> list[Check]:
    s = "Input"
    out = []
    mp = read("/sys/module/usbhid/parameters/mousepoll", "?")
    out.append(Check(s, "usbhid.mousepoll", mp, OK if mp == "1" else INFO,
                     "" if mp == "1" else "1 forces 1000 Hz on mice that ask for less."))
    for link in Path("/sys/bus/usb/devices").glob("*:*"):
        itf = link.resolve()
        if read(itf / "bInterfaceClass") != "03" or read(itf / "bInterfaceProtocol") != "02":
            continue
        dev = itf.parent
        name = read(dev / "product") or dev.name
        for ep in itf.glob("ep_8*"):
            iv = read(ep / "interval")
            fast = iv in ("1ms", "125us", "250us", "500us")
            out.append(Check(s, f"{name} polling", iv, OK if fast else WARN,
                             "" if fast else "Replug / reboot after usbhid.mousepoll=1, or set 1000 Hz in the mouse."))
        pc = read(dev / "power" / "control")
        out.append(Check(s, f"{name} autosuspend", "off" if pc == "on" else pc, OK if pc == "on" else WARN,
                         "" if pc == "on" else "USB autosuspend can add wake-up lag."))
    kw = run(["busctl", "--user", "get-property", "org.kde.KWin", "/org/kde/KWin/InputDevice",
              "org.kde.KWin.InputDeviceManager", "devicesSysNames"])
    for dev in re.findall(r'"([^"]+)"', kw):
        path = f"/org/kde/KWin/InputDevice/{dev}"
        def prop(p: str, path: str = path) -> list[str]:
            return run(["busctl", "--user", "get-property", "org.kde.KWin", path,
                        "org.kde.KWin.InputDevice", p]).split()[-1:]
        if prop("pointer") == ["true"] and prop("touchpad") != ["true"]:
            nm = " ".join(run(["busctl", "--user", "get-property", "org.kde.KWin", path,
                               "org.kde.KWin.InputDevice", "name"]).split()[1:]).strip('"')
            flat = prop("pointerAccelerationProfileFlat") == ["true"]
            out.append(Check(s, f"{nm} accel", "flat" if flat else "adaptive", OK if flat else WARN,
                             "" if flat else "The launcher flattens it while osu! runs (FLAT_MOUSE=yes)."))
    return out


def check_launcher() -> list[Check]:
    s = "Launcher config"
    conf = parse_ini(read(LAUNCHER_CONF))
    if not conf:
        return [Check(s, "config", "missing", WARN, "Run: osu-lazer-launcher install")]
    want = {"USE_DGPU": None, "FLAT_MOUSE": "yes", "PERFORMANCE_PROFILE": "yes", "DISABLE_DRIVER_VSYNC": "yes",
            "SHOW_MANGOHUD": "no", "LOW_LATENCY_AUDIO": "yes", "AUDIO_QUANTUM": None, "PW_ALSA_TUNE": "yes",
            "ALSA_PERIOD_FRAMES": None}
    out = []
    for k, good in want.items():
        v = conf.get(k, "(default)")
        st = INFO if good is None else (OK if v == good or v == "(default)" else WARN)
        out.append(Check(s, k, v, st, "" if st != WARN else f"Recommended: {good}"))
    return out


def collect() -> list[Check]:
    pid = find_osu_pid()
    env = proc_env(pid) if pid else {}
    pwtop = parse_pw_top(run(["pw-top", "-b", "-n", "2"], timeout=6)) if shutil.which("pw-top") else {}
    checks: list[Check] = []
    for fn in (lambda: check_osu(pid, env), lambda: check_audio(pid, env, pwtop),
               lambda: check_gpu(pid, env), check_osu_config, check_cpu_power, check_display,
               check_input, check_kernel, check_launcher):
        try:
            checks += fn()
        except Exception as e:  # noqa: BLE001 — one broken probe mustn't hide the rest
            checks.append(Check("diagnostics", getattr(fn, "__name__", "probe"), f"error: {e}", WARN))
    return checks


# ---------------------------------------------------------------- output
MARK = {OK: "✓", WARN: "!", BAD: "✗", INFO: "·"}


def text_report(checks: list[Check]) -> str:
    lines = [f"osu!lazer latency report  {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    section = None
    for c in checks:
        if c.section != section:
            section = c.section
            lines.append(f"\n[{section}]")
        lines.append(f"  {MARK[c.status]} {c.name:<32} {c.value}")
        if c.hint and c.status in (WARN, BAD):
            lines.append(f"      → {c.hint}")
    n = {k: sum(c.status == k for c in checks) for k in (OK, WARN, BAD)}
    lines.append(f"\n{n[OK]} ok, {n[WARN]} warnings, {n[BAD]} problems")
    return "\n".join(lines)


def tui() -> int:
    try:
        from rich.text import Text
        from textual.app import App, ComposeResult
        from textual.widgets import DataTable, Footer, Header, Static
    except ImportError:
        print("textual not found (dnf install python3-textual). Showing the text report instead.\n")
        print(text_report(collect()))
        return 0

    style = {OK: "green", WARN: "yellow", BAD: "bold red", INFO: "dim"}

    class OsuDiag(App):
        TITLE = "osu!lazer latency diagnostics"
        CSS = "#summary { padding: 0 1; height: 2; } DataTable { height: 1fr; }"
        BINDINGS = [("r", "refresh", "Refresh"), ("p", "pause", "Pause/resume"),
                    ("s", "save", "Save report"), ("q", "quit", "Quit")]

        def __init__(self):
            super().__init__()
            self.checks: list[Check] = []
            self.paused = False
            self.busy = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static("collecting…", id="summary")
            yield DataTable(zebra_stripes=True, cursor_type="row")
            yield Footer()

        def on_mount(self) -> None:
            t = self.query_one(DataTable)
            t.add_columns("", "Section", "Check", "Value", "What to do")
            self.action_refresh()
            self.set_interval(3, self._tick)

        def _tick(self) -> None:
            if not self.paused:
                self.action_refresh()

        def action_refresh(self) -> None:
            if not self.busy:
                self.busy = True
                self.run_worker(self._collect, thread=True, exclusive=True)

        def _collect(self) -> None:
            checks = collect()
            self.call_from_thread(self._show, checks)

        def _show(self, checks: list[Check]) -> None:
            self.busy = False
            self.checks = checks
            t = self.query_one(DataTable)
            row = t.cursor_row
            t.clear()
            for c in checks:
                st = style[c.status]
                t.add_row(Text(MARK[c.status], style=st), c.section, c.name, Text(c.value, style=st),
                          Text(c.hint if c.status in (WARN, BAD) else "", style="italic"))
            if checks:
                t.move_cursor(row=min(row, len(checks) - 1))
            n = {k: sum(c.status == k for c in checks) for k in (OK, WARN, BAD)}
            lat = next((c.value for c in checks if c.name == "Estimated output latency"), "osu! not playing")
            self.query_one("#summary", Static).update(Text.assemble(
                (f"✓ {n[OK]}  ", "green"), (f"! {n[WARN]}  ", "yellow"), (f"✗ {n[BAD]}", "bold red"),
                f"    audio: {lat}", ("    (paused)" if self.paused else "    refresh 3 s", "dim")))

        def action_pause(self) -> None:
            self.paused = not self.paused
            self.notify("paused" if self.paused else "resumed")

        def action_save(self) -> None:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = REPORT_DIR / f"diag-{time.strftime('%Y%m%d-%H%M%S')}.txt"
            path.write_text(text_report(self.checks) + "\n")
            self.notify(f"saved {path}")

    OsuDiag().run()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--json" in argv:
        print(json.dumps([asdict(c) for c in collect()], indent=2))
        return 0
    if "--once" in argv or not sys.stdout.isatty():
        print(text_report(collect()))
        return 0
    return tui()


if __name__ == "__main__":
    sys.exit(main())
