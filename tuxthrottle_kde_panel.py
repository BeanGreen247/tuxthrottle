#!/usr/bin/env python3
"""Panel-applet tweaks for KDE Plasma 6 that are too fiddly for a tweaks.json
one-liner — finding digital-clock applet IDs in
`plasma-org.kde.plasma.desktop-appletsrc` and swapping the launcher plugin.

Stdlib only. Runs as the invoking user (the tweak wraps it with the right
HOME / XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS). Writes go through
`kwriteconfig6` so the KConfig format stays intact; the launcher swap is a
plain text substitution. Every action restarts plasmashell so it applies and
doesn't flush a stale in-memory copy back over the file.

  tuxthrottle_kde_panel.py clock-seconds  {on|off}
  tuxthrottle_kde_panel.py classic-menu   {on|off}
  tuxthrottle_kde_panel.py panel-floating {on|off}
  tuxthrottle_kde_panel.py battery-tray   {on|off}
  tuxthrottle_kde_panel.py launcher-power {on|off}
"""
import os
import re
import subprocess
import sys
import time
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


def _kread(groups: list[str], key: str) -> str:
    args = ["kreadconfig6", "--file", APPLETS]
    for g in groups:
        args += ["--group", g]
    args += ["--key", key]
    r = subprocess.run(args, capture_output=True, text=True)
    return (r.stdout or "").strip()


def _csv(s: str) -> list[str]:
    return [x for x in (p.strip() for p in s.split(",")) if x]


def _stop_plasmashell() -> None:
    """Fully stop plasmashell BEFORE editing the appletsrc — a running
    plasmashell flushes its in-memory copy on exit and would clobber the edit
    (the reason the first version of this 'did nothing'). main()'s trailing
    restart_plasmashell() then brings it back up fresh."""
    if subprocess.run(["systemctl", "--user", "stop",
                       "plasma-plasmashell.service"],
                      capture_output=True).returncode != 0:
        subprocess.run(["kquitapp6", "plasmashell"], capture_output=True)
    time.sleep(2)


def systray_show(plugin: str, enable: bool) -> int:
    """Force a system-tray item to 'Always shown' (enable) or back to 'auto'
    (disable) by editing shownItems / hiddenItems on every
    org.kde.plasma.systemtray applet.

    The keys live in `[Containments][C][Applets][A][General]` — the same group
    as `knownItems` / `extraItems` — NOT `[Configuration][General]` (writing
    there is what the first version got wrong). plasmashell is stopped first so
    it can't overwrite the file on the way down."""
    applets = find_applets("org.kde.plasma.systemtray")
    if not applets:
        print("no system-tray applet in the panel config", file=sys.stderr)
        return 0
    _stop_plasmashell()
    for cid, aid in applets:
        base = ["Containments", cid, "Applets", aid, "General"]
        shown = _csv(_kread(base, "shownItems"))
        hidden = _csv(_kread(base, "hiddenItems"))
        if enable:
            if plugin not in shown:
                shown.append(plugin)
            hidden = [x for x in hidden if x != plugin]
        else:
            shown = [x for x in shown if x != plugin]
        _kwrite(cid, aid, ["General"], "shownItems",
                ",".join(shown) if shown else None)
        _kwrite(cid, aid, ["General"], "hiddenItems",
                ",".join(hidden) if hidden else None)
        # scrub the wrong-group key an earlier build may have written
        _kwrite(cid, aid, ["Configuration", "General"], "shownItems", None)
        _kwrite(cid, aid, ["Configuration", "General"], "hiddenItems", None)
    print(f"{plugin} -> {'always shown' if enable else 'auto'} "
          f"in {len(applets)} system tray(s)")
    return 0


_LAUNCHER_PLUGINS = ("org.kde.plasma.kickoff", "org.kde.plasma.kicker",
                     "org.kde.plasma.kickerdash")
# the full session/power footer for the app launcher, in the usual order.
# Kickoff reads `systemFavorites`; the classic Kicker menu reads a *different*
# key, `favoriteSystemActions` — writing only the first is why the classic
# menu still showed just 'Log Out'. Write both.
_SYSTEM_FAVORITES = ("suspend,hibernate,reboot,shutdown,lock-screen,"
                     "logout,save-session,switch-user")
_KICKER_SYSTEM_ACTIONS = ("lock-screen,logout,save-session,switch-user,"
                          "suspend,hibernate,reboot,shutdown")


def _restore_launcher_power(enable: bool) -> int:
    """Set (or clear) the power/session button row on every launcher applet.
    Assumes plasmashell is already stopped by the caller."""
    targets = [(plug, cid, aid) for plug in _LAUNCHER_PLUGINS
               for cid, aid in find_applets(plug)]
    for _plug, cid, aid in targets:
        _kwrite(cid, aid, ["Configuration", "General"], "systemFavorites",
                _SYSTEM_FAVORITES if enable else None)
        _kwrite(cid, aid, ["Configuration", "General"], "favoriteSystemActions",
                _KICKER_SYSTEM_ACTIONS if enable else None)
        # Plasma sets this after migrating favorites and won't re-populate the
        # row while it's true — drop it so our values are what Kicker loads.
        if enable:
            _kwrite(cid, aid, ["Configuration", "General"],
                    "favoritesPortedToKAstats", None)
    return len(targets)


