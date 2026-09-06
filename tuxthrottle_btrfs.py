#!/usr/bin/env python3
"""Btrfs filesystem-level snapshot-before-apply — stdlib only, no GUI deps.

`tuxthrottle_profiles.snapshot()` already captures a JSON *config* snapshot
(platform_profile, TDP, fan curve, ...) before anything risky. This module is
the complementary filesystem-level safety net the CachyOS "rollback is
chef's kiss" habit is built on: on a Btrfs root, take a real read-only
subvolume snapshot before a risky batch of tweaks runs, so a change that
breaks the *system* (not just TuxThrottle's own config) can still be undone
by booting into the snapshot — the standard `snapper rollback` /
`grub-btrfs` flow, not anything this module reimplements itself.

Deliberately narrow scope: this module only *creates* snapshots. It never
touches the bootloader, the default subvolume, or does the actual rollback —
that is `snapper rollback <number>` (well-tested, standard tooling) followed
by a reboot, left to the user/CLI printing the exact command. Reimplementing
subvolume-swap logic here would be the same class of mistake as the reverted
D-Bus policy that bricked boot (see project memory) — this stays additive
and safe-by-construction: if Btrfs/snapper aren't there or don't cooperate,
every function reports "unavailable" and TuxThrottle proceeds without one.

CLI:
  tuxthrottle_btrfs.py available          # print availability + method
  tuxthrottle_btrfs.py create [desc]      # take a snapshot, print its id
  tuxthrottle_btrfs.py list [--limit N]   # list recent tuxthrottle snapshots
"""
import argparse
import json
import shutil
import subprocess
import sys

SNAPPER_CONFIG = "root"
DESCRIPTION_PREFIX = "tuxthrottle: "


def is_btrfs_root() -> bool:
    """True if `/` is mounted on a btrfs filesystem."""
    try:
        out = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "/"],
            capture_output=True, text=True, timeout=5,
        )
        return out.returncode == 0 and out.stdout.strip() == "btrfs"
    except (OSError, subprocess.SubprocessError):
        return False


def snapper_available() -> bool:
    """True if `snapper` is installed and has a usable "root" config."""
    if shutil.which("snapper") is None:
        return False
    try:
        out = subprocess.run(
            ["snapper", "-c", SNAPPER_CONFIG, "list"],
            capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def method() -> str | None:
    """Which snapshot method is usable on this system, or None."""
    if not is_btrfs_root():
        return None
    if snapper_available():
        return "snapper"
    return None


def available() -> bool:
    return method() is not None


def create_snapshot(description: str = "pre-apply") -> dict:
    """Take a read-only snapshot before a risky change. Best-effort — never
    raises; callers should log `msg` either way and proceed regardless."""
    m = method()
    if m is None:
        return {"ok": False, "method": None, "id": None,
                "msg": "not on a Btrfs root with snapper configured — skipped"}

    if m == "snapper":
        try:
            out = subprocess.run(
                ["snapper", "-c", SNAPPER_CONFIG, "create",
                 "--type", "single", "--print-number",
                 "--description", DESCRIPTION_PREFIX + description],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "method": m, "id": None, "msg": f"snapper failed: {exc}"}
        if out.returncode != 0:
            return {"ok": False, "method": m, "id": None,
                     "msg": f"snapper exit {out.returncode}: {out.stderr.strip()}"}
        num = out.stdout.strip()
        return {"ok": True, "method": m, "id": num,
                 "msg": f"snapper snapshot #{num} created"}

    return {"ok": False, "method": m, "id": None, "msg": "unknown method"}


def list_snapshots(limit: int = 10) -> list[dict]:
    """Recent TuxThrottle-created snapshots, newest first. Empty if snapper
    isn't available — this never falls back to guessing raw subvolume paths."""
    if not snapper_available():
        return []
    try:
        out = subprocess.run(
            ["snapper", "-c", SNAPPER_CONFIG, "list", "--columns",
             "number,date,description"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        if DESCRIPTION_PREFIX not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        num, date = parts[0], parts[1]
        desc = parts[2].removeprefix(DESCRIPTION_PREFIX)
        if not num.isdigit():
            continue
        rows.append({"number": num, "date": date, "description": desc})
    rows.sort(key=lambda r: int(r["number"]), reverse=True)
    return rows[:limit]


def rollback_hint(number: str) -> str:
    """The exact command the user runs to actually roll back — this module
    never runs it itself (see module docstring)."""
    return f"sudo snapper -c {SNAPPER_CONFIG} rollback {number}   # then reboot"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("available")
    p_create = sub.add_parser("create")
    p_create.add_argument("description", nargs="?", default="pre-apply")
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=10)
    a = ap.parse_args(argv)

    if a.cmd == "available":
        m = method()
        print(json.dumps({"available": m is not None, "method": m}))
        return 0
    if a.cmd == "create":
        res = create_snapshot(a.description)
        print(json.dumps(res))
        if res["ok"]:
            print(f"# to roll back: {rollback_hint(res['id'])}", file=sys.stderr)
        return 0 if res["ok"] else 1
    if a.cmd == "list":
        print(json.dumps(list_snapshots(a.limit), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
