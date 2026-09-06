#!/usr/bin/env python3
"""tuxthrottlectl — headless control/status for TuxThrottle.

A thin argparse wrapper over sensors.py so scripts, the tray, keybinds and
`ssh` sessions can read state and set limits without the GUI. stdlib only.

  tuxthrottlectl status [--json]
  tuxthrottlectl watch [interval]                # live status summary, refreshes every N s (default 2)
  tuxthrottlectl get   {power-profile|tdp|fans|battery|nvpl|gamemode|clocks|gpumode} [--json]
  tuxthrottlectl set   power-profile <balanced|performance|...>
  tuxthrottlectl set   tdp {<preset>|--stapm W --fast W --slow W}
  tuxthrottlectl set   fan-boost <1|2|both> <percent>
  tuxthrottlectl set   battery <percent>
  tuxthrottlectl set   nvpl <watts>
  tuxthrottlectl set   gpumode <integrated|hybrid|nvidia>
  tuxthrottlectl set   refresh <hz>
  tuxthrottlectl set   gpu-clock {<max-mhz> [--min MHZ] | reset}
  tuxthrottlectl gamemode {on|off|toggle}
  tuxthrottlectl schedule {show|on|off}          # time-of-day profile schedule (powerd.json)

  tuxthrottlectl profile  {list|apply|save|show|delete} [<name>]   # full-state bundles
  tuxthrottlectl snapshot [<label>]                                # capture a rollback point
  tuxthrottlectl rollback [last|<file>]                            # restore one
  tuxthrottlectl daemon   {status|ping|reload}                     # the tuxthrottled control socket
  tuxthrottlectl collect-model [--slug NAME] [--out PATH]          # models/<slug>.json scaffold for this machine

When tuxthrottled (FanCurveDaemon) is running, `set` / `profile apply` are
routed through its /run/tuxthrottle/control.sock so one process owns the
hardware; otherwise they act directly.

Most `set` operations need root; without it sensors.py returns a clear error
and the command exits non-zero.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402
import tuxthrottle_control as control  # noqa: E402
import tuxthrottle_profiles as profiles  # noqa: E402


def _daemon_call(method: str, params: dict | None = None):
    """Reach a running tuxthrottled via its control socket. Returns the
    response dict, or None if the socket isn't answering."""
    if control.available():
        return control.call(method, params or {})
    return None

TDP_PRESETS = {
    "quiet": (25, 35, 25),
    "balanced": (42, 54, 42),
    "performance": (65, 80, 54),
}


def _out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{k}: {v}")
    else:
        print(obj)


def gather_status() -> dict:
    cpu = sensors.read_cpu_temp_c_value()
    ig_clk, ig_t = sensors.read_igpu_clock_temp_values()
    dg_clk, dg_t, dg_u, dg_p = sensors.read_dgpu_values()
    return {
        "model": sensors.detect_model().get("product"),
        "platform_profile": sensors.get_platform_profile(),
        "gpu_mode": sensors.gpu_mode_get(),
        "game_mode": sensors.get_game_mode_state(),
        "cpu": {"temp_c": cpu, "freq_ghz": round(sensors.read_cpu_freq_ghz_value(), 2),
                "power_w": sensors.read_cpu_power_watts()},
        "igpu": {"clock_mhz": ig_clk, "temp_c": ig_t},
        "dgpu": {"clock_mhz": dg_clk, "temp_c": dg_t, "util_pct": dg_u, "power_w": dg_p,
                 "awake": sensors.dgpu_is_awake()},
        "tdp": sensors.read_ryzenadj_info(),
        "fans": sensors.read_fans(),
        "battery_charge_limit": sensors.battery_charge_limit_info(),
        "nvidia_power_limit": sensors.nvidia_power_limit_info(),
        "gamemode": sensors.gamemode_status(),
    }


def _r(x, nd=0):
    """Round for display; '?' when the reading is missing."""
    if x is None:
        return "?"
    return int(round(x)) if nd == 0 else round(x, nd)


