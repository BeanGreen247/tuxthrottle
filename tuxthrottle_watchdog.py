#!/usr/bin/env python3
"""Confirm-or-auto-revert safety watchdog — stdlib only, no GUI deps.

The pattern Windows/NVIDIA display-settings dialogs use for anything that
can make the machine unusable: apply the change, arm an independent timer,
and auto-revert unless the user confirms within N seconds. TuxThrottle's own
scx_lavd hard-freeze (sched_ext stalling on kernel 7.x, see project memory)
is exactly the failure class this exists for — a change that can freeze the
*GUI process itself*, so the revert cannot depend on that process still
being alive to run it.

The timer therefore runs as an independent transient systemd unit
(`systemd-run --on-active=<seconds>`), not a Python thread inside the GUI —
if the GUI (or the whole desktop session) locks up, the unit still fires on
schedule and runs the rollback command outside the frozen process.

Callers are responsible for taking a rollback point *before* applying the
risky change (`tuxthrottle_profiles.snapshot(...)`) — this module only arms
the timer that will run `tuxthrottle_profiles.py rollback last` against it.

CLI:
  tuxthrottle_watchdog.py arm <seconds> --user NAME [--toolkit-dir DIR]
      # prints the unit name on stdout
  tuxthrottle_watchdog.py disarm <unit>
  tuxthrottle_watchdog.py status <unit>       # prints "armed" or "idle"
"""
import argparse
import subprocess
import sys
import uuid
from pathlib import Path

UNIT_PREFIX = "tuxthrottle-watchdog-"


def _toolkit_dir() -> str:
    return str(Path(__file__).resolve().parent)


def arm(seconds: int, user: str, toolkit_dir: str | None = None) -> str:
    """Schedule a rollback-to-last-snapshot unless disarmed within `seconds`.

    Returns the transient unit's name (pass it to `disarm`/`status`). Raises
    RuntimeError if systemd-run itself can't be invoked — callers should
    treat that as "no safety net available" and decide whether to proceed
    anyway (never silently pretend a watchdog is armed when it isn't)."""
    if seconds < 1:
        raise ValueError("seconds must be >= 1")
    toolkit_dir = toolkit_dir or _toolkit_dir()
    unit = UNIT_PREFIX + uuid.uuid4().hex[:12]
    profiles_py = str(Path(toolkit_dir) / "tuxthrottle_profiles.py")
    cmd = [
        "systemd-run", f"--unit={unit}", "--collect",
        f"--on-active={int(seconds)}",
        "--description=TuxThrottle auto-revert watchdog",
        "python3", profiles_py, "rollback", "last", "--user", user,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"systemd-run unavailable: {exc}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"systemd-run exit {out.returncode}: {out.stderr.strip()}")
    return unit


def disarm(unit: str) -> bool:
    """Cancel a still-pending watchdog (the user confirmed the change was
    good). Best-effort and idempotent — safe to call even if it already
    fired or never existed."""
    if not unit.startswith(UNIT_PREFIX):
        return False
    ok = True
    for target in (f"{unit}.timer", f"{unit}.service"):
        r = subprocess.run(["systemctl", "stop", target],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0 and "not loaded" not in r.stderr.lower():
            ok = False
    subprocess.run(["systemctl", "reset-failed", unit],
                   capture_output=True, text=True, timeout=10)
    return ok


def is_armed(unit: str) -> bool:
    """True while the countdown is still pending (not yet fired, not disarmed)."""
    try:
        r = subprocess.run(["systemctl", "is-active", f"{unit}.timer"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "active"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_arm = sub.add_parser("arm")
    p_arm.add_argument("seconds", type=int)
    p_arm.add_argument("--user", required=True)
    p_arm.add_argument("--toolkit-dir", default=None)

    p_disarm = sub.add_parser("disarm")
    p_disarm.add_argument("unit")

    p_status = sub.add_parser("status")
    p_status.add_argument("unit")

    a = ap.parse_args(argv)

    if a.cmd == "arm":
        try:
            unit = arm(a.seconds, a.user, a.toolkit_dir)
        except (RuntimeError, ValueError) as exc:
            print(f"watchdog: could not arm: {exc}", file=sys.stderr)
            return 1
        print(unit)
        return 0
    if a.cmd == "disarm":
        return 0 if disarm(a.unit) else 1
    if a.cmd == "status":
        print("armed" if is_armed(a.unit) else "idle")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
