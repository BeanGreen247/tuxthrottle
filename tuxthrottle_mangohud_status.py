#!/usr/bin/env python3
"""Keep one MangoHud config line updated with TuxThrottle's live state (Game
Mode / fan boost / temps) so it shows in the in-game overlay without ever
alt-tabbing out. Stdlib only — meant to be polled from the tray (already a
persistent background process reading sensors every 2s).

MangoHud watches its config file (inotify) and hot-reloads on change — the
existing MangoHud box in tuxthrottle.py already relies on this (atomic
replace, see _mh_write_conf's comment), so a periodic rewrite of just the
`custom_text_center=` line is enough; no game restart needed.

Enable/disable state is a tiny separate file (~/.config/tuxthrottle/
mangohud_status_line.json) so the GUI checkbox and the tray's poll loop
agree on whether this is on, without either needing the other's process.

    tuxthrottle_mangohud_status.py enable
    tuxthrottle_mangohud_status.py disable
    tuxthrottle_mangohud_status.py status
    tuxthrottle_mangohud_status.py update "<text>"
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

STATUS_KEY = "custom_text_center"


def _config_home(user: str | None = None) -> Path:
    if user:
        return Path(os.path.expanduser(f"~{user}")) / ".config"
    return Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))


def _mangohud_conf_path(user: str | None = None) -> Path:
    return _config_home(user) / "MangoHud" / "MangoHud.conf"


def _state_path(user: str | None = None) -> Path:
    return _config_home(user) / "tuxthrottle" / "mangohud_status_line.json"


def is_enabled(user: str | None = None) -> bool:
    try:
        return bool(json.loads(_state_path(user).read_text()).get("enabled"))
    except (OSError, ValueError):
        return False


def set_enabled(value: bool, user: str | None = None) -> None:
    p = _state_path(user)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(value)}))
        if user and os.geteuid() == 0:
            import pwd
            pw = pwd.getpwnam(user)
            os.chown(p, pw.pw_uid, pw.pw_gid)
            os.chown(p.parent, pw.pw_uid, pw.pw_gid)
    except (OSError, KeyError):
        pass
    if not value:
        clear_status_line(user)


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tuxthrottle-tmp")
    tmp.write_text(("\n".join(lines) + "\n") if lines else "")
    os.replace(tmp, path)


def set_status_line(text: str, user: str | None = None) -> None:
    """Rewrite (or insert) the custom_text_center= line, leaving every other
    line in MangoHud.conf untouched. No-op if the feature isn't enabled."""
    if not is_enabled(user):
        return
    path = _mangohud_conf_path(user)
    lines = path.read_text().splitlines() if path.is_file() else []
    new_line = f"{STATUS_KEY}={text}"
    out, replaced = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == STATUS_KEY:
            if not replaced:
                out.append(new_line)
                replaced = True
            continue      # drop any duplicate occurrences
        out.append(ln)
    if not replaced:
        out.append(new_line)
    try:
        _atomic_write_lines(path, out)
    except OSError:
        pass


def clear_status_line(user: str | None = None) -> None:
    path = _mangohud_conf_path(user)
    if not path.is_file():
        return
    lines = [ln for ln in path.read_text().splitlines()
            if ln.split("=", 1)[0].strip() != STATUS_KEY]
    try:
        _atomic_write_lines(path, lines)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("status")
    up = sub.add_parser("update")
    up.add_argument("text")
    a = ap.parse_args()

    if a.cmd == "enable":
        set_enabled(True, user=a.user)
    elif a.cmd == "disable":
        set_enabled(False, user=a.user)
    elif a.cmd == "status":
        print("on" if is_enabled(a.user) else "off")
    elif a.cmd == "update":
        set_status_line(a.text, user=a.user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
