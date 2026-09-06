#!/usr/bin/env python3
"""TuxThrottle power daemon — stdlib only, no GUI deps.

Two jobs, one poll loop:

  * Closed-loop **fan curve** — map CPU / GPU temperature through a
    piecewise-linear curve to an *additive* alienware_wmi fan boost
    (`fanN_boost`, the safe lever — it can only add airflow on top of the
    firmware curve, never slow a fan). Hysteresis stops it hunting at a
    breakpoint. On exit it restores the automatic fan control.

  * **AC / battery auto-switch** — when the charger is plugged or pulled,
    apply a saved profile bundle (platform_profile + a ryzenadj TDP preset).

  * **per-game auto-profiles** — apply a named profile while a matched game
    runs, restore afterwards (`GameProfileController`).

  * **thermal-event notifications** — sustained Tjmax, a stalled fan while
    hot, Performance-on-low-battery -> `notify-send` + a structured log line
    (`ThermalWatcher`, config block `thermal_notify`).

  * **control socket** — a stdlib newline-JSON RPC at
    `/run/tuxthrottle/control.sock` (`tuxthrottle_control.ControlServer`) so
    the GUI / `tuxthrottlectl` route writes through the one process that owns
    the hardware; they fall back to direct writes when it isn't up.

Config: ~/.config/tuxthrottle/powerd.json (written by the GUI's Fans /
Power & Limits tabs), re-read live when its mtime changes:

  {
    "poll_s": 2,
    "fan_curve": {
      "enabled": false,
      "sensor": "max",            # "cpu" | "gpu" | "max"
      "hysteresis_c": 3,
      "points": [[45,0],[60,25],[72,55],[82,85],[90,100]]   # [temp_c, boost_%]
    },
    "autoswitch": {
      "enabled": false,
      "on_ac": "Balanced",        # Quiet | Balanced | Performance
      "on_battery": "Quiet",
      "refresh_ac": 0,            # panel Hz on AC (0 = leave alone), KDE only
      "refresh_battery": 0        # panel Hz on battery (0 = leave alone)
    }
  }

Runs as root (systemd unit installed by the FanCurveDaemon tweak). Also
usable by hand:  sudo python3 tuxthrottle_powerd.py once --user bean
"""
import argparse
import glob
import json
import os
import pwd
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402  (sibling module, stdlib-only)
import tuxthrottle_control as control  # noqa: E402  (stdlib-only RPC socket)
import tuxthrottle_profiles as profiles  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "poll_s": 2,
    "fan_curve": {
        "enabled": False,
        "sensor": "max",
        "hysteresis_c": 3,
        "points": [[45, 0], [60, 25], [72, 55], [82, 85], [90, 100]],
    },
    "autoswitch": {"enabled": False, "on_ac": "Balanced", "on_battery": "Quiet",
                   "refresh_ac": 0, "refresh_battery": 0},
    # per-game auto-profiles: when a matched process (or, with "*", any Feral
    # GameMode client) is running, apply a named profile; restore `default`
    # (or roll back the pre-game snapshot when default is null) once it exits.
    "game_profiles": {
        "enabled": False,
        "poll_s": 6,
        "match": {},          # {"gta5.exe": "Gaming", "*": "Performance"}
        "default": None,      # profile name, or null = roll back the pre-game snapshot
    },
    # time-of-day profile schedule. Each rule: {from,to,apply,days}. `apply` is
    # a bundle (Quiet/Balanced/Performance) or a saved profile name; `days` is
    # a list of weekday ints (0=Mon), omitted = every day. Outside every rule,
    # `outside` (bundle/profile) is applied, or nothing when it's null. Yields
    # to a running per-game profile.
    "schedule": {
        "enabled": False,
        "poll_s": 60,
        "rules": [],          # [{"from": "22:00", "to": "07:00", "apply": "Quiet"}]
        "outside": None,      # bundle/profile when no rule matches, or null
    },
    # desktop notifications (best-effort notify-send) + a structured log line
    # for sustained-Tjmax / stalled-fan / performance-on-low-battery events.
    "thermal_notify": {
        "enabled": False,
        "tjmax_c": 95,            # tctl at/above this is "at Tjmax"
        "tjmax_sustain_s": 20,    # ... for this long -> event
        "stalled_fan_hot_c": 70,  # a 0-RPM fan while its sensor is this hot -> event
        "battery_perf_min_pct": 20,   # Performance profile on battery below this -> event
        "cooldown_s": 300,        # min seconds between repeats of the same event
        # G15 5515/5525 firmware bug: the fans sometimes stop spinning
        # entirely even when hot, and only toggling G-Mode (the performance
        # platform_profile) revives them. With this on, a detected stalled-fan
        # event also kicks platform_profile -> performance, holds it, then
        # restores the previous profile once the fans are turning again.
        "stalled_fan_recover": False,
        "stalled_fan_recover_s": 45,   # hold performance at least this long
    },
}

