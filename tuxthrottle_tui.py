#!/usr/bin/env python3
"""TuxThrottle TUI — a headless/SSH-friendly dashboard, built alongside the
Tkinter GUI rather than instead of it. The GUI stays the primary interface
for anything visually interactive (RGB colour picking, the drag-to-edit fan
curve, the MangoHud position picker, file browsers) — none of that maps
cleanly onto a terminal. This is the "I'm SSHed into the laptop and just
want to see temps / flip Game Mode / check what TuxThrottle quietly fixed"
tool, the same job the tray icon does on the desktop.

Reads sensors directly (unprivileged, same as tray_monitor.py — reading
hwmon/sysfs needs no root). Writes (Game Mode, fan boost) route through
`tuxthrottlectl` with pkexec/sudo, exactly like the tray's `_ctl()` — this
process never needs to run elevated itself.

    tuxthrottle_tui.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import sensors  # noqa: E402
import tuxthrottle_crashwatch as crashwatch  # noqa: E402
import tuxthrottle_fixlog as fixlog  # noqa: E402

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Button, Footer, Header, Static
except ImportError:
    print("textual not found. Install with: dnf install python3-textual python3-rich")
    print("(or: pip install --user textual rich)")
    sys.exit(1)


def _ctl(*args: str) -> tuple[bool, str]:
    """Run `tuxthrottlectl <args>` with privilege — mirrors tray_monitor.py's
    _ctl() exactly, so Game Mode / fan-boost behave identically from the TUI,
    the tray, and the GUI."""
    ctl = shutil.which("tuxthrottlectl") or "/usr/local/bin/tuxthrottlectl"
    last = ""
    for launcher in (["pkexec"], ["sudo", "-n"], []):
        if launcher and not shutil.which(launcher[0]):
            continue
        try:
            r = subprocess.run([*launcher, ctl, *args],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc)
        if r.returncode == 0:
            return True, (r.stdout or "").strip()
        last = (r.stderr or r.stdout or "").strip()
    return False, last or "no launcher found (need pkexec or passwordless sudo)"


class ValueStatic(Static):
    """A Static that tracks its own last-set text in `.value` — Static's
    internal renderable storage isn't a stable attribute across Textual
    versions (confirmed: differs between the pip "textual" on PyPI and the
    one Fedora/Nobara packages), so tests/CI check `.value`, not internals."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.value = ""

    def update(self, content="") -> None:  # type: ignore[override]
        self.value = str(content)
        super().update(content)


class StatBox(ValueStatic):
    """A single labelled stat panel, e.g. 'CPU' / '62°C, 2.9 GHz'."""

    def __init__(self, label: str, **kw):
        super().__init__("", **kw)
        self._label = label

    def set_value(self, value: str) -> None:
        self.update(f"[bold]{self._label}[/bold]\n[accent]{value}[/accent]")


