#!/usr/bin/env python3
"""Run a single tweak's `apply` command list from config/tweaks.json.

Used by install.sh to (re)install the service files for tweaks that are
already enabled, and usable standalone:

    sudo python3 apply_tweak.py KbdBacklightFix
    sudo python3 apply_tweak.py CpuMaxPerformance --only-if-present

--only-if-present : do nothing unless the tweak is already installed — i.e.
                    its `check` passes now, OR any path in its optional
                    `reinstall_if` list exists (globs allowed). This is how
                    install.sh refreshes units without force-enabling a
                    feature the user never turned on.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path


def real_user() -> str:
    for var in ("SUDO_USER", "PKEXEC_USER"):
        v = os.environ.get(var)
        if v and v != "root":
            return v
    try:
        return subprocess.check_output(["logname"], text=True).strip()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER", "root")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tweak_id")
    ap.add_argument("--only-if-present", action="store_true")
    ap.add_argument("--toolkit-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--user", default=None)
    a = ap.parse_args()

    toolkit_dir = a.toolkit_dir.rstrip("/")
    user = a.user or real_user()
    cfg = Path(toolkit_dir) / "config" / "tweaks.json"
    try:
        tweaks = json.loads(cfg.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"apply_tweak: cannot read {cfg}: {exc}", file=sys.stderr)
        return 2
    if a.tweak_id not in tweaks:
        print(f"apply_tweak: no such tweak {a.tweak_id!r}", file=sys.stderr)
        return 2
    t = tweaks[a.tweak_id]

    def sub(c: str) -> str:
        return c.replace("{TOOLKIT_DIR}", toolkit_dir).replace("{USER}", user)

    if a.only_if_present:
        present = subprocess.run(["bash", "-c", sub(t.get("check", "false"))]).returncode == 0
        if not present:
            for pat in t.get("reinstall_if", []):
                if glob.glob(sub(pat)):
                    present = True
                    break
        if not present:
            print(f"apply_tweak: {a.tweak_id} not enabled — skipped")
            return 3  # distinct from apply-failure so install.sh can tell

    apply_cmds = [sub(c) for c in t.get("apply", [])]
    for i, cmd in enumerate(apply_cmds, 1):
        head = cmd.splitlines()[0][:88]
        print(f"[{i}/{len(apply_cmds)}] {head}")
        rc = subprocess.run(["bash", "-c", cmd]).returncode
        if rc != 0:
            print(f"  !! exit {rc}", file=sys.stderr)

    ok = subprocess.run(["bash", "-c", sub(t.get("check", "true"))]).returncode == 0
    print(f"{a.tweak_id}: check {'PASS' if ok else 'FAIL'} after apply")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