def launcher_power(enable: bool) -> int:
    """Restore the full power/session button row (Sleep, Hibernate, Restart,
    Shut Down, Lock, Log Out, …) in the KDE application launcher — Kickoff or
    the classic Kicker menu. plasmashell owns these keys, so stop it first or
    it writes the old values back on exit."""
    if not any(find_applets(p) for p in _LAUNCHER_PLUGINS):
        print("no application-launcher applet in the panel config", file=sys.stderr)
        return 0
    _stop_plasmashell()
    n = _restore_launcher_power(enable)
    print(f"launcher power buttons -> {'full set' if enable else 'default'} "
          f"on {n} launcher(s)")
    return 0


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
    _stop_plasmashell()          # so it can't flush a stale copy over our edits
    text = p.read_text(errors="ignore")
    if enable:
        new = re.sub(r"^plugin=org\.kde\.plasma\.(kickoff|kickerdash)$",
                     "plugin=org.kde.plasma.kicker", text, flags=re.M)
    else:
        new = re.sub(r"^plugin=org\.kde\.plasma\.kicker$",
                     "plugin=org.kde.plasma.kickoff", text, flags=re.M)
    if new != text:
        p.write_text(new)
        print("launcher plugin -> " + ("kicker (classic menu)" if enable else "kickoff"))
    else:
        print("launcher plugin already as requested")
    # switching to the classic Kicker menu triggers a Plasma favorites
    # migration that often trims the power row to just 'Log Out' — restore
    # the full session/power buttons so this tweak doesn't cause that.
    if enable:
        _restore_launcher_power(True)
    return 0


_CONT_HDR = re.compile(r"^\[Containments\]\[(\d+)\](\[.+\])?$")


def find_panels() -> list[str]:
    """[containment_id] for every org.kde.panel containment (the panels)."""
    p = _cfg_path()
    if not p.is_file():
        return []
    out, cur, is_panel = [], None, False
    for line in p.read_text(errors="ignore").splitlines():
        m = _CONT_HDR.match(line)
        if m and not m.group(2):          # a new top-level [Containments][N]
            if cur is not None and is_panel:
                out.append(cur)
            cur, is_panel = m.group(1), False
        elif line.strip() == "plugin=org.kde.panel" and cur is not None:
            is_panel = True
    if cur is not None and is_panel:
        out.append(cur)
    return out


def _plasmashell_view_groups(cid: str) -> list[list[str]]:
    """Group chains in plasmashellrc that hold this panel's view state.

    Plasma 6 keeps the *effective* panel geometry (the floating gap) in
    `plasmashellrc` under `[PlasmaViews][Panel <cid>]` (and sometimes a
    per-screen `[PlasmaViews][Panel <cid>][Screen N]` child), NOT in the
    appletsrc containment. Writing only the appletsrc key leaves the panel
    visually floating — this was the "flush tweak does nothing" bug.
    """
    base = ["PlasmaViews", f"Panel {cid}"]
    groups = [base]
    rc = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) \
        / "plasmashellrc"
    if rc.is_file():
        pat = re.compile(rf"^\[PlasmaViews\]\[Panel {re.escape(cid)}\]\[([^\]]+)\]$")
        for line in rc.read_text(errors="ignore").splitlines():
            m = pat.match(line)
            if m and m.group(1) != "Defaults":
                groups.append(base + [m.group(1)])
    return groups


def panel_floating(enable: bool) -> int:
    """floating panel on = the ~few-mm gap from the screen edge; off = flush."""
    panels = find_panels()
    if not panels:
        print("no panel containment in the desktop config", file=sys.stderr)
        return 0  # nothing to do isn't an error
    for cid in panels:
        # 1. the appletsrc containment hint (true/false)
        subprocess.run(
            ["kwriteconfig6", "--file", APPLETS,
             "--group", "Containments", "--group", cid, "--group", "General",
             "--key", "floating", "true" if enable else "false"],
            check=False)
        # 2. the plasmashellrc view state that actually drives the gap (1/0)
        for chain in _plasmashell_view_groups(cid):
            args = ["kwriteconfig6", "--file", "plasmashellrc"]
            for g in chain:
                args += ["--group", g]
            args += ["--key", "floating", "1" if enable else "0"]
            subprocess.run(args, check=False)
    print(f"panel floating -> {'on' if enable else 'off (flush)'} "
          f"on {len(panels)} panel(s)")
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
    elif action == "panel-floating":
        rc = panel_floating(state)
    elif action == "battery-tray":
        rc = systray_show("org.kde.plasma.battery", state)
    elif action == "launcher-power":
        rc = launcher_power(state)
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        return 2
    restart_plasmashell()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