class TuxThrottleTUI(App):
    """Dark background, one accent colour (cyan) — the "gamer" look without
    fighting the terminal for anything fancier than that."""

    TITLE = "TuxThrottle"
    SUB_TITLE = "Dell G15 — live dashboard"
    CSS = """
    Screen { background: #0b0e11; }
    #stats { height: auto; padding: 1; }
    StatBox {
        border: round #1f2a33;
        padding: 1 2;
        margin: 0 1 1 0;
        width: 1fr;
        height: 6;
        content-align: center middle;
        color: #d8e2e8;
    }
    StatBox .accent { color: #29f0d6; }
    #actions { height: auto; padding: 0 1; }
    #actions Button { margin: 0 1 1 0; }
    #fixes { border: round #1f2a33; margin: 1; padding: 1; height: 1fr; }
    #fixes_title { color: #29f0d6; text-style: bold; }
    .accent { color: #29f0d6; }
    Button.-success { background: #135; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("g", "toggle_gamemode", "Toggle Game Mode"),
        ("r", "refresh_now", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal(id="stats"):
                self.cpu_box = StatBox("CPU", id="cpu_box")
                self.igpu_box = StatBox("iGPU", id="igpu_box")
                self.dgpu_box = StatBox("dGPU", id="dgpu_box")
                self.gamemode_box = StatBox("Game Mode", id="gamemode_box")
                yield self.cpu_box
                yield self.igpu_box
                yield self.dgpu_box
                yield self.gamemode_box
            with Horizontal(id="actions"):
                yield Button("Toggle Game Mode (g)", id="btn_gamemode", variant="success")
                yield Button("Fan boost: Off", id="btn_fan_0")
                yield Button("Fan boost: Half", id="btn_fan_50")
                yield Button("Fan boost: Max", id="btn_fan_100")
            with VerticalScroll(id="fixes"):
                yield Static("Recent fixes / crash-watch findings", id="fixes_title")
                self.fixes_log = ValueStatic("")
                yield self.fixes_log
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_stats()
        self.refresh_fixes()
        self.set_interval(2.0, self.refresh_stats)
        self.set_interval(15.0, self.scan_and_refresh_fixes)

    # ---------- read side (unprivileged, direct sensors.py reads) ----------

    def refresh_stats(self) -> None:
        cpu_power = sensors.read_cpu_power_watts()
        power_txt = f", {cpu_power:.1f} W" if cpu_power is not None else ""
        self.cpu_box.set_value(
            f"{sensors.read_cpu_temp_c()}, {sensors.read_cpu_freq_ghz()}{power_txt}")
        self.igpu_box.set_value(sensors.read_igpu_clock_temp())
        self.dgpu_box.set_value(sensors.read_dgpu_clock_temp_util())
        on = sensors.get_game_mode_state()
        self.gamemode_box.set_value("ON" if on else "OFF")

    def refresh_fixes(self) -> None:
        entries = fixlog.read_recent(12)
        if not entries:
            self.fixes_log.update("(nothing logged yet)")
            return
        import time
        lines = []
        for e in entries:
            when = time.strftime("%H:%M", time.localtime(e.get("ts", 0)))
            level = e.get("level", "info")
            colour = {"warn": "yellow", "error": "red"}.get(level, "dim")
            lines.append(f"[{colour}]{when}  {e.get('source', '?'):16} "
                        f"{e.get('message', '')}[/{colour}]")
        self.fixes_log.update("\n".join(lines))

    def scan_and_refresh_fixes(self) -> None:
        try:
            findings = crashwatch.scan(since_seconds=16)
            for f in findings:
                fixlog.log_event("crashwatch", f["label"],
                                 level="info" if f.get("benign") else "warn")
        except Exception:  # noqa: BLE001 — never take the TUI down over this
            pass
        self.refresh_fixes()

    # ---------- write side (privileged, via tuxthrottlectl) ----------

    def action_toggle_gamemode(self) -> None:
        self._toggle_gamemode()

    def action_refresh_now(self) -> None:
        self.refresh_stats()
        self.refresh_fixes()

    def _toggle_gamemode(self) -> None:
        ok, msg = _ctl("gamemode", "toggle")
        if not ok:
            self.notify(f"Game Mode change failed: {msg}", severity="error")
        self.refresh_stats()

    def _set_fan_boost(self, pct: int) -> None:
        ok, msg = _ctl("set", "fan-boost", "both", str(pct))
        if not ok:
            self.notify(f"Fan boost failed: {msg}", severity="error")
        else:
            self.notify(f"Fan boost -> {pct}%")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_gamemode":
            self._toggle_gamemode()
        elif bid == "btn_fan_0":
            self._set_fan_boost(0)
        elif bid == "btn_fan_50":
            self._set_fan_boost(50)
        elif bid == "btn_fan_100":
            self._set_fan_boost(100)


def main() -> int:
    TuxThrottleTUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