def _status_oneliner() -> str:
    s = gather_status()
    cpu, dg = s["cpu"], s["dgpu"]
    tdp = s.get("tdp") or {}
    fans = s.get("fans") or []
    bat = s.get("battery_charge_limit") or {}
    if dg.get("awake"):
        dgpu = (f"dGPU {_r(dg.get('temp_c'))}°C {_r(dg.get('clock_mhz'))}MHz "
                f"{_r(dg.get('util_pct'))}% {_r(dg.get('power_w'))}W")
    else:
        dgpu = "dGPU asleep"
    parts = [
        time.strftime("%H:%M:%S"),
        f"prof={s.get('platform_profile') or '?'}",
        f"game={'on' if s.get('game_mode') else 'off'}",
        (f"CPU {_r(cpu.get('temp_c'))}°C {_r(cpu.get('freq_ghz'), 1)}GHz "
         f"{_r(cpu.get('power_w'))}W"),
        (f"TDP {_r(tdp.get('stapm_limit'))}/{_r(tdp.get('fast_limit'))}/"
         f"{_r(tdp.get('slow_limit'))}W" if tdp else ""),
        dgpu,
        ("fans " + "/".join(str(f.get("rpm")) for f in fans) + "rpm") if fans else "",
        (f"bat {bat['current']}%" if bat.get("current") is not None else ""),
    ]
    return "  ".join(p for p in parts if p)


def cmd_watch(interval: float, as_json: bool) -> int:
    try:
        while True:
            if as_json:
                print(json.dumps(gather_status(), default=str), flush=True)
            else:
                sys.stdout.write("\x1b[2J\x1b[H")   # clear screen, cursor home
                print(_status_oneliner(), flush=True)
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        return 0


def cmd_get(what: str) -> dict:
    return {
        "power-profile": {"platform_profile": sensors.get_platform_profile(),
                          "choices": sensors.platform_profile_choices()},
        "tdp": sensors.read_ryzenadj_info() or {},
        "fans": {"fans": sensors.read_fans(), "boost": sensors.get_fan_boost()},
        "battery": sensors.battery_charge_limit_info(),
        "nvpl": sensors.nvidia_power_limit_info() or {},
        "gamemode": sensors.gamemode_status(),
        "clocks": {"cpu_ghz": round(sensors.read_cpu_freq_ghz_value(), 2),
                   "igpu_mhz": sensors.read_igpu_clock_temp_values()[0],
                   "dgpu_mhz": sensors.read_dgpu_values()[0]},
        "gpumode": {"mode": sensors.gpu_mode_get()},
    }[what]


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _daemon_set(params: dict) -> int | None:
    """Route a `set` through tuxthrottled (D-Bus or socket) when it's up.
    Returns an exit code, or None to fall back to a direct sensors call."""
    resp = _daemon_call("set", params)
    if resp is None:
        return None
    if resp.get("ok"):
        print(f"{params.get('target')}: ok (via tuxthrottled)")
        return 0
    return _fail(resp.get("error", "daemon rejected the request"))


