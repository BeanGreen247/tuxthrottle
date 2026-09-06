#!/usr/bin/env python3
"""About tab — extracted from tuxthrottle.py (module-split pass,
eighth slice)."""
import os
import pwd
import shutil
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import INFO, SECONDARY, WARNING

import sensors
from tuxthrottle_items import BASE_DIR, PROJECT_ISSUES_URL, PROJECT_URL, toolkit_version


class AboutTabMixin:
    def _build_about_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="About", pin=True)
        frame = self._scroll_body(outer, pad=20)

        head = tb.Frame(frame)
        head.pack(fill="x", pady=(0, 12))
        if getattr(self, "_icon_img", None) is not None:
            try:
                big = self._icon_img.subsample(max(1, self._icon_img.width() // 72))
                tb.Label(head, image=big).pack(side="left", padx=(0, 14))
                self._about_icon = big  # keep a ref
            except tk.TclError:
                pass
        tbox = tb.Frame(head); tbox.pack(side="left", anchor="n")
        tb.Label(tbox, text="TuxThrottle", font=("Sans", 20, "bold")).pack(anchor="w")
        tb.Label(tbox, text=f"version {toolkit_version()}", bootstyle=SECONDARY,
                 font=("Monospace", 10)).pack(anchor="w")

        tb.Label(frame, wraplength=1000, justify="left", text=(
            "A checkbox-driven GUI, tray monitor and G-key listener that applies "
            "hardware-specific tweaks, drivers and gaming setup to the Dell G15 5515 "
            "Ryzen Edition (Ryzen 7 5800H + RTX 3050 Ti Mobile) running Nobara Linux. "
            "Every check/apply command is written against that board — it is not a "
            "general-purpose distro tool.")).pack(anchor="w", pady=(0, 10))

        # "What's inside" — a click-to-expand dropdown listing every section
        feat = tb.Frame(frame)
        feat.pack(fill="x", pady=(4, 8))
        self._about_open = False
        self._about_btn = tb.Button(feat, text="▸   What's inside  —  click to see every section",
                                    style="Disclosure.TButton", takefocus=False,
                                    command=self._toggle_about_features)
        self._about_btn.pack(fill="x")
        self._about_body = tb.Frame(feat, style="Card.TFrame", padding=(16, 12, 12, 12))
        for name, desc in (
            ("Dashboard", "8 live ring gauges (2×4) — CPU temp/clock/power, iGPU clock, dGPU temp/clock/util/power — with rolling sparkline history, a session CSV log and a Game Mode toggle; built lazily on tab entry"),
            ("Keyboard", "Alienware AW-ELC RGB via OpenRGB — whole-keyboard solid colour, presets, brightness, firmware Spectrum Cycle; two mutually-exclusive toggles that sync the colour with the KDE accent (keyboard→accent or accent→keyboard); colour re-asserted at login by the tray / KbdBacklightFix service"),
            ("Fans", "thermal profile (balanced/performance/custom), additive fan boost + Silent/Balanced/Aggressive presets, guarded manual PWM, and a 10-point closed-loop custom fan curve run by the daemon"),
            ("Power & Limits", "CPU TDP sliders (ryzenadj STAPM/fast/slow) + presets; Curve Optimizer all-core undervolt with a 5-min stress-test + auto-revert harness; NVIDIA power-limit slider or a firmware-locked note; NVIDIA graphics-clock lock; hybrid-graphics mode (EnvyControl); battery charge limit (sysfs / Dell libsmbios) + express/standard charging; panel refresh-rate switch; AC↔battery auto-switch for profile/TDP/refresh; thermal-event alerts"),
            ("Battery", "design-vs-full wear %, charge cycles, chemistry; a live Now card (charge, power flow, time-to-empty/full); the charge-limit control mirrored from Power & Limits; an Adaptive-Sync (VRR) status line"),
            ("VRAM", "live per-GPU video-memory bars + top consumers; a Regular/Medium/Extreme KWin budget that strips desktop eye-candy to shrink the compositor footprint (reversible to a captured baseline); a Free-VRAM action (AMD/Intel driver eviction + optional compositor restart); a desktop-GPU selector (KWIN_DRM_DEVICES); a dGPU runtime-power-management toggle"),
            ("Profiles", "capture / apply / delete named full-state bundles (profile, TDP, battery, NVIDIA limits, fan curve, refresh, hybrid GPU, keyboard); an automatic snapshot before every apply with per-row + latest rollback; a per-game auto-profile map and a time-of-day schedule run by the daemon"),
            ("Presets", "one-click curated bundles of tweaks + app installs — Safe Baseline, Competitive Gaming, Streaming Rig, Game Launchers, and Maximum Performance (aggressive: mitigations-off / PCIe-NVMe-latency kernel args, forced governors, NVIDIA max-PowerMizer + PAT/ReBAR, RADV-GPL GPU env, RT-priority IRQ threads, masked idle services — no fan/thermal changes) — plus a global “apply all recommended” button"),
            ("Updates", "nobara-sync wrapper (check / cli / install / fixups / repair) + per-manager dnf, Flatpak and fwupd sections and a Fedora-GPG-key fix; pending count tagged with the metadata age"),
            ("Setup Games", "per-game click-through walkthroughs (GTA V Online first) — each step has a status pill and either a streamed Run button or a manual Copy-command step"),
            ("Game Tools", "any-game Steam/Proton helpers — Proton-prefix relocation off NTFS/exFAT, a save-game vault, one shared shader/pipeline-cache folder with Steam-link repair plus a force-rebuild-Steam's-shader-cache button and a background-Vulkan-shader-processing switch, a Steam-client low-resource mode (CEF flags + a soft memory-cap systemd scope + no-auto-chat + hidden-on-login autostart), a launch-options builder (MangoHud / gamemoderun / gamescope / PRIME / shader caches / ntsync / anti-cheat-safe layer set) with an Apply-to-every-game action, and a full MangoHud overlay editor (per-GPU fields, drag-to-place, Feral-GameMode status line, per-game configs)"),
            ("Tweaks & Apps", "reversible system tweaks by category — Gaming, GPU, Power, Performance (curated + aggressive extras: mitigations-off / PCIe-NVMe kernel args, VM-writeback sysctls, NVIDIA aggressive module options, RADV-GPL GPU env, RT-priority IRQ threads, ananicy-cpp, idle-service masking, quiet-GameMode), KDE (14 Plasma 6 toggles), Stability — each with check/undo; plus one-directional native/Flatpak app installs with cross-manager “already installed” detection"),
            ("System tray", "an always-on PySide6 tray icon — left-click opens this window, middle-click toggles Game Mode, right-click shows live CPU/GPU readouts and quick actions; an About-tab toggle adds/removes it from login autostart"),
            ("tuxthrottled", "systemd daemon: closed-loop fan curve, AC↔battery auto-switch, per-game auto-profiles with a post-game summary, a time-of-day schedule, thermal-event notifications and fan-stall auto-recovery, and a root-only control socket the GUI + CLI write through"),
            ("tuxthrottlectl", "headless CLI (status / watch / get / set / profile / snapshot / rollback / gamemode / schedule / daemon / vram / collect-model, --json) for scripts, keybinds and ssh; routes through the daemon socket when it's up"),
            ("Panel clients", "optional waybar module, KDE plasmoid and MangoHud bridge showing CPU/GPU temp + a one-click profile switch (clients/, over tuxthrottlectl --json)"),
            ("Packaging", "noarch RPM spec + a COPR workflow (packaging/) for a dnf install; the git-clone install.sh path still works"),
            ("Report a Bug", "read-only hardware / OS dump + a hardware-bundle tarball for GitHub issues and new-board onboarding"),
        ):
            row = tb.Frame(self._about_body, style="CardRow.TFrame")
            row.pack(anchor="w", fill="x", pady=2)
            tb.Label(row, text=f"▸  {name}", font=("Sans", 10, "bold"),
                     width=16, anchor="w", style="CardKey.TLabel").pack(side="left", anchor="n")
            tb.Label(row, text=desc, wraplength=900, justify="left",
                     style="Card.TLabel").pack(side="left", anchor="n")
        tb.Label(self._about_body, style="Card.TLabel", wraplength=900,
                 justify="left", text=(
                     "\nFEATURES.md in the repo has the full, detailed list "
                     "with examples for every control.")).pack(anchor="w")

        link = tb.Labelframe(frame, text="Project", padding=12)
        link.pack(fill="x", pady=6)
        row = tb.Frame(link); row.pack(fill="x")
        tb.Button(row, text="Open on GitHub", bootstyle=INFO,
                  command=lambda: self._open_url(PROJECT_URL)).pack(side="left")
        tb.Button(row, text="Report an issue", bootstyle=(WARNING, "outline"),
                  command=lambda: self._open_url(PROJECT_ISSUES_URL)).pack(side="left", padx=8)
        tb.Button(row, text="Copy link", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._copy_text(PROJECT_URL)).pack(side="left")
        url_ent = tk.Entry(link, font=("Monospace", 10), relief="flat",
                           readonlybackground="#0e1116", fg="#c9d1d9", bd=0)
        url_ent.insert(0, PROJECT_URL)
        url_ent.configure(state="readonly")
        url_ent.pack(fill="x", pady=(8, 0))

        tray = tb.Labelframe(frame, text="System tray", padding=12)
        tray.pack(fill="x", pady=6)
        tb.Label(tray, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "A small tray icon (left-click opens this window, middle-click "
            "toggles Game Mode, right-click for CPU/GPU readouts + quick "
            "actions). Needs PySide6.")).pack(anchor="w", pady=(0, 8))
        self._tray_auto_var = tk.BooleanVar(value=self._tray_autostart_enabled())
        tb.Checkbutton(
            tray, text="Start the tray icon automatically at login",
            variable=self._tray_auto_var,
            command=self._tray_toggle_autostart).pack(anchor="w")
        btnrow = tb.Frame(tray)
        btnrow.pack(fill="x", pady=(8, 0))
        b = tb.Button(btnrow, text="Launch tray now", bootstyle=(INFO, "outline"),
                      command=self._tray_launch_now)
        b.pack(side="left")
        self._tray_status_lbl = tb.Label(btnrow, text="", bootstyle=SECONDARY)
        self._tray_status_lbl.pack(side="left", padx=10)

        meta = tb.Labelframe(frame, text="Details", padding=12)
        meta.pack(fill="x", pady=6)
        m = sensors.detect_model()
        for k, v in (
            ("Target hardware", "Dell G15 5515 Ryzen Edition (0R3CDX)"),
            ("This machine", f"{m['vendor']} {m['product']}"
                             + (f", BIOS {m['bios']}" if m['bios'] else "")),
            ("Distro target", "Nobara Linux (Fedora 43 base, KDE Plasma / Wayland)"),
            ("Status", "developed and tested live on the target hardware"),
            ("License", "MIT — © 2026 BeanGreen247"),
            ("Install path", "/opt/tuxthrottle"),
        ):
            r = tb.Frame(meta); r.pack(fill="x", pady=1)
            tb.Label(r, text=f"{k}:", width=16, anchor="w", bootstyle=SECONDARY).pack(side="left")
            tb.Label(r, text=v, wraplength=880, justify="left").pack(side="left")

        tb.Label(frame, bootstyle=SECONDARY, wraplength=1000, justify="left", text=(
            "Built in the spirit of WinUtil-style Windows tweak tools and "
            "Div-Acer-Manager-Max. Not affiliated with Dell or Alienware."
        )).pack(anchor="w", pady=(10, 0))

    # ---------- system-tray autostart ----------

    def _tray_autostart_path(self) -> Path:
        try:
            home = Path(pwd.getpwnam(self.user).pw_dir)
        except KeyError:
            home = Path.home()
        return home / ".config" / "autostart" / "tuxthrottle-tray.desktop"

    def _tray_autostart_enabled(self) -> bool:
        return self._tray_autostart_path().is_file()

    def _tray_exec(self) -> str:
        """Command the autostart entry / 'launch now' runs."""
        return (shutil.which("tuxthrottle-tray")
                or f"/usr/bin/python3 {BASE_DIR}/tray_monitor.py")

    def _tray_toggle_autostart(self):
        p = self._tray_autostart_path()
        want = self._tray_auto_var.get()
        try:
            if want:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=TuxThrottle Tray\n"
                    "Comment=Tray icon + quick launcher for TuxThrottle\n"
                    f"Exec={self._tray_exec()}\n"
                    "Icon=tuxthrottle\n"
                    "Terminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n")
                self._chown_user(p)
                self._chown_user(p.parent)
                msg = "will start at next login"
            else:
                p.unlink(missing_ok=True)
                msg = "autostart removed"
        except OSError as exc:
            self._tray_auto_var.set(self._tray_autostart_enabled())
            messagebox.showwarning("Tray autostart", str(exc))
            return
        self._tray_status_lbl.configure(text=msg)

    def _chown_user(self, path: Path):
        if os.geteuid() != 0:
            return
        try:
            pw = pwd.getpwnam(self.user)
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except (KeyError, OSError):
            pass

    def _tray_launch_now(self):
        if subprocess.run(["pgrep", "-f", "tray_monitor.py"],
                          capture_output=True).returncode == 0:
            self._tray_status_lbl.configure(text="already running")
            return
        exec_cmd = self._tray_exec()
        argv = sensors.session_cmd(["bash", "-lc",
                                    f"setsid {exec_cmd} >/dev/null 2>&1 &"])
        try:
            subprocess.Popen(argv, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._tray_status_lbl.configure(text="launched — check your tray")
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showwarning("Tray", f"Couldn't start it:\n{exc}")
