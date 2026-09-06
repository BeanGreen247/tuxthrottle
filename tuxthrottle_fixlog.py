#!/usr/bin/env python3
"""Shared append-only event log for fixes TuxThrottle applies on your behalf
(a relinked shader-cache symlink, a relocated Proton prefix, a mounted drive,
a crash signature it recognised) — so the Diagnostics page can show what
quietly happened instead of it scrolling out of a terminal you never had
open. Stdlib only, one JSON object per line, capped at the last MAX_ENTRIES.

    tuxthrottle_fixlog.py log <source> <message> [--level info|warn|error]
    tuxthrottle_fixlog.py show [--json] [-n N]
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import time
from pathlib import Path

MAX_ENTRIES = 500


def log_path(user: str | None = None) -> Path:
    home = Path(os.path.expanduser(f"~{user}")) if user else Path.home()
    return home / ".local" / "share" / "tuxthrottle" / "fixlog.jsonl"


def _chown_to(path: Path, user: str) -> None:
    """When called as root writing into another user's home (the GUI is
    self-elevated), hand ownership back — otherwise that user's own
    unprivileged processes (the tray's crash watcher) can never append again."""
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(user)
        os.chown(path, pw.pw_uid, pw.pw_gid)
        os.chown(path.parent, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass


def log_event(source: str, message: str, level: str = "info",
             user: str | None = None) -> None:
    """Append one event and trim the file to the last MAX_ENTRIES lines.
    Best-effort — a logging failure must never break the caller's real work."""
    try:
        path = log_path(user)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "source": source, "level": level,
                  "message": message}
        lines = []
        if path.is_file():
            lines = path.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry))
        lines = lines[-MAX_ENTRIES:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if user:
            _chown_to(path, user)
    except OSError:
        pass


def read_recent(n: int = 50, user: str | None = None) -> list[dict]:
    path = log_path(user)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("log")
    lg.add_argument("source")
    lg.add_argument("message")
    lg.add_argument("--level", default="info", choices=["info", "warn", "error"])
    lg.add_argument("--user", default=None)

    sh = sub.add_parser("show")
    sh.add_argument("-n", type=int, default=50)
    sh.add_argument("--json", action="store_true")
    sh.add_argument("--user", default=None)

    a = ap.parse_args()
    if a.cmd == "log":
        log_event(a.source, a.message, level=a.level, user=a.user)
        return 0
    entries = read_recent(a.n, user=a.user)
    if a.json:
        print(json.dumps(entries, indent=2))
    elif not entries:
        print("no fixes logged yet")
    else:
        for e in entries:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
            print(f"[{when}] {e['level']:5} {e['source']:20} {e['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