def cmd_set(args) -> int:
    if args.target == "power-profile":
        rc = _daemon_set({"target": "power-profile", "value": args.value[0]})
        if rc is not None:
            return rc
        ok, err = sensors.set_platform_profile(args.value[0])
        return 0 if ok else _fail(err)
    if args.target == "tdp":
        v = args.value[0].lower() if args.value else ""
        if v in TDP_PRESETS:
            s, f, sl = TDP_PRESETS[v]
        else:
            s, f, sl = args.stapm, args.fast, args.slow
            if s is None and f is None and sl is None:
                return _fail("give a preset name or --stapm/--fast/--slow")
        rc = _daemon_set({"target": "tdp", "stapm": s, "fast": f, "slow": sl})
        if rc is not None:
            return rc
        ok, err = sensors.set_ryzenadj_limits(fast_w=f, slow_w=sl, stapm_w=s)
        return 0 if ok else _fail(err)
    if args.target == "fan-boost":
        which, pct = args.value[0], int(args.value[1])
        rc = _daemon_set({"target": "fan-boost", "which": which, "percent": pct})
        if rc is not None:
            return rc
        idxs = (1, 2) if which == "both" else (int(which),)
        raw = max(0, min(255, round(pct * 255 / 100)))
        errs = [err for i in idxs for ok, err in [sensors.set_fan_boost(i, raw)] if not ok]
        return 0 if not errs else _fail("; ".join(errs))
    if args.target == "battery":
        rc = _daemon_set({"target": "battery", "value": int(args.value[0])})
        if rc is not None:
            return rc
        ok, err = sensors.set_battery_charge_limit(int(args.value[0]))
        return 0 if ok else _fail(err)
    if args.target == "nvpl":
        rc = _daemon_set({"target": "nvpl", "value": int(args.value[0])})
        if rc is not None:
            return rc
        ok, err = sensors.set_nvidia_power_limit(int(args.value[0]))
        return 0 if ok else _fail(err)
    if args.target == "gpumode":
        ok, err = sensors.gpu_mode_set(args.value[0] if args.value else "")
        if ok:
            print("switched — log out or reboot to apply")
        return 0 if ok else _fail(err)
    if args.target == "refresh":
        if not args.value:
            return _fail("give a refresh rate in Hz")
        rc = _daemon_set({"target": "refresh", "value": int(args.value[0])})
        if rc is not None:
            return rc
        ok, err = sensors.set_panel_refresh(int(args.value[0]))
        return 0 if ok else _fail(err)
    if args.target == "gpu-clock":
        val = (args.value[0].lower() if args.value else "")
        params = {"target": "gpu-clock", "value": val or "reset"}
        if val and val not in ("reset", "unlock", "off", "0"):
            params["value"] = int(args.value[0])
            if args.min is not None:
                params["min"] = int(args.min)
        rc = _daemon_set(params)
        if rc is not None:
            return rc
        if params["value"] in ("reset", "unlock", "off", "0"):
            ok, err = sensors.reset_nvidia_clocks()
            profiles.write_config("nvclk.json", None)
        else:
            info = sensors.nvidia_clock_info() or {}
            hi = int(params["value"])
            lo = int(params.get("min") or info.get("gr_min") or 210)
            ok, err = sensors.set_nvidia_clock_lock(lo, hi)
            if ok:
                profiles.write_config("nvclk.json", {"gr_min": lo, "gr_max": hi})
        return 0 if ok else _fail(err)
    return _fail(f"unknown target {args.target}")


def cmd_schedule(args) -> int:
    """Show or toggle the time-of-day schedule in powerd.json. Rule editing
    stays in the GUI / the file itself."""
    import json as _json
    p = profiles._config_dir(None) / "powerd.json"
    try:
        cfg = _json.loads(p.read_text())
    except (OSError, ValueError):
        cfg = {}
    sc = cfg.get("schedule") if isinstance(cfg.get("schedule"), dict) else {}
    if args.action == "show":
        _out(sc or {"enabled": False, "rules": [], "outside": None}, args.json)
        return 0
    sc.setdefault("poll_s", 60)
    sc.setdefault("rules", [])
    sc.setdefault("outside", None)
    sc["enabled"] = (args.action == "on")
    cfg["schedule"] = sc
    profiles.write_config("powerd.json", cfg)   # chowns back to the real user
    print(f"schedule: {'enabled' if sc['enabled'] else 'disabled'} "
          f"({len(sc['rules'])} rule(s))")
    return 0


def cmd_gamemode(action: str) -> int:
    if action == "toggle":
        ok, err = sensors.toggle_game_mode_external()
    else:
        ok, err = sensors.set_game_mode(action == "on")
    return 0 if ok else _fail(err or "failed")


