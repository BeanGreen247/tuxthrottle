#!/usr/bin/env python3
"""Ryzen Curve Optimizer undervolt with a stress-test-and-auto-revert harness.

**stdlib only. Runs as root.** The CO offset is a real footgun: too aggressive
and the box silently miscalculates, segfault-storms, or hard-hangs. This script
never leaves an *unverified* offset armed for boot:

  apply <-N> [--minutes M] [--no-gpu]
        snapshot -> write co.json (confirmed:false) -> arm the boot watchdog ->
        set --set-coall=-N -> run stress-ng (+ a light GPU load) for M minutes,
        polling dmesg for MCE/WHEA and watching the load stay alive.
        Any fault  -> revert to 0, delete co.json, exit 1.
        Survived   -> leave -N applied, exit 0, tell the user to `confirm`.

  confirm       mark co.json confirmed:true (now the boot service will re-apply
                it) and disarm the watchdog.
  revert        set CO to 0, delete co.json + the watchdog.
  reapply       boot/resume hook — re-apply co.json ONLY if confirmed:true.
  status        print co.json / whether an offset is armed.

Boot safety: the RyzenCurveOptimizer tweak's boot service + sleep hook run
`tuxthrottle_co_stress.py reapply`, which applies co.json only when
confirmed:true — so an offset that hung the machine before you confirmed it is
simply not reapplied on the next boot.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402
import tuxthrottle_profiles as profiles  # noqa: E402

RUN_DIR = Path("/run/tuxthrottle")
WATCHDOG = RUN_DIR / "co_watchdog"
FAULT_RE = ("mce", "hardware error", "whea", "uncorrected", "machine check",
            "gpf", "general protection")


def _user() -> str:
    if os.environ.get("SUDO_USER"):
        return os.environ["SUDO_USER"]
    uid = os.environ.get("PKEXEC_UID")
    if uid and uid.isdigit():
        try:
            return pwd.getpwuid(int(uid)).pw_name
        except KeyError:
            pass
    return "root"


def _co_path(user: str) -> Path:
    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        home = Path.home()
    return home / ".config" / "tuxthrottle" / "co.json"


def _load(user: str) -> dict:
    try:
        return json.loads(_co_path(user).read_text())
    except (OSError, ValueError):
        return {}


def _save(user: str, data: dict) -> None:
    p = _co_path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    try:
        pw = pwd.getpwnam(user)
        os.chown(p, pw.pw_uid, pw.pw_gid)
        os.chown(p.parent, pw.pw_uid, pw.pw_gid)
    except (KeyError, PermissionError):
        pass


def _dmesg_since(ts: float) -> list[str]:
    try:
        out = subprocess.run(["dmesg", "--since", f"-{int(time.time() - ts) + 2}sec",
                              "--level=err,crit,alert,emerg", "--notime"],
                             capture_output=True, text=True, timeout=8)
        return [ln for ln in out.stdout.splitlines()
                if any(k in ln.lower() for k in FAULT_RE)]
    except Exception:  # noqa: BLE001
        return []


def _start_cpu_load(minutes: float):
    secs = max(1, int(minutes * 60))
    if shutil.which("stress-ng"):
        return subprocess.Popen(["stress-ng", "--cpu", "0", "--timeout", f"{secs}s",
                                 "--metrics-brief"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # fallback: one busy `sh` per CPU via `yes`
    n = os.cpu_count() or 8
    return subprocess.Popen(
        ["sh", "-c", f"for i in $(seq {n}); do yes >/dev/null & done; "
         f"sleep {secs}; kill $(jobs -p) 2>/dev/null"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _start_gpu_load():
    for exe, args in (("glmark2", ["--run-forever", "--off-screen"]),
                      ("vkmark", ["--run-forever"]),
                      ("glxgears", [])):
        if shutil.which(exe):
            try:
                return subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")})
            except OSError:
                return None
    return None


def cmd_apply(args) -> int:
    user = args.user or _user()
    offset = max(-50, min(-1, int(args.offset)))
    if not sensors.ryzenadj_co_supported():
        print("FAIL: ryzenadj / AMD CPU not available", file=sys.stderr)
        return 1

    print("[co] snapshot (pre-CO) …")
    profiles.snapshot(user, label="pre-curve-optimizer")

    started = time.time()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    WATCHDOG.write_text(str(started + args.minutes * 60 + 120))
    _save(user, {"offset": offset, "confirmed": False,
                 "applied": time.strftime("%Y-%m-%d %H:%M:%S")})

    ok, err = sensors.set_co_offset(offset)
    if not ok:
        print(f"FAIL: could not apply --set-coall={offset}: {err}", file=sys.stderr)
        _co_path(user).unlink(missing_ok=True)
        WATCHDOG.unlink(missing_ok=True)
        return 1
    print(f"[co] applied --set-coall={offset}; stress-testing {args.minutes} min "
          f"(Ctrl-C reverts) …", flush=True)

    cpu = _start_cpu_load(args.minutes)
    gpu = None if args.no_gpu else _start_gpu_load()
    if gpu:
        print("[co] GPU load running alongside")

    deadline = started + args.minutes * 60
    reason = None
    try:
        while time.time() < deadline:
            time.sleep(5)
            try:
                WATCHDOG.write_text(str(time.time() + 120))
            except OSError:
                pass
            faults = _dmesg_since(started)
            if faults:
                reason = "kernel error: " + faults[-1][:160]
                break
            if cpu.poll() is not None and cpu.returncode not in (0, None):
                reason = f"CPU stress process died (rc={cpu.returncode})"
                break
    except KeyboardInterrupt:
        reason = "interrupted by user"
    finally:
        for p in (cpu, gpu):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    if reason:
        sensors.set_co_offset(0)
        _co_path(user).unlink(missing_ok=True)
        WATCHDOG.unlink(missing_ok=True)
        print(f"FAIL: {reason}\n[co] reverted to stock (--set-coall=0).", file=sys.stderr)
        return 1

    print(f"OK: --set-coall={offset} survived {args.minutes} min of load.\n"
          f"    It is applied now but NOT armed for boot. To keep it:\n"
          f"      sudo {Path(__file__).name} confirm\n"
          f"    Otherwise it clears on the next reboot.")
    return 0


def cmd_confirm(args) -> int:
    user = args.user or _user()
    d = _load(user)
    if not d or d.get("offset", 0) >= 0:
        print("nothing to confirm (no armed offset)", file=sys.stderr)
        return 1
    d["confirmed"] = True
    d["confirmed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(user, d)
    WATCHDOG.unlink(missing_ok=True)
    print(f"confirmed: --set-coall={d['offset']} will be re-applied at boot "
          f"(tuxthrottle-co.service).")
    return 0


def cmd_revert(args) -> int:
    user = args.user or _user()
    ok, err = sensors.set_co_offset(0)
    _co_path(user).unlink(missing_ok=True)
    WATCHDOG.unlink(missing_ok=True)
    print("reverted to stock CO (0)." if ok else f"revert failed: {err}",
          file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


def cmd_reapply(args) -> int:
    """Boot / resume hook: re-apply co.json ONLY if it is confirmed. An
    unconfirmed offset (one that may have hung the box before the user got to
    `confirm`) is deliberately left un-applied."""
    user = args.user or _user()
    d = _load(user)
    off = int(d.get("offset", 0) or 0)
    if not d.get("confirmed") or off >= 0:
        print(f"[co] nothing to re-apply (confirmed={d.get('confirmed')}, offset={off})")
        return 0
    ok, err = sensors.set_co_offset(off)
    print(f"[co] re-applied --set-coall={off}" if ok else f"[co] re-apply failed: {err}")
    return 0 if ok else 1


def cmd_status(args) -> int:
    user = args.user or _user()
    d = _load(user)
    print(json.dumps({
        "co": d or None,
        "watchdog_armed": WATCHDOG.exists(),
        "ryzenadj_co_supported": sensors.ryzenadj_co_supported(),
    }, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", help="whose ~/.config/tuxthrottle/co.json to use")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="apply an offset and stress-test it")
    a.add_argument("offset", type=int, help="all-core CO offset, negative (e.g. -20)")
    a.add_argument("--minutes", type=float, default=5.0)
    a.add_argument("--no-gpu", action="store_true", help="CPU-only stress")

    sub.add_parser("confirm", help="keep the current offset across reboots")
    sub.add_parser("revert", help="set CO back to 0 and forget it")
    sub.add_parser("reapply", help="boot/resume hook — re-apply a CONFIRMED offset")
    sub.add_parser("status", help="show co.json / watchdog state")

    args = ap.parse_args()
    if args.cmd == "status" and os.geteuid() != 0:
        return cmd_status(args)   # read-only, allow non-root
    if os.geteuid() != 0:
        print("error: run as root (sudo)", file=sys.stderr)
        return 2
    return {"apply": cmd_apply, "confirm": cmd_confirm, "revert": cmd_revert,
            "reapply": cmd_reapply, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    raise SystemExit(main())
