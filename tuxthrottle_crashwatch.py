#!/usr/bin/env python3
"""Recognise known crash/freeze signatures from `coredumpctl` and the journal,
so the tray can surface a plain-English cause instead of the user having to
SSH in and grep logs by hand every time — the exact manual steps this module
automates were worked out live for: a benign Proton bootstrap self-quit
(wine-preloader/d3ddriverquery64.exe, harmless — Steam's own DirectX-driver
probe), a steamwebhelper CEF crash-loop, and an NTFS volume left dirty by
Windows. New signatures should be added to SIGNATURES as they're diagnosed.

Stdlib only. Runs as the normal user (coredumpctl/journalctl read their own
user's entries without root).

    tuxthrottle_crashwatch.py scan [--since SECONDS] [--json] [--notify]

State (last-seen coredump timestamp, to avoid re-reporting the same crash on
every poll) lives in ~/.local/share/tuxthrottle/crashwatch-state.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Each signature: match against a coredump's EXE path and/or its full command
# line (both substring, case-insensitive). First match wins, top to bottom —
# put more specific rules first. `benign=True` entries are recognised and
# logged but never raise a tray notification (they're normal operation that
# only *looks* like a crash to a coredump/KCrash listener).
SIGNATURES: list[dict[str, Any]] = [
    {
        "id": "proton-bootstrap-quit",
        "match_exe": "wine-preloader",
        "match_cmdline": "d3ddriverquery64.exe",
        "benign": True,
        "label": "Proton bootstrap driver check (normal)",
        "hint": "Steam quits this helper itself after querying your GPU driver — not a real crash.",
    },
    {
        "id": "steamwebhelper-crash",
        "match_exe": "steamwebhelper",
        "benign": False,
        "label": "Steam's web UI (steamwebhelper) crashed",
        "hint": "If this repeats, open Game Tools -> Steam client low-resource mode and make sure "
                "no custom -cef-single-process / -no-cef-sandbox / -no-browser flags are set — "
                "those are known to crash-loop the CEF process on current Steam builds.",
    },
    {
        "id": "wine-preloader-crash",
        "match_exe": "wine-preloader",
        "benign": False,
        "label": "A Windows game/Proton process crashed",
        "hint": "Usually game-specific — check that game's Proton log "
                "(compatdata/<appid>/pfx or PROTON_LOG=1) for the real error.",
    },
]

# Plain journal-message signatures (not coredumps) — checked separately since
# an NTFS-dirty refusal or a stalled sched_ext scheduler never dumps core.
JOURNAL_SIGNATURES: list[dict[str, Any]] = [
    {
        "id": "ntfs-dirty",
        "pattern": re.compile(r"ntfs3.*volume is dirty", re.I),
        "label": "An NTFS drive was left 'dirty' by Windows and failed to mount",
        "hint": "Enable the NtfsForceMount tweak (Stability tab), or turn off Windows "
                "'Fast Startup' (Control Panel -> Power Options) to stop it recurring.",
    },
    {
        "id": "scx-stall",
        "pattern": re.compile(r"sched_ext.*(disabl|error)|scx_lavd.*(stall|error)", re.I),
        "label": "The sched_ext scheduler (scx_lavd) reported a problem",
        "hint": "See project notes: scx_lavd is known to stall/freeze on some kernels — "
                "leave it off and use the stock EEVDF scheduler.",
    },
]


def _state_path(user: str | None = None) -> Path:
    home = Path(os.path.expanduser(f"~{user}")) if user else Path.home()
    return home / ".local" / "share" / "tuxthrottle" / "crashwatch-state.json"


def _load_state(user: str | None = None) -> dict:
    p = _state_path(user)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            pass
    return {"seen": []}


def _save_state(state: dict, user: str | None = None) -> None:
    p = _state_path(user)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        state["seen"] = state.get("seen", [])[-200:]
        p.write_text(json.dumps(state))
    except OSError:
        pass


def _classify(exe: str, cmdline: str):
    exe_l, cmd_l = exe.lower(), cmdline.lower()
    for sig in SIGNATURES:
        if sig["match_exe"].lower() not in exe_l:
            continue
        need_cmd = sig.get("match_cmdline")
        if need_cmd and need_cmd.lower() not in cmd_l:
            continue
        return sig
    return None


def _coredump_events(since_seconds: int) -> list[dict]:
    """Use --json=short, not the plain-text table: a text EXE column can
    contain spaces (e.g. a real path seen in the wild —
    '.../Proton - Experimental/files/lib/wine/x86_64-unix/wine-preloader')
    and some rows carry a trailing '-' placeholder column, both of which
    silently corrupt a whitespace-split parse (this shipped once — the
    'unknown-crash' fallback below showed up as a bare '- crashed')."""
    try:
        out = subprocess.run(
            ["coredumpctl", "list", "--json=short", "--since", f"-{since_seconds}s"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if not out.stdout.strip():
        return []
    try:
        rows = json.loads(out.stdout)
    except ValueError:
        return []
    events = []
    for row in rows:
        pid = str(row.get("pid", ""))
        if not pid:
            continue
        exe = row.get("exe") or ""
        ts = row.get("time")
        info = subprocess.run(["coredumpctl", "info", pid],
                              capture_output=True, text=True, timeout=15)
        cmdline = ""
        for il in info.stdout.splitlines():
            if il.strip().startswith("Command Line:"):
                cmdline = il.split(":", 1)[1].strip()
                break
        events.append({"key": f"core:{pid}:{ts}", "timestamp": ts,
                       "pid": pid, "exe": exe, "cmdline": cmdline})
    return events


def _journal_events(since_seconds: int) -> list[dict]:
    try:
        out = subprocess.run(
            ["journalctl", "--user", f"--since=-{since_seconds}s", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    events = []
    for sig in JOURNAL_SIGNATURES:
        m = sig["pattern"].search(out.stdout)
        if m:
            key = f"journal:{sig['id']}:{since_seconds}:{int(time.time() // since_seconds)}"
            events.append({"key": key, "sig": sig})
    return events


def scan(since_seconds: int = 120, user: str | None = None) -> list[dict]:
    """Return newly-seen (not-yet-reported) findings from the last
    `since_seconds`. Each finding: {id, label, hint, benign, detail}."""
    state = _load_state(user)
    seen = set(state.get("seen", []))
    findings = []

    for ev in _coredump_events(since_seconds):
        if ev["key"] in seen:
            continue
        seen.add(ev["key"])
        sig = _classify(ev["exe"], ev["cmdline"])
        if sig is None:
            name = Path(ev["exe"]).name if ev["exe"] else "An unidentified process"
            sig = {"id": "unknown-crash", "label": f"{name} crashed",
                  "hint": "No known signature for this one yet.", "benign": False}
        findings.append({**sig, "detail": ev["exe"]})

    for ev in _journal_events(since_seconds):
        if ev["key"] in seen:
            continue
        seen.add(ev["key"])
        sig = ev["sig"]
        findings.append({"id": sig["id"], "label": sig["label"], "hint": sig["hint"],
                         "benign": False, "detail": "journal"})

    state["seen"] = list(seen)
    _save_state(state, user)
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--since", type=int, default=120, help="lookback window, seconds")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--notify", action="store_true", help="also fire a desktop notification")
    sc.add_argument("--user", default=None)
    a = ap.parse_args()

    findings = scan(a.since, user=a.user)
    real = [f for f in findings if not f.get("benign")]

    if a.notify and real:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sensors  # noqa: E402
        for f in real:
            sensors.notify(f["label"], f["hint"])

    if a.json:
        print(json.dumps(findings, indent=2))
    elif not findings:
        print("no new findings")
    else:
        for f in findings:
            tag = "benign" if f.get("benign") else "!"
            print(f"[{tag}] {f['label']} — {f['hint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
