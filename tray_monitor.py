#!/usr/bin/env python3
"""Dell G15 tray monitor — CPU/GPU clocks & temps + Game Mode toggle.

The "live dashboard + dedicated-key toggle" piece of this Toolkit, inspired
by Div-Acer-Manager-Max's (DAMX) monitoring dashboard and Nitro/PredatorSense
button binding: https://github.com/PXDiv/Div-Acer-Manager-Max

Runs as a normal user process (unprivileged) and lives in the system tray
via Qt's StatusNotifierItem support (native on KDE Plasma). Only the actual
Game Mode toggle shells out through pkexec/sudo (see sensors.set_game_mode),
since that needs root — the monitor itself never needs elevation.

All sensor reads and the Game Mode toggle live in sensors.py (no GUI
dependency), shared with tuxthrottle.py's in-app Dashboard tab.

Requires: PySide6 (dnf install python3-pyside6, or: pip install --user PySide6)
Reuses the gaming-performance/gaming-balanced, amdgpu-perf-high/auto, and
nvidia-max-perf helper scripts installed by tuxthrottle.py's tweaks —
install those first (Presets > Safe Baseline covers the power-profile ones;
Competitive Gaming covers the GPU perf-state ones).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon, QAction
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
except ImportError:
    print("PySide6 not found. Install with: dnf install python3-pyside6")
    print("(or: pip install --user PySide6)")
    sys.exit(1)

POLL_MS = 2000


class TrayMonitor:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        icon_file = Path(__file__).resolve().parent / "assets" / "icon-256.png"
        icon = QIcon(str(icon_file)) if icon_file.is_file() else QIcon()
        if icon.isNull():
            icon = QIcon.fromTheme("utilities-system-monitor")
        if icon.isNull():
            icon = QIcon.fromTheme("computer")
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("Dell G15 5515 Monitor")

        self.menu = QMenu()
        self.cpu_action = self._info_action("CPU: …")
        self.igpu_action = self._info_action("iGPU: …")
        self.dgpu_action = self._info_action("dGPU: …")
        self.rapl_warning_action = self._info_action("")
        self.rapl_warning_action.setVisible(False)
        self.menu.addSeparator()

        self.gamemode_action = QAction("Game Mode", checkable=True)
        self.gamemode_action.toggled.connect(self._on_gamemode_toggled)
        self.menu.addAction(self.gamemode_action)

        self.menu.addSeparator()
        quit_action = QAction("Quit")
        quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(quit_action)

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self._suppress_toggle_signal = False
        self._refresh()
        self._sync_gamemode_state()

        self.timer = QTimer()
        self.timer.timeout.connect(self._refresh)
        self.timer.start(POLL_MS)

        self.state_timer = QTimer()
        self.state_timer.timeout.connect(self._sync_gamemode_state)
        self.state_timer.start(10_000)

    def _info_action(self, text: str) -> QAction:
        a = QAction(text)
        a.setEnabled(False)
        self.menu.addAction(a)
        return a

    def _refresh(self):
        cpu_power = sensors.read_cpu_power_watts()
        power_txt = f", {cpu_power:.1f} W" if cpu_power is not None else ""
        self.cpu_action.setText(f"CPU: {sensors.read_cpu_freq_ghz()}, {sensors.read_cpu_temp_c()}{power_txt}")
        self.igpu_action.setText(f"iGPU: {sensors.read_igpu_clock_temp()}")
        self.dgpu_action.setText(f"dGPU: {sensors.read_dgpu_clock_temp_util()}")
        if sensors.rapl_permissions_ok():
            self.rapl_warning_action.setVisible(False)
        else:
            self.rapl_warning_action.setText("⚠ CPU power locked — install RaplPowerPermissions tweak")
            self.rapl_warning_action.setVisible(True)
        self.tray.setToolTip(
            f"CPU {sensors.read_cpu_temp_c()} | dGPU {sensors.read_dgpu_clock_temp_util()}"
        )

    def _sync_gamemode_state(self):
        state = sensors.get_game_mode_state()
        self._suppress_toggle_signal = True
        self.gamemode_action.setChecked(state)
        self._suppress_toggle_signal = False

    def _on_gamemode_toggled(self, checked: bool):
        if self._suppress_toggle_signal:
            return
        ok, err = sensors.set_game_mode(checked)
        if not ok:
            QMessageBox.warning(None, "Game Mode", f"Failed: {err}")
            self._sync_gamemode_state()
        self._refresh()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.gamemode_action.setChecked(not self.gamemode_action.isChecked())

    def run(self):
        sys.exit(self.app.exec())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--toggle":
        ok, err = sensors.toggle_game_mode_external()
        if not ok:
            print(f"Toggle failed: {err}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    TrayMonitor().run()