# (STAPM, fast, slow) Watts — mirrors tuxthrottle.py's _TDP_PRESETS (STAPM >= slow
# so the SMU doesn't clamp it). Kept here so the daemon has no GUI dependency.
TDP_PRESETS = {
    "Quiet": (25, 35, 25),
    "Balanced": (42, 54, 42),
    "Performance": (65, 80, 54),
}
PROFILE_FOR_BUNDLE = {"Quiet": "balanced", "Balanced": "balanced",
                      "Performance": "performance"}

_STOP = False


def log(msg: str) -> None:
    print(f"[powerd] {msg}", flush=True)


def _config_path(user: str | None) -> Path:
    if user:
        try:
            home = Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            home = Path.home()
    else:
        home = Path.home()
    return home / ".config" / "tuxthrottle" / "powerd.json"


def _merge(base: dict, over: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        return _merge(DEFAULTS, raw)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))


def interp(points: list, temp: float) -> int:
    """Piecewise-linear temp -> boost%. Clamps outside the point range."""
    pts = sorted((float(t), float(b)) for t, b in points)
    if not pts:
        return 0
    if temp <= pts[0][0]:
        return int(round(pts[0][1]))
    if temp >= pts[-1][0]:
        return int(round(pts[-1][1]))
    for (t0, b0), (t1, b1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            frac = (temp - t0) / (t1 - t0) if t1 != t0 else 0.0
            return int(round(b0 + frac * (b1 - b0)))
    return int(round(pts[-1][1]))


def read_temp(sensor: str) -> float | None:
    cpu = sensors.read_cpu_temp_c_value()
    gpu = None
    _clk, gtemp, _u, _p = sensors.read_dgpu_values()
    if gtemp is not None:
        gpu = float(gtemp)
    if sensor == "cpu":
        return cpu
    if sensor == "gpu":
        return gpu
    vals = [v for v in (cpu, gpu) if v is not None]
    return max(vals) if vals else None


def _ac_online() -> bool | None:
    for p in glob.glob("/sys/class/power_supply/A[CD]*/online"):
        try:
            return bool(int(open(p).read().strip()))
        except (OSError, ValueError):
            continue
    return None


def apply_bundle(name: str) -> None:
    preset = TDP_PRESETS.get(name)
    prof = PROFILE_FOR_BUNDLE.get(name)
    if prof and prof in sensors.platform_profile_choices():
        ok, err = sensors.set_platform_profile(prof)
        log(f"bundle {name}: platform_profile -> {prof}" + ("" if ok else f" FAILED {err}"))
    if preset and sensors.ryzenadj_available():
        s, f, sl = preset
        ok, err = sensors.set_ryzenadj_limits(fast_w=f, slow_w=sl, stapm_w=s)
        log(f"bundle {name}: TDP -> {s}/{f}/{sl} W" + ("" if ok else f" FAILED {err}"))


def _running_procs() -> set:
    """Lower-cased process basenames currently running (comm + argv[0])."""
    names = set()
    for pdir in glob.glob("/proc/[0-9]*"):
        try:
            with open(f"{pdir}/comm") as f:
                names.add(f.read().strip().lower())
        except OSError:
            continue
        try:
            with open(f"{pdir}/cmdline", "rb") as f:
                arg0 = f.read().split(b"\0", 1)[0].decode(errors="ignore")
            if arg0:
                names.add(os.path.basename(arg0).lower())
        except OSError:
            pass
    return names


def _session_path(user) -> Path:
    return _config_path(user).with_name("last_session.json")


def _append_session_history(hp: Path, summary: dict, keep: int = 50) -> None:
    """Append one session summary to `hp` (jsonl), keeping the last `keep`.
    Atomic (temp + os.replace) so a crash mid-write can't truncate history,
    and a transient read error skips the append rather than clobbering it."""
    if hp.exists():
        try:
            old = hp.read_text().splitlines()
        except OSError:
            log(f"  (session history unreadable, skipping append: {hp})")
            return
    else:
        old = []
    old.append(json.dumps(summary, separators=(",", ":")))
    tmp = hp.with_suffix(hp.suffix + ".tmp")
    tmp.write_text("\n".join(old[-keep:]) + "\n")
    os.replace(tmp, hp)


def _chown_user(path: Path, user: str | None) -> None:
    """Hand a root-written file in the user's config dir back to the user."""
    if not user:
        return
    try:
        pw = pwd.getpwnam(user)
        os.chown(path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


class GameProfileController:
    """Applies a profile while a matched game runs, restores it afterwards,
    and records a per-session summary (max temps, avg clocks, throttle time)
    to ~/.config/tuxthrottle/last_session.json when the game exits."""

    def __init__(self, user):
        self._user = user
        self._active_key = None   # the match key we're currently honouring
        self._session: dict | None = None

    def tick(self, cfg: dict) -> None:
        gp = cfg.get("game_profiles", {})
        poll_s = max(3, int(gp.get("poll_s", 6)))
        if not gp.get("enabled") or not gp.get("match"):
            if self._active_key:
                self._leave(gp)
            return
        match = {k.lower(): v for k, v in gp["match"].items()}
        procs = _running_procs()
        hit = None
        for key, prof in match.items():
            if key == "*":
                continue
            if key in procs or key.removesuffix(".exe") in procs:
                hit = (key, prof)
                break
        if hit is None and "*" in match and sensors.gamemode_status().get("active"):
            hit = ("*", match["*"])

        if hit and hit[0] != self._active_key:
            key, prof = hit
            log(f"game '{key}' detected -> profile '{prof}' (snapshot first)")
            profiles.snapshot(self._user, label="pre-game")
            self._apply_profile(prof)
            self._active_key = key
            self._session_start(key, prof)
        elif not hit and self._active_key:
            self._leave(gp)

        if self._active_key and self._session is not None:
            self._session_sample(poll_s)

    def _leave(self, gp: dict) -> None:
        dflt = gp.get("default")
        if dflt:
            log(f"game gone -> default profile '{dflt}'")
            self._apply_profile(dflt)
        else:
            log("game gone -> rolling back the pre-game snapshot")
            profiles.rollback("last", self._user)
        self._session_end()
        self._active_key = None

    # ---- per-session summary ------------------------------------------------ #

    def _session_start(self, key: str, prof: str) -> None:
        self._session = {
            "game": key, "profile": prof, "started": time.time(),
            "n": 0, "cpu_temp_max": 0.0, "gpu_temp_max": 0.0,
            "cpu_clk_sum": 0.0, "cpu_clk_n": 0,
            "gpu_clk_sum": 0.0, "gpu_clk_n": 0,
            "cpu_pow_max": 0.0, "gpu_pow_max": 0.0,
            "throttle_s": 0.0, "sample_s": 0.0,
        }

    def _session_sample(self, poll_s: float) -> None:
        s = self._session
        if s is None:
            return
        s["n"] += 1
        s["sample_s"] += poll_s
        info = sensors.read_ryzenadj_info() or {}
        tctl = info.get("tctl_value")
        if tctl is not None:
            s["cpu_temp_max"] = max(s["cpu_temp_max"], float(tctl))
            if tctl >= 90:                       # near Tjmax -> counts as throttling
                s["throttle_s"] += poll_s
        pw = sensors.read_cpu_power_watts()
        if pw is not None:
            s["cpu_pow_max"] = max(s["cpu_pow_max"], float(pw))
        clk = sensors.read_cpu_freq_ghz_value()
        if clk:
            s["cpu_clk_sum"] += clk
            s["cpu_clk_n"] += 1
        g_clk, g_temp, _g_util, g_pow = sensors.read_dgpu_values()
        if g_temp is not None:
            s["gpu_temp_max"] = max(s["gpu_temp_max"], float(g_temp))
        if g_clk:
            s["gpu_clk_sum"] += float(g_clk)
            s["gpu_clk_n"] += 1
        if g_pow is not None:
            s["gpu_pow_max"] = max(s["gpu_pow_max"], float(g_pow))

    def _session_end(self) -> None:
        s = self._session
        self._session = None
        if not s or s["n"] < 2:
            return
        dur = max(1.0, time.time() - s["started"])
        summary = {
            "game": s["game"], "profile": s["profile"],
            "started": s["started"], "ended": time.time(),
            "duration_s": round(dur),
            "cpu_temp_max_c": round(s["cpu_temp_max"]),
            "gpu_temp_max_c": round(s["gpu_temp_max"]),
            "cpu_clock_avg_ghz": round(s["cpu_clk_sum"] / s["cpu_clk_n"], 2) if s["cpu_clk_n"] else None,
            "gpu_clock_avg_mhz": round(s["gpu_clk_sum"] / s["gpu_clk_n"]) if s["gpu_clk_n"] else None,
            "cpu_power_max_w": round(s["cpu_pow_max"]) if s["cpu_pow_max"] else None,
            "gpu_power_max_w": round(s["gpu_pow_max"]) if s["gpu_pow_max"] else None,
            "throttle_s": round(s["throttle_s"]),
            "throttle_pct": round(100 * s["throttle_s"] / max(1.0, s["sample_s"])),
        }
        try:
            p = _session_path(self._user)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(summary, indent=2))
            _chown_user(p, self._user)
            # also append to a rolling history the GUI's Session-history view
            # reads (keep the last 50)
            hp = p.with_name("sessions.jsonl")
            _append_session_history(hp, summary)
            _chown_user(hp, self._user)
            log(f"session summary: {summary['game']} {summary['duration_s']}s, "
                f"CPU max {summary['cpu_temp_max_c']}°C, throttled "
                f"{summary['throttle_pct']}% -> {p.name} (+{hp.name})")
        except OSError as exc:
            log(f"  (session summary write failed: {exc})")

    def _apply_profile(self, name: str) -> None:
        st = profiles.load_profile(name, self._user)
        if not st:
            log(f"  (no such profile '{name}' — skipped)")
            return
        for r in profiles.apply_state(st, self._user):
            if not r["ok"]:
                log(f"  {r['key']}: FAILED {r['msg']}")

    @property
    def active(self) -> bool:
        return self._active_key is not None


def _hhmm_to_min(s) -> "int | None":
    try:
        h, m = str(s).split(":")
        return (int(h) % 24) * 60 + int(m) % 60
    except (ValueError, AttributeError):
        return None


def _apply_bundle_or_profile(name: str, user) -> None:
    if name in TDP_PRESETS:
        apply_bundle(name)
        return
    st = profiles.load_profile(name, user)
    if not st:
        log(f"  (no such profile '{name}' — skipped)")
        return
    for r in profiles.apply_state(st, user):
        if not r["ok"]:
            log(f"  {r['key']}: FAILED {r['msg']}")


class ScheduleController:
    """Time-of-day profile schedule (`schedule` config block). Applies a
    bundle/profile per matching rule, or `schedule.outside` when no rule is
    active. Only acts on a *change* of target, and yields while a per-game
    profile is in effect."""

    def __init__(self, user):
        self._user = user
        self._last = None          # last target we applied
        self._last_check = 0.0

    def tick(self, cfg: dict, game_active: bool) -> None:
        sc = cfg.get("schedule", {})
        if not sc.get("enabled"):
            self._last = None
            return
        poll_s = max(15, int(sc.get("poll_s", 60)))
        now = time.monotonic()
        if self._last is not None and now - self._last_check < poll_s:
            return
        self._last_check = now
        if game_active:
            return

        lt = time.localtime()
        cur = lt.tm_hour * 60 + lt.tm_min
        target = sc.get("outside")
        for rule in sc.get("rules", []) or []:
            days = rule.get("days")
            if days and lt.tm_wday not in days:
                continue
            a, b = _hhmm_to_min(rule.get("from")), _hhmm_to_min(rule.get("to"))
            if a is None or b is None:
                continue
            within = (a <= cur < b) if a <= b else (cur >= a or cur < b)
            if within:
                target = rule.get("apply")
                break

        if not target or target == self._last:
            return
        log(f"schedule -> apply '{target}'")
        _apply_bundle_or_profile(target, self._user)
        self._last = target


class FanController:
    def __init__(self):
        self._applied = None      # last boost% we wrote
        self._active = False      # are we currently overriding the fans?

    def tick(self, cfg: dict) -> None:
        fc = cfg["fan_curve"]
        if not fc.get("enabled"):
            if self._active:
                sensors.restore_fan_auto()
                log("fan curve disabled -> restored automatic control")
                self._active, self._applied = False, None
            return
        temp = read_temp(fc.get("sensor", "max"))
        if temp is None:
            return
        pts = fc.get("points") or DEFAULTS["fan_curve"]["points"]
        hys = float(fc.get("hysteresis_c", 3))
        target = interp(pts, temp)
        if self._applied is None:
            self._commit(target, temp)
            return
        if target >= self._applied:
            if target != self._applied:
                self._commit(target, temp)                 # ramp up immediately
        elif interp(pts, temp + hys) < self._applied:
            self._commit(target, temp)                     # cool down past hysteresis

    def _commit(self, boost_pct: int, temp: float) -> None:
        raw = max(0, min(255, round(boost_pct * 255 / 100)))
        for i in (1, 2):
            sensors.set_fan_boost(i, raw)
        self._applied, self._active = boost_pct, True
        log(f"temp {temp:.0f}C -> fan boost {boost_pct}%")

    def restore(self) -> None:
        if self._active:
            sensors.restore_fan_auto()
            log("exiting -> restored automatic fan control")


class ThermalWatcher:
    """Best-effort thermal-safety notifications. Fires at most once per
    `cooldown_s` per event kind; every event is also a structured log line so
    it shows up in `journalctl -u tuxthrottled` even with no notify daemon."""

    def __init__(self, user):
        self._user = user
        self._hot_since: float | None = None   # first sample at/above Tjmax
        self._last_fired: dict[str, float] = {}
        self._recover_prev: str | None = None  # profile to restore after a fan-stall kick
        self._recover_until: float = 0.0

    def _fire(self, kind: str, cd: float, summary: str, body: str) -> None:
        now = time.monotonic()
        if now - self._last_fired.get(kind, -1e9) < cd:
            return
        self._last_fired[kind] = now
        log(f"THERMAL-EVENT {kind}: {summary} — {body}")
        try:
            sensors.notify(summary, body)
        except Exception as exc:  # noqa: BLE001
            log(f"  (notify failed: {exc})")

    def tick(self, cfg: dict) -> None:
        tn = cfg.get("thermal_notify", {})
        if not tn.get("enabled"):
            self._hot_since = None
            return
        cd = float(tn.get("cooldown_s", 300))

        info = sensors.read_ryzenadj_info() or {}
        tctl = info.get("tctl_value")
        tjmax = float(tn.get("tjmax_c", 95))
        if tctl is not None and tctl >= tjmax:
            if self._hot_since is None:
                self._hot_since = time.monotonic()
            if time.monotonic() - self._hot_since >= float(tn.get("tjmax_sustain_s", 20)):
                self._fire("tjmax", cd, "CPU at thermal limit",
                           f"Tctl {tctl:.0f} °C ≥ {tjmax:.0f} °C sustained — "
                           f"clocks are being throttled.")
        else:
            self._hot_since = None

        fans = sensors.read_fans()
        hot = read_temp("max")
        stalled = [f for f in fans if (f.get("rpm") or 0) == 0]
        stalled_hot = bool(stalled) and hot is not None \
            and hot >= float(tn.get("stalled_fan_hot_c", 70))
        if stalled_hot:
            names = ", ".join(f.get("label", f"fan{f['index']}") for f in stalled)
            self._fire("stalled_fan", cd, "Fan not spinning while hot",
                       f"{names} at 0 RPM with a sensor at {hot:.0f} °C.")
            if tn.get("stalled_fan_recover"):
                self._kick_fan_recover(names, float(tn.get("stalled_fan_recover_s", 45)))
        elif self._recover_prev is not None and time.monotonic() >= self._recover_until:
            self._end_fan_recover()

        ac = _ac_online()
        if ac is False and self._recover_prev is None:
            bat = sensors.battery_charge_limit_info()
            cap = bat.get("capacity")
            prof = (sensors.get_platform_profile() or "").lower()
            floor = float(tn.get("battery_perf_min_pct", 20))
            if prof in ("performance", "custom") and cap is not None and cap < floor:
                self._fire("battery_perf", cd, "Performance profile on low battery",
                           f"{prof} profile active on battery at {cap}% — "
                           f"consider Balanced/Quiet.")

    def _kick_fan_recover(self, names: str, hold_s: float) -> None:
        """G15 firmware fan-stall workaround: force the performance
        platform_profile (G-Mode) — the only thing that reliably restarts a
        stalled fan — and hold it for at least `hold_s`."""
        now = time.monotonic()
        if self._recover_prev is None:
            prev = (sensors.get_platform_profile() or "balanced").lower()
            self._recover_prev = prev
            if prev != "performance":
                ok, err = sensors.set_platform_profile("performance")
                log(f"THERMAL-EVENT stalled_fan_recover: {names} stalled -> "
                    f"platform_profile performance (was {prev})"
                    + ("" if ok else f" FAILED {err}"))
            else:
                log(f"THERMAL-EVENT stalled_fan_recover: {names} stalled, "
                    f"already in performance — holding")
        self._recover_until = now + max(10.0, hold_s)

    def _end_fan_recover(self) -> None:
        prev = self._recover_prev
        self._recover_prev = None
        self._recover_until = 0.0
        if prev and prev != "performance":
            ok, err = sensors.set_platform_profile(prev)
            log(f"THERMAL-EVENT stalled_fan_recover: fans turning again -> "
                f"restored platform_profile {prev}" + ("" if ok else f" FAILED {err}"))


def _build_dispatch(user, reload_flag: list):
    """Return {method: handler} for the control socket. Handlers raise on
    error; the server turns that into {'ok': false, 'error': ...}."""

    def _status(_p):
        ig_clk, ig_t = sensors.read_igpu_clock_temp_values()
        dg_clk, dg_t, dg_u, dg_p = sensors.read_dgpu_values()
        return {
            "daemon": "tuxthrottled",
            "platform_profile": sensors.get_platform_profile(),
            "cpu": {"temp_c": sensors.read_cpu_temp_c_value(),
                    "power_w": sensors.read_cpu_power_watts()},
            "igpu": {"clock_mhz": ig_clk, "temp_c": ig_t},
            "dgpu": {"clock_mhz": dg_clk, "temp_c": dg_t,
                     "util_pct": dg_u, "power_w": dg_p},
            "tdp": sensors.read_ryzenadj_info(),
            "fans": sensors.read_fans(),
            "battery": sensors.battery_charge_limit_info(),
            "ac_online": _ac_online(),
        }

    def _reload(_p):
        reload_flag[0] = True
        return {"reload": "queued"}

    def _apply_profile(p):
        name = str(p.get("name") or "")
        st = profiles.load_profile(name, user)
        if not st:
            raise ValueError(f"no such profile: {name}")
        profiles.snapshot(user, label=f"pre-apply-{name}")
        rows = profiles.apply_state(st, user, with_gpu_mode=bool(p.get("with_gpu_mode")))
        return {"name": name, "results": rows}

    def _snapshot(p):
        return {"path": str(profiles.snapshot(user, label=str(p.get("label") or "manual")))}

    def _rollback(p):
        return {"results": profiles.rollback(str(p.get("target") or "last"), user)}

    def _set(p):
        target = str(p.get("target") or "")
        if target == "power-profile":
            ok, err = sensors.set_platform_profile(str(p["value"]))
        elif target == "tdp":
            ok, err = sensors.set_ryzenadj_limits(
                fast_w=p.get("fast"), slow_w=p.get("slow"), stapm_w=p.get("stapm"))
        elif target == "battery":
            ok, err = sensors.set_battery_charge_limit(int(p["value"]))
        elif target == "nvpl":
            ok, err = sensors.set_nvidia_power_limit(int(p["value"]))
        elif target == "fan-boost":
            raw = max(0, min(255, round(int(p["percent"]) * 255 / 100)))
            idxs = (1, 2) if str(p.get("which", "both")) == "both" else (int(p["which"]),)
            errs = [e for i in idxs for o, e in [sensors.set_fan_boost(i, raw)] if not o]
            ok, err = (not errs), "; ".join(errs)
        elif target == "gpumode":
            ok, err = sensors.gpu_mode_set(str(p["value"]))
        elif target == "refresh":
            ok, err = sensors.set_panel_refresh(int(p["value"]))
        elif target == "gpu-clock":
            val = str(p.get("value", "")).lower()
            if val in ("reset", "unlock", "off", "0", ""):
                ok, err = sensors.reset_nvidia_clocks()
                profiles.write_config("nvclk.json", None, user)
            else:
                info = sensors.nvidia_clock_info() or {}
                hi = int(p["value"])
                lo = int(p.get("min") or info.get("gr_min") or 210)
                ok, err = sensors.set_nvidia_clock_lock(lo, hi)
                if ok:
                    profiles.write_config("nvclk.json",
                                          {"gr_min": lo, "gr_max": hi}, user)
        else:
            raise ValueError(f"unknown set target: {target}")
        if not ok:
            raise RuntimeError(err or "failed")
        return {"target": target, "ok": True}

    return {
        "status": _status, "reload": _reload, "apply_profile": _apply_profile,
        "snapshot": _snapshot, "rollback": _rollback, "set": _set,
    }


def _handle_stop(signum, _frame):
    global _STOP
    _STOP = True
    log(f"signal {signum} -> stopping")


def run(cfg_path: Path, user=None, once: bool = False) -> int:
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _handle_stop)
    fans = FanController()
    games = GameProfileController(user)
    thermal = ThermalWatcher(user)
    sched = ScheduleController(user)
    last_mtime = -1.0
    cfg = load_config(cfg_path)
    last_ac = None
    last_game_check = 0.0
    reload_flag = [False]   # set true by the control socket's "reload" method

    srv = None
    if not once and os.geteuid() == 0:
        try:
            srv = control.ControlServer()
            for name, fn in _build_dispatch(user, reload_flag).items():
                srv.register(name, fn)
            srv.start()
            log(f"control socket -> {srv.path}")
        except OSError as exc:
            log(f"control socket unavailable: {exc}")
            srv = None

    log(f"start; config {cfg_path} ({'exists' if cfg_path.exists() else 'defaults'})")
    try:
        while not _STOP:
            try:
                m = cfg_path.stat().st_mtime
            except OSError:
                m = 0.0
            if reload_flag[0]:
                reload_flag[0] = False
                last_mtime = -1.0
            if m != last_mtime:
                cfg = load_config(cfg_path)
                last_mtime = m
                log(f"config loaded: fan_curve={cfg['fan_curve']['enabled']} "
                    f"autoswitch={cfg['autoswitch']['enabled']} "
                    f"game_profiles={cfg['game_profiles']['enabled']} "
                    f"schedule={cfg['schedule']['enabled']}")

            fans.tick(cfg)
            thermal.tick(cfg)
            sched.tick(cfg, games.active)

            if cfg["autoswitch"].get("enabled"):
                ac = _ac_online()
                if ac is not None and ac != last_ac:
                    bundle = cfg["autoswitch"]["on_ac" if ac else "on_battery"]
                    log(f"power source -> {'AC' if ac else 'battery'}; apply '{bundle}'")
                    apply_bundle(bundle)
                    hz = cfg["autoswitch"].get("refresh_ac" if ac else "refresh_battery")
                    if hz:
                        ok, msg = sensors.set_panel_refresh(int(hz))
                        log(f"autoswitch: panel refresh -> {hz} Hz"
                            + ("" if ok else f" FAILED {msg}"))
                    last_ac = ac

            gp_poll = max(3, int(cfg["game_profiles"].get("poll_s", 6)))
            if once or time.monotonic() - last_game_check >= gp_poll:
                games.tick(cfg)
                last_game_check = time.monotonic()

            if once:
                break
            time.sleep(max(1, int(cfg.get("poll_s", 2))))
    finally:
        fans.restore()
        if srv is not None:
            srv.stop()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="TuxThrottle daemon — fan curve + AC-switch + per-game profiles")
    ap.add_argument("mode", choices=["run", "once"], nargs="?", default="run")
    ap.add_argument("--user", help="resolve the config path / profiles in this user's home")
    ap.add_argument("--config", type=Path, help="explicit config file path")
    args = ap.parse_args()
    if os.geteuid() != 0:
        log("warning: not root — fan/profile writes will fail")
    user = args.user or os.environ.get("SUDO_USER")
    sensors.set_session_user(user)   # systemd gives us no SUDO_*/PKEXEC_* env
    cfg_path = args.config or _config_path(user)
    return run(cfg_path, user=user, once=(args.mode == "once"))


if __name__ == "__main__":
    raise SystemExit(main())
