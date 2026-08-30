#!/usr/bin/env python3
"""Panel-applet tweaks for KDE Plasma 6 that are too fiddly for a tweaks.json
one-liner — finding digital-clock applet IDs in
`plasma-org.kde.plasma.desktop-appletsrc` and swapping the launcher plugin.

Stdlib only. Runs as the invoking user (the tweak wraps it with the right
HOME / XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS). Writes go through
`kwriteconfig6` so the KConfig format stays intact; the launcher swap is a
plain text substitution. Every action restarts plasmashell so it applies and
doesn't flush a stale in-memory copy back over the file.

  tuxthrottle_kde_panel.py clock-seconds {on|off}
  tuxthrottle_kde_panel.py classic-menu  {on|off}
"""
import os
import re
import subprocess
import sys
from pathlib import Path

APPLETS = "plasma-org.kde.plasma.desktop-appletsrc"


def _cfg_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APPLETS


_HDR = re.compile(r"^\[Containments\]\[(\d+)\]\[Applets\]\[(\d+)\]$")


def find_applets(plugin: str) -> list[tuple[str, str]]:
    """[(containment_id, applet_id)] for every applet with this plugin."""
    p = _cfg_path()
    if not p.is_file():
        return []
    out, hdr = [], None
    for line in p.read_text(errors="ignore").splitlines():
        m = _HDR.match(line)
        if m:
            hdr = (m.group(1), m.group(2))
        elif line.strip() == f"plugin={plugin}" and hdr:
            out.append(hdr)
    return out


def _kwrite(cid: str, aid: str, group_chain: list[str], key: str, value):
    args = ["kwriteconfig6", "--file", APPLETS,
            "--group", "Containments", "--group", cid,
            "--group", "Applets", "--group", aid]
    for g in group_chain:
        args += ["--group", g]
    args += ["--key", key]
    args += ["--delete"] if value is None else [str(value)]
    subprocess.run(args, check=False)


def clock_seconds(enable: bool) -> int:
    applets = find_applets("org.kde.plasma.digitalclock")
    if not applets:
        print("no digital-clock applet in the panel config", file=sys.stderr)
        return 0  # nothing to do isn't an error
    for cid, aid in applets:
        _kwrite(cid, aid, ["Configuration", "Appearance"], "showSeconds",
                2 if enable else None)
    print(f"showSeconds {'2' if enable else 'default'} on {len(applets)} clock applet(s)")
    return 0


def classic_menu(enable: bool) -> int:
    p = _cfg_path()
    if not p.is_file():
        print("panel config not found", file=sys.stderr)
        return 1
    text = p.read_text(errors="ignore")
    if enable:
        new = re.sub(r"^plugin=org\.kde\.plasma\.(kickoff|kickerdash)$",
                     "plugin=org.kde.plasma.kicker", text, flags=re.M)
    else:
        new = re.sub(r"^plugin=org\.kde\.plasma\.kicker$",
                     "plugin=org.kde.plasma.kickoff", text, flags=re.M)
    if new == text:
        print("launcher plugin already as requested")
        return 0
    p.write_text(new)
    print("launcher plugin -> " + ("kicker (classic menu)" if enable else "kickoff"))
    return 0


def restart_plasmashell() -> None:
    r = subprocess.run(
        ["systemctl", "--user", "restart", "plasma-plasmashell.service"],
        capture_output=True)
    if r.returncode == 0:
        return
    subprocess.run(["kquitapp6", "plasmashell"], capture_output=True)
    subprocess.run(["sh", "-c", "sleep 1; setsid kstart plasmashell "
                    ">/dev/null 2>&1 &"], check=False)


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in ("on", "off"):
        print(__doc__)
        return 2
    action, state = sys.argv[1], sys.argv[2] == "on"
    if action == "clock-seconds":
        rc = clock_seconds(state)
    elif action == "classic-menu":
        rc = classic_menu(state)
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    restart_plasmashell()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
