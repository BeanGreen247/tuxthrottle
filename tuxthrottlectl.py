#!/usr/bin/env python3
"""tuxthrottlectl — headless control/status for TuxThrottle.

A thin argparse wrapper over sensors.py so scripts, the tray, keybinds and
`ssh` sessions can read state and set limits without the GUI. stdlib only.

  tuxthrottlectl status [--json]
  tuxthrottlectl get   {power-profile|tdp|fans|battery|nvpl|gamemode|clocks|gpumode} [--json]
  tuxthrottlectl set   power-profile <balanced|performance|...>
  tuxthrottlectl set   tdp {<preset>|--stapm W --fast W --slow W}
  tuxthrottlectl set   fan-boost <1|2|both> <percent>
  tuxthrottlectl set   battery <percent>
  tuxthrottlectl set   nvpl <watts>
  tuxthrottlectl set   gpumode <integrated|hybrid|nvidia>
  tuxthrottlectl gamemode {on|off|toggle}

  tuxthrottlectl profile  {list|apply|save|show|delete} [<name>]   # full-state bundles
  tuxthrottlectl snapshot [<label>]                                # capture a rollback point
  tuxthrottlectl rollback [last|<file>]                            # restore one

Most `set` operations need root; without it sensors.py returns a clear error
and the command exits non-zero.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402
import tuxthrottle_profiles as profiles  # noqa: E402

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


def cmd_set(args) -> int:
    if args.target == "power-profile":
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
        ok, err = sensors.set_ryzenadj_limits(fast_w=f, slow_w=sl, stapm_w=s)
        return 0 if ok else _fail(err)
    if args.target == "fan-boost":
        which, pct = args.value[0], int(args.value[1])
        idxs = (1, 2) if which == "both" else (int(which),)
        raw = max(0, min(255, round(pct * 255 / 100)))
        errs = [err for i in idxs for ok, err in [sensors.set_fan_boost(i, raw)] if not ok]
        return 0 if not errs else _fail("; ".join(errs))
    if args.target == "battery":
        ok, err = sensors.set_battery_charge_limit(int(args.value[0]))
        return 0 if ok else _fail(err)
    if args.target == "nvpl":
        ok, err = sensors.set_nvidia_power_limit(int(args.value[0]))
        return 0 if ok else _fail(err)
    if args.target == "gpumode":
        ok, err = sensors.gpu_mode_set(args.value[0] if args.value else "")
        if ok:
            print("switched — log out or reboot to apply")
        return 0 if ok else _fail(err)
    return _fail(f"unknown target {args.target}")


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
    if act == "apply":
        st = profiles.load_profile(args.name)
        if not st:
            return _fail(f"no such profile: {args.name}")
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

    g = sub.add_parser("get", help="read one thing", parents=[common])
    g.add_argument("what", choices=["power-profile", "tdp", "fans", "battery",
                                    "nvpl", "gamemode", "clocks", "gpumode"])

    s = sub.add_parser("set", help="change one thing")
    s.add_argument("target", choices=["power-profile", "tdp", "fan-boost",
                                      "battery", "nvpl", "gpumode"])
    s.add_argument("value", nargs="*")
    s.add_argument("--stapm", type=int)
    s.add_argument("--fast", type=int)
    s.add_argument("--slow", type=int)

    gm = sub.add_parser("gamemode", help="Game Mode on/off/toggle")
    gm.add_argument("action", choices=["on", "off", "toggle"])

    pr = sub.add_parser("profile", help="named full-state bundles", parents=[common])
    pr.add_argument("action", choices=["list", "apply", "save", "show", "delete"])
    pr.add_argument("name", nargs="?")
    pr.add_argument("--with-gpu-mode", action="store_true",
                    help="also switch hybrid graphics (needs logout)")

    sn = sub.add_parser("snapshot", help="capture a rollback point")
    sn.add_argument("label", nargs="?", default="manual")

    rb = sub.add_parser("rollback", help="restore a snapshot", parents=[common])
    rb.add_argument("target", nargs="?", default="last")
    rb.add_argument("--with-gpu-mode", action="store_true")

    args = ap.parse_args()
    if args.cmd == "status":
        _out(gather_status(), args.json)
        return 0
    if args.cmd == "get":
        _out(cmd_get(args.what), args.json)
        return 0
    if args.cmd == "set":
        return cmd_set(args)
    if args.cmd == "gamemode":
        return cmd_gamemode(args.action)
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