def _print_profile_results(rows: list) -> int:
    bad = 0
    for r in rows:
        print(f"  [{'ok ' if r['ok'] else 'ERR'}] {r['key']}"
              + (f" — {r['msg']}" if r.get("msg") else ""))
        bad += not r["ok"]
    return 1 if bad else 0


def cmd_profile(args) -> int:
    act = args.action
    if act == "list":
        names = profiles.list_profiles()
        print(json.dumps(names) if args.json else ("\n".join(names) or "(none)"))
        return 0
    if not args.name:
        return _fail(f"'profile {act}' needs a name")
    if act == "save":
        st = profiles.capture_state()
        profiles.save_profile(args.name, st)
        _out(st, args.json) if args.json else print(f"saved profile '{args.name}'")
        return 0
    if act == "show":
        st = profiles.load_profile(args.name)
        _out(st, args.json)
        return 0 if st else 1
    if act == "delete":
        return 0 if profiles.delete_profile(args.name) else _fail("no such profile")
    if act == "export":
        if not args.path:
            return _fail("'profile export' needs a name and a destination path")
        try:
            dest = profiles.export_profile(args.name, Path(args.path))
        except FileNotFoundError as exc:
            return _fail(str(exc))
        print(f"exported '{args.name}' -> {dest}")
        return 0
    if act == "import":
        try:
            final_name = profiles.import_profile(Path(args.name), args.path)
        except ValueError as exc:
            return _fail(str(exc))
        print(f"imported '{final_name}' from {args.name}")
        return 0
    if act == "apply":
        st = profiles.load_profile(args.name)
        if not st:
            return _fail(f"no such profile: {args.name}")
        resp = _daemon_call("apply_profile",
                            {"name": args.name, "with_gpu_mode": args.with_gpu_mode})
        if resp is not None:
            if resp.get("ok"):
                rows = resp["result"].get("results", [])
                if args.json:
                    print(json.dumps(rows, indent=2))
                    return 1 if any(not r["ok"] for r in rows) else 0
                print("(via tuxthrottled)")
                return _print_profile_results(rows)
            return _fail(resp.get("error", "daemon rejected apply_profile"))
        profiles.snapshot(label=f"pre-apply-{args.name}")
        rows = profiles.apply_state(st, with_gpu_mode=args.with_gpu_mode)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 1 if any(not r["ok"] for r in rows) else 0
        return _print_profile_results(rows)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(prog="tuxthrottlectl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="full state dump", parents=[common])

    w = sub.add_parser("watch", help="reprint a status summary every N seconds",
                       parents=[common])
    w.add_argument("interval", nargs="?", type=float, default=2.0,
                   help="seconds between refreshes (default 2)")

    g = sub.add_parser("get", help="read one thing", parents=[common])
    g.add_argument("what", choices=["power-profile", "tdp", "fans", "battery",
                                    "nvpl", "gamemode", "clocks", "gpumode"])

    s = sub.add_parser("set", help="change one thing")
    s.add_argument("target", choices=["power-profile", "tdp", "fan-boost",
                                      "battery", "nvpl", "gpumode", "refresh",
                                      "gpu-clock"])
    s.add_argument("value", nargs="*")
    s.add_argument("--stapm", type=int)
    s.add_argument("--fast", type=int)
    s.add_argument("--slow", type=int)
    s.add_argument("--min", type=int, help="gpu-clock: minimum lock MHz")

    gm = sub.add_parser("gamemode", help="Game Mode on/off/toggle")
    gm.add_argument("action", choices=["on", "off", "toggle"])

    scd = sub.add_parser("schedule", help="time-of-day profile schedule",
                         parents=[common])
    scd.add_argument("action", choices=["show", "on", "off"], nargs="?",
                     default="show")

    pr = sub.add_parser("profile", help="named full-state bundles", parents=[common])
    pr.add_argument("action",
                    choices=["list", "apply", "save", "show", "delete", "export", "import"])
    pr.add_argument("name", nargs="?",
                    help="profile name, or (for import) the source file path")
    pr.add_argument("path", nargs="?",
                    help="export: destination file path. import: target profile "
                         "name (default: the name it was exported with)")
    pr.add_argument("--with-gpu-mode", action="store_true",
                    help="also switch hybrid graphics (needs logout)")

    sn = sub.add_parser("snapshot", help="capture a rollback point")
    sn.add_argument("label", nargs="?", default="manual")

    rb = sub.add_parser("rollback", help="restore a snapshot", parents=[common])
    rb.add_argument("target", nargs="?", default="last")
    rb.add_argument("--with-gpu-mode", action="store_true")

    bf = sub.add_parser("btrfs-snapshot",
                        help="Btrfs filesystem snapshot (snapper) before a risky change",
                        add_help=False)
    bf.add_argument("rest", nargs=argparse.REMAINDER,
                    help="available | create [desc] | list [--limit N]")

    wd = sub.add_parser("watchdog",
                        help="confirm-or-auto-revert timer for a risky live change",
                        add_help=False)
    wd.add_argument("rest", nargs=argparse.REMAINDER,
                    help="arm SECONDS --user NAME | disarm UNIT | status UNIT")

    dm = sub.add_parser("daemon", help="control-socket status / actions",
                        parents=[common])
    dm.add_argument("action", choices=["status", "ping", "reload"], nargs="?",
                    default="status")

    cm = sub.add_parser("collect-model",
                        help="emit a models/<slug>.json scaffold for this machine")
    cm.add_argument("--slug", help="model id / filename stem (default: from DMI)")
    cm.add_argument("--out", help="write to this path instead of stdout")

    vr = sub.add_parser("vram", help="VRAM budget / free / desktop-GPU selector",
                        add_help=False)
    vr.add_argument("rest", nargs=argparse.REMAINDER,
                    help="status [--json] | profile {regular|medium|extreme} | "
                         "free [--json] [--restart-compositor] | "
                         "compositor-gpu {auto|igpu|dgpu}")

    args = ap.parse_args()
    if args.cmd == "vram":
        import tuxthrottle_vram as vram
        return vram.main(args.rest or ["status"])
    if args.cmd == "btrfs-snapshot":
        import tuxthrottle_btrfs as btrfs
        return btrfs.main(args.rest or ["available"])
    if args.cmd == "watchdog":
        import tuxthrottle_watchdog as watchdog
        return watchdog.main(args.rest)
    if args.cmd == "collect-model":
        import tuxthrottle_modelgen as modelgen
        return modelgen.main(
            (["--slug", args.slug] if args.slug else [])
            + (["--out", args.out] if args.out else []))
    if args.cmd == "daemon":
        pres = control.presence()
        if pres != "up":
            msg = ("running, but root-only — re-run with sudo"
                   if pres == "root-only" else "not running")
            print(json.dumps({"up": False, "state": pres}) if args.json
                  else f"tuxthrottled control socket: {msg}")
            return 1
        if args.action == "ping":
            print(json.dumps({"up": True}) if args.json else "tuxthrottled: up")
            return 0
        resp = control.call(args.action)   # status | reload
        _out(resp, args.json)
        return 0 if resp and resp.get("ok") else 1
    if args.cmd == "status":
        _out(gather_status(), args.json)
        return 0
    if args.cmd == "watch":
        return cmd_watch(args.interval, args.json)
    if args.cmd == "get":
        _out(cmd_get(args.what), args.json)
        return 0
    if args.cmd == "set":
        return cmd_set(args)
    if args.cmd == "gamemode":
        return cmd_gamemode(args.action)
    if args.cmd == "schedule":
        return cmd_schedule(args)
    if args.cmd == "profile":
        return cmd_profile(args)
    if args.cmd == "snapshot":
        print(profiles.snapshot(label=args.label))
        return 0
    if args.cmd == "rollback":
        rows = profiles.rollback(args.target, with_gpu_mode=args.with_gpu_mode)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 1 if any(not r["ok"] for r in rows) else 0
        return _print_profile_results(rows)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
