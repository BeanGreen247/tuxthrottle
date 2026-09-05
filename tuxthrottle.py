#!/usr/bin/env python3
"""Dell G15 5515 (Ryzen Edition) Toolkit — Nobara Linux.

Checkbox-driven GUI for hardware-specific tweaks, drivers, and gaming
software, built the same way as the Windows UltimateToolkit this mirrors:
data-driven JSON config, live status detection, apply/undo, presets.
Inspired by Div-Acer-Manager-Max (DAMX): https://github.com/PXDiv/Div-Acer-Manager-Max

Not a general-purpose distro tool — targets this one laptop's hardware only.

Requires: ttkbootstrap (pip install --user ttkbootstrap — confirmed NOT
packaged in Fedora/Nobara's repos, pip is the only install path) for the
themed dark UI + round-toggle switches + gauge widgets on the Dashboard tab.
"""
import base64
import csv
import glob
import json
import os
import pwd
import queue
import re
import shlex
import shutil
import site
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import messagebox

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
ASSETS_DIR = BASE_DIR / "assets"
PROJECT_URL = "https://github.com/BeanGreen247/tuxthrottle"
PROJECT_ISSUES_URL = PROJECT_URL + "/issues"

# Editable points in the custom fan-curve editor. powerd's interp() is generic
# over any N, and old (5-point) powerd.json configs still load — the editor
# resamples them up to this count on open.
FAN_CURVE_POINTS = 10
sys.path.insert(0, str(BASE_DIR))

try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING
except ImportError:
    print("ttkbootstrap not found. Install with: pip install --user ttkbootstrap")
    print("(not packaged in Fedora/Nobara's repos — pip is the only path)")
    sys.exit(1)

import sensors  # noqa: E402  (local module, no GUI deps)

try:
    import tuxthrottle_kbd  # noqa: E402  (AW-ELC RGB keyboard, stdlib-only)
except Exception:  # noqa: BLE001
    tuxthrottle_kbd = None

import tuxthrottle_btrfs  # noqa: E402  (stdlib, filesystem snapshot-before-apply)
import tuxthrottle_fixlog as fixlog  # noqa: E402  (stdlib, shared fix-history log)
import tuxthrottle_mangohud_status as mangohud_status  # noqa: E402  (stdlib)
import tuxthrottle_profiles  # noqa: E402  (stdlib, imports sensors)
import tuxthrottle_protondb as protondb  # noqa: E402  (stdlib, network optional)
import tuxthrottle_watchdog  # noqa: E402  (stdlib, confirm-or-auto-revert timer)
from tuxthrottle_gui_widgets import (  # noqa: E402  (standalone widgets/theme — extracted)
    ACCENT_FALLBACK,
    BIOS_PANEL,
    BIOS_SUNKEN,
    CHART_AXIS,
    HistoryChart,
    RingGauge,
    SidebarNav,
    _human_bytes,
    _Tooltip,
    apply_bios_style,
    read_desktop_accent,
)
from tuxthrottle_items import (  # noqa: E402  (tweaks/apps data layer — extracted, no Tk deps)
    _STATE_UI,
    Item,
    evaluate_item,
    format_status_report,
    ledger_load,
    ledger_record,
    load_json,
    resolve_real_user,
    run_cmd3,
)
from tuxthrottle_items import load_all_items as _load_all_items  # noqa: E402

try:
    import tuxthrottle_vram  # noqa: E402  (stdlib, imports sensors)
except Exception:  # noqa: BLE001
    tuxthrottle_vram = None

try:
    from tuxthrottle_powerd import interp as fancurve_interp  # noqa: E402
except Exception:  # noqa: BLE001
    def fancurve_interp(points, temp):  # minimal fallback
        s = sorted((float(t), float(b)) for t, b in points)
        if not s or temp <= s[0][0]:
            return s[0][1] if s else 0
        if temp >= s[-1][0]:
            return s[-1][1]
        for (t0, b0), (t1, b1) in zip(s, s[1:]):
            if t0 <= temp <= t1:
                return b0 + (temp - t0) / (t1 - t0) * (b1 - b0)
        return s[-1][1]

CATEGORY_ORDER = ["Gaming", "GPU", "Power", "Performance", "KDE (Desktop GUI Tweaks)",
                  "Software", "Monitoring", "Streaming", "RGB"]
THEME = "darkly"


DISPLAY_VARS = ["DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"]


def self_elevate():
    if os.geteuid() == 0:
        return
    script = str(Path(__file__).resolve())
    # pkexec/sudo scrub the environment on re-exec, dropping DISPLAY/XAUTHORITY
    # (or their Wayland equivalents) — without these the elevated process
    # can't reach the X/Wayland session at all ("no display name" crash).
    present = {v: os.environ[v] for v in DISPLAY_VARS if v in os.environ}

    # pkexec/sudo re-exec as root, which no longer sees the invoking user's
    # ~/.local/lib/pythonX.Y/site-packages — where `pip install --user
    # ttkbootstrap` (the documented install path, since it isn't packaged for
    # Fedora/Nobara) lands. Carry that dir forward on PYTHONPATH so the import
    # at the top of this file still resolves after elevation.
    user_site = site.getusersitepackages()
    if os.path.isdir(user_site):
        existing = os.environ.get("PYTHONPATH", "")
        present["PYTHONPATH"] = f"{user_site}:{existing}" if existing else user_site

    if shutil.which("pkexec"):
        env_pairs = [f"{k}={v}" for k, v in present.items()]
        os.execvp("pkexec", ["pkexec", "env", *env_pairs, sys.executable, script])
    if shutil.which("sudo"):
        args = ["sudo"]
        if present:
            args.append("--preserve-env=" + ",".join(present))
        args += [sys.executable, script]
        os.execvp("sudo", args)
    print("Need root. Run: sudo python3 tuxthrottle.py")
    sys.exit(1)


def _maximize(root: "tb.Window") -> None:
    """Start maximised. '-zoomed' is the reliable path on X11/XWayland (KDE);
    fall back to sizing the window to the screen if the WM rejects it."""
    try:
        root.attributes("-zoomed", True)
        root.update_idletasks()
        if root.winfo_width() > 100:  # WM honoured it
            return
    except tk.TclError:
        pass
    try:
        root.state("zoomed")  # works on some builds/WMs
        return
    except tk.TclError:
        pass
    root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")


class ToolkitApp:
    def __init__(self, root: "tb.Window"):
        self.root = root
        # Size + maximise the window before any widgets exist so the WM has the
        # final geometry from the first map. (An earlier version withdrew the
        # window until _build_ui finished — but if a startup probe stalls, that
        # leaves a blank invisible window and looks like a hang, so it's gone.)
        self.user = resolve_real_user()
        sensors.set_session_user(self.user)  # for kscreen-doctor when elevated
        self._tooltips: list = []            # keep refs so bindings stay alive
        root.title("TuxThrottle — Nobara Linux")
        root.geometry("1080x760")  # fallback size if the WM ignores maximise
        _maximize(root)
        self._set_window_icon(root)

        self.accent = read_desktop_accent()
        try:
            apply_bios_style(root.style, self.accent)
            root.configure(background=BIOS_PANEL)
        except Exception:  # noqa: BLE001
            self.accent = ACCENT_FALLBACK

        self.has_nvidia = sensors.has_nvidia_gpu()
        self.has_amd = sensors.has_amd_gpu()

        # Fan the slow read-only hardware probes out over threads *now*, so the
        # dozen tab sections that each need one don't run them back-to-back on
        # the UI thread during _build_ui (that was ~seconds of dead time at
        # startup, much worse with the dGPU asleep). _probe() below reads the
        # result, waiting on the in-flight thread only if it's not ready yet.
        self._pw: dict = {}
        self._pw_pending: set = set()
        self._prewarm_probes()

        self.items: dict[str, Item] = {}
        self._load_items()
        self.presets = load_json("presets.json")
        try:
            self.games = load_json("games.json")
        except (OSError, ValueError):
            self.games = {}

        self.log_queue: queue.Queue = queue.Queue()
        self.dash_queue: queue.Queue = queue.Queue()
        self.status_queue: queue.Queue = queue.Queue()
        self.worker_running = False
        self.gamemode_var = tk.BooleanVar(value=False)
        self._suppress_gamemode_signal = False

        # App-wide "a long task is running" lock: every tab's long operation
        # (Apply Selected, presets, system updates) calls _begin_busy() on the
        # main thread and hands _end_busy() back via _busy_queue when done.
        # While busy, a click-eating overlay covers the whole notebook and the
        # footer buttons disable, so nothing else can be launched mid-run.
        self._busy = False
        self._busy_queue: queue.Queue = queue.Queue()
        self._prog_q: queue.Queue = queue.Queue()   # (overall:int|None, step:str|None, phase:str|None)
        self._games_q: queue.Queue = queue.Queue()  # Setup Games step-check results
        self._game_steps: list = []
        self._busy_overlay = None
        self._busy_steps = 0
        self._cur_step = ""
        self._footer_btns: list = []

        self._scroll_canvases: list = []   # every scrollable tab body (for the wheel)
        self._log_lines: list[str] = []    # full log buffer, mirrored to any popped-out window
        self._pop_win = None               # detached log Toplevel, when open
        self._pop_text = None
        self._log_collapsed = False

        self._build_ui()
        self.root.after(100, self._poll_log_queue)
        self.root.after(100, self._poll_dash_queue)
        self.root.after(100, self._poll_status_queue)
        self.root.after(120, self._poll_busy_queue)
        self.root.after(130, self._poll_games_queue)
        # The 95 status checks each fork a privileged helper (sudo/kreadconfig/
        # flatpak/rpm). Firing them all at launch starved power-profiles-daemon
        # hard enough to trip scx_lavd's stall watchdog once — so hold them
        # until the window is up and interactive, then run them narrow.
        self.root.after(1200, lambda: threading.Thread(
            target=self._refresh_all_status, daemon=True).start())

        self.dash_running = True
        # start the sensor-polling thread a beat after the window is up, so its
        # nvidia-smi reads don't pile onto the startup probe burst
        self.root.after(1800, self._start_dash_loop)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.update_idletasks()
        _maximize(root)

    def _start_dash_loop(self):
        if self.dash_running and not getattr(self, "_dash_loop_started", False):
            self._dash_loop_started = True
            threading.Thread(target=self._dashboard_loop, daemon=True).start()

    def _on_close(self):
        self.dash_running = False
        self._fan_live = False
        self._power_live = False
        self._close_csv_log()
        if self._pop_win is not None:
            try:
                self._pop_win.destroy()
            except tk.TclError:
                pass
        self.root.destroy()

    # ---------- scrolling ----------

    def _scroll_body(self, parent, pad: int = 0):
        """A vertically-scrollable frame. Returns the inner frame to fill.
        Mouse-wheel is handled globally by _global_wheel via _scroll_canvases."""
        canvas = tk.Canvas(parent, highlightthickness=0, bg=self.root.style.colors.bg)
        vsb = tb.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tb.Frame(canvas, padding=pad)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        self._scroll_canvases.append(canvas)
        return inner

    def _global_wheel(self, event):
        w = self.root.winfo_containing(event.x_root, event.y_root)
        if w is None:
            return
        wp = str(w)
        for cv in self._scroll_canvases:
            cp = str(cv)
            if wp == cp or wp.startswith(cp + "."):
                num, delta = getattr(event, "num", 0), getattr(event, "delta", 0)
                step = -1 if (num == 4 or delta > 0) else 1
                cv.yview_scroll(step, "units")
                return

    def _tip(self, widget, text: str):
        """Attach a hover tooltip. Returns the widget so it chains onto a
        `.pack()` call: `self._tip(tb.Button(...), "…").pack(...)`."""
        try:
            self._tooltips.append(_Tooltip(widget, text))
        except tk.TclError:
            pass
        return widget

    def _set_window_icon(self, root):
        for name in ("icon-256.png", "icon-128.png", "icon.png"):
            path = ASSETS_DIR / name
            if not path.is_file():
                continue
            try:
                self._icon_img = tk.PhotoImage(file=str(path))  # keep a reference
                root.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass
            return

    # slow-ish, side-effect-free probes each used once at build time — value is
    # stable for the life of the window, so warm them up front. The ones that
    # shell out to nvidia-smi share ONE worker and run one-at-a-time: a burst of
    # concurrent nvidia-smi can wake and wedge a runtime-suspended dGPU.
    _PREWARM = {
        "panel_modes":    lambda: sensors.panel_modes(),
        "gpu_mode":       lambda: sensors.gpu_mode_get(),
        "bat_limit":      lambda: sensors.battery_charge_limit_info(),
        "bat_health":     lambda: sensors.battery_health_info(),
        "bat_mode":       lambda: sensors.battery_charge_mode(),
        "vrr":            lambda: sensors.vrr_status(),
        "touchpad":       lambda: sensors.touchpad_info(),
        "ryzenadj_avail": lambda: sensors.ryzenadj_available(),
        "ryzenadj_co":    lambda: sensors.ryzenadj_co_supported(),
    }
    _PREWARM_NVIDIA = {
        "gpu_devs":       lambda: sensors.gpu_devices(),
        "nvpl":           lambda: sensors.nvidia_power_limit_info(),
        "nvclk":          lambda: sensors.nvidia_clock_info(),
    }

    def _prewarm_probes(self):
        def run(name, fn):
            try:
                self._pw[name] = fn()
            except Exception:  # noqa: BLE001
                self._pw[name] = None
            finally:
                self._pw_pending.discard(name)

        def run_chain(items):
            for name, fn in items:
                run(name, fn)

        self._pw_pending.update(self._PREWARM)
        self._pw_pending.update(self._PREWARM_NVIDIA)
        for name, fn in self._PREWARM.items():
            threading.Thread(target=run, args=(name, fn), daemon=True).start()
        threading.Thread(target=run_chain,
                         args=(list(self._PREWARM_NVIDIA.items()),),
                         daemon=True).start()

    def _probe(self, name: str, fn=None, *, timeout: float = 3.0):
        """Cached value of a prewarmed probe. Blocks only until the in-flight
        prewarm thread for `name` finishes (or `timeout`); falls back to a
        direct call for a key that was never prewarmed."""
        if name in self._pw:
            return self._pw[name]
        end = time.monotonic() + timeout
        while name in self._pw_pending and time.monotonic() < end:
            time.sleep(0.02)
        if name in self._pw:
            return self._pw[name]
        try:
            call = fn or self._PREWARM.get(name) or self._PREWARM_NVIDIA[name]
            self._pw[name] = call()
        except Exception:  # noqa: BLE001
            self._pw[name] = None
        return self._pw.get(name)

    def _load_items(self):
        tweaks = load_json("tweaks.json")
        apps = load_json("apps.json")
        for item_id, data in tweaks.items():
            item = Item(item_id, data, "tweak", self.user)
            self._apply_vendor_gate(item)
            self.items[item_id] = item
        for item_id, data in apps.items():
            item = Item(item_id, data, "app", self.user)
            self._apply_vendor_gate(item)
            self.items[item_id] = item

    def _apply_vendor_gate(self, item: Item):
        if item.requires_vendor == "nvidia" and not self.has_nvidia:
            item.hw_supported = False
            item.description += "  (no NVIDIA GPU detected on this system — disabled)"
        elif item.requires_vendor == "amd" and not self.has_amd:
            item.hw_supported = False
            item.description += "  (no AMD GPU detected on this system — disabled)"
        # Nobara 43 already ships /sys/class/powercap world-readable, so the
        # RAPL-permissions tweak is a no-op there — hide it unless it's needed
        # or the user has already applied it (so they can still undo).
        # per-board gate: hide an entry that names a `models` list this
        # machine isn't in, or that the model profile's `tweaks_skip` names.
        # See models/README.md.
        if (not sensors.model_allows(item.requires_models)
                or sensors.model_skips_tweak(item.id)):
            item.hidden = True
            item.hw_supported = False

        if (item.id == "RaplPowerPermissions" and sensors.rapl_permissions_ok()
                and not os.path.exists(
                    "/etc/udev/rules.d/90-tuxthrottle-powercap-perms.rules")):
            item.hidden = True

    # ---------- UI construction ----------

    def _build_ui(self):
        # global mouse-wheel dispatch for every scrollable tab body
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.root.bind_all(seq, self._global_wheel, add="+")

        header = tb.Frame(self.root, padding=(16, 12, 16, 8))
        header.pack(fill="x")
        if getattr(self, "_icon_img", None) is not None:
            try:
                small = self._icon_img.subsample(max(1, self._icon_img.width() // 40))
                tb.Label(header, image=small).pack(side="left", padx=(0, 12))
                self._icon_small = small  # keep a ref
            except tk.TclError:
                pass
        titlebox = tb.Frame(header)
        titlebox.pack(side="left")
        tb.Label(titlebox, text="TuxThrottle",
                 font=("Sans", 16, "bold")).pack(anchor="w")
        tb.Label(header, text=f"elevated · {self.user}",
                 bootstyle=(SECONDARY, "inverse"), font=("Sans", 8, "bold"),
                 padding=(8, 3)).pack(side="right")

        # DMI identity — only surface it when the board is NOT the one the
        # tweaks target (a wrong-hardware warning); the happy-path "✓ matches"
        # bar was just noise restating the CPU/GPU.
        m = sensors.detect_model()
        if not m["is_target"]:
            if m["is_close"]:
                txt = (f"⚠  Detected {m['vendor']} {m['product']} — a G15 5515 variant, "
                       f"not the exact unit this was built against; some sysfs paths may differ.")
                style = WARNING
            else:
                txt = (f"⚠  Detected {m['vendor']} {m['product']} — this is NOT a Dell G15 5515. "
                       f"The checks and tweaks are written for that board; expect breakage.")
                style = DANGER
            tb.Label(self.root, text=txt, bootstyle=style, padding=(16, 2, 16, 8),
                     wraplength=1600, justify="left").pack(fill="x")

        tb.Separator(self.root).pack(fill="x")

        self.notebook = SidebarNav(self.root)
        self.notebook.pack(fill="both", expand=True)
        self._content = self.notebook   # overlay target for _begin_busy
        # let the global mouse-wheel handler drive the scrollable nav rail too
        self._scroll_canvases.append(self.notebook._nav_canvas)  # noqa: SLF001

        self._build_dashboard_tab()
        self._build_keyboard_tab()
        self._build_touchpad_tab()
        self._build_fan_tab()
        self._build_power_tab()
        self._build_display_tab()
        self._build_battery_health_tab()
        self._build_vram_tab()
        self._build_profiles_tab()
        self._build_presets_tab()
        self._build_updates_tab()
        if self.games:
            self._build_games_tab()
        self._build_gametools_tab()

        categories = sorted(
            {item.category for item in self.items.values() if not item.hidden},
            key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99,
        )
        for cat in categories:
            self._build_category_tab(cat)

        # last, pinned to the foot of the rail (always visible, below the
        # scrollable list): About, then the "gather logs for a GitHub issue" page
        self._build_about_tab()
        self._build_diagnostics_tab()

        # per-section "apply the developer's picks" button, right side of the
        # page title — only shows on a tweak/app category page that still has
        # unapplied recommendations
        self._rec_btn = tb.Button(
            self.notebook._header_actions,  # noqa: SLF001
            text="★  Apply section recommendations", bootstyle=(SUCCESS, "outline"),
            takefocus=False, command=self._on_apply_recommended)
        self._tip(self._rec_btn, "Apply the developer's curated picks for THIS "
                  "category in one go (a snapshot is taken first). Only shows "
                  "when something's still unapplied.")
        self.notebook.on_select = self._on_nav_page
        self._on_nav_page(self.notebook.tab(0))

        # ---- footer: actions + status ----
        tb.Separator(self.root).pack(fill="x", padx=16)
        btn_bar = tb.Frame(self.root, padding=(16, 10))
        btn_bar.pack(fill="x")
        btn_refresh = tb.Button(btn_bar, text="↻  Refresh Status", bootstyle=(INFO, "outline"),
                                command=self._on_refresh_click)
        btn_refresh.pack(side="left")
        self._tip(btn_refresh, "Re-run every item's check command and update the "
                  "✓ Applied / Installed marks to the real current state.")
        btn_apply = tb.Button(btn_bar, text="✓  Apply Selected", bootstyle=SUCCESS,
                              command=self._on_apply_click)
        btn_apply.pack(side="left", padx=8)
        self._tip(btn_apply, "Act on the ticks: apply ticked-but-not-applied "
                  "tweaks, install ticked-but-missing apps, and undo unticked "
                  "tweaks that are currently applied. Already-done items are "
                  "skipped. A snapshot is taken first.")
        btn_report = tb.Button(btn_bar, text="≣  Status report", bootstyle=(SECONDARY, "outline"),
                               command=self._show_status_report)
        btn_report.pack(side="left")
        self._tip(btn_report, "Open a copyable table: every item, its state, the "
                  "exact check command + exit code, and what the toolkit last "
                  "did to it.")
        self._footer_btns = [btn_refresh, btn_apply, btn_report]
        self.status_var = tk.StringVar(value="Ready.")
        tb.Label(btn_bar, textvariable=self.status_var, bootstyle=SECONDARY,
                 font=("Sans", 9)).pack(side="right")
        self._busy_bar = tb.Progressbar(btn_bar, mode="indeterminate", length=160,
                                        bootstyle=(INFO, "striped"))
        # packed only while busy (see _begin_busy / _end_busy)

        # ---- log console (collapsible / detachable) ----
        self.log_frame = tb.Frame(self.root, padding=(16, 0, 16, 12))
        self.log_frame.pack(fill="both", expand=False)
        bar = tb.Frame(self.log_frame)
        bar.pack(fill="x", pady=(0, 4))
        tb.Label(bar, text="LOG", font=("Sans", 8, "bold"), bootstyle=SECONDARY).pack(side="left")
        self.log_popout_btn = tb.Button(bar, text="⇱ pop out", bootstyle=(SECONDARY, "link"),
                                        command=self._toggle_log_popout)
        self.log_popout_btn.pack(side="right")
        self.log_collapse_btn = tb.Button(bar, text="▾ hide", bootstyle=(SECONDARY, "link"),
                                          command=self._toggle_log_collapse)
        self.log_collapse_btn.pack(side="right")
        self.log_text = self._make_log_text(self.log_frame)
        self.log_text.pack(fill="both", expand=True)
        self._toggle_log_collapse()   # start collapsed; expand on demand

    @staticmethod
    def _make_log_text(parent) -> tk.Text:
        t = tk.Text(parent, height=9, font=("Monospace", 9), bg="#0e1116", fg="#c9d1d9",
                    insertbackground="#c9d1d9", relief="flat", wrap="word",
                    padx=10, pady=8, borderwidth=0)
        t.configure(state="disabled")
        return t

    def _toggle_log_collapse(self):
        self._log_collapsed = not self._log_collapsed
        if self._log_collapsed:
            self.log_text.pack_forget()
            self.log_collapse_btn.configure(text="▸ show")
        else:
            self.log_text.pack(fill="both", expand=True)
            self.log_collapse_btn.configure(text="▾ hide")

    def _toggle_log_popout(self):
        if self._pop_win is None:
            self._pop_win = tk.Toplevel(self.root)
            self._pop_win.title("TuxThrottle — Log")
            self._pop_win.geometry("900x480")
            if getattr(self, "_icon_img", None) is not None:
                try:
                    self._pop_win.iconphoto(True, self._icon_img)
                except tk.TclError:
                    pass
            self._pop_text = self._make_log_text(self._pop_win)
            self._pop_text.pack(fill="both", expand=True, padx=8, pady=8)
            self._pop_text.configure(state="normal")
            self._pop_text.insert("end", "\n".join(self._log_lines[-2000:]) + ("\n" if self._log_lines else ""))
            self._pop_text.configure(state="disabled")
            self._pop_text.see("end")
            self._pop_win.protocol("WM_DELETE_WINDOW", self._toggle_log_popout)
            if not self._log_collapsed:
                self._toggle_log_collapse()
            self.log_popout_btn.configure(text="⇲ dock")
        else:
            try:
                self._pop_win.destroy()
            except tk.TclError:
                pass
            self._pop_win = self._pop_text = None
            self.log_popout_btn.configure(text="⇱ pop out")
            if self._log_collapsed:
                self._toggle_log_collapse()

    _DASH_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _build_dashboard_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Dashboard")
        # host frame stays; the heavy gauge/chart body is built on entering the
        # tab and torn down on leaving it, with a spinner shown until the first
        # sensor sample lands (keeps launch cheap, frees the polling otherwise)
        self._dash_outer = self._scroll_body(outer, pad=16)
        self._dash_body = None
        self._dash_built = False
        self._dash_active = False
        self._dash_shown = False
        self._dash_first_data = False
        self._dash_spin_i = 0
        self._dash_spin_job = None
        self._csv_file = None
        self._csv_writer = None
        self._csv_logging = tk.BooleanVar(value=False)
        self._dash_spinner = tb.Label(self._dash_outer, font=("Monospace", 13),
                                      bootstyle=SECONDARY, text="Loading sensors…")

    def _dash_spin(self):
        if self._dash_spin_job is None:
            return
        ch = self._DASH_SPIN[self._dash_spin_i % len(self._DASH_SPIN)]
        self._dash_spin_i += 1
        try:
            self._dash_spinner.configure(text=f"{ch}   Loading sensors…")
        except tk.TclError:
            return
        self._dash_spin_job = self.root.after(90, self._dash_spin)

    def _dash_start_spinner(self):
        self._dash_spinner.pack(anchor="w", padx=8, pady=48)
        if self._dash_spin_job is None:
            self._dash_spin_job = self.root.after(0, self._dash_spin)

    def _dash_stop_spinner(self):
        if self._dash_spin_job is not None:
            try:
                self.root.after_cancel(self._dash_spin_job)
            except tk.TclError:
                pass
            self._dash_spin_job = None
        try:
            self._dash_spinner.pack_forget()
        except tk.TclError:
            pass

    def _dash_enter(self):
        self._dash_active = True
        if self._dash_built:
            return
        self._dash_first_data = False
        self._dash_start_spinner()
        self.root.after(60, self._dash_build_body)   # let the spinner paint first

    def _dash_leave(self):
        self._dash_active = False
        self._dash_stop_spinner()
        if self._csv_writer is not None:             # don't log a torn-down tab
            self._csv_logging.set(False)
            self._toggle_csv_log()
        if self._dash_body is not None:
            try:
                self._dash_body.destroy()
            except tk.TclError:
                pass
            self._dash_body = None
        self._dash_built = False
        self._dash_first_data = False

    def _dash_reveal(self):
        """First real sample arrived — swap the spinner for the live body."""
        if self._dash_first_data or not self._dash_built:
            return
        self._dash_first_data = True
        self._dash_stop_spinner()
        if self._dash_body is not None:
            self._dash_body.pack(fill="both", expand=True)

    def _dash_build_body(self):
        if self._dash_built or not self._dash_active:
            return
        frame = tb.Frame(self._dash_outer)      # stays unpacked until first data
        self._dash_body = frame
        self._dash_built = True

        gauges = tb.Frame(frame)
        gauges.pack(fill="x", pady=(0, 18))
        acc = getattr(self, "accent", ACCENT_FALLBACK)
        # Two rows of four. dGPU/iGPU clock gauges sit next to their temps so a
        # glance shows whether a chip is boosting or parked.
        specs = [
            ("meter_cpu_temp",  "CPU temp",   "°C",  100, acc,       "{:.0f}"),
            ("meter_cpu_freq",  "CPU clock",  "GHz", 5.0, "#3fb950", "{:.2f}"),
            ("meter_cpu_power", "CPU power",  "W",    65, acc,       "{:.0f}"),
            ("meter_igpu_freq", "iGPU clock", "MHz", 2000, "#3fb950", "{:.0f}"),
            ("meter_dgpu_temp", "dGPU temp",  "°C",  100, "#d29922", "{:.0f}"),
            ("meter_dgpu_freq", "dGPU clock", "MHz", 2100, "#d29922", "{:.0f}"),
            ("meter_dgpu_util", "dGPU util",  "%",   100, "#f85149", "{:.0f}"),
            ("meter_dgpu_power","dGPU power", "W",    80, "#d29922", "{:.0f}"),
        ]
        for i, (attr, cap, unit, mx, col, fmt) in enumerate(specs):
            g = RingGauge(gauges, caption=cap, unit=unit, maximum=mx,
                          color=col, fmt=fmt, size=132)
            g.grid(row=i // 4, column=i % 4, padx=8, pady=6, sticky="n")
            gauges.columnconfigure(i % 4, weight=1)
            setattr(self, attr, g)

        self.rapl_warning = tb.Label(
            frame, text="", bootstyle=WARNING, wraplength=900,
        )
        self.rapl_warning.pack(anchor="w", pady=(0, 12))

        details = tb.Labelframe(frame, text="Details", padding=12)
        details.pack(fill="x", pady=(0, 12))
        self.dash_cpu_label = tb.Label(details, text="CPU: …", font=("Monospace", 10))
        self.dash_cpu_label.pack(anchor="w")
        self.dash_igpu_label = tb.Label(details, text="iGPU: …", font=("Monospace", 10))
        self.dash_igpu_label.pack(anchor="w")
        self.dash_dgpu_label = tb.Label(details, text="dGPU: …", font=("Monospace", 10))
        self.dash_dgpu_label.pack(anchor="w")

        # rolling history strip
        hist = tb.Labelframe(frame, text="History  (rolling ~3 min)", padding=12)
        hist.pack(fill="x", pady=(0, 12))
        hgrid = tb.Frame(hist); hgrid.pack(fill="x")
        self._hist_charts = {}
        for i, (key, cap, unit, col) in enumerate([
            ("cpu_temp",  "CPU °C",   "",  acc),
            ("cpu_power", "CPU W",    "",  acc),
            ("dgpu_temp", "dGPU °C",  "",  "#d29922"),
            ("dgpu_power","dGPU W",   "",  "#d29922"),
        ]):
            c = HistoryChart(hgrid, caption=cap, unit=unit, color=col, samples=90)
            c.grid(row=i // 2, column=i % 2, sticky="ew", padx=6, pady=4)
            hgrid.columnconfigure(i % 2, weight=1)
            self._hist_charts[key] = c
        logrow = tb.Frame(hist); logrow.pack(anchor="w", pady=(6, 0))
        tb.Checkbutton(logrow, text="Log this session to CSV",
                       variable=self._csv_logging, bootstyle="round-toggle",
                       command=self._toggle_csv_log).pack(side="left")
        self._csv_path_lbl = tb.Label(logrow, text="", bootstyle=SECONDARY,
                                      font=("Monospace", 8))
        self._csv_path_lbl.pack(side="left", padx=10)

        toggle_frame = tb.Labelframe(frame, text="Game Mode", padding=16)
        toggle_frame.pack(fill="x")
        row = tb.Frame(toggle_frame)
        row.pack(fill="x")
        tb.Checkbutton(
            row, text="Performance profile + GPU perf-state forcing",
            variable=self.gamemode_var, bootstyle="round-toggle",
            command=self._on_gamemode_toggle,
        ).pack(side="left")
        tb.Label(
            toggle_frame,
            text="Same effect as pressing the G-key or clicking the tray icon. "
                 "Needs the Power/GPU tweaks below installed first.",
            bootstyle=SECONDARY, wraplength=900,
        ).pack(anchor="w", pady=(6, 0))

    # ---------- keyboard RGB tab ----------

    _KBD_PRESETS = [
        ("White", "#ffffff"), ("Red", "#ff0000"), ("Green", "#00ff00"),
        ("Blue", "#0000ff"), ("Cyan", "#00ffff"), ("Magenta", "#ff00ff"),
        ("Amber", "#ff6a00"),
    ]

    def _build_keyboard_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Keyboard")
        frame = self._scroll_body(outer, pad=16)

        import shutil as _sh
        have_openrgb = _sh.which("openrgb") is not None
        detected = tuxthrottle_kbd is not None and tuxthrottle_kbd.Keyboard._find() is not None
        if not have_openrgb:
            tb.Label(
                frame, bootstyle=WARNING, justify="left", wraplength=1000,
                text="Install the OpenRGB app (Software tab) to use this.\n\n"
                     "The G15 5515's AW-ELC keyboard has no kernel driver and ignores raw HID "
                     "writes — OpenRGB's 16-zone protocol is the only thing that drives it. "
                     "The backlight must also be enabled in BIOS setup (F2 -> Keyboard "
                     "Backlight) or nothing lights.",
            ).pack(anchor="w")
            return
        if not detected:
            tb.Label(
                frame, bootstyle=SECONDARY, justify="left",
                text="No Alienware AW-ELC RGB keyboard (USB 187c:0550) found.\n"
                     "This tab drives the RGB backlight on the Dell G15 5515.",
            ).pack(anchor="w")
            return

        note = tb.Labelframe(frame, text="How this works", padding=12)
        note.pack(fill="x", pady=(0, 14))
        tb.Label(
            note, wraplength=1100, justify="left", bootstyle=SECONDARY,
            text="Driven through OpenRGB (the AW-ELC has no kernel driver). Enable the backlight "
                 "in BIOS setup (F2 -> Keyboard Backlight) first — if the keys stay dark, that's "
                 "why. This keyboard is a single controllable zone: it does one solid colour "
                 "(pick or preset), a brightness level, or the firmware Spectrum Cycle. There is "
                 "no per-zone colour or gradient — the hardware ignores zone-scoped writes. "
                 "Colours don't survive a reboot on their own — apply the KbdBacklightFix tweak "
                 "(Power tab) to re-assert the last setting at login and after resume.",
        ).pack(anchor="w")

        self._kbd_busy = False
        self.kbd_brightness = tk.IntVar(value=100)
        self.kbd_all_hex = tk.StringVar(value="#ffffff")
        self.kbd_speed = tk.IntVar(value=50)

        # pre-fill from saved state if present
        saved = tuxthrottle_kbd.load_state()
        if saved:
            zc, br = saved
            self.kbd_brightness.set(br)
            if zc:
                r, g, b = tuple(sorted(zc.items())[0][1])
                self.kbd_all_hex.set("#%02x%02x%02x" % (r, g, b))
        _meta = tuxthrottle_kbd.load_meta()
        self.kbd_speed.set(_meta.get("speed", 50))
        self._kbd_mode = _meta.get("mode", "zones")
        self.kbd_match_accent = tk.BooleanVar(value=self._kbd_mode == "accent")
        self.kbd_push_accent = tk.BooleanVar(value=bool(_meta.get("push_accent")))

        # ---- brightness ----
        br_box = tb.Labelframe(frame, text="Brightness", padding=12)
        br_box.pack(fill="x", pady=(0, 12))
        scale = tb.Scale(br_box, from_=0, to=100, variable=self.kbd_brightness, orient="horizontal")
        scale.pack(side="left", fill="x", expand=True, padx=(0, 10))
        scale.bind("<ButtonRelease-1>", lambda _e: self._kbd_apply_brightness())
        tb.Label(br_box, textvariable=self.kbd_brightness, width=4).pack(side="left")

        # ---- whole keyboard ----
        whole = tb.Labelframe(frame, text="Whole keyboard", padding=12)
        whole.pack(fill="x", pady=(0, 12))
        r1 = tb.Frame(whole)
        r1.pack(fill="x")
        self._kbd_swatch(r1, self.kbd_all_hex).pack(side="left", padx=(0, 8))
        tb.Button(r1, text="Pick colour…", bootstyle=SECONDARY,
                  command=lambda: self._kbd_pick(self.kbd_all_hex)).pack(side="left", padx=4)
        tb.Button(r1, text="Apply colour", bootstyle=SUCCESS,
                  command=self._kbd_apply_all).pack(side="left", padx=4)
        r2 = tb.Frame(whole)
        r2.pack(fill="x", pady=(8, 0))
        for name, hexv in self._KBD_PRESETS:
            tb.Button(r2, text=name, bootstyle=SECONDARY, width=8,
                      command=lambda h=hexv: (self.kbd_all_hex.set(h), self._kbd_apply_all())
                      ).pack(side="left", padx=2)

        # ---- desktop accent ----  (the two toggles are mutually exclusive)
        acc = tb.Labelframe(frame, text="Desktop accent colour", padding=12)
        acc.pack(fill="x", pady=(0, 12))
        self._tip(tb.Checkbutton(
            acc, text="Keyboard follows the desktop accent colour",
            variable=self.kbd_match_accent, bootstyle="round-toggle",
            command=self._kbd_toggle_accent),
            "On: the keyboard takes the Plasma accent now and re-reads the "
            "CURRENT accent on every re-assert (login, resume, tray start) — it "
            "follows the accent if you change it later. Turning this on turns "
            "off the option below.").pack(anchor="w")
        self._kbd_push_toggle = self._tip(tb.Checkbutton(
            acc, text="Desktop accent follows the keyboard colour",
            variable=self.kbd_push_accent, bootstyle="round-toggle",
            command=self._kbd_toggle_push),
            "On: every keyboard colour you set here is also written into "
            "Plasma's accent-colour setting (kdeglobals AccentColor), with "
            "accent-from-wallpaper turned off — the desktop repaints to match. "
            "Disabled while Spectrum Cycle is running (no single colour to "
            "copy). Turning this on turns off the option above.")
        self._kbd_push_toggle.pack(anchor="w", pady=(6, 0))
        self._kbd_refresh_accent_ui()

        # ---- effects ----
        fx = tb.Labelframe(frame, text="Effect", padding=12)
        fx.pack(fill="x", pady=(0, 12))
        srow = tb.Frame(fx)
        srow.pack(fill="x", pady=(0, 6))
        tb.Label(srow, text="Speed", width=10).pack(side="left")
        sp = tb.Scale(srow, from_=0, to=100, variable=self.kbd_speed, orient="horizontal")
        sp.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tb.Label(srow, textvariable=self.kbd_speed, width=4).pack(side="left")
        frow = tb.Frame(fx)
        frow.pack(fill="x")
        tb.Button(frow, text="Spectrum Cycle", bootstyle=SECONDARY,
                  command=lambda: self._kbd_apply_effect("spectrum")).pack(side="left", padx=3)
        tb.Button(frow, text="Solid colour  (leave the effect)", bootstyle=SUCCESS,
                  command=self._kbd_apply_all).pack(side="left", padx=3)

        bottom = tb.Frame(frame)
        bottom.pack(fill="x", pady=(4, 0))
        tb.Button(bottom, text="Turn backlight off", bootstyle=(SECONDARY, "outline"),
                  command=self._kbd_off).pack(side="left")
        tb.Button(bottom, text="↻ Reset backlight  (unfreeze)", bootstyle=(WARNING, "outline"),
                  command=self._kbd_reset).pack(side="left", padx=8)

    def _kbd_swatch(self, parent, hexvar: tk.StringVar):
        lbl = tk.Label(parent, width=4, relief="solid", bd=1, bg=self._safe_hex(hexvar.get()))
        hexvar.trace_add("write", lambda *_: lbl.configure(bg=self._safe_hex(hexvar.get())))
        return lbl

    @staticmethod
    def _safe_hex(s: str) -> str:
        s = s if s.startswith("#") else "#" + s
        return s if len(s) == 7 else "#ffffff"

    def _kbd_pick(self, hexvar: tk.StringVar):
        from tkinter import colorchooser
        _rgb, hx = colorchooser.askcolor(color=self._safe_hex(hexvar.get()),
                                         parent=self.root, title="Keyboard colour")
        if hx:
            hexvar.set(hx)

    @staticmethod
    def _hex_to_rgb(hx: str) -> tuple[int, int, int]:
        hx = hx.lstrip("#")
        return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))

    def _kbd_run(self, fn, desc: str):
        if self._kbd_busy:
            self._log("[Keyboard] busy — try again in a moment")
            return
        self._kbd_busy = True
        self._log(f"[Keyboard] {desc} …")

        def work():
            try:
                kb = tuxthrottle_kbd.Keyboard()
                try:
                    fn(kb)
                finally:
                    kb.close()
                self._log(f"[Keyboard] {desc} ✓")
            except Exception as exc:  # noqa: BLE001
                self._log(f"[Keyboard FAILED] {exc}")
            finally:
                self._kbd_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _kbd_all_colors(self) -> dict[int, tuple[int, int, int]]:
        rgb = self._hex_to_rgb(self._safe_hex(self.kbd_all_hex.get()))
        return dict.fromkeys(range(tuxthrottle_kbd.ZONE_COUNT), rgb)

    def _kbd_static_mode(self) -> str:
        """The mode string to persist for a static-colour apply — 'accent'
        while the keyboard-follows-accent toggle is on, else 'zones'."""
        return "accent" if getattr(self, "kbd_match_accent", None) is not None \
            and self.kbd_match_accent.get() else "zones"

    def _kbd_refresh_accent_ui(self):
        """'Desktop accent follows the keyboard' only makes sense for a static
        colour — disable it while Spectrum Cycle runs. The other toggle stays
        enabled always (ticking it just switches the keyboard to a colour)."""
        tog = getattr(self, "_kbd_push_toggle", None)
        if tog is None:
            return
        static = getattr(self, "_kbd_mode", "zones") not in tuxthrottle_kbd.ALL_EFFECTS
        tog.configure(state="normal" if static else "disabled")

    def _kbd_set_mode(self, mode: str):
        """Record the active keyboard mode and keep the two accent toggles
        consistent: a non-accent mode clears 'keyboard follows accent'; an
        effect mode also clears 'accent follows keyboard'."""
        self._kbd_mode = mode
        if mode != "accent" and getattr(self, "kbd_match_accent", None) is not None:
            self.kbd_match_accent.set(False)
        if mode in tuxthrottle_kbd.ALL_EFFECTS \
                and getattr(self, "kbd_push_accent", None) is not None:
            self.kbd_push_accent.set(False)
        self._kbd_refresh_accent_ui()

    def _kbd_maybe_push_accent(self):
        if getattr(self, "kbd_push_accent", None) is not None \
                and self.kbd_push_accent.get() \
                and self._kbd_mode not in tuxthrottle_kbd.ALL_EFFECTS:
            self._kbd_push_accent_now()

    def _kbd_apply_brightness(self):
        b = self.kbd_brightness.get()
        hx = self._safe_hex(self.kbd_all_hex.get())
        colors = self._kbd_all_colors()
        mode = self._kbd_static_mode()
        pa = self.kbd_push_accent.get()
        self._kbd_run(lambda kb: (kb.set_all(hx, b),
                      tuxthrottle_kbd.save_state(colors, b, mode=mode, push_accent=pa)),
                      f"brightness {b}%")

    def _kbd_apply_all(self):
        hx = self._safe_hex(self.kbd_all_hex.get())
        b = self.kbd_brightness.get()
        colors = self._kbd_all_colors()
        self._kbd_set_mode("zones")
        pa = self.kbd_push_accent.get()
        self._kbd_run(lambda kb: (kb.set_all(hx, b),
                      tuxthrottle_kbd.save_state(colors, b, mode="zones", push_accent=pa)),
                      f"colour {hx} @ {b}%")
        self._kbd_maybe_push_accent()

    def _kbd_apply_effect(self, key: str):
        b = self.kbd_brightness.get()
        sp = self.kbd_speed.get()
        colors = self._kbd_all_colors()
        self._kbd_set_mode(key)
        self._kbd_run(lambda kb: (kb.set_effect(key, sp, b),
                      tuxthrottle_kbd.save_state(colors, b, mode=key, speed=sp,
                                                push_accent=False)),
                      f"effect {key} @ speed {sp}, {b}%")

    def _kbd_reset(self):
        self._kbd_run(lambda kb: kb.reset(), "reset backlight (restart OpenRGB + re-apply)")

    def _kbd_off(self):
        self.kbd_brightness.set(0)
        self._kbd_run(lambda kb: kb.off(), "backlight off")

    def _kbd_toggle_accent(self):
        """'Keyboard follows the desktop accent' toggle."""
        b = self.kbd_brightness.get()
        if self.kbd_match_accent.get():
            self.kbd_push_accent.set(False)          # mutually exclusive
            hx = "#" + tuxthrottle_kbd.accent_hex().lower()
            self.kbd_all_hex.set(hx)
            colors = self._kbd_all_colors()
            self._kbd_mode = "accent"
            self._kbd_refresh_accent_ui()
            self._kbd_run(lambda kb: (kb.set_all(hx, b),
                          tuxthrottle_kbd.save_state(colors, b, mode="accent",
                                                    push_accent=False)),
                          f"follow desktop accent {hx} @ {b}%")
        else:
            self._kbd_apply_all()                    # back to a fixed colour

    def _kbd_toggle_push(self):
        """'Desktop accent follows the keyboard colour' toggle."""
        if self.kbd_push_accent.get():
            self.kbd_match_accent.set(False)         # mutually exclusive
            if self._kbd_mode == "accent":
                self._kbd_mode = "zones"
            self._kbd_refresh_accent_ui()
            # commit current state with the flag on, then push once now
            colors = self._kbd_all_colors()
            b = self.kbd_brightness.get()
            tuxthrottle_kbd.save_state(colors, b, mode="zones", push_accent=True)
            self._kbd_push_accent_now()
        else:
            colors = self._kbd_all_colors()
            b = self.kbd_brightness.get()
            tuxthrottle_kbd.save_state(colors, b, mode=self._kbd_static_mode(),
                                      push_accent=False)

    def _kbd_push_accent_now(self):
        """Set Plasma's accent colour to the current whole-keyboard colour and
        repaint the live session. `plasma-apply-colorscheme -a` is the only
        thing that reliably re-themes running apps for an accent change; plain
        kdeglobals writes only take effect at next login. Off-thread."""
        hx = self._safe_hex(self.kbd_all_hex.get()).lstrip("#")
        if len(hx) < 6:
            return
        try:
            rgb = f"{int(hx[0:2], 16)},{int(hx[2:4], 16)},{int(hx[4:6], 16)}"
        except ValueError:
            return
        self._log(f"[Keyboard] desktop accent → #{hx} …")

        def work():
            sc = sensors.session_cmd
            applied = False
            # accent-only: NO positional colour-scheme arg — that would force
            # BreezeLight/Dark and flip the whole session's light/dark mode.
            if shutil.which("plasma-apply-colorscheme"):
                try:
                    r = subprocess.run(
                        sc(["plasma-apply-colorscheme", "--accent-color", f"#{hx}"]),
                        capture_output=True, text=True, timeout=25)
                    applied = r.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    pass
            # hygiene / fallback: pin the keys so it also survives a relogin
            base = ["kwriteconfig6", "--file", "kdeglobals",
                    "--group", "General", "--key"]
            for key, val in (("AccentColor", rgb),
                             ("AccentColorFromWallpaper", "false"),
                             ("LastUsedCustomAccentColor", rgb)):
                try:
                    subprocess.run(sc(base + [key, val]),
                                   capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass
            if not applied:
                for c in (["dbus-send", "--session", "--type=signal",
                           "/KGlobalSettings",
                           "org.kde.KGlobalSettings.notifyChange",
                           "int32:0", "int32:0"],
                          ["qdbus-qt6", "org.kde.KWin", "/KWin", "reconfigure"]):
                    try:
                        subprocess.run(sc(c), capture_output=True, timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        pass
            self._log(f"[Keyboard] desktop accent set to #{hx}"
                      + ("" if applied else " (relogin if it didn't repaint)"))

        threading.Thread(target=work, daemon=True).start()

    # ---------- fan control ----------

    def _build_fan_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Fans")
        frame = self._scroll_body(outer, pad=16)

        fans = sensors.read_fans()
        if not fans:
            tb.Label(frame, bootstyle=WARNING, justify="left", wraplength=1000,
                     text="No fan interface found (expected the alienware_wmi / "
                          "dell_smm hwmon devices). Is the alienware-wmi kernel "
                          "module loaded on this Dell G15 5515?").pack(anchor="w")
            return

        self._fan_rpm_labels: dict = {}
        self._fan_boost_vars: dict = {}
        self._fan_pwm_vars: dict = {}
        self._fan_manual = tk.BooleanVar(value=False)

        note = tb.Labelframe(frame, text="How this works", padding=12)
        note.pack(fill="x", pady=(0, 14))
        tb.Label(
            note, wraplength=1100, justify="left", bootstyle=SECONDARY,
            text="Thermal profile + fan boost steer the firmware (AWCC-style) fan "
                 "curve — boost only adds airflow on top, it can't slow a fan below "
                 "the automatic curve. Manual PWM (advanced) takes the EC off its "
                 "curve entirely; it's floored so the fans never fully stop, but "
                 "watch temperatures and hit “Restore automatic” when done. Nothing "
                 "here persists across a reboot yet.",
        ).pack(anchor="w")

        choices = sensors.platform_profile_choices()
        if choices:
            pf = tb.Labelframe(frame, text="Thermal profile", padding=12)
            pf.pack(fill="x", pady=6)
            prow = tb.Frame(pf); prow.pack(anchor="w")
            self._fan_profile_var = tk.StringVar(value=sensors.get_platform_profile())
            for c in choices:
                tb.Radiobutton(prow, text=c.capitalize(), value=c,
                               variable=self._fan_profile_var, bootstyle="toolbutton",
                               command=lambda v=c: self._fan_set_profile(v)
                               ).pack(side="left", padx=4)
            if tuxthrottle_kbd is not None:
                tie_cfg = self._read_power_state("kbd_profile_tie.json") or {}
                self._kbd_tie_var = tk.BooleanVar(value=bool(tie_cfg.get("enabled")))
                tb.Checkbutton(
                    pf, text="Tie keyboard colour to the active profile "
                             "(Quiet=blue / Balanced=white / Performance=red)",
                    variable=self._kbd_tie_var, bootstyle="round-toggle",
                    command=self._kbd_tie_toggle).pack(anchor="w", pady=(8, 0))

        lf = tb.Labelframe(frame, text="Fans & boost", padding=12)
        lf.pack(fill="x", pady=6)
        boosts = sensors.get_fan_boost()
        for k, fan in enumerate(fans):
            i = fan["index"]
            r = tb.Frame(lf); r.pack(fill="x", pady=6)
            tb.Label(r, text=fan["label"], font=("Sans", 10, "bold"), width=16,
                     anchor="w").pack(side="left")
            rpm_lab = tb.Label(r, text="— rpm", width=11, anchor="w",
                               bootstyle=SECONDARY)
            rpm_lab.pack(side="left")
            self._fan_rpm_labels[i] = rpm_lab
            bv = tk.IntVar(value=round((boosts[k] if k < len(boosts) else 0) / 255 * 100))
            self._fan_boost_vars[i] = bv
            tb.Label(r, text="Boost").pack(side="left", padx=(12, 4))
            sc = tb.Scale(r, from_=0, to=100, variable=bv, orient="horizontal", length=240)
            sc.pack(side="left", fill="x", expand=True)
            sc.bind("<ButtonRelease-1>", lambda _e, idx=i: self._fan_set_boost(idx))
            tb.Label(r, textvariable=bv, width=4).pack(side="left")
            for lbl, pct in (("0", 0), ("50", 50), ("Max", 100)):
                tb.Button(r, text=lbl, bootstyle=(SECONDARY, "outline"), width=4,
                          command=lambda idx=i, p=pct, v=bv: (v.set(p),
                                                              self._fan_set_boost(idx))
                          ).pack(side="left", padx=2)

        pr = tb.Frame(frame); pr.pack(anchor="w", pady=(8, 0))
        tb.Label(pr, text="Presets:", bootstyle=SECONDARY).pack(side="left", padx=(0, 6))
        tb.Button(pr, text="Auto / silent", bootstyle=SUCCESS,
                  command=lambda: self._fan_preset("auto")).pack(side="left", padx=4)
        tb.Button(pr, text="Cooler", bootstyle=(INFO, "outline"),
                  command=lambda: self._fan_preset("cool")).pack(side="left", padx=4)
        tb.Button(pr, text="Max cooling", bootstyle=(DANGER, "outline"),
                  command=lambda: self._fan_preset("max")).pack(side="left", padx=4)
        self._tip(tb.Button(pr, text="⏱ Boost 100% for 60s", bootstyle=(WARNING, "outline"),
                  command=self._fan_boost_60s),
                  "Spin every fan to max right now, then automatically put boost "
                  "back to whatever it was before — for the 'about to load into "
                  "a match' moment, without committing to a whole profile change. "
                  "The revert is a background timer independent of this window, "
                  "so it still happens even if you close the app.").pack(side="left", padx=(12, 4))

        if sensors.get_pwm_state():
            adv = tb.Labelframe(frame, text="Manual PWM — advanced / risky",
                                bootstyle=DANGER, padding=12)
            adv.pack(fill="x", pady=(14, 6))
            tb.Checkbutton(adv, variable=self._fan_manual, bootstyle="round-toggle",
                           text="Enable manual PWM control (takes the EC off its "
                                "automatic curve)",
                           command=self._fan_manual_toggle).pack(anchor="w")
            self._fan_pwm_box = tb.Frame(adv)
            self._fan_pwm_box.pack(fill="x", pady=(8, 0))
            for fan in fans:
                i = fan["index"]
                r = tb.Frame(self._fan_pwm_box); r.pack(fill="x", pady=4)
                tb.Label(r, text=fan["label"], width=16, anchor="w").pack(side="left")
                pv = tk.IntVar(value=50)
                self._fan_pwm_vars[i] = pv
                sc = tb.Scale(r, from_=30, to=100, variable=pv, orient="horizontal",
                              length=280)
                sc.pack(side="left", fill="x", expand=True)
                sc.bind("<ButtonRelease-1>", lambda _e, idx=i: self._fan_set_pwm(idx))
                tb.Label(r, textvariable=pv, width=4).pack(side="left")
            tb.Button(adv, text="Restore automatic", bootstyle=SUCCESS,
                      command=self._fan_restore).pack(anchor="w", pady=(10, 0))
            self._fan_manual_toggle()

        self._build_fancurve_section(frame)

        self._fan_live = True
        self._fan_poll()

    # --- closed-loop fan curve (tuxthrottle_powerd.py) ---

    _FANCURVE_DEFAULT = [[40, 0], [48, 12], [55, 25], [62, 38], [69, 52],
                         [75, 66], [81, 78], [86, 88], [91, 95], [95, 100]]

    @staticmethod
    def _fc_resample(pts, n=FAN_CURVE_POINTS):
        """Piecewise-linearly resample an arbitrary point list to n points
        evenly spaced across its temperature range (used when loading an old
        5-point powerd.json into the n-row editor)."""
        clean = sorted([[float(t), float(b)] for t, b in pts
                        if t is not None and b is not None])
        if len(clean) == n:
            return [[int(round(t)), int(round(b))] for t, b in clean]
        if len(clean) < 2:
            return [list(p) for p in ToolkitApp._FANCURVE_DEFAULT[:n]]

        def at(temp):
            for (a_t, a_b), (b_t, b_b) in zip(clean, clean[1:]):
                if temp <= b_t:
                    if b_t == a_t:
                        return a_b
                    f = (temp - a_t) / (b_t - a_t)
                    return a_b + f * (b_b - a_b)
            return clean[-1][1]

        t0, t1 = clean[0][0], clean[-1][0]
        return [[int(round(t0 + (t1 - t0) * i / (n - 1))),
                 int(round(at(t0 + (t1 - t0) * i / (n - 1))))]
                for i in range(n)]

    def _build_fancurve_section(self, parent):
        cfg = self._read_power_state("powerd.json") or {}
        fc = cfg.get("fan_curve", {})
        pts = self._fc_resample(fc.get("points") or self._FANCURVE_DEFAULT)

        lf = tb.Labelframe(parent, text="Custom fan curve (closed-loop)", padding=12)
        lf.pack(fill="x", pady=(14, 6))
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "A background daemon maps temperature → additive fan boost on a curve "
            "you set. It only ever *adds* airflow over the firmware curve, and "
            "restores automatic control when stopped. Needs the “Fan-curve + "
            "AC-switch daemon” tweak (Power tab) enabled to actually run at boot.")
            ).pack(anchor="w", pady=(0, 8))

        top = tb.Frame(lf); top.pack(fill="x")
        self._fc_enabled = tk.BooleanVar(value=bool(fc.get("enabled")))
        tb.Checkbutton(top, text="Fan curve enabled", variable=self._fc_enabled,
                       bootstyle="round-toggle").pack(side="left")
        tb.Label(top, text="   Drive from:").pack(side="left")
        self._fc_sensor = tk.StringVar(value=fc.get("sensor", "max"))
        for lbl, val in (("Hotter of CPU/GPU", "max"), ("CPU", "cpu"), ("GPU", "gpu")):
            tb.Radiobutton(top, text=lbl, value=val, variable=self._fc_sensor,
                           bootstyle="toolbutton").pack(side="left", padx=3)

        # n rows, split into two side-by-side blocks so 10+ points stay compact
        grid = tb.Frame(lf); grid.pack(anchor="w", pady=(10, 4))
        half = (FAN_CURVE_POINTS + 1) // 2
        for blk in (0, 1):
            col0 = blk * 3
            tb.Label(grid, text="Temp °C", width=9, bootstyle=SECONDARY
                     ).grid(row=0, column=col0)
            tb.Label(grid, text="Boost %", width=9, bootstyle=SECONDARY
                     ).grid(row=0, column=col0 + 1)
        self._fc_rows = []
        for r in range(FAN_CURVE_POINTS):
            t, b = pts[r]
            tv = tk.IntVar(value=int(t)); bv = tk.IntVar(value=int(b))
            gr, gc = (r + 1, 0) if r < half else (r - half + 1, 3)
            tb.Spinbox(grid, from_=25, to=105, textvariable=tv, width=7,
                       command=self._fc_redraw).grid(row=gr, column=gc, padx=4, pady=2)
            tb.Spinbox(grid, from_=0, to=100, textvariable=bv, width=7,
                       command=self._fc_redraw).grid(row=gr, column=gc + 1, padx=4, pady=2)
            self._fc_rows.append((tv, bv))

        self._fc_canvas = tk.Canvas(lf, height=120, bg="#0e1116", highlightthickness=0)
        self._fc_canvas.pack(fill="x", pady=(6, 6))
        self._fc_canvas.bind("<Configure>", lambda _e: self._fc_redraw())

        pr = tb.Frame(lf); pr.pack(anchor="w", pady=(6, 0))
        tb.Label(pr, text="Presets:", bootstyle=SECONDARY).pack(side="left", padx=(0, 6))
        for name in self._FANCURVE_PRESETS:
            tb.Button(pr, text=name, bootstyle=(INFO, "outline"),
                      command=lambda n=name: self._fc_apply_preset(n)).pack(side="left", padx=3)
        tb.Button(pr, text="Linear fill", bootstyle=(SECONDARY, "outline"),
                  command=self._fc_linfill).pack(side="left", padx=(12, 0))

        hr = tb.Frame(lf); hr.pack(anchor="w", pady=(8, 0))
        tb.Label(hr, text="Cool-down hysteresis").pack(side="left")
        self._fc_hys = tk.IntVar(value=int(fc.get("hysteresis_c", 3)))
        tb.Spinbox(hr, from_=0, to=10, textvariable=self._fc_hys, width=6).pack(side="left", padx=6)
        tb.Label(hr, text="°C").pack(side="left")
        tb.Button(hr, text="Save curve", bootstyle=SUCCESS,
                  command=self._fc_save).pack(side="left", padx=(16, 0))
        self._fc_live = tb.Label(hr, text="", bootstyle=SECONDARY)
        self._fc_live.pack(side="left", padx=12)
        self._fc_redraw()

    _FANCURVE_PRESETS = {
        "Silent": [[45, 0], [55, 0], [62, 10], [68, 20], [74, 32],
                   [80, 45], [85, 60], [89, 75], [93, 90], [96, 100]],
        "Balanced": None,   # == _FANCURVE_DEFAULT, filled in _fc_apply_preset
        "Aggressive": [[38, 15], [45, 30], [52, 45], [58, 58], [64, 70],
                       [70, 80], [76, 88], [82, 94], [88, 98], [93, 100]],
    }

    def _fc_apply_preset(self, name: str):
        pts = self._FANCURVE_PRESETS.get(name) or self._FANCURVE_DEFAULT
        pts = self._fc_resample(pts)
        for (tv, bv), (t, b) in zip(self._fc_rows, pts):
            tv.set(int(t)); bv.set(int(b))
        self._fc_redraw()

    def _fc_linfill(self):
        """Spread every intermediate point on a straight line between the
        first and last row, so the user only has to place the two endpoints."""
        rows = self._fc_rows
        n = len(rows)
        t0, tN = rows[0][0].get(), rows[-1][0].get()
        b0, bN = rows[0][1].get(), rows[-1][1].get()
        if tN <= t0:
            tN = t0 + n
        for i, (tv, bv) in enumerate(rows):
            f = i / (n - 1)
            tv.set(int(round(t0 + (tN - t0) * f)))
            bv.set(int(round(b0 + (bN - b0) * f)))
        self._fc_redraw()

    def _fc_points(self) -> list:
        return sorted([[tv.get(), bv.get()] for tv, bv in self._fc_rows])

    def _fc_redraw(self):
        c = getattr(self, "_fc_canvas", None)
        if c is None:
            return
        c.delete("all")
        w = c.winfo_width() or 600
        h = int(c["height"])
        pad = 6
        tmin, tmax = 30, 100
        def X(t): return pad + (t - tmin) / (tmax - tmin) * (w - 2 * pad)
        def Y(b): return h - pad - b / 100 * (h - 2 * pad)
        for gb in (0, 25, 50, 75, 100):
            c.create_line(pad, Y(gb), w - pad, Y(gb), fill="#2b3542")
        pts = self._fc_points()
        acc = getattr(self, "accent", "#58a6ff")
        for (t0, b0), (t1, b1) in zip(pts, pts[1:]):
            c.create_line(X(t0), Y(b0), X(t1), Y(b1), fill=acc, width=2)
        for t, b in pts:
            c.create_oval(X(t) - 3, Y(b) - 3, X(t) + 3, Y(b) + 3, fill=acc, outline="")
            c.create_text(X(t), Y(b) - 10, text=f"{t}°", fill=CHART_AXIS, font=("Sans", 7))

        live = getattr(self, "_fc_live_point", None)
        if live is not None:
            lt, lb = live
            lt = max(tmin, min(tmax, lt))
            lb = max(0, min(100, lb))
            x, y = X(lt), Y(lb)
            c.create_oval(x - 6, y - 6, x + 6, y + 6, outline="#ff5555", width=2)
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#ff5555", outline="")

    def _fc_save(self):
        merged = self._read_power_state("powerd.json") or {}
        merged["fan_curve"] = {
            "enabled": bool(self._fc_enabled.get()),
            "sensor": self._fc_sensor.get(),
            "hysteresis_c": int(self._fc_hys.get()),
            "points": self._fc_points(),
        }
        self._write_power_state("powerd.json", merged)
        # nudge a running daemon (it re-reads on mtime change); also apply once now
        threading.Thread(target=self._fc_apply_now, daemon=True).start()
        self._log(f"[Fans] fan curve saved ({'on' if self._fc_enabled.get() else 'off'}, "
                  f"{self._fc_sensor.get()}, {self._fc_points()})")

    def _fc_apply_now(self):
        r = subprocess.run(["bash", "-c",
                            f"test -f {shlex.quote(str(BASE_DIR))}/tuxthrottle_powerd.py && "
                            f"python3 {shlex.quote(str(BASE_DIR))}/tuxthrottle_powerd.py once "
                            f"--user {shlex.quote(self.user)}"],
                           capture_output=True, text=True, timeout=20)
        out = (r.stdout or r.stderr or "").strip()
        if out:
            self._log(f"[Fans] {out.splitlines()[-1]}")

    def _fan_set_profile(self, name: str):
        ok, err = sensors.set_platform_profile(name)
        self._log(f"[Fans] thermal profile → {name}" + ("" if ok else f"  FAILED: {err}"))
        if ok:
            self._kbd_tie_to_profile(name)

    # Quiet=blue / Balanced=white / Performance=red, matching the physical
    # LED-per-profile convention other vendor tools (LenovoLegionLinux /
    # Legion-Linux-Toolkit) use — free status indicator on a single-zone
    # keyboard. Opt-in (off by default): see the checkbox in the Fans tab.
    _KBD_PROFILE_COLORS = {"quiet": "3B82F6", "balanced": "FFFFFF", "performance": "FF3B3B"}

    def _kbd_tie_toggle(self):
        self._write_power_state("kbd_profile_tie.json",
                                {"enabled": bool(self._kbd_tie_var.get())})
        if self._kbd_tie_var.get():
            self._kbd_tie_to_profile(sensors.get_platform_profile())

    def _kbd_tie_to_profile(self, name: str):
        if tuxthrottle_kbd is None or not getattr(self, "_kbd_tie_var", None) \
                or not self._kbd_tie_var.get():
            return
        color = self._KBD_PROFILE_COLORS.get((name or "").lower())
        if not color:
            return
        # tuxthrottle_kbd.set_all() blocks ~1-4s (OpenRGB round-trip + its
        # own retry write) and is internally lock-serialized against other
        # writers — always call it off the GUI thread, never in a loop.
        def work():
            try:
                tuxthrottle_kbd.set_all(color)
                self._log(f"[Keyboard] tied to profile '{name}' → #{color}")
            except Exception as exc:  # noqa: BLE001
                self._log(f"[Keyboard] profile tie-in failed: {exc}")
        threading.Thread(target=work, daemon=True).start()

    def _fan_set_boost(self, index: int):
        pct = self._fan_boost_vars[index].get()
        ok, err = sensors.set_fan_boost(index, round(pct * 255 / 100))
        self._log(f"[Fans] fan {index} boost → {pct}%" + ("" if ok else f"  FAILED: {err}"))

    def _fan_boost_60s(self):
        prev_raw = {i: round(bv.get() * 255 / 100) for i, bv in self._fan_boost_vars.items()}
        for i, bv in self._fan_boost_vars.items():
            bv.set(100)
            self._fan_set_boost(i)
        restore = "; ".join(f"sensors.set_fan_boost({i}, {v})" for i, v in prev_raw.items())
        script = f"import sys; sys.path.insert(0, {str(BASE_DIR)!r}); import sensors; {restore}"
        unit = f"tuxthrottle-fanboost-{int(time.time())}"
        try:
            subprocess.run(
                ["systemd-run", f"--unit={unit}", "--on-active=60", "--collect",
                 "--description=TuxThrottle: restore fan boost after a 60s burst",
                 "/usr/bin/python3", "-c", script],
                capture_output=True, timeout=10, text=True,
            )
            was = ", ".join(f"fan {i}: {round(v / 255 * 100)}%" for i, v in prev_raw.items())
            self._log(f"[Fans] boosted to 100% for 60s — auto-restoring to ({was}) after; "
                      "the revert runs as an independent timer, so it still fires even if "
                      "you close TuxThrottle")
            fixlog.log_event("fan-boost-60s", f"boosted to 100% for 60s, will restore {was}",
                             user=self.user)
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(f"[Fans] boosted to 100% but couldn't schedule the auto-revert "
                      f"({exc}) — set the sliders back manually in ~60s")

    def _fan_manual_toggle(self):
        on = self._fan_manual.get()
        if on and not messagebox.askyesno(
            "Manual fan control",
            "This takes the fans off the firmware's automatic curve. They stay "
            "floored so they can't stop, but keep an eye on temperatures and use "
            "“Restore automatic” when you're done.\n\nProceed?"):
            self._fan_manual.set(False)
            on = False
        for r in self._fan_pwm_box.winfo_children():
            for w in r.winfo_children():
                try:
                    w.configure(state="normal" if on else "disabled")
                except tk.TclError:
                    pass
        if on:
            for i in self._fan_pwm_vars:
                self._fan_set_pwm(i)

    def _fan_set_pwm(self, index: int):
        if not self._fan_manual.get():
            return
        pct = self._fan_pwm_vars[index].get()
        ok, err = sensors.set_pwm_manual(index, round(pct * 255 / 100))
        self._log(f"[Fans] fan {index} manual PWM → {pct}%" + ("" if ok else f"  FAILED: {err}"))

    def _fan_restore(self):
        ok, err = sensors.restore_fan_auto()
        self._fan_manual.set(False)
        if hasattr(self, "_fan_pwm_box"):
            self._fan_manual_toggle()
        for bv in self._fan_boost_vars.values():
            bv.set(0)
        self._log("[Fans] restored automatic control" + ("" if ok else f"  (errors: {err})"))

    def _fan_preset(self, kind: str):
        prof = {"auto": "balanced", "cool": "performance", "max": "performance"}[kind]
        boost = {"auto": 0, "cool": 60, "max": 100}[kind]
        if kind == "auto":
            sensors.restore_fan_auto()
        if hasattr(self, "_fan_profile_var") and prof in sensors.platform_profile_choices():
            sensors.set_platform_profile(prof)
            self._fan_profile_var.set(prof)
            self._kbd_tie_to_profile(prof)
        for i, bv in self._fan_boost_vars.items():
            bv.set(boost)
            sensors.set_fan_boost(i, round(boost * 255 / 100))
        self._log(f"[Fans] preset: {kind} (profile {prof}, boost {boost}%)")

    def _fan_poll(self):
        if not getattr(self, "_fan_live", False):
            return
        for fan in sensors.read_fans():
            lab = self._fan_rpm_labels.get(fan["index"])
            if lab is not None:
                try:
                    lab.configure(text=f"{fan['rpm']} rpm")
                except tk.TclError:
                    pass
        live = getattr(self, "_fc_live", None)
        if live is not None:
            try:
                cpu = sensors.read_cpu_temp_c_value()
                _c, gt, _u, _p = sensors.read_dgpu_values()
                sensor = self._fc_sensor.get()
                temp = {"cpu": cpu, "gpu": gt}.get(sensor)
                if temp is None:
                    temp = max([v for v in (cpu, gt) if v is not None] or [0])
                tgt = fancurve_interp(self._fc_points(), temp)
                live.configure(text=f"now {temp:.0f}°C → target boost {tgt:.0f}%")
                self._fc_live_point = (temp, tgt)
                self._fc_redraw()
            except (tk.TclError, ValueError):
                pass
        self.root.after(2000, self._fan_poll)

    # ---------- Power & Limits tab ----------

    # (STAPM, fast, slow) Watts. STAPM (sustained ceiling) is kept >= slow so the
    # SMU doesn't clamp it. Dell's stock envelope on this board is 65/65/54.
    _TDP_PRESETS = {
        "Quiet":       (25, 35, 25),
        "Balanced":    (42, 54, 42),
        "Performance": (65, 80, 54),
    }

    def _power_state_path(self, name: str) -> Path:
        try:
            home = Path(pwd.getpwnam(self.user).pw_dir)
        except (KeyError, Exception):  # noqa: BLE001
            home = Path.home()
        return home / ".config" / "tuxthrottle" / name

    def _read_power_state(self, name: str) -> dict:
        try:
            d = json.loads(self._power_state_path(name).read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_power_state(self, name: str, data: dict) -> None:
        """Persist a limit so a later-installed boot service can re-apply it.
        Best-effort; chowns back to the real user when running elevated."""
        p = self._power_state_path(name)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2, sort_keys=True))
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                os.chown(p, pw.pw_uid, pw.pw_gid)
                os.chown(p.parent, pw.pw_uid, pw.pw_gid)
        except (OSError, KeyError):
            pass

    def _build_battery_health_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Battery")
        frame = self._scroll_body(outer, pad=16)

        info = self._probe("bat_health")
        if not info:
            tb.Label(frame, bootstyle=SECONDARY, wraplength=1000, justify="left",
                     text="No battery detected (/sys/class/power_supply/BAT* is "
                          "empty) — this page is for laptops.").pack(anchor="w")
            return

        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY,
                 text="Battery wear and charge cycles, read straight from the "
                      "kernel power-supply sysfs. Wear is how much of the pack's "
                      "original design capacity is gone; keeping the charge limit "
                      "below 100 % (section further down) slows it.").pack(
            anchor="w", pady=(0, 14))

        # --- health card ---
        hf = tb.Labelframe(frame, text="Health", padding=12)
        hf.pack(fill="x", pady=6)

        wear = info.get("wear_pct")
        wf = tb.Frame(hf); wf.pack(fill="x", pady=(0, 10))
        tb.Label(wf, text="Wear", width=18, anchor="w").pack(side="left")
        if wear is None:
            tb.Label(wf, text="n/a (battery doesn't report design capacity)",
                     bootstyle=SECONDARY).pack(side="left")
        else:
            style = (SUCCESS if wear < 15 else WARNING if wear < 30 else DANGER)
            tb.Label(wf, text=f"{wear:.1f}%", bootstyle=style,
                     font=("", 15, "bold")).pack(side="left")
            tb.Label(wf, bootstyle=SECONDARY,
                     text=f"   design {info['design']} {info['unit']}"
                          f"  →  now holds {info['full']} {info['unit']}").pack(side="left")

        rows = [
            ("Charge cycles", info.get("cycle_count")),
            ("Chemistry", info.get("technology")),
            ("Manufacturer", info.get("manufacturer")),
            ("Model", info.get("model")),
        ]
        for cap, val in rows:
            if val in (None, ""):
                continue
            r = tb.Frame(hf); r.pack(fill="x", pady=2)
            tb.Label(r, text=cap, width=18, anchor="w").pack(side="left")
            tb.Label(r, text=str(val), bootstyle=SECONDARY).pack(side="left")

        # --- live card ---
        lf = tb.Labelframe(frame, text="Now", padding=12)
        lf.pack(fill="x", pady=6)
        self._bath_live = {}
        for key, cap in (("charge", "Charge"), ("state", "State"),
                         ("rate", "Power flow"), ("eta", "Time remaining"),
                         ("voltage", "Voltage")):
            r = tb.Frame(lf); r.pack(fill="x", pady=2)
            tb.Label(r, text=cap, width=18, anchor="w").pack(side="left")
            v = tb.Label(r, text="—", bootstyle=SECONDARY)
            v.pack(side="left")
            self._bath_live[key] = v

        # --- charge-limit controls, same section as Power & Limits (namespaced
        #     so the two instances don't clobber each other) ---
        self._build_battery_section(frame, prefix="_bath_bat")

        # --- charging speed (Dell libsmbios) ---
        if sensors._smbios_battery_ctl():
            mode = self._probe("bat_mode")
            cf = tb.Labelframe(frame, text="Charging speed", padding=12)
            cf.pack(fill="x", pady=6)
            note = ("Express charges the pack faster (more heat, a little more "
                    "wear); Standard is the gentler default. Firmware setting — "
                    "persists with no service.")
            if mode is None:
                note += "  (current mode unreadable on this firmware — setting still works)"
            tb.Label(cf, bootstyle=SECONDARY, wraplength=1000, justify="left",
                     text=note).pack(anchor="w", pady=(0, 6))
            self._chg_mode = tk.StringVar(value=mode or "standard")
            row = tb.Frame(cf); row.pack(anchor="w")
            for m in ("standard", "express"):
                tb.Radiobutton(row, text=m.capitalize(), value=m,
                               variable=self._chg_mode, bootstyle="toolbutton",
                               command=self._apply_charge_mode).pack(side="left", padx=3)

        self._bath_live_on = True
        self._bath_poll()

    def _apply_charge_mode(self):
        m = self._chg_mode.get()
        ok, err = sensors.set_battery_charge_mode(m)
        self._log(f"[Battery] charging mode → {m}" + ("" if ok else f"  FAILED: {err}"))

    def _bath_poll(self):
        if not getattr(self, "_bath_live_on", False):
            return
        try:
            i = sensors.battery_health_info()
            cap = i.get("capacity_pct")
            self._bath_live["charge"].config(
                text=f"{cap}%" if cap is not None else "—")
            self._bath_live["state"].config(text=i.get("status") or "—")
            pw = i.get("power_w")
            st = (i.get("status") or "").lower()
            arrow = "→ in" if st == "charging" else "← out" if st == "discharging" else ""
            self._bath_live["rate"].config(
                text=f"{pw:.1f} W {arrow}".strip() if pw is not None else "—")
            em, ek = i.get("eta_min"), i.get("eta_kind")
            if em and ek:
                h, m = divmod(int(em), 60)
                pretty = (f"{h} h {m:02d} m" if h else f"{m} m")
                self._bath_live["eta"].config(text=f"~{pretty} {ek} at this rate")
            else:
                self._bath_live["eta"].config(
                    text="—" if (i.get("status") or "").lower() in ("charging", "discharging")
                    else "full / plugged in")
            vv = i.get("voltage_v")
            self._bath_live["voltage"].config(
                text=f"{vv:.2f} V" if vv is not None else "—")
            live = getattr(self, "_bath_bat_live", None)
            if live is not None:
                cl = sensors.battery_charge_limit_info().get("current")
                live.configure(text=f"now: {cl} %" if cl is not None else "now: — %")
        except Exception:  # noqa: BLE001
            pass
        self.root.after(4000, self._bath_poll)

    # ------------------------------------------------------------------ #
    #  VRAM budget — a laptop iGPU shares a small slice of system RAM as
    #  VRAM and the KDE desktop fills it; keep the dGPU free for editing /
    #  games / 3D. Tiers are pure KWin/Plasma config (vendor-agnostic);
    #  the live panel + GPU names are all read from real hardware.
    # ------------------------------------------------------------------ #
    _VRAM_TIERS = (
        ("regular", "Regular",
         "Full desktop — every effect, image wallpaper, all window "
         "previews kept in VRAM. The baseline your settings started at."),
        ("medium", "Medium",
         "Blur & background-contrast off, quicker animations, cheaper "
         "texture filtering, fewer hidden-window pixmaps kept. Barely "
         "visible; frees tens of MiB."),
        ("extreme", "Extreme",
         "Everything in Medium plus: solid-colour wallpaper (drops a "
         "full-screen texture per screen), no Overview / Present-Windows "
         "/ Desktop-Grid, nearest-neighbour textures, hidden windows drop "
         "their pixmaps, on-screen (Maliit) keyboard off. Restarts the "
         "panel; the keyboard and some savings only fully apply next login."),
    )

    def _vram_gpu_name(self, pci: str) -> str:
        for d in sensors.gpu_devices():
            if d.get("pci", "").lower() == (pci or "").lower():
                return d["name"]
        return ""

    def _vram_gpu_choices(self):
        """(value, caption, desc) rows for the desktop-GPU selector, built
        from the GPUs actually present."""
        try:
            gpus = sensors.drm_gpus()
        except Exception:  # noqa: BLE001
            gpus = []
        ig = next((g for g in gpus if g["kind"] == "integrated"), None)
        dg = next((g for g in gpus if g["kind"] == "discrete"), None)
        ig_name = (self._vram_gpu_name(ig["pci"]) if ig else "") or "integrated GPU"
        dg_name = (self._vram_gpu_name(dg["pci"]) if dg else "") or "discrete GPU"
        rows = [
            ("auto", "Automatic",
             f"Let KWin choose — normally the {ig_name}."),
            ("igpu", f"Integrated — {ig_name} (pin)",
             "Pin the compositor to the integrated GPU so a driver / device "
             "re-enumeration can't move it. Usually the same render path as "
             "Automatic, just nailed down."),
        ]
        rows.append((
            "dgpu", f"Discrete — {dg_name}",
            "Pin the whole desktop to the discrete GPU. Not possible when the "
            "panel is wired to the integrated GPU (muxless) — most hybrid "
            "laptops; offered only where a hardware MUX exists."))
        return rows

    def _build_vram_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="VRAM")
        frame = self._scroll_body(outer, pad=16)

        if tuxthrottle_vram is None:
            tb.Label(frame, bootstyle=WARNING,
                     text="tuxthrottle_vram helper failed to import.").pack(anchor="w")
            return

        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY,
                 text="A laptop's integrated GPU shares a small slice of system "
                      "RAM as video memory and the KDE/Wayland desktop routinely "
                      "fills it (spilling to slower GTT); the discrete GPU is "
                      "kept free for video editing, games and 3D. Lower tiers "
                      "strip desktop eye-candy to shrink the compositor's "
                      "footprint. Everything below is read live from your "
                      "hardware.").pack(anchor="w", pady=(0, 14))

        self._vram_q = queue.Queue()
        self._vram_bars = {}

        lf = tb.Labelframe(frame, text="Live VRAM usage", padding=12)
        lf.pack(fill="x", pady=6)
        for g in sensors.drm_gpus():
            name = self._vram_gpu_name(g["pci"]) or g["driver"] or g["pci"]
            row = tb.Frame(lf)
            row.pack(fill="x", pady=3)
            tb.Label(row, text=f"{name}  ({g['kind']})", width=36,
                     anchor="w").pack(side="left")
            pb = tb.Progressbar(row, maximum=100, length=240, bootstyle=INFO)
            pb.pack(side="left", padx=8)
            vl = tb.Label(row, text="…", width=26, anchor="w")
            vl.pack(side="left")
            self._vram_bars[g["pci"].lower()] = (pb, vl)
        if not self._vram_bars:
            tb.Label(lf, bootstyle=SECONDARY,
                     text="no render GPU found under /sys/class/drm").pack(anchor="w")
        self._vram_consumers_lbl = tb.Label(
            lf, justify="left", bootstyle=SECONDARY, font=("Monospace", 9))
        self._vram_consumers_lbl.pack(anchor="w", pady=(8, 0))

        br = tb.Frame(lf)
        br.pack(fill="x", pady=(10, 0))
        b1 = tb.Button(br, text="↻  Free VRAM now", bootstyle=(INFO, "outline"),
                       command=self._vram_free)
        b1.pack(side="left")
        self._tip(b1, "Evict the iGPU's cached buffers to system RAM (they page "
                  "back in as needed). Good before/after a game or a Resolve "
                  "session to clear accumulated slack.")
        b2 = tb.Button(br, text="Restart compositor", bootstyle=(WARNING, "outline"),
                       command=self._vram_restart_compositor)
        b2.pack(side="left", padx=8)
        self._tip(b2, "Also restart KWin — releases allocations the evict can't. "
                  "Windows stay open; the screen blacks for about a second.")

        lf2 = tb.Labelframe(frame, text="VRAM budget tier", padding=12)
        lf2.pack(fill="x", pady=6)
        self._vram_tier_var = tk.StringVar(value=tuxthrottle_vram.current_tier())
        for val, cap, desc in self._VRAM_TIERS:
            tb.Radiobutton(lf2, text=cap, value=val,
                           variable=self._vram_tier_var,
                           command=self._vram_apply_tier).pack(anchor="w", pady=(6, 0))
            tb.Label(lf2, text=desc, bootstyle=SECONDARY, wraplength=1000,
                     justify="left").pack(anchor="w", padx=26)
        tb.Label(lf2, bootstyle=SECONDARY, wraplength=1000, justify="left",
                 text="“Regular” restores the exact KWin/Plasma values captured "
                      "the first time you left it — not necessarily stock Plasma "
                      "defaults.").pack(anchor="w", pady=(8, 0))

        lf3 = tb.Labelframe(frame, text="Which GPU renders the desktop", padding=12)
        lf3.pack(fill="x", pady=6)
        tb.Label(lf3, bootstyle=WARNING, wraplength=1000, justify="left",
                 text="Takes effect after you log out and back in. If the "
                      "desktop then fails to start and drops you at the login "
                      "screen: switch to a text console (Ctrl+Alt+F3), log in, "
                      "and run  rm ~/.config/plasma-workspace/env/"
                      "09-tuxthrottle-gpu.sh").pack(anchor="w")
        try:
            _modes = set(tuxthrottle_vram.compositor_gpu_modes())
        except Exception:  # noqa: BLE001
            _modes = {"auto", "igpu", "dgpu"}
        self._vram_gpu_var = tk.StringVar(
            value=tuxthrottle_vram.current_compositor_gpu())
        for val, cap, desc in self._vram_gpu_choices():
            state = "normal" if val in _modes else "disabled"
            tb.Radiobutton(lf3, text=cap, value=val, variable=self._vram_gpu_var,
                           state=state,
                           command=self._vram_apply_gpu).pack(anchor="w", pady=(6, 0))
            tb.Label(lf3, text=desc, bootstyle=SECONDARY, wraplength=1000,
                     justify="left").pack(anchor="w", padx=26)

        lf4 = tb.Labelframe(frame, text="Discrete GPU idle power", padding=12)
        lf4.pack(fill="x", pady=6)
        pm = sensors.nvidia_runtime_pm()
        if pm:
            self._vram_rtd3_var = tk.BooleanVar(value=pm["control"] == "auto")
            tb.Checkbutton(
                lf4, text="Let the dGPU power down when idle (runtime PM)",
                variable=self._vram_rtd3_var,
                command=self._vram_apply_rtd3).pack(anchor="w")
            tb.Label(lf4, bootstyle=SECONDARY, wraplength=1000, justify="left",
                     text="Frees its VRAM and ~5 W when nothing uses it; it wakes "
                          "on its own for a PRIME-offloaded app. Live only — add "
                          "the “NVIDIA runtime power management” tweak on the GPU "
                          "tab to make it stick across reboots.").pack(
                anchor="w", pady=(2, 0))
        else:
            tb.Label(lf4, text="No NVIDIA GPU detected.",
                     bootstyle=SECONDARY).pack(anchor="w")

        self._vram_live = False
        self._poll_vram_queue()

    def _vram_helper(self, args: str) -> str:
        return f"python3 {BASE_DIR}/tuxthrottle_vram.py {args}"

    def _vram_poll(self):
        if not getattr(self, "_vram_live", False):
            return
        threading.Thread(target=self._vram_poll_worker, daemon=True).start()
        self.root.after(5000, self._vram_poll)

    def _vram_poll_worker(self):
        try:
            info = sensors.vram_info()
            cons = sensors.vram_consumers(8)
        except Exception:  # noqa: BLE001
            info, cons = [], []
        self._vram_q.put((info, cons))

    def _poll_vram_queue(self):
        try:
            while True:
                info, cons = self._vram_q.get_nowait()
                self._vram_apply(info, cons)
        except queue.Empty:
            pass
        self.root.after(400, self._poll_vram_queue)

    def _vram_apply(self, info, cons):
        for g in info:
            pair = self._vram_bars.get((g.get("pci") or "").lower())
            if not pair:
                continue
            pb, vl = pair
            if g.get("asleep"):
                pb.configure(value=0)
                vl.configure(text="asleep")
                continue
            u, t = g.get("used_mb"), g.get("total_mb")
            if u is None or not t:
                pb.configure(value=0)
                vl.configure(text="n/a")
                continue
            pb.configure(value=round(100 * u / t))
            gtt = f"  +{g['gtt_used_mb']} GTT" if g.get("gtt_used_mb") else ""
            vl.configure(text=f"{u} / {t} MiB  ({g['pct']}%){gtt}")
        if cons:
            txt = "\n".join(
                f"{c['vram_mb']:>7.0f} MiB  {c['comm'][:22]:<22} [{c['driver']}]"
                for c in cons)
            self._vram_consumers_lbl.configure(text="holding VRAM now:\n" + txt)
        else:
            self._vram_consumers_lbl.configure(text="")

    def _vram_free(self):
        self._run_stream("free VRAM (evict iGPU caches)",
                         self._vram_helper("free"), tag="VRAM")

    def _vram_restart_compositor(self):
        if not messagebox.askyesno(
            "Restart compositor",
            "Restart KWin to release its VRAM allocations.\n\nOpen windows stay "
            "put; the screen blacks for about a second. Continue?"):
            return
        self._run_stream("free VRAM + restart compositor",
                         self._vram_helper("free --restart-compositor"), tag="VRAM")

    def _vram_apply_tier(self):
        self._run_stream(f"VRAM budget → {self._vram_tier_var.get()}",
                         self._vram_helper(f"profile {self._vram_tier_var.get()}"),
                         tag="VRAM")

    def _vram_apply_gpu(self):
        mode = self._vram_gpu_var.get()
        self._run_stream(f"desktop GPU → {mode}",
                         self._vram_helper(f"compositor-gpu {mode}"), tag="VRAM")
        messagebox.showinfo(
            "Log out to apply",
            "The desktop-GPU choice is written. Log out and back in for KWin "
            "to pick it up.")

    def _vram_apply_rtd3(self):
        ok, msg = sensors.set_nvidia_runtime_pm(self._vram_rtd3_var.get())
        self.status_var.set(msg)
        if not ok:
            messagebox.showwarning("Runtime PM", msg)

    def _build_power_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Power & Limits")
        frame = self._scroll_body(outer, pad=16)

        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY,
                 text="Live power/thermal envelope controls — the Linux equivalent "
                      "of ThrottleStop / the ASUS Armoury tuning sliders. Changes "
                      "apply immediately; installing the matching tweak on the Power "
                      "tab makes them stick across a reboot.").pack(anchor="w", pady=(0, 14))

        self._build_tdp_section(frame)
        self._build_co_section(frame)
        self._build_nvpl_section(frame)
        self._build_gpuclock_section(frame)
        self._build_gpumode_section(frame)
        self._build_battery_section(frame)
        self._build_autoswitch_section(frame)

        self._power_live = True
        self._power_poll()

    # --- CPU TDP (ryzenadj) ---

    def _build_tdp_section(self, parent):
        lf = tb.Labelframe(parent, text="CPU power limits — Ryzen 7 5800H (ryzenadj)",
                           padding=12)
        lf.pack(fill="x", pady=6)
        if not self._probe("ryzenadj_avail"):
            tb.Label(lf, bootstyle=WARNING, wraplength=1000, justify="left",
                     text="ryzenadj isn't installed. Add the “CPU TDP control "
                          "(ryzenadj)” tweak on the Power tab, then reopen this tab.").pack(anchor="w")
            return
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY,
                 text="STAPM = sustained limit (long window), Fast = short burst, "
                      "Slow = medium window. Higher = more performance, more heat.").pack(anchor="w", pady=(0, 8))

        self._tdp_vars = {}
        self._tdp_val_labels = {}
        for key, cap in (("stapm", "STAPM (sustained)"), ("fast", "Fast (burst)"),
                         ("slow", "Slow (medium)")):
            r = tb.Frame(lf); r.pack(fill="x", pady=4)
            tb.Label(r, text=cap, width=20, anchor="w").pack(side="left")
            v = tk.IntVar(value=45)
            self._tdp_vars[key] = v
            sc = tb.Scale(r, from_=10, to=90, variable=v, orient="horizontal", length=300)
            sc.pack(side="left", fill="x", expand=True)
            sc.bind("<ButtonRelease-1>", lambda _e: self._tdp_apply())
            tb.Label(r, textvariable=v, width=3).pack(side="left")
            tb.Label(r, text="W").pack(side="left", padx=(0, 8))
            live = tb.Label(r, text="now: — W", width=12, bootstyle=SECONDARY)
            live.pack(side="left")
            self._tdp_val_labels[key] = live

        pr = tb.Frame(lf); pr.pack(anchor="w", pady=(10, 0))
        tb.Label(pr, text="Presets:", bootstyle=SECONDARY).pack(side="left", padx=(0, 6))
        for name in self._TDP_PRESETS:
            tb.Button(pr, text=name, bootstyle=(INFO, "outline"),
                      command=lambda n=name: self._tdp_preset(n)).pack(side="left", padx=3)
        tb.Button(pr, text="Firmware default", bootstyle=(SECONDARY, "outline"),
                  command=self._tdp_reset).pack(side="left", padx=(12, 0))

    def _tdp_preset(self, name: str):
        stapm, fast, slow = self._TDP_PRESETS[name]
        self._tdp_vars["stapm"].set(stapm)
        self._tdp_vars["fast"].set(fast)
        self._tdp_vars["slow"].set(slow)
        self._tdp_apply(note=f"preset {name}")

    def _tdp_reset(self):
        # No portable "reset to BIOS" in ryzenadj; re-assert the board's stock
        # 5800H envelope (54/54/54 STAPM/slow, 65 fast is Dell's default here).
        self._tdp_vars["stapm"].set(54)
        self._tdp_vars["fast"].set(65)
        self._tdp_vars["slow"].set(54)
        self._tdp_apply(note="firmware default")

    def _tdp_apply(self, note: str = ""):
        vals = {k: v.get() for k, v in self._tdp_vars.items()}
        self._write_power_state("tdp.json", vals)
        tail = f" ({note})" if note else ""

        def work():
            ok, err = sensors.set_ryzenadj_limits(
                fast_w=vals["fast"], slow_w=vals["slow"], stapm_w=vals["stapm"])
            self._log(f"[Power] TDP → STAPM {vals['stapm']} / fast {vals['fast']} / "
                      f"slow {vals['slow']} W{tail}" + ("" if ok else f"  FAILED: {err}"))

        threading.Thread(target=work, daemon=True).start()

    # --- Ryzen Curve Optimizer (undervolt) ---

    def _build_co_section(self, parent):
        if not self._probe("ryzenadj_co"):
            return
        lf = tb.Labelframe(parent, text="Curve Optimizer — all-core undervolt  (advanced)",
                           padding=12)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=DANGER, wraplength=1000, justify="left",
                 text="⚠  An undervolt that's too aggressive causes silent errors, a "
                      "segfault storm, or a hard hang (needs a full power-off). "
                      "'Apply & stress-test' snapshots first, runs a stress-ng + GPU "
                      "load for 5 min while watching dmesg for MCE/WHEA, and auto-"
                      "reverts on any fault. The offset is NOT kept across a reboot "
                      "until you press “Keep”.").pack(anchor="w", pady=(0, 8))

        co = self._read_power_state("co.json")
        r = tb.Frame(lf); r.pack(fill="x", pady=4)
        tb.Label(r, text="All-core offset", width=20, anchor="w").pack(side="left")
        self._co_var = tk.IntVar(value=int(co.get("offset", 0) or 0))
        sc = tb.Scale(r, from_=0, to=-40, variable=self._co_var,
                      orient="horizontal", length=300)
        sc.pack(side="left", fill="x", expand=True)
        tb.Label(r, textvariable=self._co_var, width=4).pack(side="left")
        self._co_live = tb.Label(r, text="", width=26, bootstyle=SECONDARY)
        self._co_live.pack(side="left")

        br = tb.Frame(lf); br.pack(anchor="w", pady=(10, 0))
        tb.Button(br, text="Apply & stress-test (5 min)", bootstyle=(WARNING, "outline"),
                  command=self._co_stress).pack(side="left", padx=3)
        tb.Button(br, text="Keep (confirm)", bootstyle=(SUCCESS, "outline"),
                  command=lambda: self._co_action("confirm")).pack(side="left", padx=3)
        tb.Button(br, text="Revert to 0", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._co_action("revert")).pack(side="left", padx=3)
        self._co_refresh_live()

    def _co_refresh_live(self):
        co = self._read_power_state("co.json")
        if not co:
            txt = "now: stock (0)"
        else:
            txt = (f"now: {co.get('offset', 0)}  "
                   + ("✓ kept" if co.get("confirmed") else "· not kept (reboots off)"))
        try:
            self._co_live.configure(text=txt)
        except (AttributeError, tk.TclError):
            pass

    def _co_stress(self):
        v = int(self._co_var.get())
        if v >= 0:
            messagebox.showinfo("Curve Optimizer",
                                "Set a negative offset first (e.g. -20).")
            return
        if not messagebox.askyesno(
                "Stress-test undervolt",
                f"Apply --set-coall={v} and hammer the CPU + GPU for 5 minutes?\n\n"
                "It snapshots first and auto-reverts on any kernel error, but a "
                "bad offset can still hard-hang the machine (recoverable only by a "
                "full power-off). Continue?"):
            return
        self._run_stream(f"Curve Optimizer stress-test (offset {v})",
                         f"python3 {shlex.quote(str(BASE_DIR))}/tuxthrottle_co_stress.py "
                         f"apply {v} --minutes 5 --user {shlex.quote(self.user)}",
                         tag="Power")
        self.root.after(4000, self._co_refresh_live)

    def _co_action(self, action: str):
        def work():
            r = subprocess.run(
                ["python3", str(BASE_DIR / "tuxthrottle_co_stress.py"), action,
                 "--user", self.user],
                capture_output=True, text=True)
            self._log(f"[Power] Curve Optimizer {action}: "
                      + (r.stdout or r.stderr or "").strip())
            self.root.after(0, self._co_refresh_live)
        threading.Thread(target=work, daemon=True).start()

    # --- NVIDIA board power limit ---

    def _build_nvpl_section(self, parent):
        if not self.has_nvidia:
            return
        lf = tb.Labelframe(parent, text="NVIDIA board power limit — RTX 3050 Ti",
                           padding=12)
        lf.pack(fill="x", pady=6)
        info = self._probe("nvpl")
        if info is not None and not info.get("supported", True):
            tb.Label(lf, bootstyle=WARNING, wraplength=1000, justify="left",
                     text="This laptop's GPU firmware locks the board power limit "
                          "(NVIDIA Dynamic Boost manages it) — nvidia-smi -pl is "
                          "rejected on the G15 5515. Nothing to set here. Use the "
                          "'nvidia-max-perf' GPU tweak + the CPU TDP slider above "
                          "to influence the shared power/thermal budget instead.").pack(anchor="w")
            return
        self._nvpl_lf = lf
        self._nvpl_var = tk.IntVar(value=(info or {}).get("current") or 60)
        r = tb.Frame(lf); r.pack(fill="x", pady=4)
        tb.Label(r, text="Power limit", width=20, anchor="w").pack(side="left")
        lo = (info or {}).get("min", 30)
        hi = (info or {}).get("max", 80)
        self._nvpl_scale = tb.Scale(r, from_=lo, to=hi, variable=self._nvpl_var,
                                    orient="horizontal", length=300)
        self._nvpl_scale.pack(side="left", fill="x", expand=True)
        self._nvpl_scale.bind("<ButtonRelease-1>", lambda _e: self._nvpl_apply())
        tb.Label(r, textvariable=self._nvpl_var, width=3).pack(side="left")
        tb.Label(r, text="W").pack(side="left", padx=(0, 8))
        self._nvpl_live = tb.Label(r, text="now: — W", width=12, bootstyle=SECONDARY)
        self._nvpl_live.pack(side="left")
        br = tb.Frame(lf); br.pack(anchor="w", pady=(8, 0))
        if info and info.get("default"):
            tb.Button(br, text=f"Default ({info['default']} W)", bootstyle=(SECONDARY, "outline"),
                      command=lambda: (self._nvpl_var.set(info["default"]), self._nvpl_apply())
                      ).pack(side="left")
        self._nvpl_note = tb.Label(lf, bootstyle=SECONDARY, wraplength=1000,
                                   text="" if info else "dGPU is asleep — wake it (run something on it) "
                                        "to read/set the limit.")
        self._nvpl_note.pack(anchor="w", pady=(6, 0))

    def _nvpl_apply(self):
        w = self._nvpl_var.get()
        self._write_power_state("nvpl.json", {"watts": w})

        def work():
            ok, err = sensors.set_nvidia_power_limit(w)
            self._log(f"[Power] NVIDIA power limit → {w} W"
                      + ("" if ok else f"  FAILED: {err}"))

        threading.Thread(target=work, daemon=True).start()

    # --- NVIDIA graphics-clock lock (works where -pl is firmware-locked) ---

    def _build_gpuclock_section(self, parent):
        if not self.has_nvidia:
            return
        info = self._probe("nvclk")
        lf = tb.Labelframe(parent, text="NVIDIA GPU clock lock — RTX 3050 Ti",
                           padding=12)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "Clamps the dGPU graphics clock. Lowering the ceiling is the one GPU "
            "lever that works on this chassis (the board power limit is "
            "firmware-locked) — good for heat and battery; raising it back to the "
            "max is the default. Applies immediately; the “GPU clock lock at "
            "boot” tweak re-applies it after a reboot / resume.")
            ).pack(anchor="w", pady=(0, 8))
        if not info:
            self._gpuclk_note = tb.Label(lf, bootstyle=SECONDARY, text=(
                "dGPU is asleep — run something on it to read the clock range."))
            self._gpuclk_note.pack(anchor="w")
            return

        saved = self._read_power_state("nvclk.json")
        lo = int(info.get("gr_min") or 210)
        hi = int(info.get("gr_max") or 2100)
        self._gpuclk_min, self._gpuclk_max = lo, hi
        self._gpuclk_var = tk.IntVar(value=int(saved.get("gr_max") or hi))
        r = tb.Frame(lf); r.pack(fill="x", pady=4)
        tb.Label(r, text="Max graphics clock", width=20, anchor="w").pack(side="left")
        sc = tb.Scale(r, from_=lo, to=hi, variable=self._gpuclk_var,
                      orient="horizontal", length=300)
        sc.pack(side="left", fill="x", expand=True)
        sc.bind("<ButtonRelease-1>", lambda _e: self._gpuclk_apply())
        tb.Label(r, textvariable=self._gpuclk_var, width=5).pack(side="left")
        tb.Label(r, text="MHz").pack(side="left", padx=(0, 8))
        self._gpuclk_live = tb.Label(r, text="now: — MHz", width=14, bootstyle=SECONDARY)
        self._gpuclk_live.pack(side="left")

        br = tb.Frame(lf); br.pack(anchor="w", pady=(8, 0))
        for lbl, frac in (("Battery (−45%)", 0.55), ("Cool (−25%)", 0.75),
                          ("Full", 1.0)):
            mhz = lo if frac == 0.0 else round(lo + (hi - lo) * frac)
            tb.Button(br, text=lbl, bootstyle=(INFO, "outline"),
                      command=lambda m=mhz: (self._gpuclk_var.set(m),
                                             self._gpuclk_apply())
                      ).pack(side="left", padx=3)
        tb.Button(br, text="Unlock / reset", bootstyle=(SECONDARY, "outline"),
                  command=self._gpuclk_reset).pack(side="left", padx=(12, 0))
        tb.Label(lf, bootstyle=WARNING, wraplength=1000, justify="left", text=(
            "After applying, watch the Report a Bug log / dmesg for Xid errors; "
            "if the GPU misbehaves, hit “Unlock / reset”.")).pack(anchor="w", pady=(6, 0))

    def _gpuclk_apply(self):
        hi = int(self._gpuclk_var.get())
        lo = int(getattr(self, "_gpuclk_min", 210))
        self._write_power_state("nvclk.json", {"gr_min": lo, "gr_max": hi})

        def work():
            ok, err = sensors.set_nvidia_clock_lock(lo, hi)
            self._log(f"[Power] GPU clock lock → {lo}-{hi} MHz"
                      + ("" if ok else f"  FAILED: {err}"))

        threading.Thread(target=work, daemon=True).start()

    def _gpuclk_reset(self):
        try:
            self._power_state_path("nvclk.json").unlink()
        except (OSError, AttributeError):
            pass
        if getattr(self, "_gpuclk_var", None) is not None:
            self._gpuclk_var.set(int(getattr(self, "_gpuclk_max", 2100)))

        def work():
            ok, err = sensors.reset_nvidia_clocks()
            self._log("[Power] GPU clock lock → reset (unlocked)"
                      + ("" if ok else f"  FAILED: {err}"))

        threading.Thread(target=work, daemon=True).start()

    # ---------- Display tab ----------
    # Consolidates the panel-tuning controls that used to be scattered across
    # Power & Limits (refresh rate) and Battery (VRR, as a buried info line)
    # into one place — same idea as Legion-Linux-Toolkit's Display tab.

    def _build_display_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Display")
        frame = self._scroll_body(outer, pad=16)
        self._build_refresh_section(frame)
        self._build_vrr_section(frame)

    def _build_vrr_section(self, parent):
        vrr = self._probe("vrr")
        lf = tb.Labelframe(parent, text="Adaptive Sync (VRR)", padding=12)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1000, justify="left",
                 text=(f"{', '.join(vrr['capable'])} report VRR-capable — enable it "
                       f"per-display in System Settings → Display, and apply the "
                       f"KDE “allow tearing” tweak (Gaming category) for lowest latency."
                       if vrr["capable"]
                       else "no VRR-capable panel detected on this system")
                 ).pack(anchor="w")

    # ---------- Touchpad tab ----------
    # Live KWin D-Bus property writes (org.kde.KWin.InputDevice) — the
    # Wayland-native mechanism, not xinput (X11-only, doesn't exist here).
    # Session-only by design: nothing here is boot-persisted, so a disabled
    # touchpad can never survive past the next logout/reboot on its own —
    # see sensors.py's touchpad section docstring for why that's deliberate.

    def _build_touchpad_tab(self):
        info = self._probe("touchpad")
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Touchpad")
        frame = self._scroll_body(outer, pad=16)

        if not info or not info.get("available"):
            tb.Label(frame, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
                "No touchpad reachable via KWin's D-Bus interface — needs KDE "
                "Plasma on Wayland with a touchpad (xinput-based X11 toggles "
                "don't apply here).")).pack(anchor="w")
            return

        tb.Label(frame, wraplength=1000, justify="left", bootstyle=SECONDARY,
                 text=f"Device: {info.get('name') or '(unnamed)'}").pack(anchor="w", pady=(0, 12))

        ef = tb.Labelframe(frame, text="Enable / disable", padding=12)
        ef.pack(fill="x", pady=6)
        tb.Label(ef, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "Takes effect immediately. This is a live session setting, not a "
            "boot-persisted tweak — a reboot or logout always brings the "
            "touchpad back, so turning it off can't lock you out permanently.")
                 ).pack(anchor="w", pady=(0, 8))
        self._tp_enabled_var = tk.BooleanVar(value=bool(info.get("enabled", True)))
        tb.Checkbutton(ef, text="Touchpad enabled", variable=self._tp_enabled_var,
                       bootstyle="round-toggle",
                       command=lambda: self._touchpad_set("enabled", self._tp_enabled_var,
                                                          sensors.set_touchpad_enabled)
                       ).pack(anchor="w")

        tf = tb.Labelframe(frame, text="Behaviour", padding=12)
        tf.pack(fill="x", pady=6)
        self._tp_tap_var = tk.BooleanVar(value=bool(info.get("tap_to_click", True)))
        tb.Checkbutton(tf, text="Tap to click", variable=self._tp_tap_var,
                       bootstyle="round-toggle",
                       command=lambda: self._touchpad_set("tap_to_click", self._tp_tap_var,
                                                          sensors.set_touchpad_tap_to_click)
                       ).pack(anchor="w", pady=2)
        self._tp_scroll_var = tk.BooleanVar(value=bool(info.get("natural_scroll", False)))
        tb.Checkbutton(tf, text="Natural scrolling", variable=self._tp_scroll_var,
                       bootstyle="round-toggle",
                       command=lambda: self._touchpad_set(
                           "natural_scroll", self._tp_scroll_var,
                           sensors.set_touchpad_natural_scroll)
                       ).pack(anchor="w", pady=2)
        self._tp_dwt_var = tk.BooleanVar(value=bool(info.get("disable_while_typing", True)))
        tb.Checkbutton(tf, text="Disable while typing", variable=self._tp_dwt_var,
                       bootstyle="round-toggle",
                       command=lambda: self._touchpad_set(
                           "disable_while_typing", self._tp_dwt_var,
                           sensors.set_touchpad_disable_while_typing)
                       ).pack(anchor="w", pady=2)

    def _touchpad_set(self, label: str, var: tk.BooleanVar, setter):
        val = var.get()

        def work():
            ok, msg = setter(val)
            self._log(f"[Touchpad] {label} → {val}" + ("" if ok else f"  FAILED: {msg}"))
            if not ok:
                self.root.after(0, lambda: var.set(not val))  # snap the toggle back

        threading.Thread(target=work, daemon=True).start()

    # --- Panel refresh rate (KDE / KScreen) ---

    def _build_refresh_section(self, parent):
        info = self._probe("panel_modes")
        lf = tb.Labelframe(parent, text="Panel refresh rate", padding=12)
        lf.pack(fill="x", pady=6)
        if not info or len(info.get("rates", [])) < 2:
            tb.Label(lf, bootstyle=SECONDARY, wraplength=1000, justify="left", text=(
                "Needs kscreen-doctor (KDE) and a panel with more than one "
                "refresh rate. Nothing to switch here.")).pack(anchor="w")
            return
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "Dropping the high-refresh panel to 60 Hz on battery is a real power "
            "saving. Resolution is kept; KScreen remembers the choice across "
            "reboots.")).pack(anchor="w", pady=(0, 8))
        row = tb.Frame(lf); row.pack(anchor="w")
        cur = info.get("current_hz")
        self._refresh_var = tk.IntVar(
            value=int(round(cur)) if cur else info["rates"][-1])
        for hz in info["rates"]:
            tb.Radiobutton(row, text=f"{hz} Hz", value=hz,
                           variable=self._refresh_var, bootstyle="toolbutton",
                           command=lambda h=hz: self._refresh_apply(h)
                           ).pack(side="left", padx=4)
        self._refresh_now = tb.Label(
            lf, bootstyle=SECONDARY,
            text=f"current: {round(cur)} Hz" if cur else "current: unknown")
        self._refresh_now.pack(anchor="w", pady=(6, 0))
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "Tip: the AC/battery auto-switch on the Power & Limits tab can flip "
            "this with the charger — “AC → 120 Hz, battery → 60 Hz”.")
                 ).pack(anchor="w", pady=(6, 0))

    def _refresh_apply(self, hz: int):
        def work():
            ok, msg = sensors.set_panel_refresh(hz)
            self._log(f"[Power] panel refresh → {hz} Hz"
                      + (f"  ({msg})" if ok else f"  FAILED: {msg}"))
            if ok:
                self.root.after(0, lambda: self._refresh_now.configure(
                    text=f"current: {hz} Hz"))

        threading.Thread(target=work, daemon=True).start()

    # --- Battery charge limit ---

    def _build_battery_section(self, parent, prefix: str = "_bat"):
        # `prefix` namespaces the IntVar / "now:" label so this section can be
        # placed on two pages (Power & Limits and Battery) without the second
        # build clobbering the first's widget references.
        info = self._probe("bat_limit")
        lf = tb.Labelframe(parent, text="Battery charge limit", padding=12)
        lf.pack(fill="x", pady=6)
        if not info["supported"]:
            msg = ("This machine doesn't expose a charge-stop threshold "
                   "(no charge_control_end_threshold in sysfs).")
            if info.get("dell_libsmbios_possible"):
                msg += ("  On this Dell you can still get a firmware-level charge "
                        "limit — install the “Dell battery threshold (libsmbios)” "
                        "tweak on the Power tab, then reopen this tab.")
            tb.Label(lf, bootstyle=SECONDARY, wraplength=1000, justify="left",
                     text=msg).pack(anchor="w")
            return
        via = "firmware (libsmbios)" if info.get("method") == "libsmbios" else "kernel sysfs"
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY,
                 text="Stops charging at the set level to spare the cell when the "
                      f"laptop mostly runs on AC. 80 % is the usual longevity sweet spot. "
                      f"Controlled via {via}.").pack(anchor="w", pady=(0, 8))
        var = tk.IntVar(value=info["current"] or 100)
        setattr(self, f"{prefix}_var", var)
        r = tb.Frame(lf); r.pack(fill="x", pady=4)
        tb.Label(r, text="Stop charging at", width=20, anchor="w").pack(side="left")
        sc = tb.Scale(r, from_=50, to=100, variable=var,
                      orient="horizontal", length=300,
                      command=lambda _v: var.set(round(var.get() / 5) * 5))
        sc.pack(side="left", fill="x", expand=True)
        sc.bind("<ButtonRelease-1>", lambda _e: self._bat_apply(prefix))
        tb.Label(r, textvariable=var, width=3).pack(side="left")
        tb.Label(r, text="%").pack(side="left", padx=(0, 8))
        live = tb.Label(r, text="now: — %", width=12, bootstyle=SECONDARY)
        live.pack(side="left")
        setattr(self, f"{prefix}_live", live)
        br = tb.Frame(lf); br.pack(anchor="w", pady=(8, 0))
        for lbl, pct in (("60 %", 60), ("80 %", 80), ("Full (100 %)", 100)):
            tb.Button(br, text=lbl, bootstyle=(SECONDARY, "outline"),
                      command=lambda p=pct: (var.set(p), self._bat_apply(prefix))
                      ).pack(side="left", padx=3)

    def _bat_apply(self, prefix: str = "_bat"):
        p = getattr(self, f"{prefix}_var").get()
        self._write_power_state("battery.json", {"percent": p})
        ok, err = sensors.set_battery_charge_limit(p)
        self._log(f"[Power] battery charge limit → {p}%" + ("" if ok else f"  FAILED: {err}"))
        # keep the twin section (if built) in sync
        for other in ("_bat", "_bath_bat"):
            v = getattr(self, f"{other}_var", None)
            if v is not None and v.get() != p:
                v.set(p)

    # --- hybrid graphics mode (EnvyControl) ---

    def _build_gpumode_section(self, parent):
        if not self.has_nvidia:
            return
        lf = tb.Labelframe(parent, text="Hybrid graphics mode", padding=12)
        lf.pack(fill="x", pady=6)
        if not sensors.envycontrol_available():
            tb.Label(lf, bootstyle=WARNING, wraplength=1000, justify="left",
                     text="EnvyControl isn't installed. Install the “EnvyControl” "
                          "app (Presets / Software tab), then reopen this tab.").pack(anchor="w")
            return
        cur = self._probe("gpu_mode")
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "hybrid = both GPUs, dGPU on demand (default, best battery/perf balance). "
            "integrated = dGPU fully off (max battery, no NVIDIA rendering). "
            "nvidia = dGPU always on (max performance, worst battery). "
            "A switch takes effect after logging out or rebooting.")).pack(anchor="w", pady=(0, 8))
        row = tb.Frame(lf); row.pack(anchor="w")
        self._gpumode_var = tk.StringVar(value=cur or "hybrid")
        for m in ("integrated", "hybrid", "nvidia"):
            tb.Radiobutton(row, text=m.capitalize(), value=m, variable=self._gpumode_var,
                           bootstyle="toolbutton").pack(side="left", padx=3)
        tb.Button(row, text="Apply", bootstyle=(WARNING, "outline"),
                  command=self._gpumode_apply).pack(side="left", padx=(12, 0))
        self._gpumode_now = tb.Label(lf, bootstyle=SECONDARY,
                                     text=f"current: {cur or 'unknown'}")
        self._gpumode_now.pack(anchor="w", pady=(6, 0))

    def _gpumode_apply(self):
        mode = self._gpumode_var.get()
        if not messagebox.askyesno(
                "Switch graphics mode",
                f"Switch to '{mode}' graphics mode?\n\nThis rewrites the Xorg / "
                "display-manager config and only takes effect after you log out "
                "or reboot."):
            return

        def work():
            ok, err = sensors.gpu_mode_set(mode)
            self._log(f"[Power] graphics mode → {mode}"
                      + ("  (log out / reboot to apply)" if ok else f"  FAILED: {err}"))
            if ok:
                self.root.after(0, lambda: self._gpumode_now.configure(
                    text=f"current: {mode}  — log out or reboot to apply"))

        threading.Thread(target=work, daemon=True).start()

    # --- AC / battery auto profile switch (tuxthrottle_powerd.py) ---

    _AUTOSWITCH_BUNDLES = ("Quiet", "Balanced", "Performance")

    def _build_autoswitch_section(self, parent):
        cfg = self._read_power_state("powerd.json") or {}
        aw = cfg.get("autoswitch", {})
        lf = tb.Labelframe(parent, text="AC / battery auto profile switch", padding=12)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "When the charger is plugged or pulled, the fan-curve daemon applies a "
            "bundle: Quiet = balanced profile + 25/35/25 W TDP, Balanced = 42/54/42 W, "
            "Performance = performance profile + 65/80/54 W. Needs the “Fan-curve + "
            "AC-switch daemon” tweak enabled.")).pack(anchor="w", pady=(0, 8))
        self._aw_enabled = tk.BooleanVar(value=bool(aw.get("enabled")))
        tb.Checkbutton(lf, text="Auto-switch enabled", variable=self._aw_enabled,
                       bootstyle="round-toggle").pack(anchor="w")
        row = tb.Frame(lf); row.pack(anchor="w", pady=(8, 0))
        self._aw_on_ac = tk.StringVar(value=aw.get("on_ac", "Balanced"))
        self._aw_on_bat = tk.StringVar(value=aw.get("on_battery", "Quiet"))
        tb.Label(row, text="On AC →", width=12, anchor="w").pack(side="left")
        tb.Combobox(row, textvariable=self._aw_on_ac, values=self._AUTOSWITCH_BUNDLES,
                    state="readonly", width=14).pack(side="left", padx=(0, 16))
        tb.Label(row, text="On battery →", width=12, anchor="w").pack(side="left")
        tb.Combobox(row, textvariable=self._aw_on_bat, values=self._AUTOSWITCH_BUNDLES,
                    state="readonly", width=14).pack(side="left")

        self._aw_refresh_rates = []
        pm = self._probe("panel_modes")
        if pm and len(pm.get("rates", [])) > 1:
            self._aw_refresh_rates = pm["rates"]
            opts = ["leave alone"] + [f"{h} Hz" for h in pm["rates"]]

            def _cur(key):
                v = int(aw.get(key) or 0)
                return f"{v} Hz" if v in pm["rates"] else "leave alone"

            rr = tb.Frame(lf); rr.pack(anchor="w", pady=(8, 0))
            self._aw_hz_ac = tk.StringVar(value=_cur("refresh_ac"))
            self._aw_hz_bat = tk.StringVar(value=_cur("refresh_battery"))
            tb.Label(rr, text="Refresh AC →", width=12, anchor="w").pack(side="left")
            tb.Combobox(rr, textvariable=self._aw_hz_ac, values=opts,
                        state="readonly", width=14).pack(side="left", padx=(0, 16))
            tb.Label(rr, text="Refresh batt →", width=12, anchor="w").pack(side="left")
            tb.Combobox(rr, textvariable=self._aw_hz_bat, values=opts,
                        state="readonly", width=14).pack(side="left")

        tb.Button(lf, text="Save auto-switch", bootstyle=SUCCESS,
                  command=self._aw_save).pack(anchor="w", pady=(10, 0))

    @staticmethod
    def _aw_hz_val(s: str) -> int:
        try:
            return int(str(s).split()[0])
        except (ValueError, IndexError):
            return 0

    def _aw_save(self):
        merged = self._read_power_state("powerd.json") or {}
        merged["autoswitch"] = {
            "enabled": bool(self._aw_enabled.get()),
            "on_ac": self._aw_on_ac.get(),
            "on_battery": self._aw_on_bat.get(),
        }
        if self._aw_refresh_rates:
            merged["autoswitch"]["refresh_ac"] = self._aw_hz_val(self._aw_hz_ac.get())
            merged["autoswitch"]["refresh_battery"] = self._aw_hz_val(self._aw_hz_bat.get())
        self._write_power_state("powerd.json", merged)
        self._log(f"[Power] auto-switch saved ({'on' if self._aw_enabled.get() else 'off'}: "
                  f"AC→{self._aw_on_ac.get()}, battery→{self._aw_on_bat.get()})")

    # --- live readouts ---

    def _power_poll(self):
        """Refresh the 'now:' readouts. The reads (ryzenadj -i, nvidia-smi)
        can each take ~1s, so they run on a worker and the label writes are
        marshalled back to the Tk thread."""
        if not getattr(self, "_power_live", False):
            return
        threading.Thread(target=self._power_poll_worker, daemon=True).start()
        self.root.after(3000, self._power_poll)

    def _power_poll_worker(self):
        tdp = sensors.read_ryzenadj_info() if getattr(self, "_tdp_val_labels", None) else None
        nvpl = sensors.nvidia_power_limit_info() if getattr(self, "_nvpl_live", None) is not None else None
        bat = sensors.battery_charge_limit_info() if getattr(self, "_bat_live", None) is not None else None
        nvclk = sensors.nvidia_clock_info() if getattr(self, "_gpuclk_live", None) is not None else None
        try:
            self.root.after(0, lambda: self._power_poll_apply(tdp, nvpl, bat, nvclk))
        except (RuntimeError, tk.TclError):
            pass  # window torn down while this worker was in flight

    def _power_poll_apply(self, tdp, nvpl, bat, nvclk=None):
        if tdp is not None:
            for key, lab in self._tdp_val_labels.items():
                v = tdp.get(f"{key}_limit")
                try:
                    lab.configure(text=f"now: {v:.0f} W" if v is not None else "now: — W")
                except tk.TclError:
                    pass
        if getattr(self, "_nvpl_live", None) is not None:
            try:
                self._nvpl_live.configure(
                    text=f"now: {nvpl['current']} W" if nvpl else "now: asleep")
            except tk.TclError:
                pass
        if getattr(self, "_bat_live", None) is not None and bat is not None:
            try:
                self._bat_live.configure(
                    text=f"now: {bat['current']} %" if bat["current"] is not None else "now: — %")
            except tk.TclError:
                pass
        if getattr(self, "_gpuclk_live", None) is not None:
            try:
                self._gpuclk_live.configure(
                    text=f"now: {nvclk['gr_cur']} MHz" if nvclk and nvclk.get("gr_cur")
                    else "now: asleep")
            except tk.TclError:
                pass

    # ---------- Profiles + snapshots tab ----------

    def _build_profiles_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Profiles")
        frame = self._scroll_body(outer, pad=16)

        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY, text=(
            "A profile is a named snapshot of the whole power surface — thermal "
            "profile, CPU TDP, battery limit, NVIDIA limit, fan curve, "
            "auto-switch, keyboard colour. Applying one (or the tweak “Apply "
            "Selected”, or a rollback) first drops an automatic snapshot here, so "
            "there is always a known-good state to return to if something "
            "misbehaves.")).pack(anchor="w", pady=(0, 12))

        cap = tb.Labelframe(frame, text="Capture current state", padding=12)
        cap.pack(fill="x", pady=6)
        crow = tb.Frame(cap); crow.pack(anchor="w")
        self._prof_name = tk.StringVar()
        tb.Entry(crow, textvariable=self._prof_name, width=28).pack(side="left")
        tb.Button(crow, text="Save as profile", bootstyle=SUCCESS,
                  command=self._profile_save).pack(side="left", padx=8)
        self._prof_preview = tb.Label(cap, bootstyle=SECONDARY, font=("Monospace", 9),
                                      justify="left")
        self._prof_preview.pack(anchor="w", pady=(8, 0))

        pf = tb.Labelframe(frame, text="Saved profiles", padding=12)
        pf.pack(fill="x", pady=6)
        tb.Label(pf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "A saved profile is a plain JSON file — export one to share a "
            "known-good curve/TDP loadout with another G15 owner, or import "
            "one someone shared with you.")).pack(anchor="w", pady=(0, 6))
        tb.Button(pf, text="Import profile…", bootstyle=(INFO, "outline"),
                  command=self._profile_import).pack(anchor="w", pady=(0, 8))
        self._prof_list = tb.Frame(pf); self._prof_list.pack(fill="x")

        sf = tb.Labelframe(frame, text="Snapshots — automatic rollback points", padding=12)
        sf.pack(fill="x", pady=6)
        tb.Button(sf, text="↩  Roll back to the latest snapshot", bootstyle=(WARNING, "outline"),
                  command=lambda: self._snapshot_rollback("last")).pack(anchor="w", pady=(0, 8))
        self._snap_list = tb.Frame(sf); self._snap_list.pack(fill="x")

        gpf = tb.Labelframe(frame, text="Per-game auto-profiles", padding=12)
        gpf.pack(fill="x", pady=6)
        tb.Label(gpf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "When a listed process is running (match on the executable name — for "
            "Proton games that's the Windows .exe), the daemon snapshots the "
            "current state and applies the chosen profile, then restores it when "
            "the game exits. Use \"*\" to match any Feral GameMode session. Needs "
            "the “Fan-curve + AC-switch daemon” tweak enabled.")).pack(anchor="w", pady=(0, 8))
        cfg = self._read_power_state("powerd.json").get("game_profiles", {})
        self._gp_enabled = tk.BooleanVar(value=bool(cfg.get("enabled")))
        tb.Checkbutton(gpf, text="Per-game auto-profiles enabled", variable=self._gp_enabled,
                       bootstyle="round-toggle").pack(anchor="w")
        self._gp_rows = []
        grid = tb.Frame(gpf); grid.pack(anchor="w", pady=(8, 4))
        tb.Label(grid, text="Process (exe name)", width=26, bootstyle=SECONDARY).grid(row=0, column=0)
        tb.Label(grid, text="Profile", width=20, bootstyle=SECONDARY).grid(row=0, column=1)
        items = list((cfg.get("match") or {}).items())
        for r in range(4):
            proc, prof = items[r] if r < len(items) else ("", "")
            pv = tk.StringVar(value=proc); cv = tk.StringVar(value=prof)
            tb.Entry(grid, textvariable=pv, width=26).grid(row=r + 1, column=0, padx=3, pady=2)
            cb = tb.Combobox(grid, textvariable=cv, width=18, state="readonly")
            cb.grid(row=r + 1, column=1, padx=3, pady=2)
            self._gp_rows.append((pv, cv, cb))
        drow = tb.Frame(gpf); drow.pack(anchor="w", pady=(4, 0))
        tb.Label(drow, text="When no game runs →").pack(side="left")
        self._gp_default = tk.StringVar(value=cfg.get("default") or "")
        self._gp_default_cb = tb.Combobox(drow, textvariable=self._gp_default, width=18,
                                          state="readonly")
        self._gp_default_cb.pack(side="left", padx=6)
        tb.Label(drow, text="(blank = roll back the pre-game snapshot)",
                 bootstyle=SECONDARY).pack(side="left")
        tb.Button(gpf, text="Save game map", bootstyle=SUCCESS,
                  command=self._gameprof_save).pack(anchor="w", pady=(10, 0))

        self._build_schedule_section(frame)
        self._profiles_refresh()

    _SCHED_ROWS = 4

    def _build_schedule_section(self, parent):
        sc = self._read_power_state("powerd.json").get("schedule", {})
        lf = tb.Labelframe(parent, text="Time schedule", padding=12)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, wraplength=1000, justify="left", bootstyle=SECONDARY, text=(
            "The daemon applies a profile by time of day — e.g. Quiet 22:00–07:00. "
            "“Apply” is a preset (Quiet / Balanced / Performance) or a saved "
            "profile name; times are 24-hour and may wrap past midnight. Tick the "
            "weekdays a rule runs on (all ticked = every day). A running per-game "
            "profile wins. Needs the “Fan-curve + AC-switch daemon” tweak "
            "enabled.")).pack(anchor="w", pady=(0, 8))
        self._sched_enabled = tk.BooleanVar(value=bool(sc.get("enabled")))
        tb.Checkbutton(lf, text="Time schedule enabled", variable=self._sched_enabled,
                       bootstyle="round-toggle").pack(anchor="w")
        grid = tb.Frame(lf); grid.pack(anchor="w", pady=(8, 4))
        for c, t in enumerate(("From", "To", "Apply")):
            tb.Label(grid, text=t, width=[8, 8, 20][c], bootstyle=SECONDARY).grid(row=0, column=c)
        for c, d in enumerate(("M", "T", "W", "T", "F", "S", "S")):
            tb.Label(grid, text=d, width=2, bootstyle=SECONDARY).grid(row=0, column=3 + c)
        self._sched_rows = []
        rules = sc.get("rules", []) or []
        for r in range(self._SCHED_ROWS):
            rule = rules[r] if r < len(rules) else {}
            fv = tk.StringVar(value=rule.get("from", ""))
            tv = tk.StringVar(value=rule.get("to", ""))
            av = tk.StringVar(value=rule.get("apply", ""))
            tb.Entry(grid, textvariable=fv, width=8).grid(row=r + 1, column=0, padx=3, pady=2)
            tb.Entry(grid, textvariable=tv, width=8).grid(row=r + 1, column=1, padx=3, pady=2)
            cb = tb.Combobox(grid, textvariable=av, width=18, state="readonly")
            cb.grid(row=r + 1, column=2, padx=3, pady=2)
            days = rule.get("days")
            dvars = []
            for c in range(7):
                dv = tk.BooleanVar(value=(days is None) or (c in days))
                tb.Checkbutton(grid, variable=dv, bootstyle="toolbutton", width=1
                               ).grid(row=r + 1, column=3 + c, padx=1)
                dvars.append(dv)
            self._sched_rows.append((fv, tv, av, cb, dvars))
        drow = tb.Frame(lf); drow.pack(anchor="w", pady=(4, 0))
        tb.Label(drow, text="Outside every rule →").pack(side="left")
        self._sched_outside = tk.StringVar(value=sc.get("outside") or "")
        self._sched_outside_cb = tb.Combobox(drow, textvariable=self._sched_outside,
                                             width=18, state="readonly")
        self._sched_outside_cb.pack(side="left", padx=6)
        tb.Label(drow, text="(blank = leave the profile alone)",
                 bootstyle=SECONDARY).pack(side="left")
        tb.Button(lf, text="Save schedule", bootstyle=SUCCESS,
                  command=self._schedule_save).pack(anchor="w", pady=(10, 0))

    def _schedule_save(self):
        rules = []
        for fv, tv, av, _cb, dvars in self._sched_rows:
            f, t, a = fv.get().strip(), tv.get().strip(), av.get().strip()
            if not (f and t and a):
                continue
            days = [i for i, dv in enumerate(dvars) if dv.get()]
            rule = {"from": f, "to": t, "apply": a}
            if 0 < len(days) < 7:            # all (or none) selected = every day
                rule["days"] = days
            rules.append(rule)
        merged = self._read_power_state("powerd.json") or {}
        merged["schedule"] = {
            "enabled": bool(self._sched_enabled.get()),
            "poll_s": 60,
            "rules": rules,
            "outside": self._sched_outside.get().strip() or None,
        }
        self._write_power_state("powerd.json", merged)
        self._log(f"[Profiles] schedule saved ({'on' if self._sched_enabled.get() else 'off'}): "
                  f"{len(rules)} rule(s), outside={self._sched_outside.get() or '(none)'}")

    def _gameprof_save(self):
        match = {pv.get().strip(): cv.get() for pv, cv, _ in self._gp_rows
                 if pv.get().strip() and cv.get()}
        merged = self._read_power_state("powerd.json") or {}
        merged["game_profiles"] = {
            "enabled": bool(self._gp_enabled.get()),
            "poll_s": 6,
            "match": match,
            "default": self._gp_default.get().strip() or None,
        }
        self._write_power_state("powerd.json", merged)
        self._log(f"[Profiles] game map saved ({'on' if self._gp_enabled.get() else 'off'}): "
                  f"{match or '(empty)'} default={self._gp_default.get() or '(rollback)'}")

    def _profiles_refresh(self):
        for box in (self._prof_list, self._snap_list):
            for w in box.winfo_children():
                w.destroy()
        try:
            preview = tuxthrottle_profiles.capture_state(self.user)
            keys = ", ".join(k for k in preview if k != "captured") or "(nothing readable)"
            self._prof_preview.configure(text=f"will capture: {keys}")
        except Exception as exc:  # noqa: BLE001
            self._prof_preview.configure(text=f"(capture preview failed: {exc})")

        names = tuxthrottle_profiles.list_profiles(self.user)
        sched_opts = ["", "Quiet", "Balanced", "Performance"] + names
        for _pv, _cv, cb in getattr(self, "_gp_rows", []):
            cb.configure(values=[""] + names)
        if getattr(self, "_gp_default_cb", None) is not None:
            self._gp_default_cb.configure(values=[""] + names)
        for _fv, _tv, _av, cb, _dv in getattr(self, "_sched_rows", []):
            cb.configure(values=sched_opts)
        if getattr(self, "_sched_outside_cb", None) is not None:
            self._sched_outside_cb.configure(values=sched_opts)
        if not names:
            tb.Label(self._prof_list, text="(no profiles yet)", bootstyle=SECONDARY).pack(anchor="w")
        for name in names:
            r = tb.Frame(self._prof_list); r.pack(fill="x", pady=2)
            tb.Label(r, text=name, width=26, anchor="w",
                     font=("Sans", 10, "bold")).pack(side="left")
            tb.Button(r, text="Apply", bootstyle=SUCCESS, width=7,
                      command=lambda n=name: self._profile_apply(n, False)).pack(side="left", padx=2)
            tb.Button(r, text="Apply +GPU", bootstyle=(WARNING, "outline"), width=11,
                      command=lambda n=name: self._profile_apply(n, True)).pack(side="left", padx=2)
            tb.Button(r, text="Export…", bootstyle=(INFO, "outline"), width=9,
                      command=lambda n=name: self._profile_export(n)).pack(side="left", padx=2)
            tb.Button(r, text="Delete", bootstyle=(DANGER, "outline"), width=7,
                      command=lambda n=name: self._profile_delete(n)).pack(side="left", padx=2)

        snaps = tuxthrottle_profiles.list_snapshots(self.user)[:15]
        if not snaps:
            tb.Label(self._snap_list, text="(no snapshots yet)", bootstyle=SECONDARY).pack(anchor="w")
        for s in snaps:
            r = tb.Frame(self._snap_list); r.pack(fill="x", pady=1)
            tb.Label(r, text=f"{s['captured']}   {s['label']}", width=44, anchor="w",
                     font=("Monospace", 9)).pack(side="left")
            tb.Button(r, text="Roll back", bootstyle=(WARNING, "outline"), width=10,
                      command=lambda p=s["path"]: self._snapshot_rollback(p)).pack(side="left", padx=2)

    def _profile_save(self):
        name = (self._prof_name.get() or "").strip()
        if not name:
            messagebox.showinfo("Name needed", "Type a name for the profile first.")
            return
        try:
            st = tuxthrottle_profiles.capture_state(self.user)
            tuxthrottle_profiles.save_profile(name, st, self.user)
            self._log(f"[Profiles] saved '{name}': "
                      + ", ".join(k for k in st if k != 'captured'))
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Profiles] save failed: {exc}")
        self._prof_name.set("")
        self._profiles_refresh()

    def _profile_delete(self, name: str):
        if not messagebox.askyesno("Delete profile", f"Delete profile '{name}'?"):
            return
        tuxthrottle_profiles.delete_profile(name, self.user)
        self._log(f"[Profiles] deleted '{name}'")
        self._profiles_refresh()

    def _profile_export(self, name: str):
        from tkinter import filedialog
        try:
            home = pwd.getpwnam(self.user).pw_dir
        except KeyError:
            home = os.path.expanduser("~")
        safe = "".join(c for c in name if c.isalnum() or c in "-_ ").strip() or "profile"
        path = filedialog.asksaveasfilename(
            parent=self.root, initialdir=home, initialfile=f"{safe}.tuxthrottle-profile.json",
            defaultextension=".json", title=f"Export profile '{name}'")
        if not path:
            return
        try:
            dest = tuxthrottle_profiles.export_profile(name, Path(path), self.user)
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                os.chown(dest, pw.pw_uid, pw.pw_gid)
            self._log(f"[Profiles] exported '{name}' -> {dest}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Profiles] export failed: {exc}")

    def _profile_import(self):
        from tkinter import filedialog
        try:
            home = pwd.getpwnam(self.user).pw_dir
        except KeyError:
            home = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            parent=self.root, initialdir=home, filetypes=[("TuxThrottle profile", "*.json")],
            title="Import profile")
        if not path:
            return
        try:
            name = tuxthrottle_profiles.import_profile(Path(path), user=self.user)
            self._log(f"[Profiles] imported '{name}' from {path}")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        self._profiles_refresh()

    def _profile_apply(self, name: str, with_gpu: bool):
        extra = "\n\nThis will ALSO switch hybrid-graphics mode (needs a logout)." if with_gpu else ""
        if not messagebox.askyesno(
                "Apply profile",
                f"Apply profile '{name}'? A snapshot is taken first so you can roll "
                f"back.{extra}"):
            return
        threading.Thread(target=self._profile_apply_worker,
                         args=(name, with_gpu), daemon=True).start()

    def _profile_apply_worker(self, name: str, with_gpu: bool):
        try:
            tuxthrottle_profiles.snapshot(self.user, label=f"pre-apply-{name}")
            st = tuxthrottle_profiles.load_profile(name, self.user)
            rows = tuxthrottle_profiles.apply_state(st, self.user, with_gpu_mode=with_gpu)
            for r in rows:
                self._log(f"[Profiles] {name}: {r['key']} "
                          + ("ok" if r["ok"] else f"FAILED — {r['msg']}")
                          + (f" ({r['msg']})" if r["ok"] and r["msg"] else ""))
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Profiles] apply '{name}' failed: {exc}")
        self.root.after(0, self._profiles_refresh)

    def _snapshot_rollback(self, target: str):
        if not messagebox.askyesno(
                "Roll back", "Restore this saved state? The current state is "
                "snapshotted first, so this is itself undoable."):
            return
        threading.Thread(target=self._rollback_worker, args=(target,), daemon=True).start()

    def _rollback_worker(self, target: str):
        try:
            rows = tuxthrottle_profiles.rollback(target, self.user)
            for r in rows:
                self._log(f"[Profiles] rollback: {r['key']} "
                          + ("ok" if r["ok"] else f"FAILED — {r['msg']}"))
        except Exception as exc:  # noqa: BLE001
            self._log(f"[Profiles] rollback failed: {exc}")
        self.root.after(0, self._profiles_refresh)

    def _build_category_tab(self, category: str):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text=category)
        inner = self._scroll_body(outer)

        for item in self.items.values():
            if item.category != category or item.hidden:
                continue
            row = tb.Frame(inner, padding=16, bootstyle="dark")
            row.pack(fill="x", padx=2, pady=4)

            item.var = tk.BooleanVar(value=False)
            cb = tb.Checkbutton(row, variable=item.var, bootstyle="round-toggle")
            cb.pack(side="left", anchor="n", padx=(0, 14))
            item.checkbutton = cb
            if not item.hw_supported:
                item.var.set(False)
                cb.configure(state="disabled")

            item.status_label = tb.Label(row, text="checking…", width=15, anchor="e",
                                         font=("Sans", 9, "bold"), bootstyle=SECONDARY)
            item.status_label.pack(side="right", anchor="n", padx=(14, 0))

            text_frame = tb.Frame(row, bootstyle="dark")
            text_frame.pack(side="left", fill="both", expand=True)
            title_row = tb.Frame(text_frame, bootstyle="dark")
            title_row.pack(anchor="w", fill="x")
            tb.Label(title_row, text=item.content, font=("Sans", 11, "bold"),
                     bootstyle="inverse-dark").pack(side="left")
            if item.risk == "advanced":
                tb.Label(title_row, text="ADVANCED", bootstyle=(WARNING, "inverse"),
                         font=("Sans", 7, "bold"), padding=(5, 1)).pack(side="left", padx=8)
            tb.Label(text_frame, text=item.description, wraplength=1250,
                     bootstyle="inverse-dark", justify="left").pack(anchor="w", pady=(4, 0))

    def _build_presets_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Presets")
        frame = self._scroll_body(outer, pad=14)
        tb.Label(frame, text="One click applies a curated bundle of tweaks + installs apps.", bootstyle=SECONDARY).pack(anchor="w", pady=(0, 12))

        recs = self._recommended_all()
        rb = tb.Labelframe(frame, text="Developer recommendations", padding=14)
        rb.pack(fill="x", pady=(0, 10))
        tb.Label(rb, wraplength=900, bootstyle=SECONDARY, text=(
            "Applies every item the developer marked ★ recommended — across all "
            "categories — in one pass, and offers to turn on the background "
            "daemon that powers the fan curve, AC/battery auto-switch and the "
            "time schedule. A snapshot is taken first so you can roll back from "
            "the Profiles tab.")).pack(anchor="w", pady=(0, 8))
        self._tip(tb.Button(rb, text="★  Apply all recommendations", bootstyle=SUCCESS,
                  command=self._on_apply_all_recommended),
                  "One-click sensible setup: applies every ★ recommended tweak "
                  "across all categories and offers to enable the background "
                  "daemon. Snapshot taken first; roll back from the Profiles tab."
                  ).pack(anchor="w")
        self._rec_all_lbl = tb.Label(
            rb, bootstyle=SECONDARY,
            text=(f"{len(recs)} not yet applied" if recs else "all applied ✓"))
        self._rec_all_lbl.pack(anchor="w", pady=(4, 0))

        for preset_id, data in self.presets.items():
            box = tb.Frame(frame, padding=14, bootstyle="secondary")
            box.pack(fill="x", pady=6)
            tb.Label(box, text=data["Content"], font=("Sans", 12, "bold")).pack(anchor="w")
            tb.Label(box, text=data["Description"], wraplength=900, bootstyle=SECONDARY).pack(anchor="w", pady=(2, 8))
            self._tip(tb.Button(
                box, text="Apply This Preset", bootstyle=SUCCESS,
                command=lambda pid=preset_id: self._on_apply_preset(pid)),
                "Apply this whole bundle of tweaks + app installs at once "
                "(snapshot taken first).").pack(anchor="e")

    # ---------- Setup Games ----------

    _SHADERCACHE_SUBDIRS = ("mesa-shader-cache", "dxvk-state-cache",
                            "nv-shader-cache", "steam-shadercache")
    _SHADERCACHE_DEFAULT = "~/.cache/tuxthrottle-shaders"
    _SHADERCACHE_DEFAULT_GB = 80

    def _shadercache_cfg_file(self) -> "Path":
        try:
            home = Path(pwd.getpwnam(self.user).pw_dir)
        except (KeyError, Exception):  # noqa: BLE001
            home = Path.home()
        return home / ".config" / "tuxthrottle" / "shadercache.json"

    def _shadercache_load(self) -> dict:
        try:
            d = json.loads(self._shadercache_cfg_file().read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _shadercache_dir(self) -> str:
        return self._shadercache_load().get("dir") or self._SHADERCACHE_DEFAULT

    def _shadercache_gb(self) -> int:
        try:
            return int(self._shadercache_load().get("max_size_gb")
                       or self._SHADERCACHE_DEFAULT_GB)
        except (TypeError, ValueError):
            return self._SHADERCACHE_DEFAULT_GB

    def _shadercache_abs_dir(self) -> str:
        raw = self._shadercache_dir()
        try:
            home = pwd.getpwnam(self.user).pw_dir
        except (KeyError, Exception):  # noqa: BLE001
            home = os.path.expanduser("~")
        if raw.startswith("~"):
            raw = home + raw[1:]
        return os.path.abspath(raw)

    def _shadercache_ensure_dirs(self) -> str:
        base = self._shadercache_abs_dir()
        try:
            pw = pwd.getpwnam(self.user)
        except KeyError:
            pw = None
        for sub in self._SHADERCACHE_SUBDIRS:
            d = Path(base) / sub
            try:
                d.mkdir(parents=True, exist_ok=True)
                if os.geteuid() == 0 and pw:
                    for p in (d, d.parent):
                        try:
                            os.chown(p, pw.pw_uid, pw.pw_gid)
                        except OSError:
                            pass
            except OSError:
                pass
        return base

    def _build_steamperf_box(self, parent):
        lf = tb.Labelframe(parent, text="Steam client — low-resource mode", padding=10)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Runs the Steam client (not games) as light as it goes — most of "
            "Steam's idle CPU/RAM/VRAM is its embedded Chromium UI. Adds launch "
            "flags via a user-level launcher override "
            "(~/.local/share/applications/steam.desktop, shadows the system "
            "one; autostart entry patched too):  -silent (start to tray),  "
            "-cef-disable-gpu + -cef-disable-gpu-compositing (no GPU accel in "
            "the store/library/friends web views — the big one on this hybrid "
            "GPU),  -cef-disable-breakpad / -cef-disable-extra-info-spew (no "
            "crash reporter, quieter logs),  and -noverifyfiles / "
            "-nobootstrapupdate / -norepairfiles (skip the file-scan + "
            "self-update + repair passes each launch — Steam still re-verifies "
            "on demand). It runs Steam in a systemd scope "
            "with a SOFT memory limit (MemoryHigh=1200M — the kernel just "
            "reclaims cache above that, it never kills anything), and flips "
            "every low-resource setting Steam keeps in a file (needs Steam "
            "closed): no auto Friends & Chat sign-in (that renderer never "
            "spawns), no friends animations, and background Vulkan-shader "
            "processing off (the Steam Overlay + screenshots are kept). A few "
            "more toggles live "
            "in Steam's own store and can't be scripted — Enable prints them "
            "in the log for you to tick (Library → Low Bandwidth / Low "
            "Performance Mode, Interface → smooth scrolling off, Downloads → "
            "Shader Pre-Caching off). No hard MemoryMax (that OOM-kills "
            "Steam). Takes effect next Steam start (quit fully + relaunch from "
            "the menu). Trade-off: manual chat sign-in. The Steam Overlay and "
            "screenshots stay working. The toggle below adds a hidden-on-login "
            "autostart entry; Disable reverts everything.")).pack(anchor="w")
        row = tb.Frame(lf); row.pack(anchor="w", fill="x", pady=(6, 0))
        tb.Label(row, text="Low-resource mode:", bootstyle=SECONDARY).pack(side="left")
        self._sp_lbl = tb.Label(row, bootstyle=SECONDARY, text="—")
        self._sp_lbl.pack(side="left", padx=(4, 8))
        self._tip(tb.Button(row, text="Enable", bootstyle=SUCCESS,
                  command=lambda: self._sp_set(True)),
                  "Write the lightweight Steam launcher override. Restart Steam "
                  "after.").pack(side="left")
        self._tip(tb.Button(row, text="Disable", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._sp_set(False)),
                  "Remove the override — Steam goes back to the stock launcher."
                  ).pack(side="left", padx=6)
        orow = tb.Frame(lf); orow.pack(anchor="w", fill="x", pady=(4, 0))
        self._sp_autostart = tk.BooleanVar(value=True)
        self._tip(tb.Checkbutton(orow, text="Autostart Steam hidden on login",
                  variable=self._sp_autostart, bootstyle="round-toggle"),
                  "If you have no Steam autostart entry, Enable creates one with "
                  "-silent so Steam comes up on login straight to the tray "
                  "(no window). Disable removes it again.").pack(side="left")
        self.root.after(5400, self._sp_refresh)   # well clear of the startup probe burst

    def _sp_helper(self, args: str) -> str:
        return self._user_py("tuxthrottle_steamperf.py", args)

    # ---------- Fixes: one-click diagnosis + the small repairs that don't ----------
    # ---------- warrant their own on/off tweak (unmounted-drive detection, ----------
    # ---------- "Steam won't start" checks, and a log of what auto-fixed itself) ---

    def _build_fixes_box(self, parent):
        lf = tb.Labelframe(parent, text="Fixes — quick diagnosis & one-click repairs", padding=10)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Checks for the causes behind Steam problems already tracked down on this "
            "laptop — the client forced onto the discrete GPU, stale removed CEF flags, "
            "an unmounted Steam library drive, an NTFS volume Windows left 'dirty'. "
            "Read-only until you press a fix button below.")).pack(anchor="w")

        row = tb.Frame(lf); row.pack(anchor="w", fill="x", pady=(8, 2))
        self._tip(tb.Button(row, text="Diagnose: “Steam won't start”",
                  bootstyle=(INFO, "outline"), command=self._fx_diagnose),
                  "Run the checks above and list what's wrong, if anything. "
                  "Read-only.").pack(side="left")
        self._tip(tb.Button(row, text="Check for unmounted library drives",
                  bootstyle=(INFO, "outline"), command=self._fx_check_mounts),
                  "Look for a 'nofail' drive in /etc/fstab that isn't currently "
                  "mounted — the race that makes Steam briefly report a library's "
                  "games as missing right after login. Read-only; offers a Mount "
                  "button per drive found.").pack(side="left", padx=6)

        self._fx_text = self._make_log_text(lf)
        self._fx_text.configure(height=6)
        self._fx_text.pack(fill="x", pady=(8, 0))
        self._fx_mount_row = tb.Frame(lf)
        self._fx_mount_row.pack(anchor="w", fill="x", pady=(4, 0))

        tb.Separator(lf).pack(fill="x", pady=(10, 6))
        hrow = tb.Frame(lf); hrow.pack(anchor="w", fill="x")
        tb.Label(hrow, text="Recently auto-fixed / diagnosed:", bootstyle=SECONDARY).pack(side="left")
        self._tip(tb.Button(hrow, text="Refresh", bootstyle=(SECONDARY, "outline"),
                  command=self._fx_refresh_history),
                  "Reload the fix-history log (also written to by the tray's "
                  "background crash watcher).").pack(side="left", padx=6)
        self._fx_history_text = self._make_log_text(lf)
        self._fx_history_text.configure(height=5)
        self._fx_history_text.pack(fill="x", pady=(4, 0))
        self._fx_refresh_history()

    def _fx_set_text(self, widget, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _fx_diagnose(self):
        self._fx_set_text(self._fx_text, "checking…")
        threading.Thread(target=self._fx_diagnose_worker, daemon=True).start()

    def _fx_diagnose_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._sp_helper("diagnose --json"), timeout=20)
            results = json.loads(out[out.index("["):out.rindex("]") + 1])
        except (ValueError, OSError):
            results = [["bad", "could not run the diagnostic — see the log console"]]
        lines = [f"{'✓' if s == 'ok' else '✗'}  {m}" for s, m in results]
        for s, m in results:
            if s != "ok":
                fixlog.log_event("diagnose", m, level="warn", user=self.user)
        self.root.after(0, lambda: self._fx_set_text(self._fx_text, "\n".join(lines)))
        self.root.after(0, self._fx_refresh_history)

    def _fx_check_mounts(self):
        self._fx_set_text(self._fx_text, "checking…")
        threading.Thread(target=self._fx_check_mounts_worker, daemon=True).start()

    def _fx_check_mounts_worker(self):
        unmounted = []
        try:
            for line in Path("/etc/fstab").read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) < 4:
                    continue
                mountpoint, opts = fields[1], fields[3]
                if mountpoint in ("none", "swap") or "nofail" not in opts.split(","):
                    continue
                r = subprocess.run(["findmnt", "-n", mountpoint], capture_output=True)
                if r.returncode != 0:
                    unmounted.append(mountpoint)
        except OSError:
            pass
        self.root.after(0, lambda: self._fx_show_unmounted(unmounted))

    def _fx_show_unmounted(self, unmounted: list):
        if not unmounted:
            self._fx_set_text(self._fx_text, "✓  every 'nofail' drive in /etc/fstab is mounted")
        else:
            self._fx_set_text(self._fx_text, "✗  not mounted:\n"
                              + "\n".join(f"  {m}" for m in unmounted))
        for w in self._fx_mount_row.winfo_children():
            w.destroy()
        for m in unmounted:
            self._tip(tb.Button(self._fx_mount_row, text=f"Mount {m}",
                      bootstyle=(WARNING, "outline"),
                      command=lambda mp=m: self._fx_mount_now(mp)),
                      f"Run 'mount {m}' now (the GUI is already elevated).").pack(
                      side="left", padx=(0, 6))

    def _fx_mount_now(self, mountpoint: str):
        try:
            r = subprocess.run(["mount", mountpoint], capture_output=True,
                              text=True, timeout=30)
            ok = r.returncode == 0
            msg = (f"mounted {mountpoint}" if ok else
                  f"mount {mountpoint} failed: {(r.stderr or '').strip()}")
        except (OSError, subprocess.SubprocessError) as exc:
            ok, msg = False, f"mount {mountpoint} failed: {exc}"
        self._log(f"[Fixes] {msg}")
        fixlog.log_event("mount-now", msg, level="info" if ok else "error", user=self.user)
        self._fx_check_mounts()

    def _fx_refresh_history(self):
        try:
            entries = fixlog.read_recent(20, user=self.user)
        except Exception:  # noqa: BLE001
            entries = []
        if not entries:
            text = "no fixes logged yet"
        else:
            lines = []
            for e in entries:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("ts", 0)))
                lines.append(f"[{when}] {e.get('source', '?')}: {e.get('message', '')}")
            text = "\n".join(lines)
        self._fx_set_text(self._fx_history_text, text)

    def _sp_set(self, enable: bool):
        if enable:
            args = "on"
            if getattr(self, "_sp_autostart", None) is not None \
                    and not self._sp_autostart.get():
                args += " --no-autostart"
        else:
            args = "off"
        self._run_stream(f"Steam low-resource mode {args}",
                         self._sp_helper(args), tag="Steam")
        self.root.after(1500, self._sp_refresh)

    def _sp_refresh(self):
        self._sp_state = None
        threading.Thread(target=self._sp_worker, daemon=True).start()
        self.root.after(300, self._sp_poll)

    def _sp_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._sp_helper("status"), timeout=15)
            self._sp_state = (out or "").strip().splitlines()[-1] if out else "?"
        except Exception:  # noqa: BLE001
            self._sp_state = "?"

    def _sp_poll(self):
        st = getattr(self, "_sp_state", None)
        if st is None:
            self.root.after(300, self._sp_poll)
            return
        lbl = getattr(self, "_sp_lbl", None)
        if lbl is not None:
            base = st.split("+")[0]
            style = {"on": SUCCESS}.get(base, SECONDARY)
            txt = {"on": "ON", "off": "OFF"}.get(base, st)
            if "+autostart" in st:
                txt += " · autostart"
            lbl.configure(text=txt, bootstyle=style)

    def _build_shadercache_box(self, parent):
        lf = tb.Labelframe(parent, text="Shader / pipeline cache storage", padding=10)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "One folder for every generated shader cache — Mesa (AMD), DXVK "
            "(D3D→Vulkan), the NVIDIA driver, and optionally Steam's own — so "
            "they can sit on the drive you choose and survive a Proton prefix "
            "wipe. The launch-options builder below and the “NVIDIA shader-cache” "
            "tweak both read this location. Changing it does NOT rewrite launch "
            "options you've already pasted into a game — regenerate + re-paste "
            "those if you move it.")).pack(anchor="w")
        drow = tb.Frame(lf); drow.pack(anchor="w", fill="x", pady=(6, 2))
        tb.Label(drow, text="Cache folder:").pack(side="left")
        self._sc_dir_var = tk.StringVar(value=self._shadercache_dir())
        tb.Entry(drow, textvariable=self._sc_dir_var, width=44).pack(side="left", padx=(4, 4))
        self._tip(tb.Button(drow, text="Browse…", bootstyle=(SECONDARY, "outline"),
                  command=self._sc_browse),
                  "Pick the folder (on any drive) where all the shader caches go."
                  ).pack(side="left")
        self._tip(tb.Button(drow, text="Save location", bootstyle=SUCCESS,
                  command=lambda: self._sc_persist("location")),
                  "Save the folder above to shadercache.json, create the "
                  "mesa/dxvk/nv/steam sub-folders, and point the launch-options "
                  "builder + NVIDIA tweak at it.").pack(side="left", padx=(8, 0))

        srow = tb.Frame(lf); srow.pack(anchor="w", fill="x", pady=(2, 2))
        tb.Label(srow, text="Max size (GB):").pack(side="left")
        self._sc_gb_var = tk.IntVar(value=self._shadercache_gb())
        tb.Spinbox(srow, from_=5, to=1000, increment=10, width=6,
                   textvariable=self._sc_gb_var).pack(side="left", padx=(4, 4))
        self._tip(tb.Button(srow, text="Apply shader cache size", bootstyle=SUCCESS,
                  command=lambda: self._sc_persist("size")),
                  "Save the size cap. It limits the Mesa and NVIDIA caches "
                  "(they self-prune to it); DXVK's and Steam's own cache have "
                  "no size setting.").pack(side="left")
        tb.Label(srow, bootstyle=SECONDARY, text=(
            "  — caps the Mesa & NVIDIA caches (self-prune); DXVK / Steam have no cap"
        )).pack(side="left")

        brow = tb.Frame(lf); brow.pack(anchor="w", fill="x", pady=(4, 0))
        self._tip(tb.Button(brow, text="Link Steam's shader cache here",
                  bootstyle=(INFO, "outline"),
                  command=lambda: self._sc_link_steam(False)),
                  "Move each Steam library's steamapps/shadercache into this "
                  "folder and leave a symlink, so Steam's own cache lives "
                  "alongside the rest. Close Steam first.").pack(side="left")
        self._tip(tb.Button(brow, text="Undo Steam link", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._sc_link_steam(True)),
                  "Reverse the above — copy Steam's cache back out to a normal "
                  "folder in each Steam library. Close Steam first."
                  ).pack(side="left", padx=6)
        self._tip(tb.Button(brow, text="Check links", bootstyle=(SECONDARY, "outline"),
                  command=self._sc_link_check),
                  "Verify each Steam library's steamapps/shadercache symlink "
                  "still points at this folder. A link left dangling — e.g. "
                  "after moving the cache folder — makes Steam fail every shader "
                  "write with “disk write error”. If it reports broken, press "
                  "“Link Steam's shader cache here” to repair it."
                  ).pack(side="left")
        lkrow = tb.Frame(lf); lkrow.pack(anchor="w", fill="x", pady=(3, 0))
        self._sc_link_lbl = tb.Label(lkrow, bootstyle=SECONDARY, text="link status: —")
        self._sc_link_lbl.pack(side="left")

        szrow = tb.Frame(lf); szrow.pack(anchor="w", fill="x", pady=(8, 0))
        self._sc_size_lbl = tb.Label(szrow, bootstyle=SECONDARY,
                                     text="sizes: not calculated yet")
        self._sc_size_lbl.pack(side="left")
        self._tip(tb.Button(szrow, text="↻ Refresh", bootstyle=(SECONDARY, "outline"),
                  command=self._sc_refresh_sizes),
                  "Recalculate the folder sizes with `du` (runs in the "
                  "background).").pack(side="left", padx=(10, 0))
        self._tip(tb.Button(szrow, text="Clean cache", bootstyle=(WARNING, "outline"),
                  command=self._sc_clean),
                  "Empty every shader cache under this folder. Optional — the "
                  "caches rebuild on next launch (first run of each game will "
                  "stutter). Close Steam first.").pack(side="left", padx=6)

        rbrow = tb.Frame(lf); rbrow.pack(anchor="w", fill="x", pady=(8, 0))
        self._tip(tb.Button(rbrow, text="Force-rebuild Steam's shader cache",
                  bootstyle=(WARNING, "outline"), command=self._sc_rebuild),
                  "Delete Steam's own shader cache (the fossilize cache in "
                  "steamapps/shadercache) so Steam regenerates it from scratch "
                  "on the next launch. Use after a driver update or a "
                  "corrupt-cache stutter. The Mesa / DXVK / NVIDIA caches are "
                  "left alone. Close Steam first.").pack(side="left")
        bgrow = tb.Frame(lf); bgrow.pack(anchor="w", fill="x", pady=(4, 0))
        tb.Label(bgrow, text="Steam background Vulkan shader processing:",
                 bootstyle=SECONDARY).pack(side="left")
        self._sc_bg_lbl = tb.Label(bgrow, bootstyle=SECONDARY, text="—")
        self._sc_bg_lbl.pack(side="left", padx=(4, 8))
        self._tip(tb.Button(bgrow, text="Turn OFF", bootstyle=(DANGER, "outline"),
                  command=lambda: self._sc_bg_shaders(False)),
                  "Untick Steam → Settings → Downloads → “Allow background "
                  "processing of Vulkan shaders” — stops the fossilize_replay "
                  "background compiles that peg the CPU after every download. "
                  "Close Steam first; restart Steam after.").pack(side="left")
        self._tip(tb.Button(bgrow, text="Turn ON", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._sc_bg_shaders(True)),
                  "Re-enable Steam's background Vulkan shader processing."
                  ).pack(side="left", padx=6)

        # staggered + well after the startup hardware-probe / tweak-status burst,
        # so this box's du / subprocess pollers never pile onto it
        self.root.after(3000, self._sc_refresh_sizes)
        self.root.after(3800, self._sc_link_check)     # surface broken links on open
        self.root.after(4600, self._sc_bg_refresh)

    def _sc_link_check(self):
        self._sc_link_lbl.configure(text="link status: checking…", bootstyle=SECONDARY)
        self._sc_link_result = None
        self._sc_link_tries = 0
        threading.Thread(target=self._sc_link_check_worker, daemon=True).start()
        self.root.after(300, self._sc_link_check_poll)

    def _sc_link_check_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._sc_helper("link-check --json"), timeout=25)
            self._sc_link_result = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, OSError):
            self._sc_link_result = {"ok": True, "summary": "could not check", "linked": 0}

    def _sc_link_check_poll(self):
        r = getattr(self, "_sc_link_result", None)
        if r is None:
            self._sc_link_tries += 1
            if self._sc_link_tries < 40:
                self.root.after(300, self._sc_link_check_poll)
                return
            r = {"ok": True, "summary": "check timed out", "linked": 0}
        healthy = r.get("ok", True)
        style = SUCCESS if (healthy and r.get("linked")) else DANGER if not healthy else SECONDARY
        try:
            self._sc_link_lbl.configure(text="link status: " + r.get("summary", "?"),
                                        bootstyle=style)
        except tk.TclError:
            return
        if not healthy:
            self._log(f"[Cache] ⚠ {r.get('summary')} — press "
                      f"“Link Steam's shader cache here” to repair")

    def _sc_helper(self, args: str) -> str:
        return self._user_py("tuxthrottle_shadercache.py", args)

    def _sc_browse(self):
        from tkinter import filedialog
        try:
            start = pwd.getpwnam(self.user).pw_dir
        except KeyError:
            start = os.path.expanduser("~")
        d = filedialog.askdirectory(
            parent=self.root, initialdir=start,
            title="Pick a folder for the shader caches (can be on any drive)")
        if d:
            self._sc_dir_var.set(d)

    def _sc_persist(self, what: str):
        """Write dir + max_size_gb to shadercache.json (both fields, whichever
        button was pressed). Only the tiny JSON write happens on the Tk thread;
        creating the subdirs (possibly on a slow/cold drive), the size `du` and
        the NVIDIA re-stamp all run off-thread so the UI never blocks."""
        d = (self._sc_dir_var.get() or "").strip() or self._SHADERCACHE_DEFAULT
        try:
            gb = int(self._sc_gb_var.get())
        except (tk.TclError, ValueError):
            gb = self._SHADERCACHE_DEFAULT_GB
        gb = max(5, min(1000, gb))
        f = self._shadercache_cfg_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"dir": d, "max_size_gb": gb},
                                    indent=2, sort_keys=True))
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                for p in (f, f.parent):
                    try:
                        os.chown(p, pw.pw_uid, pw.pw_gid)
                    except OSError:
                        pass
        except (OSError, KeyError) as exc:
            self._log(f"[Cache] save failed: {exc}")
            return
        self._log(f"[Cache] shader cache {what} saved → {d}  (max {gb} GB)")
        self._lo_refresh()                     # cheap now — string build only
        self._sc_refresh_sizes()              # creates the subdirs + du, off-thread
        # keep the system-wide NVIDIA environment.d file in step, if that tweak
        # is already installed (else it picks this up on its next apply)
        nv = self.items.get("NvidiaShaderCache")
        if nv is not None and nv.done:
            self._run_stream("update NVIDIA shader-cache config",
                             f"python3 {shlex.quote(str(BASE_DIR))}/apply_tweak.py "
                             f"NvidiaShaderCache", tag="Cache")

    def _sc_refresh_sizes(self):
        lbl = getattr(self, "_sc_size_lbl", None)
        if lbl is None:
            return
        if getattr(self, "_sc_size_busy", False):
            return
        try:
            lbl.configure(text="sizes: calculating…")
        except tk.TclError:
            return
        self._sc_size_busy = True
        self._sc_size_result = None
        self._sc_size_tries = 0
        threading.Thread(target=self._sc_size_worker, daemon=True).start()
        self.root.after(400, self._sc_size_poll)

    def _sc_size_poll(self):
        res = getattr(self, "_sc_size_result", None)
        if res is None:
            self._sc_size_tries += 1
            if self._sc_size_tries > 350:        # ~140 s — worker never returned
                self._sc_size_busy = False
                try:
                    self._sc_size_lbl.configure(text="sizes: (timed out — ↻ Refresh)")
                except tk.TclError:
                    pass
                return
            self.root.after(400, self._sc_size_poll)
            return
        self._sc_size_busy = False
        try:
            self._sc_size_lbl.configure(text=res)
        except tk.TclError:
            pass

    def _sc_size_worker(self):
        # off the Tk thread — safe to touch a slow filesystem here
        base = self._shadercache_ensure_dirs()

        def du_b(sub):
            p = os.path.join(base, sub)
            if not os.path.isdir(p):
                return 0
            try:
                r = subprocess.run(["du", "-sb", "--", p], capture_output=True,
                                   text=True, timeout=120)
                return int(r.stdout.split()[0]) if r.stdout.strip() else 0
            except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                return 0

        steam = du_b("steam-shadercache")
        other = sum(du_b(s) for s in ("mesa-shader-cache", "dxvk-state-cache",
                                      "nv-shader-cache"))
        # hand the string back to the Tk thread via a plain attribute (GIL-safe);
        # _sc_size_poll picks it up — no cross-thread Tk calls.
        self._sc_size_result = (
            f"sizes:  total {_human_bytes(steam + other)}   ·   "
            f"Steam cache {_human_bytes(steam)}   ·   "
            f"other (Mesa+DXVK+NVIDIA) {_human_bytes(other)}")

    def _sc_link_steam(self, undo: bool):
        verb = "Undo the Steam shader-cache symlink" if undo else \
               "Move Steam's shadercache folder into your cache directory and symlink it back"
        if not messagebox.askyesno("Steam shader cache",
                                   f"{verb}?\n\nClose Steam first."):
            return
        self._run_stream("Steam shader-cache " + ("unlink" if undo else "link"),
                         self._sc_helper("link-steam --undo" if undo else "link-steam"),
                         tag="Cache")
        self.root.after(3000, self._sc_refresh_sizes)

    def _sc_clean(self):
        if not messagebox.askyesno(
                "Clean shader caches",
                "Empty every shader/pipeline cache under your cache folder?\n\n"
                "This is optional — the caches rebuild themselves on next launch "
                "(first run of each game will stutter while they refill). Close "
                "Steam first."):
            return
        self._run_stream("clean shader caches", self._sc_helper("clean all"),
                         tag="Cache")
        self.root.after(2500, self._sc_refresh_sizes)

    def _sc_rebuild(self):
        if not messagebox.askyesno(
                "Force-rebuild Steam's shader cache",
                "Delete Steam's own shader cache (the fossilize cache in "
                "steamapps/shadercache) so Steam rebuilds it from scratch on "
                "the next launch?\n\nThe Mesa / DXVK / NVIDIA caches are left "
                "alone. The first run of each game will stutter while Steam's "
                "cache refills. Close Steam first."):
            return
        self._run_stream("force-rebuild Steam shader cache",
                         self._sc_helper("rebuild all"), tag="Cache")
        self.root.after(2500, self._sc_refresh_sizes)

    def _sc_bg_shaders(self, enable: bool):
        verb = "Re-enable" if enable else "Disable"
        if not messagebox.askyesno(
                "Steam background shader processing",
                f"{verb} Steam's “Allow background processing of Vulkan "
                f"shaders”?\n\nClose Steam first — it rewrites config.vdf on "
                f"exit. Restart Steam afterwards for it to take effect."):
            return
        self._run_stream(
            f"steam background shaders {'on' if enable else 'off'}",
            self._sc_helper(f"steam-bg-shaders {'on' if enable else 'off'}"),
            tag="Cache")
        self.root.after(2500, self._sc_bg_refresh)

    def _sc_bg_refresh(self):
        self._sc_bg_state = None
        threading.Thread(target=self._sc_bg_worker, daemon=True).start()
        self.root.after(300, self._sc_bg_poll)

    def _sc_bg_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._sc_helper("steam-bg-shaders status"),
                                     timeout=15)
            self._sc_bg_state = (out or "").strip().splitlines()[-1] if out else "?"
        except Exception:  # noqa: BLE001
            self._sc_bg_state = "?"

    def _sc_bg_poll(self):
        st = getattr(self, "_sc_bg_state", None)
        if st is None:
            self.root.after(300, self._sc_bg_poll)
            return
        lbl = getattr(self, "_sc_bg_lbl", None)
        if lbl is not None:
            style = SUCCESS if st == "off" else (WARNING if st == "on" else SECONDARY)
            lbl.configure(text={"on": "ON", "off": "OFF"}.get(st, st), bootstyle=style)

    def _build_launch_opts_box(self, parent):
        lf = tb.Labelframe(parent, text="Steam / Lutris launch-options builder",
                           padding=10)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left",
                 text="Tick what you want and copy the string into a game's "
                      "Properties → Launch Options (Steam) or the wrapper field "
                      "(Lutris/Heroic). The `%command%` placeholder is where "
                      "Steam substitutes the game. The persistent-cache toggles "
                      "write into the folder set in “Shader / pipeline cache "
                      "storage” above.").pack(anchor="w")
        self._lo = {
            "mangohud": tk.BooleanVar(value=True),
            "gamemode": tk.BooleanVar(value=True),
            "gamescope": tk.BooleanVar(value=False),
            "prime": tk.BooleanVar(value=self.has_nvidia),
            "nvcache": tk.BooleanVar(value=self.has_nvidia),
            # OFF by default — __GL_THREADED_OPTIMIZATIONS breaks a fair number of
            # Wine/Proton and legacy-OpenGL games at startup (e.g. Mount & Blade)
            "nv_threaded": tk.BooleanVar(value=False),
            "radv_gpl": tk.BooleanVar(value=self.has_amd and not self.has_nvidia),
            # only useful for games that actually render on the AMD iGPU
            "mesa_cache": tk.BooleanVar(value=self.has_amd and not self.has_nvidia),
            "no_vsync": tk.BooleanVar(value=False),
            "dxvk_cache": tk.BooleanVar(value=True),
            "dxvk_async": tk.BooleanVar(value=False),
            "proton_nolog": tk.BooleanVar(value=True),
            # kernel ntsync (6.10+) — Proton 9+ uses it in place of e/fsync;
            # lower overhead for CPU-bound games. Ignored where unsupported.
            "ntsync": tk.BooleanVar(value=True),
            "anticheat": tk.BooleanVar(value=False),
        }
        self._lo_w = tk.StringVar(value="1920")
        self._lo_h = tk.StringVar(value="1080")
        self._lo_fps = tk.StringVar(value="")
        row = tb.Frame(lf); row.pack(anchor="w", pady=(8, 2))
        for key, label in (("mangohud", "MangoHud overlay"),
                           ("gamemode", "Feral GameMode"),
                           ("prime", "Render on the NVIDIA dGPU (PRIME offload)"),
                           ("nvcache", "Keep NVIDIA shader cache"),
                           ("nv_threaded", "NVIDIA threaded optimizations "
                                           "(⚠ crashes some Wine/Proton & older "
                                           "OpenGL games — leave off unless it helps)"),
                           ("radv_gpl", "RADV_PERFTEST=gpl (AMD)"),
                           ("mesa_cache", "Persistent Mesa shader cache (AMD iGPU)"),
                           ("no_vsync", "Disable Mesa vsync + threaded GL (AMD)"),
                           ("dxvk_cache", "Persistent DXVK state cache (D3D→Vulkan)"),
                           ("dxvk_async", "DXVK async shader compile (less stutter, "
                                          "can cause brief visual glitches)"),
                           ("proton_nolog", "Proton log off"),
                           ("ntsync", "Proton ntsync (PROTON_USE_NTSYNC=1 — "
                                      "lighter sync than esync/fsync for "
                                      "CPU-bound games; needs Proton 9+ & "
                                      "kernel 6.10+, ignored otherwise)"),
                           ("anticheat", "Anti-cheat safe — no injected Vulkan "
                                         "layers (MangoHud / vkBasalt / all "
                                         "implicit layers off)")):
            tb.Checkbutton(row, text=label, variable=self._lo[key],
                           bootstyle="round-toggle",
                           command=self._lo_refresh).pack(anchor="w")
        grow = tb.Frame(lf); grow.pack(anchor="w", pady=(4, 2))
        tb.Checkbutton(grow, text="gamescope  ", variable=self._lo["gamescope"],
                       bootstyle="round-toggle",
                       command=self._lo_refresh).pack(side="left")
        for cap, var, w in (("W", self._lo_w, 6), ("H", self._lo_h, 6),
                            ("fps cap", self._lo_fps, 6)):
            tb.Label(grow, text=cap).pack(side="left", padx=(8, 2))
            e = tb.Entry(grow, textvariable=var, width=w)
            e.pack(side="left")
            e.bind("<KeyRelease>", lambda _e: self._lo_refresh())
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "If a game won't launch or crashes on start, clear these options and "
            "add them back a few at a time — the usual offenders are NVIDIA "
            "threaded optimizations, then DXVK async, then gamescope."
        )).pack(anchor="w", pady=(4, 2))
        orow = tb.Frame(lf); orow.pack(fill="x", pady=(8, 2))
        self._lo_out = tk.StringVar()
        tb.Entry(orow, textvariable=self._lo_out, state="readonly").pack(
            side="left", fill="x", expand=True)
        self._tip(tb.Button(orow, text="⧉ Copy", bootstyle=INFO,
                  command=lambda: self._copy_text(self._lo_out.get(), "launch options")),
                  "Copy this string. Paste it into Steam → the game → Properties "
                  "→ Launch Options (or Lutris/Heroic's wrapper field)."
                  ).pack(side="left", padx=(6, 0))

        arow = tb.Frame(lf); arow.pack(fill="x", pady=(6, 0))
        self._lo_only_empty = tk.BooleanVar(value=True)
        self._tip(tb.Button(arow, text="Apply to every game", bootstyle=(WARNING, "outline"),
                  command=self._lo_apply_all),
                  "Write this string into the Launch Options of every installed "
                  "Steam game (localconfig.vdf). Steam must be CLOSED first — it "
                  "rewrites its config on exit. Each file is backed up "
                  "(*.tuxthrottle-bak-*). Restart Steam afterwards."
                  ).pack(side="left")
        self._tip(tb.Checkbutton(arow, text="only games with no options yet",
                  variable=self._lo_only_empty, bootstyle="round-toggle"),
                  "On: skip any game that already has custom Launch Options. "
                  "Off: overwrite every game's Launch Options with this string."
                  ).pack(side="left", padx=(10, 0))
        self._tip(tb.Button(arow, text="Remove from every game", bootstyle=(SECONDARY, "outline"),
                  command=self._lo_remove_all),
                  "Changed your mind about a flag you bulk-applied earlier? This "
                  "strips just that token out of whichever game's Launch Options "
                  "contains it, leaving the rest of each string intact — it does "
                  "NOT replace the whole string like 'Apply to every game' does."
                  ).pack(side="left", padx=(10, 0))
        self._lo_refresh()

    def _lo_remove_all(self):
        opts = self._lo_out.get().strip()
        if not opts:
            self._log("[Game Tools] launch-options string is empty — nothing to remove")
            return
        if not messagebox.askyesno(
                "Remove from every game",
                "Strip this token out of every installed Steam game's Launch "
                "Options that contains it (the rest of each game's string is "
                "left as-is):"
                f"\n\n{opts}\n\n"
                "Steam must be closed first — it rewrites its config on exit. "
                "Every localconfig.vdf is backed up."):
            return
        blob = base64.b64encode(opts.encode()).decode()
        cmd = (f"su - {self.user} -c 'python3 {BASE_DIR}/tuxthrottle_launchopts.py "
               f"remove-token --b64 {blob}'")
        self._run_stream("launch options → remove from every installed game", cmd, tag="Game Tools")

    def _lo_apply_all(self):
        opts = self._lo_out.get().strip()
        if not opts:
            self._log("[Game Tools] launch-options string is empty — nothing to apply")
            return
        only = self._lo_only_empty.get()
        if not messagebox.askyesno(
                "Apply to every game",
                ("Write this launch-options string into EVERY installed Steam "
                 "game that has none yet:" if only else
                 "Write this launch-options string into EVERY installed Steam "
                 "game, REPLACING whatever each one has now:")
                + f"\n\n{opts}\n\n"
                "Steam must be closed first — it rewrites its config on exit. "
                "Every localconfig.vdf is backed up. Restart Steam afterwards."):
            return
        blob = base64.b64encode(opts.encode()).decode()
        flag = " --only-empty" if only else ""
        cmd = (f"su - {self.user} -c 'python3 {BASE_DIR}/tuxthrottle_launchopts.py "
               f"set-all --b64 {blob}{flag}'")
        self._run_stream("launch options → every installed game", cmd, tag="Game Tools")

    def _lo_refresh(self):
        env, wrap = [], []
        # the shader caches all live under the user-chosen folder (Shader /
        # pipeline cache storage box above); `$HOME` keeps the string portable.
        # NOTE: this runs on every keystroke/toggle and at startup — it must not
        # touch the filesystem. Directory creation is done off-thread by
        # `_sc_refresh_sizes` / `_sc_persist`.
        base = self._shadercache_dir()
        base = base.replace("~", "$HOME", 1) if base.startswith("~") else base
        gb = self._shadercache_gb()
        if self._lo["prime"].get():
            env += ["__NV_PRIME_RENDER_OFFLOAD=1", "__VK_LAYER_NV_optimus=NVIDIA_only",
                    "__GLX_VENDOR_LIBRARY_NAME=nvidia"]
        if self._lo["nvcache"].get():
            env += ["__GL_SHADER_DISK_CACHE=1",
                    f"__GL_SHADER_DISK_CACHE_PATH={base}/nv-shader-cache",
                    f"__GL_SHADER_DISK_CACHE_SIZE={gb * 1_000_000_000}",
                    "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1"]
        if self._lo["nv_threaded"].get():
            env.append("__GL_THREADED_OPTIMIZATIONS=1")
        if self._lo["radv_gpl"].get():
            env.append("RADV_PERFTEST=gpl")
        if self._lo["mesa_cache"].get():
            env += ["MESA_GLSL_CACHE_ENABLE=1",
                    f"MESA_SHADER_CACHE_DIR={base}/mesa-shader-cache",
                    f"MESA_SHADER_CACHE_MAX_SIZE={gb}G"]
        if self._lo["no_vsync"].get():
            env += ["vblank_mode=0", "mesa_glthread=true"]
        if self._lo["dxvk_cache"].get():
            env += ["DXVK_STATE_CACHE=1",
                    f"DXVK_STATE_CACHE_PATH={base}/dxvk-state-cache"]
        if self._lo["dxvk_async"].get():
            env.append("DXVK_ASYNC=1")
        if self._lo["proton_nolog"].get():
            env.append("PROTON_LOG=0")
        if self._lo["ntsync"].get():
            env.append("PROTON_USE_NTSYNC=1")
        anticheat = self._lo["anticheat"].get()
        if anticheat:
            env += ["MANGOHUD=0", "DISABLE_VKBASALT=1",
                    "VK_LOADER_LAYERS_DISABLE=~implicit~"]
        if self._lo["gamemode"].get():
            wrap.append("gamemoderun")
        if self._lo["gamescope"].get():
            gs = ["gamescope"]
            if self._lo_w.get().strip().isdigit():
                gs += ["-W", self._lo_w.get().strip()]
            if self._lo_h.get().strip().isdigit():
                gs += ["-H", self._lo_h.get().strip()]
            if self._lo_fps.get().strip().isdigit():
                gs += ["-r", self._lo_fps.get().strip()]
            gs += ["-f", "--"]
            wrap += gs
        if self._lo["mangohud"].get() and not anticheat:
            wrap.append("mangohud")
        self._lo_out.set(" ".join(env + wrap + ["%command%"]))

    def _mh_conf_path(self) -> "Path":
        try:
            home = Path(pwd.getpwnam(self.user).pw_dir)
        except (KeyError, Exception):  # noqa: BLE001
            home = Path.home()
        base = home / ".config" / "MangoHud"
        g = getattr(self, "_mh_game_var", None)
        g = (g.get().strip() if g is not None else "")
        # MangoHud reads ~/.config/MangoHud/<exe-basename>.conf per game
        g = re.sub(r"\.exe$", "", g, flags=re.I).strip()
        return base / (f"{g}.conf" if g else "MangoHud.conf")

    def _build_mangohud_box(self, parent):
        lf = tb.Labelframe(parent, text="MangoHud overlay", padding=10)
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "The overlay's CPU / GPU names, position and detail level. Names load "
            "from the config if set, else auto-detect from the hardware. “Show in "
            "full” toggles each group between load-% only and load + temp + power "
            "(+ VRAM for memory); the extra switches add the frametime graph and "
            "GPU clocks. FPS, the graphics-API line and each GPU's real name "
            "(gpu_name) always stay; on Write, everything else in the stat "
            "section is stripped. `width` is pinned to fit your longest name; "
            "other config lines (styling, keybinds) are left alone.")
            ).pack(anchor="w")

        self._mh_game_var = tk.StringVar()
        conf = self._mh_load_conf()
        cpu0 = conf["cpu"]
        self._mh_pos = conf["position"] or "top-left"
        self._mh_ox, self._mh_oy = conf["offset_x"], conf["offset_y"]
        self._mh_pos_set = bool(conf["position"])

        crow = tb.Frame(lf); crow.pack(anchor="w", fill="x", pady=(6, 2))
        tb.Label(crow, text="CPU name:", width=11, anchor="w").pack(side="left")
        self._mh_cpu_var = tk.StringVar(value=cpu0)
        tb.Entry(crow, textvariable=self._mh_cpu_var, width=42).pack(side="left", padx=(2, 0))
        # one GPU-name field per GPU actually in the machine (count from the
        # startup prewarm, so no blocking probe here); rebuilt by ↻ Detect if
        # the count turns out different. conf names win over detected ones; the
        # PCI address is shown beside each so two identical cards are distinct.
        detected = self._probe("gpu_devs") or []
        self._mh_gpu_pci = [d.get("pci", "") for d in detected]
        det_names = [d.get("name", "") for d in detected]
        self._mh_gpu_count = max(len(detected), len(conf["gpus"]), 1)
        self._mh_gpu_vars: list = []
        self._mh_gpu_box = tb.Frame(lf)
        self._mh_gpu_box.pack(anchor="w", fill="x", pady=(2, 2))
        self._mh_build_gpu_fields(prefill=conf["gpus"] or det_names)
        prow = tb.Frame(lf); prow.pack(anchor="w", fill="x", pady=(2, 2))
        tb.Label(prow, text="Position:", width=11, anchor="w").pack(side="left")
        self._mh_pos_lbl = tb.Label(prow, bootstyle=SECONDARY, text=self._mh_pos_summary())
        self._mh_pos_lbl.pack(side="left", padx=(2, 8))
        self._tip(tb.Button(prow, text="Place on screen…", bootstyle=(INFO, "outline"),
                  command=self._mh_position_dialog),
                  "Open a full-screen picker: drag the overlay box to where you "
                  "want it (snaps to a 16×16 grid). Saved as a MangoHud anchor "
                  "+ pixel offset.").pack(side="left")
        gm = tb.Frame(lf); gm.pack(anchor="w", fill="x", pady=(2, 2))
        tb.Label(gm, text="Per-game:", width=11, anchor="w").pack(side="left")
        ge = tb.Entry(gm, textvariable=self._mh_game_var, width=24)
        ge.pack(side="left", padx=(2, 0))
        ge.bind("<KeyRelease>", lambda _e: self._mh_reload_from_conf())
        tb.Label(gm, text="  optional — the game's .exe / binary name; blank = "
                          "the global MangoHud.conf", bootstyle=SECONDARY).pack(side="left")

        # per-group detail toggle: off = minimal (load % only), on = full
        levels = self._mh_levels_from_conf(conf["elements"])
        self._mh_lvl = {}
        dl = tb.Frame(lf); dl.pack(anchor="w", fill="x", pady=(4, 2))
        tb.Label(dl, text="Show in full:", width=11, anchor="w").pack(side="left")
        for grp, label, extra in (("cpu", "CPU", "temp + power"),
                                  ("gpu", "GPU", "temp + power"),
                                  ("mem", "Memory", "+ VRAM")):
            v = tk.BooleanVar(value=(levels[grp] == "full"))
            self._mh_lvl[grp] = v
            self._tip(tb.Checkbutton(dl, text=f"{label} ({extra})", variable=v,
                                     bootstyle="round-toggle"),
                      f"Off = {label} shows only its load %. On = {label} adds "
                      f"{extra}. FPS and the graphics-API line always stay; the "
                      f"frametime graph and GPU-in-use name have their own "
                      f"toggles below; everything else is stripped on Write."
                      ).pack(side="left", padx=(0, 12))

        # explicit extras — a hard on/off for the frametime graph (not tied to
        # the group toggles) plus GPU-clock lines that help identify the card
        dl2 = tb.Frame(lf); dl2.pack(anchor="w", fill="x", pady=(2, 2))
        tb.Label(dl2, text="Also show:", width=11, anchor="w").pack(side="left")
        self._mh_graph = tk.BooleanVar(
            value=bool({"frame_timing", "frametime"} & conf["elements"]))
        self._tip(tb.Checkbutton(dl2, text="Frametime graph", variable=self._mh_graph,
                                 bootstyle="round-toggle"),
                  "The frametime number and its graph. Independent of the group "
                  "toggles — off writes `frame_timing=0` / `frametime=0` so it "
                  "never shows (MangoHud defaults it ON, so removing the line "
                  "isn't enough), on writes them =1."
                  ).pack(side="left", padx=(0, 12))
        self._mh_gpuname = tk.BooleanVar(value=("gpu_name" not in conf["off"]))
        self._tip(tb.Checkbutton(dl2, text="GPU in use (name)", variable=self._mh_gpuname,
                                 bootstyle="round-toggle"),
                  "MangoHud's `gpu_name` line — the name of the card actually "
                  "rendering, so on a PRIME / hybrid setup it confirms which GPU "
                  "the game landed on. Off writes `gpu_name=0`."
                  ).pack(side="left", padx=(0, 12))
        self._mh_gpu_extra = {}
        for key, lbl in (("gpu_core_clock", "GPU core clock"),
                         ("gpu_mem_clock", "GPU mem clock")):
            gv = tk.BooleanVar(value=(key in conf["elements"]))
            self._mh_gpu_extra[key] = gv
            self._tip(tb.Checkbutton(dl2, text=lbl, variable=gv,
                                     bootstyle="round-toggle"),
                      f"Add the {lbl.lower()} to the GPU block — a quick way to "
                      f"confirm which card is doing the work.").pack(side="left", padx=(0, 12))
        self._mh_gamemode = tk.BooleanVar(value=("gamemode" in conf["elements"]))
        self._tip(tb.Checkbutton(dl2, text="Feral GameMode status",
                                 variable=self._mh_gamemode,
                                 bootstyle="round-toggle"),
                  "Add MangoHud's `gamemode` line — shows GAMEMODE ON/OFF in the "
                  "overlay so you can see at a glance whether Feral GameMode "
                  "(gamemoderun) actually engaged for the running game."
                  ).pack(side="left", padx=(0, 12))
        self._mh_status_line = tk.BooleanVar(value=mangohud_status.is_enabled(self.user))
        self._tip(tb.Checkbutton(dl2, text="TuxThrottle live status line",
                                 variable=self._mh_status_line, bootstyle="round-toggle",
                                 command=self._mh_toggle_status_line),
                  "Show Game Mode / fan boost / CPU+dGPU temps as a line in the "
                  "overlay, kept live by the tray monitor (~2s refresh) — see "
                  "throttle-relevant state without alt-tabbing out. Needs the "
                  "tray running (Diagnostics → “Launch tray now”, or its "
                  "autostart entry).").pack(side="left", padx=(0, 12))

        brow = tb.Frame(lf); brow.pack(anchor="w", fill="x", pady=(4, 0))
        self._tip(tb.Button(brow, text="↻ Detect", bootstyle=(SECONDARY, "outline"),
                  command=self._mh_detect),
                  "Fill the CPU / GPU 0 / GPU 1 name fields from the hardware "
                  "(/proc/cpuinfo, nvidia-smi, lspci). Overwrites what's there."
                  ).pack(side="left")
        self._tip(tb.Button(brow, text="Write to MangoHud config", bootstyle=SUCCESS,
                  command=self._mh_apply),
                  "Save the names, position and toggles into the MangoHud config "
                  "(per-game file if the field above is filled). Rewrites the "
                  "stat section to exactly FPS + graphics API + GPU name(s) + "
                  "your chosen groups / extras, recomputes `width` for the "
                  "longest name, and leaves styling / keybind lines untouched. "
                  "Press again after any change.").pack(side="left", padx=6)
        self._tip(tb.Button(brow, text="Reset config (clean)", bootstyle=(WARNING, "outline"),
                  command=self._mh_reset_conf),
                  "Replace the config with a fresh minimal one (FPS, GPU name, "
                  "the groups / extras at the toggle levels above, toggle on "
                  "Shift_R+F12) plus the current names/position. Old file kept "
                  "as .bak.").pack(side="left")
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Every write rewrites the file so each key appears once (latest value "
            "wins), leading comments are kept and blank lines / junk are dropped. "
            "“Reset config” goes further — a clean minimal baseline, old file "
            "kept as .bak.\n"
            "The overlay `width` is recalculated from the longest name (× the "
            "config's font_size) on each “Write”, not live as you type — change "
            "a name and press Write again. Names of 8 characters or fewer get no "
            "width line (MangoHud sizes those itself). “Place on screen…” only "
            "writes the position, not the width."
        )).pack(anchor="w", pady=(4, 0))
        # already set → keep what's there; only auto-detect (off-thread, so
        # startup never blocks) when nothing has been set yet
        self._mh_detect_result = None
        if not (cpu0 or conf["gpus"]):
            self.root.after(600, self._mh_detect)

    def _mh_build_gpu_fields(self, prefill=None, force: bool = False):
        """(Re)draw `self._mh_gpu_count` GPU-name rows in `self._mh_gpu_box`.
        Keeps whatever's already typed unless `force` (↻ Detect) is set."""
        box = self._mh_gpu_box
        for w in box.winfo_children():
            w.destroy()
        keep = [] if force else [v.get() for v in self._mh_gpu_vars]
        prefill = list(prefill or [])
        pci = getattr(self, "_mh_gpu_pci", [])
        self._mh_gpu_vars = []
        multi = self._mh_gpu_count > 1
        for i in range(self._mh_gpu_count):
            val = (keep[i] if i < len(keep) and keep[i]
                   else prefill[i] if i < len(prefill) else "")
            var = tk.StringVar(value=val)
            self._mh_gpu_vars.append(var)
            row = tb.Frame(box); row.pack(anchor="w", fill="x", pady=1)
            tb.Label(row, text=(f"GPU {i} name:" if multi else "GPU name:"),
                     width=11, anchor="w").pack(side="left")
            tb.Entry(row, textvariable=var, width=42).pack(side="left", padx=(2, 0))
            if multi:
                self._tip(tb.Button(row, text="⇅", width=3,
                          bootstyle=(SECONDARY, "outline"),
                          command=lambda i=i: self._mh_swap_gpu_rows(i)),
                          "Swap this GPU's name (and its slot in `gpu_list`) with "
                          "the next row — reorder if MangoHud has them backwards. "
                          "Press Write after.").pack(side="left", padx=(6, 0))
            addr = f"[{pci[i]}] " if i < len(pci) and pci[i] else ""
            tb.Label(row, text=f"  {addr}name label + gpu_list slot for this card",
                     bootstyle=SECONDARY).pack(side="left")

    def _mh_swap_gpu_rows(self, i: int):
        """Swap GPU row i with the next row — both the typed name and its PCI
        address (so `gpu_text` and the remapped `gpu_list` slot move together).
        Wraps last→first. User still has to press Write."""
        n = len(self._mh_gpu_vars)
        if n < 2:
            return
        j = (i + 1) % n
        vals = [v.get() for v in self._mh_gpu_vars]
        vals[i], vals[j] = vals[j], vals[i]
        pci = list(getattr(self, "_mh_gpu_pci", []))
        if i < len(pci) and j < len(pci):
            pci[i], pci[j] = pci[j], pci[i]
        self._mh_gpu_pci = pci
        for v, nv in zip(self._mh_gpu_vars, vals):
            v.set(nv)
        self._mh_build_gpu_fields()          # redraw so the [pci] hints follow
        self._log(f"[MangoHud] swapped GPU rows {i} ↔ {j} — press "
                  f"“Write to MangoHud config” to save")

    def _mh_reload_from_conf(self):
        """Re-read whichever MangoHud config the Per-game field now points at."""
        conf = self._mh_load_conf()
        self._mh_cpu_var.set(conf["cpu"])
        if conf["gpus"]:
            self._mh_gpu_count = max(len(conf["gpus"]), self._mh_gpu_count)
        self._mh_build_gpu_fields(prefill=conf["gpus"], force=True)
        self._mh_pos = conf["position"] or "top-left"
        self._mh_ox, self._mh_oy = conf["offset_x"], conf["offset_y"]
        self._mh_pos_set = bool(conf["position"])
        self._mh_pos_lbl.configure(text=self._mh_pos_summary())
        for grp, lvl in self._mh_levels_from_conf(conf["elements"]).items():
            if grp in getattr(self, "_mh_lvl", {}):
                self._mh_lvl[grp].set(lvl == "full")
        if hasattr(self, "_mh_graph"):
            self._mh_graph.set(bool({"frame_timing", "frametime"} & conf["elements"]))
        if hasattr(self, "_mh_gpuname"):
            self._mh_gpuname.set("gpu_name" not in conf["off"])
        if hasattr(self, "_mh_gamemode"):
            self._mh_gamemode.set("gamemode" in conf["elements"])
        for k, gv in getattr(self, "_mh_gpu_extra", {}).items():
            gv.set(k in conf["elements"])

    def _mh_pos_summary(self) -> str:
        s = self._mh_pos
        if self._mh_ox or self._mh_oy:
            s += f"   (nudge {self._mh_ox}, {self._mh_oy} px)"
        return s

    def _mh_load_conf(self) -> dict:
        """cpu_text / gpu_text / position / offset_x / offset_y / font_size +
        the set of bare element toggles present, straight from MangoHud.conf."""
        out = {"cpu": "", "gpu": "", "gpus": [], "position": "", "offset_x": 0,
               "offset_y": 0, "font_size": 0, "elements": set(), "off": set()}
        try:
            lines = self._mh_conf_path().read_text().splitlines()
        except OSError:
            return out
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            m = re.match(r"(cpu_text|gpu_text|position|offset_x|offset_y|font_size)\s*=\s*(.*)", s)
            if m:
                key, val = m.group(1), m.group(2).strip()
                if key == "cpu_text":
                    out["cpu"] = val
                elif key == "gpu_text":
                    parts = [x.strip() for x in val.split(",") if x.strip()]
                    out["gpus"] = parts
                    out["gpu"] = ", ".join(parts)
                elif key == "position":
                    out["position"] = val
                else:
                    try:
                        out[key] = int(float(val))
                    except ValueError:
                        pass
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
                out["elements"].add(s)             # a bare toggle like `cpu_temp`
            else:
                mk = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)", s)
                if mk:                             # `key=1` / `key=0` element line
                    v = mk.group(2).strip().lower()
                    if v in ("1", "true", "on", "yes"):
                        out["elements"].add(mk.group(1))
                    elif v in ("0", "false", "off", "no"):
                        out["off"].add(mk.group(1))
        return out

    # per-group detail level: (minimal elements, full elements)
    _MH_GROUPS = {
        "cpu": (["cpu_stats"], ["cpu_stats", "cpu_temp", "cpu_power"]),
        "gpu": (["gpu_stats"], ["gpu_stats", "gpu_temp", "gpu_power"]),
        "mem": (["ram"], ["ram", "vram"]),
    }
    # always kept: framerate + the graphics-API / engine line
    _MH_ALWAYS = ["fps", "engine_version"]
    # the frametime number + its graph — gated by the "Frametime graph" toggle
    _MH_FRAMEGRAPH = ["frametime", "frame_timing"]
    # MangoHud's gpu_name — the card actually rendering (PRIME/hybrid tell) —
    # gated by the "GPU in use (name)" toggle
    _MH_GPUNAME = "gpu_name"
    # keys MangoHud defaults to ON, so "disabled" must be written as `key=0`,
    # never just removed (removal → MangoHud falls back to its own default)
    _MH_EXPLICIT = ("frametime", "frame_timing", "gpu_name")
    # every element toggle the box takes ownership of on Write (so "minimal"
    # actually strips the rest). Does NOT include gpu_list (multi-GPU, managed).
    _MH_STAT_KEYS = {
        "fps", "fps_only", "frametime", "frame_timing", "frame_count",
        "cpu_stats", "cpu_temp", "cpu_power", "cpu_mhz", "cpu_load_change",
        "core_load", "core_load_change", "gpu_stats", "gpu_temp", "gpu_power",
        "gpu_core_clock", "gpu_mem_clock", "gpu_load_change", "gpu_name",
        "gpu_voltage", "gpu_fan", "gpu_throttling", "vram", "ram", "swap",
        "procmem", "procmem_shared", "procmem_virt", "io_read", "io_write",
        "io_stats", "arch", "engine_version", "vulkan_driver", "wine",
        "exec_name", "gamemode", "vkbasalt", "battery", "media_player",
        "resolution", "show_fps_limit", "throttling_status",
        "throttling_status_graph", "fan", "present_mode", "refresh_rate",
        "winesync", "version",
    }

    def _mh_levels_from_conf(self, els: set) -> dict:
        out = {}
        for g, (_mini, full) in self._MH_GROUPS.items():
            # "full" if any of the full-only extras is present
            extra = set(full) - set(self._MH_GROUPS[g][0])
            out[g] = "full" if (els & extra) else "minimal"
        return out

    # representative on-screen size of the MangoHud overlay for the picker
    _MH_BOX_W, _MH_BOX_H = 300, 240

    def _mh_anchor_offset(self, bx, by, sw, sh):
        """Free box top-left (bx,by) on a sw×sh screen → nearest MangoHud
        `position` + the (offset_x, offset_y) that reproduces it."""
        cx, cy = bx + self._MH_BOX_W / 2, by + self._MH_BOX_H / 2
        h = "left" if cx < sw / 3 else "right" if cx >= 2 * sw / 3 else "center"
        v = "top" if cy < sh / 3 else "bottom" if cy >= 2 * sh / 3 else "middle"
        if h == "center" and v == "middle":          # no middle-centre anchor
            v = "top" if cy < sh / 2 else "bottom"
        if v == "middle":                            # only middle-left / -right
            h = "left" if cx < sw / 2 else "right"
            pos = f"middle-{h}"
        elif h == "center":                          # only top/bottom-center
            pos = f"{v}-center"
        else:
            pos = f"{v}-{h}"
        ax = (0 if "left" in pos else sw - self._MH_BOX_W if "right" in pos
              else (sw - self._MH_BOX_W) / 2)
        ay = (0 if "top" in pos else sh - self._MH_BOX_H if "bottom" in pos
              else (sh - self._MH_BOX_H) / 2)
        return pos, int(round(bx - ax)), int(round(by - ay))

    def _mh_box_xy(self, pos, ox, oy, sw, sh):
        """Inverse of _mh_anchor_offset: anchor + offset → box top-left."""
        ax = (0 if "left" in pos else sw - self._MH_BOX_W if "right" in pos
              else (sw - self._MH_BOX_W) / 2)
        ay = (0 if "top" in pos else sh - self._MH_BOX_H if "bottom" in pos
              else (sh - self._MH_BOX_H) / 2)
        bx = max(0, min(sw - self._MH_BOX_W, ax + ox))
        by = max(0, min(sh - self._MH_BOX_H, ay + oy))
        return bx, by

    def _mh_position_dialog(self):
        saved = self._mh_load_conf()
        saved_pos = saved["position"] or "top-left"
        saved_ox, saved_oy = saved["offset_x"], saved["offset_y"]

        win = tk.Toplevel(self.root)
        win.title("Place the MangoHud overlay")
        win.transient(self.root)
        try:
            win.attributes("-fullscreen", True)
        except tk.TclError:
            win.geometry(f"{win.winfo_screenwidth()}x{win.winfo_screenheight()}+0+0")
        try:
            win.attributes("-alpha", 0.85)
        except tk.TclError:
            pass
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        cv = tk.Canvas(win, bg="#3b3b3b", highlightthickness=0, cursor="fleur")
        cv.pack(fill="both", expand=True)

        st = {"bx": 0.0, "by": 0.0, "dx": 0.0, "dy": 0.0, "drag": False, "snap": True}
        bx0, by0 = self._mh_box_xy(self._mh_pos, self._mh_ox, self._mh_oy, sw, sh)
        st["bx"], st["by"] = bx0, by0
        BW, BH = self._MH_BOX_W, self._MH_BOX_H
        acc = getattr(self, "accent", "#58a6ff")
        GRID = 16                                # 16 snap points per axis
        gx = (sw - BW) / (GRID - 1)
        gy = (sh - BH) / (GRID - 1)

        def snap(bx, by):
            if not st["snap"]:
                return bx, by
            return (round(bx / gx) * gx if gx else bx,
                    round(by / gy) * gy if gy else by)

        def redraw():
            cv.delete("all")
            for i in range(1, GRID - 1):         # 16×16 snap grid
                lx = i * (sw / (GRID - 1))
                ly = i * (sh / (GRID - 1))
                cv.create_line(lx, 0, lx, sh, fill="#484848")
                cv.create_line(0, ly, sw, ly, fill="#484848")
            for i in (1, 2):                     # thirds guides (anchor bands)
                cv.create_line(sw * i / 3, 0, sw * i / 3, sh, fill="#606060", dash=(4, 4))
                cv.create_line(0, sh * i / 3, sw, sh * i / 3, fill="#606060", dash=(4, 4))
            bx, by = st["bx"], st["by"]
            cv.create_rectangle(bx, by, bx + BW, by + BH, fill=acc, outline="#ffffff",
                                width=2)
            cv.create_text(bx + BW / 2, by + 22, text="MangoHud", fill="#ffffff",
                           font=("Sans", 13, "bold"))
            cv.create_text(bx + BW / 2, by + BH / 2,
                           text="drag me where you\nwant the overlay",
                           fill="#eaeaea", font=("Sans", 10), justify="center")
            pos, ox, oy = self._mh_anchor_offset(bx, by, sw, sh)
            info.configure(text=f"{pos}    offset  {ox:+d}, {oy:+d} px")

        def press(e):
            if st["bx"] <= e.x <= st["bx"] + BW and st["by"] <= e.y <= st["by"] + BH:
                st["drag"] = True
                st["dx"], st["dy"] = e.x - st["bx"], e.y - st["by"]

        def motion(e):
            if not st["drag"]:
                return
            bx, by = snap(e.x - st["dx"], e.y - st["dy"])
            st["bx"] = max(0, min(sw - BW, bx))
            st["by"] = max(0, min(sh - BH, by))
            redraw()

        def release(_e):
            st["drag"] = False
            redraw()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", motion)
        cv.bind("<ButtonRelease-1>", release)
        win.bind("<Escape>", lambda _e: win.destroy())

        # floating control bar
        bar = tk.Frame(win, bg=BIOS_PANEL, bd=1, relief="solid")
        bar.place(relx=0.5, y=24, anchor="n")
        info = tk.Label(bar, bg=BIOS_PANEL, fg="#eaeaea", font=("Sans", 11, "bold"),
                        padx=14, pady=6)
        info.pack(side="top")
        snap_var = tk.BooleanVar(value=True)

        def _toggle_snap():
            st["snap"] = bool(snap_var.get())
            if st["snap"]:
                st["bx"], st["by"] = snap(st["bx"], st["by"])
            redraw()

        tk.Checkbutton(bar, text=f"snap to {GRID}×{GRID} grid", variable=snap_var,
                       command=_toggle_snap, bg=BIOS_PANEL, fg="#eaeaea",
                       selectcolor=BIOS_SUNKEN, activebackground=BIOS_PANEL,
                       activeforeground="#eaeaea").pack(side="top", pady=(0, 2))
        btns = tk.Frame(bar, bg=BIOS_PANEL); btns.pack(side="top", padx=10, pady=(0, 8))

        def set_to(pos, ox, oy):
            st["bx"], st["by"] = self._mh_box_xy(pos, ox, oy, sw, sh)
            redraw()

        def save():
            pos, ox, oy = self._mh_anchor_offset(st["bx"], st["by"], sw, sh)
            self._mh_pos, self._mh_ox, self._mh_oy = pos, ox, oy
            self._mh_pos_set = True
            self._mh_pos_lbl.configure(text=self._mh_pos_summary())
            self._mh_write_position(pos, ox, oy)
            win.destroy()

        self._tip(tb.Button(btns, text="Save position", bootstyle=SUCCESS,
                  command=save),
                  "Write this position into the MangoHud config now "
                  "(only the position / offset lines) and close.").pack(side="left", padx=3)
        self._tip(tb.Button(btns, text="Restore last saved", bootstyle=(INFO, "outline"),
                  command=lambda: set_to(saved_pos, saved_ox, saved_oy)),
                  "Move the box back to whatever position is currently in the "
                  "config file.").pack(side="left", padx=3)
        self._tip(tb.Button(btns, text="Restore default (top-left)",
                  bootstyle=(SECONDARY, "outline"),
                  command=lambda: set_to("top-left", 0, 0)),
                  "Move the box to MangoHud's default — top-left, no offset."
                  ).pack(side="left", padx=3)
        self._tip(tb.Button(btns, text="Cancel", bootstyle=(DANGER, "outline"),
                  command=win.destroy),
                  "Close without changing anything (Esc).").pack(side="left", padx=3)

        redraw()
        win.grab_set()
        win.wait_window()

    def _mh_write_position(self, pos, ox, oy):
        """Clean-merge just position / offset_x / offset_y into the config,
        leaving cpu_text/gpu_text/etc. alone."""
        if not self._mh_guard_global_preload():
            return
        ok = self._mh_write_conf({
            "position": pos,
            "offset_x": str(ox) if ox else None,
            "offset_y": str(oy) if oy else None,
        })
        if ok:
            self._log(f"[MangoHud] position → {self._mh_conf_path()}  "
                      f"({pos}, offset {ox:+d},{oy:+d}; deduped)")

    def _mh_detect(self):
        self._mh_cpu_var.set("detecting…")
        for v in self._mh_gpu_vars:
            v.set("detecting…")
        self._mh_detect_result = None
        self._mh_detect_tries = 0
        threading.Thread(target=self._mh_detect_worker, daemon=True).start()
        self.root.after(300, self._mh_detect_poll)

    def _mh_detect_worker(self):
        try:
            self._mh_detect_result = (sensors.cpu_model_name(),
                                      list(sensors.gpu_devices()))
        except Exception:  # noqa: BLE001
            self._mh_detect_result = ("", [])

    def _mh_detect_poll(self):
        res = getattr(self, "_mh_detect_result", None)
        if res is None:
            self._mh_detect_tries += 1
            if self._mh_detect_tries > 60:       # ~18 s — give up
                res = ("", [])
            else:
                self.root.after(300, self._mh_detect_poll)
                return
        try:
            self._mh_cpu_var.set(res[0])
            devs = res[1] or []
            names = [d.get("name", "") for d in devs]
            self._mh_gpu_pci = [d.get("pci", "") for d in devs]
            if names:
                self._mh_gpu_count = max(len(names), 1)
            self._mh_build_gpu_fields(prefill=names, force=True)
        except tk.TclError:
            pass

    _MH_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")

    def _mh_global_preload_confs(self) -> list:
        """environment.d files that force libMangoHud.so into EVERY process via
        LD_PRELOAD — which drags the overlay layer into kwin_wayland /
        plasmashell. Rewriting the live MangoHud.conf then crashes the desktop
        (KWin SIGABRT → Plasma session reset → tray loses the power/battery
        applets). This is the 'MangoHud Write relaunches my session' bug."""
        import glob as _glob
        try:
            uhome = Path(pwd.getpwnam(self.user).pw_dir)
        except (KeyError, Exception):  # noqa: BLE001
            uhome = Path.home()
        hits = []
        for f in (_glob.glob(str(uhome / ".config" / "environment.d" / "*.conf"))
                  + _glob.glob("/etc/environment.d/*.conf")):
            try:
                if re.search(r"^\s*LD_PRELOAD=\S*libMangoHud",
                             Path(f).read_text(), re.M):
                    hits.append(Path(f))
            except OSError:
                pass
        return hits

    def _mh_guard_global_preload(self) -> bool:
        """True = ok to write the config; False = abort. If a global
        LD_PRELOAD is present, offer to strip just that line (MANGOHUD=1 stays,
        so games still show the overlay) before touching the live conf."""
        confs = self._mh_global_preload_confs()
        if not confs:
            return True
        try:
            uhome = Path(pwd.getpwnam(self.user).pw_dir)
        except (KeyError, Exception):  # noqa: BLE001
            uhome = Path.home()
        flist = "\n".join(f"  • {c}" for c in confs)
        if not messagebox.askyesno(
                "MangoHud is loaded into your whole desktop session",
                "These files force MangoHud into every process with LD_PRELOAD, "
                "including KWin and Plasma:\n\n" + flist +
                "\n\nRewriting the overlay config while that is active can crash "
                "the desktop — it comes back with no power/battery tray.\n\n"
                "Remove the global LD_PRELOAD line now (MANGOHUD=1 stays, so games "
                "keep the overlay), then log out and back in?\n\n"
                "Yes = fix it and continue   ·   No = cancel this write"):
            self._log("[MangoHud] write cancelled — global LD_PRELOAD still active")
            return False
        for c in confs:
            try:
                kept = [ln for ln in c.read_text().splitlines()
                        if not re.match(r"\s*LD_PRELOAD=\S*libMangoHud", ln)]
                body = "\n".join(kept).rstrip()
                if body:
                    c.write_text(body + "\n")
                else:
                    c.unlink()
                if os.geteuid() == 0 and c.exists() and str(c).startswith(str(uhome)):
                    try:
                        pw = pwd.getpwnam(self.user)
                        os.chown(c, pw.pw_uid, pw.pw_gid)
                    except (KeyError, OSError):
                        pass
                self._log(f"[MangoHud] removed global LD_PRELOAD from {c}")
            except OSError as exc:
                self._log(f"[MangoHud] could not edit {c}: {exc}")
                return False
        messagebox.showinfo(
            "Log out to finish",
            "Global MangoHud LD_PRELOAD removed. Log out and back in so KWin / "
            "Plasma restart without the overlay layer, then the config is safe "
            "to edit.")
        return True

    def _mh_write_conf(self, managed: dict) -> bool:
        """Rewrite the MangoHud config CLEANLY: every key appears once (last
        value wins), leading comments kept, blank lines / inline comments / junk
        dropped. `managed` values: None or '' remove the key, True writes a bare
        toggle (`key`), anything else writes `key=value`. Returns True on success."""
        p = self._mh_conf_path()
        try:
            raw = p.read_text().splitlines() if p.is_file() else []
        except OSError:
            raw = []
        header, order, vals = [], [], {}
        for ln in raw:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("#"):
                if not order:                       # keep only leading comments
                    header.append(s)
                continue
            m = self._MH_KEY_RE.fullmatch(s)
            if m:
                key, val = m.group(1), m.group(2).strip()
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s):
                key, val = s, None              # bare toggle, e.g. `fps`
            else:
                continue                        # unparseable junk — drop it
            if key not in vals:
                order.append(key)
            vals[key] = val
        for k, v in managed.items():
            if v is None or v == "":
                vals.pop(k, None)
                if k in order:
                    order.remove(k)
            elif v is True:
                if k not in vals:
                    order.append(k)
                vals[k] = None                  # bare toggle
            else:
                if k not in vals:
                    order.append(k)
                vals[k] = str(v)
        out = list(header) or ["# MangoHud config (managed by TuxThrottle)"]
        out += [k if vals[k] is None else f"{k}={vals[k]}" for k in order]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace, not an in-place truncate+write: MangoHud's live
            # config watcher (inotify) must never see a half-written / empty
            # file — a truncated read is one way the in-session layer chokes.
            tmp = p.with_name(p.name + ".tuxthrottle-tmp")
            tmp.write_text("\n".join(out).rstrip() + "\n")
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                for path in (tmp, p.parent):
                    try:
                        os.chown(path, pw.pw_uid, pw.pw_gid)
                    except OSError:
                        pass
            os.replace(tmp, p)
        except (OSError, KeyError) as exc:
            self._log(f"[MangoHud] write failed: {exc}")
            return False
        return True

    def _mh_hud_width(self, labels: list, full: bool) -> "str | None":
        """MangoHud's auto-width doesn't grow for a long custom cpu_text /
        gpu_text — the label collides with its value column — so pin `width`
        wide enough. None when there's no long custom label (let MangoHud size
        itself). Calibrated against vkcube: an 18-char label at font_size 20
        needs ~560 px with a full value column ("65.5 W", "0.8 GiB"), less when
        every group is minimal (values are just "42 %"), so the fixed term
        shrinks accordingly."""
        longest = max((len(x) for x in labels if x), default=0)
        if longest <= 8:                        # short enough not to clip
            return None
        fs = self._mh_load_conf().get("font_size") or 24
        # label glyphs ~0.85*fs wide in MangoHud's font; the +term is the value
        # column + gaps — wider with full stats ("65.5 W" / "0.8 GiB") than a
        # minimal HUD where every value is just "42 %"
        pad = 12 if full else 8
        w = int(longest * fs * 0.85) + int(fs * pad)
        return str(max(320, min(1700, w)))

    def _mh_gpu_list(self, n_gpu: int) -> "str | None":
        """`gpu_list` value aligned to our discrete-first GPU rows: for each
        row's PCI address, its index in MangoHud's own cardN ordering. Falls
        back to positional 0,1,… when the mapping can't be resolved 1:1."""
        if n_gpu <= 1:
            return None
        fallback = ",".join(str(i) for i in range(n_gpu))
        pcis = [p for p in getattr(self, "_mh_gpu_pci", []) if p][:n_gpu]
        if len(pcis) != n_gpu:
            return fallback
        try:
            order = sensors.mangohud_gpu_order()
        except Exception:  # noqa: BLE001
            return fallback
        if not order or any(p not in order for p in pcis):
            return fallback
        idxs = [order.index(p) for p in pcis]
        return ",".join(str(i) for i in idxs) if len(set(idxs)) == n_gpu else fallback

    def _mh_toggle_status_line(self):
        enabled = self._mh_status_line.get()
        mangohud_status.set_enabled(enabled, user=self.user)
        self._log(f"[MangoHud] live status line {'enabled' if enabled else 'disabled'}"
                  + ("" if enabled else " (line removed from MangoHud.conf)")
                  + " — shown by the tray monitor, refreshed every ~2s")
        fixlog.log_event("mangohud-status-line",
                         f"status line {'enabled' if enabled else 'disabled'}", user=self.user)

    def _mh_apply(self):
        if not self._mh_guard_global_preload():
            return
        cpu = (self._mh_cpu_var.get() or "").strip()
        allgpu = [(v.get() or "").strip() for v in self._mh_gpu_vars]
        gpus = [g for g in allgpu if g and g != "detecting…"]
        n_gpu = len(self._mh_gpu_vars)
        full = any(v.get() for v in self._mh_lvl.values())
        width = self._mh_hud_width([cpu, *gpus], full)
        managed = {
            "cpu_text": cpu or None,
            "gpu_text": (",".join(gpus) if gpus else None),
            # list every GPU index the machine has, so MangoHud prints each
            # card's own name/stats (that's how you tell two same GPUs apart).
            # MangoHud numbers GPUs by /sys/class/drm/cardN (iGPU usually card0),
            # the reverse of our discrete-first rows — so emit the real MangoHud
            # indices in *our* row order, else gpu_list=0,1 pins the dGPU label
            # on the iGPU's stats line (the "GPU ids are swapped" bug).
            "gpu_list": self._mh_gpu_list(n_gpu),
            "width": width,                     # fit the longest label (or None)
        }
        # stat section: strip every element toggle we own, then add back
        # FPS + API, the frametime graph + GPU-in-use name (each its own switch),
        # the chosen per-group set and the GPU-clock extras
        for k in self._MH_STAT_KEYS:
            managed[k] = ""
        elements = list(self._MH_ALWAYS)
        if getattr(self, "_mh_graph", None) is not None and self._mh_graph.get():
            elements += self._MH_FRAMEGRAPH
        if getattr(self, "_mh_gpuname", None) is not None and self._mh_gpuname.get():
            elements.append(self._MH_GPUNAME)
        for grp, (mini, grpfull) in self._MH_GROUPS.items():
            elements += grpfull if self._mh_lvl[grp].get() else mini
        elements += [k for k, v in getattr(self, "_mh_gpu_extra", {}).items() if v.get()]
        if getattr(self, "_mh_gamemode", None) is not None and self._mh_gamemode.get():
            elements.append("gamemode")
        for e in elements:
            managed[e] = True                   # bare toggle
        # MangoHud defaults these ON — a removed line ≠ off, so pin 0/1 explicitly
        for k in self._MH_EXPLICIT:
            managed[k] = "1" if k in elements else "0"
        if getattr(self, "_mh_pos_set", False):
            managed["position"] = self._mh_pos
            managed["offset_x"] = str(self._mh_ox) if self._mh_ox else None
            managed["offset_y"] = str(self._mh_oy) if self._mh_oy else None
        if not self._mh_write_conf(managed):
            return
        lvls = ",".join(f"{g}:{'full' if v.get() else 'min'}"
                        for g, v in self._mh_lvl.items())
        posn = (f", pos={self._mh_pos}" if getattr(self, "_mh_pos_set", False) else "")
        graph = "on" if getattr(self, "_mh_graph", None) and self._mh_graph.get() else "off"
        self._log(f"[MangoHud] {self._mh_conf_path()}  (cpu={cpu or '—'}, "
                  f"gpu={', '.join(gpus) or '—'}, {lvls}, graph={graph}{posn}; "
                  f"deduped, width={width or 'auto'})")

    def _mh_reset_conf(self):
        if not self._mh_guard_global_preload():
            return
        p = self._mh_conf_path()
        if not messagebox.askyesno(
                "Reset MangoHud config",
                f"Replace {p} with a clean config — FPS, the graphics-API line, "
                f"each GPU's name, the CPU/GPU/Memory groups + extras at the "
                f"levels set by the toggles above, your names + position, "
                f"toggle on Shift_R+F12 — and nothing else?\n\nThe current file "
                f"is backed up to {p.name}.bak first."):
            return
        try:
            if p.is_file():
                p.with_suffix(p.suffix + ".bak").write_text(p.read_text())
                p.unlink()
        except OSError as exc:
            self._log(f"[MangoHud] reset failed: {exc}")
            return
        # styling baseline only — _mh_apply layers on the stat toggles + labels
        base = ["# MangoHud config (managed by TuxThrottle)", "position=top-left",
                "font_size=20", "background_alpha=0.4", "round_corners=6",
                "toggle_hud=Shift_R+F12"]
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(base) + "\n")
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                os.chown(p, pw.pw_uid, pw.pw_gid)
        except (OSError, KeyError) as exc:
            self._log(f"[MangoHud] reset failed: {exc}")
            return
        self._mh_apply()
        self._log(f"[MangoHud] reset to a clean config → {p} (.bak kept)")

    def _build_last_session_card(self, parent):
        lf = tb.Labelframe(parent, text="Last game session", padding=10)
        lf.pack(fill="x", pady=6)
        self._last_sess_lbl = tb.Label(lf, bootstyle=SECONDARY, justify="left",
                                       wraplength=1100)
        self._last_sess_lbl.pack(anchor="w")
        tb.Button(lf, text="↻ Refresh", bootstyle=(SECONDARY, "outline"),
                  command=self._refresh_last_session).pack(anchor="w", pady=(6, 0))
        self._refresh_last_session()

    def _refresh_last_session(self):
        import datetime
        try:
            s = json.loads(self._power_state_path("last_session.json").read_text())
        except (OSError, ValueError):
            self._last_sess_lbl.config(
                text="No session recorded yet. Turn on per-game auto-profiles "
                     "(Profiles tab) and the daemon logs a summary here when a "
                     "mapped game exits.")
            return
        mins = round(s.get("duration_s", 0) / 60)
        when = datetime.datetime.fromtimestamp(
            s.get("ended", 0)).strftime("%b %d %H:%M") if s.get("ended") else "?"
        parts = [f"{s.get('game', '?')} — {mins} min  ({when})",
                 f"CPU max {s.get('cpu_temp_max_c', '?')} °C",
                 f"GPU max {s.get('gpu_temp_max_c', '?')} °C"]
        if s.get("cpu_clock_avg_ghz"):
            parts.append(f"avg CPU {s['cpu_clock_avg_ghz']} GHz")
        if s.get("gpu_clock_avg_mhz"):
            parts.append(f"avg GPU {s['gpu_clock_avg_mhz']} MHz")
        tp = s.get("throttle_pct")
        if tp is not None:
            parts.append(f"thermally throttled {tp}% of the session")
        self._last_sess_lbl.config(text="   ·   ".join(parts))

    def _build_games_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Setup Games")

        intro = tb.Frame(outer, padding=(16, 12, 16, 6))
        intro.pack(fill="x")
        tb.Label(intro, text="Per-game setup walkthroughs",
                 font=("Sans", 12, "bold")).pack(anchor="w")
        tb.Label(intro, bootstyle=SECONDARY, wraplength=1100, justify="left",
                 text="Pick a game from the tabs below, then work down the steps. Steps "
                      "with a Run button do the work (output streams to the log console "
                      "at the bottom of the window); manual steps are quick clicks inside "
                      "the game launcher that can't be scripted — tick “Mark done” once "
                      "you've done them.  Proton-prefix, save-file, shader-cache and "
                      "launch-option tools are on the “Game Tools” tab.").pack(anchor="w", pady=(2, 0))

        gnb = tb.Notebook(outer)
        gnb.pack(fill="both", expand=True, padx=8, pady=8)
        self._games_notebook_body(gnb)

    def _build_gametools_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Game Tools")
        frame = self._scroll_body(outer, pad=16)
        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY, text=(
            "Steam / Proton helpers that work for any game — not just the ones "
            "with a walkthrough on “Setup Games”: relocate a Proton prefix off "
            "a drive that can't host it, pull stray save files back, keep a "
            "save-game vault, choose one home for every shader cache, and build "
            "a launch-options string.")).pack(anchor="w", pady=(0, 12))

        pf = tb.Labelframe(frame, text="Proton prefix & save-file tools", padding=10)
        pf.pack(fill="x", pady=6)
        tb.Label(pf, bootstyle=SECONDARY, wraplength=1100, justify="left",
                 text="A game installed on an NTFS or exFAT drive can't build its Proton "
                      "prefix there (those filesystems reject ':' in a filename, so the "
                      "'dosdevices/c:' … links fail and the game won't start). "
                      "Relocation moves just the prefix onto your Linux drive and "
                      "symlinks it back — game files stay put. The save-file scan finds "
                      "prefixes whose Documents / Saved Games / AppData folder is a "
                      "symlink onto another drive and pulls it back in. Close Steam "
                      "first.").pack(anchor="w")
        row = tb.Frame(pf); row.pack(anchor="w", fill="x", pady=(8, 2))
        self._tip(tb.Button(row, text="Scan Steam prefixes", bootstyle=(INFO, "outline"),
                  command=self._prefix_scan),
                  "List every game's Proton prefix and flag any sitting on an "
                  "NTFS/exFAT drive (those can't host a prefix — the game won't "
                  "start). Read-only.").pack(side="left")
        self._tip(tb.Button(row, text="Migrate all at-risk prefixes",
                  bootstyle=(WARNING, "outline"),
                  command=self._prefix_migrate_all),
                  "Move every prefix that's on an NTFS/exFAT drive onto your "
                  "Linux drive and symlink it back. Game files aren't touched. "
                  "Close Steam first.").pack(side="left", padx=6)
        tb.Label(row, text="   or one — AppID:").pack(side="left")
        self._prefix_appid_var = tk.StringVar()
        tb.Entry(row, textvariable=self._prefix_appid_var, width=12).pack(side="left", padx=(2, 6))
        self._tip(tb.Button(row, text="Relocate this prefix", bootstyle=(WARNING, "outline"),
                  command=self._prefix_relocate_entry),
                  "Do the move above for just the AppID typed in the box. "
                  "Close Steam and the game first.").pack(side="left")
        row2 = tb.Frame(pf); row2.pack(anchor="w", fill="x", pady=(2, 0))
        self._tip(tb.Button(row2, text="Scan for saves on another drive",
                  bootstyle=(INFO, "outline"), command=self._saves_scan),
                  "Find game saves whose prefix folder is a symlink onto another "
                  "drive, plus loose Documents/My Games folders at a drive root. "
                  "Read-only.").pack(side="left")
        self._tip(tb.Button(row2, text="Move all stray saves into their prefixes",
                  bootstyle=(WARNING, "outline"), command=self._saves_move_all),
                  "Pull those symlinked save folders back into each game's "
                  "prefix. The off-drive copy is left in place (nothing deleted)."
                  ).pack(side="left", padx=6)
        self._tip(tb.Button(row2, text="Import loose saves for AppID above",
                  bootstyle=(WARNING, "outline"), command=self._saves_import_entry),
                  "Copy a drive-root Documents / My Games folder into the prefix "
                  "of the AppID typed in the box above.").pack(side="left")

        tb.Separator(pf).pack(fill="x", pady=(10, 6))
        tb.Label(pf, bootstyle=SECONDARY, wraplength=1100, justify="left",
                 text="Save-game vault — a folder on a SEPARATE drive (not the OS/Steam "
                      "drive) holding a copy of every game's saves as <vault>/<appid>/… . "
                      "Export copies saves out of the prefix(es) into it; Import copies "
                      "them back. Leave the AppID field blank to do every prefix at once. "
                      "Close Steam before importing.").pack(anchor="w")
        vrow = tb.Frame(pf); vrow.pack(anchor="w", fill="x", pady=(6, 2))
        tb.Label(vrow, text="Vault folder:").pack(side="left")
        self._vault_var = tk.StringVar(value=self._load_saves_vault())
        tb.Entry(vrow, textvariable=self._vault_var, width=52).pack(side="left", padx=(4, 4))
        self._tip(tb.Button(vrow, text="Browse…", bootstyle=(SECONDARY, "outline"),
                  command=self._vault_browse),
                  "Pick the vault folder. Must be on a drive other than the "
                  "OS/Steam drive (enforced).").pack(side="left")
        vrow2 = tb.Frame(pf); vrow2.pack(anchor="w", fill="x", pady=(2, 0))
        self._tip(tb.Button(vrow2, text="List vault", bootstyle=(INFO, "outline"),
                  command=lambda: self._vault_cmd("list")),
                  "Show which games have saves stored in the vault.").pack(side="left")
        self._tip(tb.Button(vrow2, text="Export saves → vault", bootstyle=(WARNING, "outline"),
                  command=lambda: self._vault_cmd("export")),
                  "Copy Documents / Saved Games / AppData out of the prefix(es) "
                  "into the vault. Blank AppID = every game.").pack(side="left", padx=6)
        self._tip(tb.Button(vrow2, text="Import saves ← vault", bootstyle=(WARNING, "outline"),
                  command=lambda: self._vault_cmd("import")),
                  "Copy the vault's saves back into the prefix(es). Close Steam "
                  "first. Blank AppID = every game.").pack(side="left")

        self._build_shadercache_box(frame)
        self._build_steamperf_box(frame)
        self._build_fixes_box(frame)
        self._build_launch_opts_box(frame)
        self._build_mangohud_box(frame)
        self._build_last_session_card(frame)

    def _games_notebook_body(self, gnb):
        self._game_steps = []
        for gid, game in sorted(self.games.items(),
                                key=lambda kv: (kv[1].get("order", 99), kv[0])):
            page = tb.Frame(gnb)
            gnb.add(page, text=game.get("Tab", game.get("Content", gid)))
            inner = self._scroll_body(page, pad=14)
            desc = game.get("Description", "")
            if desc:
                tb.Label(inner, text=desc, wraplength=1150, justify="left",
                         bootstyle=SECONDARY).pack(anchor="w", pady=(0, 12))

            appid = str(game.get("appid") or "")
            if appid.isdigit():
                pdb_lbl = tb.Label(inner, text="", bootstyle=SECONDARY,
                                   font=("Sans", 9, "italic"))
                pdb_lbl.pack(anchor="w", pady=(0, 10))
                self._pdb_start(appid, pdb_lbl)

            steps = game.get("steps", [])
            n_auto = sum(1 for s in steps if s.get("run"))
            n_manual = sum(1 for s in steps if s.get("manual") and not s.get("run"))
            if n_auto:
                hdr = tb.Frame(inner, padding=12, bootstyle="dark")
                hdr.pack(fill="x", padx=2, pady=(0, 10))
                tb.Button(hdr, text=f"▶▶  Run all {n_auto} automatic steps",
                          bootstyle=SUCCESS,
                          command=lambda g=gid: self._run_game_all(g)).pack(side="left")
                tb.Label(hdr, bootstyle="inverse-dark", wraplength=900, justify="left",
                         text=(f"  Runs the {n_auto} Run-step actions below in order, "
                               "skipping any already done. Steam may open during the "
                               f"BattlEye step. The {n_manual} manual step(s) after still "
                               "need doing by hand.")).pack(side="left")

            for step in steps:
                self._game_step_card(inner, gid, step)

        self._games_q.put("refresh")

    def _pdb_start(self, appid: str, widget):
        """ProtonDB lookup off-thread (network + disk cache); the worker only
        writes a plain dict, never touches Tk — a root.after poller (started
        here, on the main thread) is what applies the result. Mirrors
        _sc_link_check_worker/_sc_link_check_poll: calling root.after() from
        the worker thread itself can crash with 'main thread is not in main
        loop' if it fires before the event loop is fully pumping."""
        result: dict = {}

        def work():
            try:
                result["text"] = protondb.label(protondb.lookup(appid))
            except Exception:  # noqa: BLE001 — a badge is never worth a crash
                result["text"] = ""

        threading.Thread(target=work, daemon=True).start()
        self._pdb_poll(widget, result, tries=0)

    def _pdb_poll(self, widget, result: dict, tries: int):
        if "text" not in result:
            if tries < 100:      # ~20s cap (200ms steps) then give up quietly
                self.root.after(200, self._pdb_poll, widget, result, tries + 1)
            return
        text = result["text"]
        if not text:
            return
        try:
            widget.configure(text=f"⚗ {text}")
        except tk.TclError:
            pass

    def _game_subst(self, gid: str, s: str) -> str:
        return (s.replace("{USER}", self.user)
                 .replace("{TOOLKIT_DIR}", str(BASE_DIR))
                 .replace("{APPID}", str(self.games.get(gid, {}).get("appid", ""))))

    _GAME_CARD_PACK = {"fill": "x", "padx": 2, "pady": 5}

    def _game_step_card(self, parent, gid: str, step: dict):
        card = tb.Frame(parent, padding=14, bootstyle="dark")
        card.pack(**self._GAME_CARD_PACK)
        prev = (self._game_steps[-1]["card"]
                if self._game_steps and self._game_steps[-1]["gid"] == gid
                and "card" in self._game_steps[-1] else None)

        top = tb.Frame(card, bootstyle="dark")
        top.pack(fill="x")
        tb.Label(top, text=step.get("title", step.get("id", "step")),
                 font=("Sans", 11, "bold"), bootstyle="inverse-dark").pack(side="left")
        status = tb.Label(top, text="…", width=12, anchor="e",
                          font=("Sans", 9, "bold"), bootstyle=SECONDARY)
        status.pack(side="right")

        if step.get("desc"):
            tb.Label(card, text=step["desc"], wraplength=1150, justify="left",
                     bootstyle="inverse-dark").pack(anchor="w", pady=(4, 8))

        row = tb.Frame(card, bootstyle="dark")
        row.pack(fill="x")
        rec = {"gid": gid, "step": step, "status": status,
               "card": card, "prev_card": prev}

        if step.get("run"):
            rec["run_btn"] = tb.Button(
                row, text="▶  Run step", bootstyle=SUCCESS,
                command=lambda: self._run_game_step(gid, step))
            rec["run_btn"].pack(side="left")
        copy_txt = self._game_subst(gid, step["copy"]) if step.get("copy") else ""
        if copy_txt:
            tb.Button(row, text="⧉  Copy command", bootstyle=(INFO, "outline"),
                      command=lambda t=copy_txt: self._to_clipboard(t)
                      ).pack(side="left", padx=6)
        if step.get("manual") and not step.get("run"):
            mv = tk.BooleanVar(value=False)
            rec["manual_var"] = mv
            tb.Checkbutton(row, text="Mark done", variable=mv, bootstyle="round-toggle",
                           command=lambda: self._games_q.put("refresh")).pack(side="left", padx=6)

        if copy_txt:
            tb.Label(card, text="  " + copy_txt, font=("Monospace", 9),
                     bootstyle="inverse-dark").pack(anchor="w", pady=(8, 0))

        self._game_steps.append(rec)

    def _refresh_game_steps(self):
        """Off-thread: run each step's `check` and post (index, state) to _games_q.
        Manual-toggle state is snapshotted here on the main thread (Tk vars
        aren't safe to read from the worker)."""
        snap = []
        for rec in self._game_steps:
            mv = rec.get("manual_var")
            snap.append((rec["gid"], rec["step"],
                         bool(mv.get()) if mv is not None else None))

        def work():
            for i, (gid, step, manual_done) in enumerate(snap):
                gate = step.get("show_if")
                if gate:
                    try:
                        if subprocess.run(["bash", "-c", self._game_subst(gid, gate)],
                                          capture_output=True, text=True,
                                          timeout=25).returncode != 0:
                            self._games_q.put((i, "hidden"))
                            continue
                    except Exception:  # noqa: BLE001
                        pass  # can't tell → fall through and show the step
                chk = step.get("check")
                if not chk:
                    state = ("manual-done" if manual_done
                             else "manual" if step.get("manual") else "ready")
                else:
                    chk = self._game_subst(gid, chk)
                    try:
                        rc = subprocess.run(["bash", "-c", chk], capture_output=True,
                                            text=True, timeout=25).returncode
                        state = "done" if rc == 0 else "todo"
                    except Exception:  # noqa: BLE001
                        state = "unknown"
                self._games_q.put((i, state))

        threading.Thread(target=work, daemon=True).start()

    def _poll_games_queue(self):
        try:
            while True:
                msg = self._games_q.get_nowait()
                if msg == "refresh":
                    self._refresh_game_steps()
                elif isinstance(msg, tuple):
                    idx, state = msg
                    if 0 <= idx < len(self._game_steps):
                        self._apply_game_state(self._game_steps[idx], state)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_games_queue)

    @classmethod
    def _apply_game_state(cls, rec: dict, state: str) -> None:
        card = rec.get("card")
        if state == "hidden":
            rec["_hidden"] = True
            try:
                card.pack_forget()
            except (tk.TclError, AttributeError):
                pass
            return
        if rec.pop("_hidden", False) and card is not None:
            kw = dict(cls._GAME_CARD_PACK)
            prev = rec.get("prev_card")
            if prev is not None and prev.winfo_exists():
                kw["after"] = prev
            try:
                card.pack(**kw)
            except tk.TclError:
                pass

        txt, style = {
            "done": ("done ✓", SUCCESS),
            "manual-done": ("done ✓", SUCCESS),
            "todo": ("to do", WARNING),
            "manual": ("manual", INFO),
            "ready": ("optional", SECONDARY),
            "unknown": ("check err", DANGER),
        }.get(state, ("…", SECONDARY))
        try:
            rec["status"].configure(text=txt, bootstyle=style)
        except tk.TclError:
            pass
        # grey out a "Run step" button whose check already passes — e.g. the
        # move-the-Proton-prefix step once this game's compatdata/<appid> prefix
        # is on a Linux drive (already relocated, or never on NTFS/exFAT).
        btn = rec.get("run_btn")
        if btn is not None:
            done = state in ("done", "manual-done")
            try:
                btn.configure(text="✓  Already done" if done else "▶  Run step",
                              state=tk.DISABLED if done else tk.NORMAL)
            except tk.TclError:
                pass

    def _run_stream(self, desc: str, cmd: str, *, tag: str = "Setup Games") -> None:
        """Run one shell command under the busy overlay, streaming stdout to the
        log; a non-zero exit pops the output dialog (via _upd_last). MAIN THREAD
        entry — spawns its own worker."""
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        self._begin_busy(f"{tag} — {desc}", steps=0)
        self._progress(step=desc)
        self._log(f"[{tag}] {desc} …")

        def work():
            rc, tail = -1, []
            try:
                proc = subprocess.Popen(
                    ["bash", "-c", cmd], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    self._log(line)
                    tail.append(line)
                    del tail[:-400]
                    ph = self._phase_from_line(line)
                    if ph:
                        self._progress(step=desc, phase=ph)
                rc = proc.wait()
            except Exception as exc:  # noqa: BLE001
                self._log(f"[{tag} FAILED] {exc}")
                tail.append(f"[{tag} FAILED] {exc}")
            finally:
                result = "done ✓" if rc == 0 else f"exit {rc}"
                self._log(f"[{tag}] {desc} — {result}")
                self._upd_last = {"ok": rc == 0, "rc": rc, "desc": desc,
                                  "reboot": False, "tail": tail}
                self._busy_queue.put(f"{tag}: {desc} — {result}")
                self._games_q.put("refresh")

        threading.Thread(target=work, daemon=True).start()

    def _run_game_step(self, gid: str, step: dict):
        desc = step.get("title", step.get("id", "step"))
        self._run_stream(desc, self._game_subst(gid, step["run"]))

    # ---- Proton prefix relocation (general, any Steam appid) ----

    def _user_py(self, script: str, args: str) -> str:
        """`su - <user> -c 'python3 <BASE_DIR>/<script> <args>'` — run a helper
        as the real user (the GUI itself is elevated)."""
        return (f"su - {shlex.quote(self.user)} -c "
                f"{shlex.quote(f'python3 {BASE_DIR}/{script} {args}')}")

    def _prefix_helper_cmd(self, args: str) -> str:
        return self._user_py("tuxthrottle_prefix_relocate.py", args)

    def _prefix_scan(self):
        self._run_stream("scan Steam prefixes for NTFS/exFAT problems",
                         self._prefix_helper_cmd("--scan"), tag="Prefix tools")

    def _prefix_migrate_all(self):
        if not messagebox.askyesno(
            "Migrate all at-risk prefixes",
            "Move every Proton prefix that's on an NTFS/exFAT drive onto your "
            "Linux drive (symlink left in place). Game files aren't touched.\n\n"
            "Close Steam and all games first. Run “Scan Steam prefixes” beforehand "
            "if you want to see the list.",
        ):
            return
        self._run_stream("migrate all at-risk Proton prefixes",
                         self._prefix_helper_cmd("--all"), tag="Prefix tools")

    def _prefix_relocate_entry(self):
        appid = (self._prefix_appid_var.get() or "").strip()
        if not appid.isdigit():
            messagebox.showinfo("Steam AppID needed",
                                "Enter the numeric Steam AppID of the game "
                                "(shown on its store-page URL, or in the scan output).")
            return
        if not messagebox.askyesno(
            "Relocate Proton prefix",
            f"Move AppID {appid}'s Proton prefix (compatdata/{appid}) onto your "
            "Linux drive and leave a symlink in its place?\n\n"
            "Close Steam and the game first. The game files are not touched; only "
            "the prefix moves. No-op if it's already on a Linux filesystem.",
        ):
            return
        self._run_stream(f"relocate prefix for AppID {appid}",
                         self._prefix_helper_cmd(appid), tag="Prefix tools")

    def _saves_scan(self):
        self._run_stream("scan for save files on another drive",
                         self._prefix_helper_cmd("--saves-scan"), tag="Prefix tools")

    def _saves_move_all(self):
        if not messagebox.askyesno(
            "Move stray saves into prefixes",
            "For every game whose Documents / Saved Games / AppData folder is a "
            "symlink onto another drive, copy that folder into the game's Proton "
            "prefix and replace the symlink.\n\n"
            "The original off-drive copy is left in place — nothing is deleted. "
            "Close Steam and all games first. Run the scan first to see the list.",
        ):
            return
        self._run_stream("move all stray saves into their prefixes",
                         self._prefix_helper_cmd("--saves-all"), tag="Prefix tools")

    def _saves_import_entry(self):
        appid = (self._prefix_appid_var.get() or "").strip()
        if not appid.isdigit():
            messagebox.showinfo("Steam AppID needed",
                                "Put the game's numeric Steam AppID in the field "
                                "above first, then run “Scan for saves on another "
                                "drive” to see which loose folders exist.")
            return
        if not messagebox.askyesno(
            "Import loose saves",
            f"Copy the loose Documents / My Games / Saved Games folders from the "
            f"drive that hosts AppID {appid} into that game's Proton prefix?\n\n"
            "Existing files in the prefix are kept; the originals on the other "
            "drive are left untouched. Close Steam and the game first.",
        ):
            return
        self._run_stream(f"import loose saves for AppID {appid}",
                         self._prefix_helper_cmd(f"--saves-import {appid}"),
                         tag="Prefix tools")

    # ---- save-game vault (bulk export/import to a folder on another drive) ----

    def _saves_vault_file(self) -> "Path":
        try:
            home = Path(pwd.getpwnam(self.user).pw_dir)
        except (KeyError, Exception):  # noqa: BLE001
            home = Path.home()
        return home / ".config" / "tuxthrottle" / "saves_vault"

    def _load_saves_vault(self) -> str:
        try:
            return self._saves_vault_file().read_text().strip()
        except OSError:
            return ""

    def _save_saves_vault(self, path: str) -> None:
        f = self._saves_vault_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(path.strip() + "\n")
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                for p in (f, f.parent):
                    try:
                        os.chown(p, pw.pw_uid, pw.pw_gid)
                    except OSError:
                        pass
        except (OSError, KeyError):
            pass

    def _vault_browse(self):
        from tkinter import filedialog
        try:
            start = pwd.getpwnam(self.user).pw_dir
        except KeyError:
            start = os.path.expanduser("~")
        d = filedialog.askdirectory(
            parent=self.root, initialdir=start,
            title="Pick a save-vault folder on a SEPARATE drive (not the OS/Steam drive)")
        if d:
            self._vault_var.set(d)
            self._save_saves_vault(d)

    def _vault_cmd(self, mode: str):
        vault = (self._vault_var.get() or "").strip()
        if not vault:
            messagebox.showinfo(
                "Pick a vault folder",
                "Choose the save-vault folder first (Browse…). It has to be on a "
                "separate drive — not the OS / Steam drive.")
            return
        self._save_saves_vault(vault)
        appid = (self._prefix_appid_var.get() or "").strip()
        who = appid if appid.isdigit() else "all"
        who_txt = f"AppID {appid}" if appid.isdigit() else "EVERY prefix"
        if mode in ("export", "import"):
            if mode == "export":
                detail = (f"Copy save data for {who_txt} FROM the prefix(es) INTO "
                          f"the vault:\n{vault}\n\nExisting vault files are overwritten.")
            else:
                detail = (f"Copy save data for {who_txt} FROM the vault:\n{vault}\n"
                          f"INTO the prefix(es).\n\nExisting prefix save files are "
                          f"overwritten by the vault copy. Close Steam first.")
            if not messagebox.askyesno(f"{mode.capitalize()} save vault", detail):
                return
        self._run_stream(
            f"save vault {mode} ({who_txt})",
            self._user_py("tuxthrottle_savevault.py",
                          f"{mode} {shlex.quote(vault)} {who}"),
            tag="Save vault")

    def _run_game_all(self, gid: str):
        """Run every step of a game that has a `run` command, in order,
        skipping ones whose `check` already passes. Manual steps are listed
        at the end as a reminder."""
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        game = self.games.get(gid, {})
        steps = game.get("steps", [])
        auto = [s for s in steps if s.get("run")]
        manual = [s for s in steps if s.get("manual") and not s.get("run")]
        if not auto:
            return
        name = game.get("Content", gid)
        lines = "\n".join(f"  {s.get('title', s.get('id'))}" for s in auto)
        if not messagebox.askyesno(
            "Run all automatic steps",
            f"{name}: run these {len(auto)} steps in order?\n\n{lines}\n\n"
            "Steps already done are skipped. Steam may open during the BattlEye "
            f"step. {len(manual)} manual step(s) will still need doing by hand afterwards.",
        ):
            return
        self._begin_busy(f"Setup Games — {name}: all automatic steps", steps=len(auto))
        threading.Thread(target=self._game_all_worker, args=(gid, auto, manual),
                         daemon=True).start()

    def _game_all_worker(self, gid: str, auto: list[dict], manual: list[dict]):
        done = 0
        failed = None
        for step in auto:
            desc = step.get("title", step.get("id", "step"))
            chk = step.get("check")
            if chk:
                ok, _rc, _out = run_cmd3(self._game_subst(gid, chk), timeout=30)
                if ok:
                    self._log(f"[Setup Games] {desc} — already done, skipping")
                    done += 1
                    self._progress(overall=done, step=desc)
                    continue
            self._progress(overall=done, step=desc)
            self._log(f"[Setup Games] {desc} …")
            cmd = self._game_subst(gid, step["run"])
            if self._stream_apply_cmd(cmd):
                self._log(f"[Setup Games] {desc} — done ✓")
                done += 1
                self._progress(overall=done, step=desc)
            else:
                self._log(f"[Setup Games] {desc} — FAILED, stopping the run")
                failed = desc
                break
        self._progress(overall=done)
        if failed:
            self._upd_last = {"ok": False, "rc": 1, "reboot": False,
                              "desc": f"{failed} (batch stopped here)",
                              "tail": [f"'{failed}' failed — see the log above. "
                                       "Fix it, then use its own Run step button or "
                                       "re-run all."]}
            msg = f"Setup Games: stopped at “{failed}”"
        else:
            hint = ""
            if manual:
                hint = "  Now do the manual steps: " + "; ".join(
                    s.get("title", s.get("id")) for s in manual)
            self._log(f"=== {len(auto)} automatic step(s) done.{hint} ===")
            msg = f"Setup Games: {done}/{len(auto)} automatic steps done"
        self._busy_queue.put(msg)
        self._games_q.put("refresh")

    # ---------- app-wide busy lock ----------

    def _begin_busy(self, text: str = "Working…", steps: int = 0) -> None:
        """Lock the UI for a long task. MAIN THREAD ONLY (call from the button
        handler, not the worker). Covers the notebook with a click-eating
        overlay showing two progress bars — overall (determinate when `steps`
        is known) and current task (indeterminate) — plus a step/phase line
        and an elapsed timer. Reversed by _poll_busy_queue on _busy_queue."""
        self._busy = True
        self.worker_running = True
        self._busy_t0 = time.monotonic()
        self._busy_steps = steps
        self._cur_step = ""
        for btn in self._footer_btns:
            btn.configure(state="disabled")
        self.status_var.set(text)
        self._busy_bar.pack(side="right", padx=(8, 0))
        self._busy_bar.start(12)
        if self._busy_overlay is None:
            ov = tk.Frame(self.notebook, cursor="watch", bg="#0e1116")
            ov.place(x=0, y=0, relwidth=1, relheight=1)
            # swallow every pointer/key event so no tab control can be used
            for seq in ("<Button>", "<Key>", "<MouseWheel>", "<Button-4>", "<Button-5>"):
                ov.bind(seq, lambda _e: "break")
            box = tb.Frame(ov, padding=28, bootstyle="dark")
            box.place(relx=0.5, rely=0.4, anchor="center")
            self._busy_label = tb.Label(box, text=text, bootstyle="inverse-dark",
                                        font=("Sans", 12, "bold"))
            self._busy_label.pack(anchor="w", pady=(0, 16))

            tb.Label(box, text="OVERALL", bootstyle="inverse-dark",
                     font=("Sans", 8, "bold")).pack(anchor="w")
            self._busy_bar_overall = tb.Progressbar(box, length=400,
                                                    bootstyle=(SUCCESS, "striped"))
            self._busy_bar_overall.pack(fill="x", pady=(2, 1))
            self._busy_overall_lbl = tb.Label(box, text="", bootstyle="inverse-dark",
                                              font=("Sans", 9))
            self._busy_overall_lbl.pack(anchor="w", pady=(0, 14))

            tb.Label(box, text="CURRENT TASK", bootstyle="inverse-dark",
                     font=("Sans", 8, "bold")).pack(anchor="w")
            self._busy_bar_task = tb.Progressbar(box, length=400, mode="indeterminate",
                                                 bootstyle=(INFO, "striped"))
            self._busy_bar_task.pack(fill="x", pady=(2, 1))
            self._busy_bar_task.start(12)
            self._busy_step = tb.Label(box, text="Preparing…", bootstyle="inverse-dark",
                                       font=("Sans", 9), wraplength=400, justify="left")
            self._busy_step.pack(anchor="w", pady=(0, 14))

            self._busy_elapsed = tb.Label(box, text="Elapsed: 0s",
                                          bootstyle="inverse-dark", font=("Sans", 10))
            self._busy_elapsed.pack(anchor="w")
            tb.Label(box, text="Full output is in the log console below.",
                     bootstyle="inverse-dark", font=("Sans", 9)).pack(anchor="w", pady=(4, 0))
            self._busy_overlay = ov
        else:
            self._busy_label.configure(text=text)
            self._busy_elapsed.configure(text="Elapsed: 0s")
            self._busy_step.configure(text="Preparing…")
            self._busy_overall_lbl.configure(text="")

        ob = self._busy_bar_overall
        if steps > 0:
            ob.stop()
            ob.configure(mode="determinate", maximum=steps, value=0)
        else:
            ob.configure(mode="indeterminate")
            ob.start(16)
        self._busy_overlay.lift()
        self._tick_busy()

    def _progress(self, overall: int | None = None, step: str | None = None,
                  phase: str | None = None) -> None:
        """Thread-safe: feed the two-bar overlay. `overall` = completed-step
        count, `step` = what's being worked on, `phase` = downloading /
        installing / …  (drained in _poll_busy_queue)."""
        self._prog_q.put((overall, step, phase))

    @staticmethod
    def _phase_from_line(line: str) -> str | None:
        low = line.lower()
        pairs = (("downloading", "downloading"), ("get:", "downloading"),
                 ("fetching", "downloading"), ("resolving dependencies", "resolving"),
                 ("dependencies resolved", "resolving"),
                 ("running transaction check", "checking"),
                 ("running scriptlet", "running scripts"),
                 ("running transaction", "installing"),
                 ("upgrading ", "upgrading"), ("installing ", "installing"),
                 ("reinstalling ", "installing"), ("removing ", "removing"),
                 ("erasing ", "removing"), ("verifying ", "verifying"),
                 ("importing gpg key", "importing keys"))
        for needle, label in pairs:
            if needle in low:
                return label
        return None

    @staticmethod
    def _fmt_dur(sec: float) -> str:
        sec = int(sec)
        h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
        if h:
            return f"{h}h {m:02d}m {s:02d}s"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    def _tick_busy(self) -> None:
        """1 Hz elapsed-time updater for the running task; stops itself when
        the busy lock clears."""
        if not self._busy:
            return
        el = self._fmt_dur(time.monotonic() - self._busy_t0)
        try:
            self._busy_elapsed.configure(text=f"Elapsed: {el}")
            self.status_var.set(f"{self._busy_label.cget('text')}   ·   {el}")
        except (tk.TclError, AttributeError):
            pass
        self.root.after(1000, self._tick_busy)

    def _poll_busy_queue(self) -> None:
        """Drain completion signals from worker threads and unlock the UI."""
        # live progress → two-bar overlay
        try:
            while True:
                ov, step, phase = self._prog_q.get_nowait()
                if self._busy_overlay is None:
                    continue
                try:
                    if ov is not None and self._busy_steps > 0:
                        self._busy_bar_overall.configure(value=ov)
                        self._busy_overall_lbl.configure(text=f"{ov} / {self._busy_steps}")
                    if step is not None:
                        self._cur_step = step
                    if step is not None or phase is not None:
                        base = self._cur_step or "Working…"
                        self._busy_step.configure(
                            text=f"{base}   —   {phase}" if phase else base)
                except tk.TclError:
                    pass
        except queue.Empty:
            pass

        done = None
        try:
            while True:
                done = self._busy_queue.get_nowait()
        except queue.Empty:
            pass
        if done is not None:
            elapsed = self._fmt_dur(time.monotonic() - getattr(self, "_busy_t0", time.monotonic()))
            self._busy = False
            self.worker_running = False
            for btn in self._footer_btns:
                btn.configure(state="normal")
            self._busy_bar.stop()
            self._busy_bar.pack_forget()
            self.status_var.set(f"{done}   ·   took {elapsed}")
            if self._busy_overlay is not None:
                self._busy_overlay.destroy()
                self._busy_overlay = None
            # post-task follow-ups from _run_updates (failure detail / reboot)
            info, self._upd_last = getattr(self, "_upd_last", None), None
            if info:
                if not info["ok"]:
                    self._show_output_dialog(
                        f"{info['desc']} — failed (exit {info['rc']})", info["tail"])
                elif info["reboot"] and messagebox.askyesno(
                    "Reboot recommended",
                    f"{info['desc']} finished.\n\nNobara recommends a reboot after a "
                    "system update. Reboot now?"):
                    subprocess.Popen(["systemctl", "reboot"])
            if hasattr(self, "_upd_count_var"):
                self._refresh_update_count()
        if hasattr(self, "_upd_count_q"):
            try:
                self._upd_count_var.set(self._upd_count_q.get_nowait())
            except queue.Empty:
                pass
        self.root.after(150, self._poll_busy_queue)

    def _show_output_dialog(self, title: str, lines: list[str]) -> None:
        """Modal scrollable dump of a task's captured output (used on failure)."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("900x520")
        win.transient(self.root)
        tb.Label(win, text=title, bootstyle=DANGER, font=("Sans", 10, "bold"),
                 padding=(12, 10)).pack(anchor="w")
        tb.Label(win, text="Full output is in the log console at the bottom of the "
                 "main window.", bootstyle=SECONDARY, padding=(12, 0)).pack(anchor="w")
        txt = self._make_log_text(win)
        txt.pack(fill="both", expand=True, padx=12, pady=10)
        txt.configure(state="normal")
        txt.insert("end", "\n".join(lines[-400:]))
        txt.see("end")
        txt.configure(state="disabled")
        tb.Button(win, text="Close", bootstyle=SECONDARY,
                  command=win.destroy).pack(pady=(0, 12))

    # ---------- updates ----------

    def _build_updates_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Updates")
        frame = self._scroll_body(outer, pad=16)

        have_ns = shutil.which("nobara-sync") is not None
        have_flatpak = shutil.which("flatpak") is not None
        have_fwupd = shutil.which("fwupdmgr") is not None

        note = tb.Labelframe(frame, text="System updates", padding=12)
        note.pack(fill="x", pady=(0, 14))
        mgrs = ", ".join(m for m, ok in (("dnf" if have_ns else "dnf (no nobara-sync)", True),
                                         ("flatpak", have_flatpak), ("fwupd", have_fwupd)) if ok)
        tb.Label(
            note, wraplength=1100, justify="left", bootstyle=SECONDARY,
            text=f"Package managers found: {mgrs}. Each has its own section below; "
                 "“Update everything” runs the system + Flatpak updates back to back. "
                 "Output streams to the log console at the bottom of the window; a reboot "
                 "is recommended after a system or firmware update.",
        ).pack(anchor="w")

        self._upd_count_q: queue.Queue = queue.Queue()
        self._upd_count_var = tk.StringVar(value="Updates available:  checking…")
        crow = tb.Frame(note); crow.pack(anchor="w", fill="x", pady=(8, 0))
        tb.Label(crow, textvariable=self._upd_count_var, bootstyle=SECONDARY,
                 font=("Sans", 10, "bold")).pack(side="left")
        tb.Button(crow, text="↻ recount", bootstyle=(INFO, "link"),
                  command=self._refresh_update_count).pack(side="left", padx=8)

        def add(parent, text, style, cmd, desc, reboot=False):
            tb.Button(parent, text=text, bootstyle=style,
                      command=lambda: self._run_updates(cmd, desc, reboot=reboot)
                      ).pack(side="left", padx=4, pady=4)

        def section(title, style):
            lf = tb.Labelframe(frame, text=title, bootstyle=style, padding=12)
            lf.pack(fill="x", pady=6)
            row = tb.Frame(lf); row.pack(anchor="w")
            return row

        sys_update = (f"nobara-sync cli {shlex.quote(self.user)}" if have_ns
                      else "dnf upgrade --refresh -y")

        # ---- Overall ----
        r0 = section("Everything", SUCCESS)
        check_all = " ; ".join(
            [f"echo '### {n}' ; {c} || true" for n, c in (
                ("dnf", "nobara-sync check-updates" if have_ns else "dnf check-update"),
                ("flatpak", "flatpak remote-ls --updates"),
                ("fwupd", "fwupdmgr get-updates")) if (
                n != "flatpak" or have_flatpak) and (n != "fwupd" or have_fwupd)])
        add(r0, "Check everything", (INFO, "outline"), check_all, "check all package managers")
        update_all = " ; ".join([sys_update] + (["flatpak update -y"] if have_flatpak else []))
        add(r0, "Update everything (system + Flatpak)", SUCCESS,
            update_all, "update everything", reboot=True)

        # ---- dnf / Nobara ----
        r1 = section("System — dnf" + (" / nobara-sync" if have_ns else ""), INFO)
        if have_ns:
            add(r1, "Check for updates", (INFO, "outline"),
                "nobara-sync check-updates || true", "check for updates")
            add(r1, "Update system", SUCCESS,
                f"nobara-sync cli {shlex.quote(self.user)}", "update system + fixups",
                reboot=True)
            add(r1, "Apply known fixups", (WARNING, "outline"),
                "nobara-sync install-fixups", "apply known fixups")
            add(r1, "Repair (distro-sync)", (WARNING, "outline"),
                "nobara-sync repair", "repair via distro-sync", reboot=True)
            add(r1, "List enabled repos", (SECONDARY, "outline"),
                "nobara-sync check-repos || true", "list enabled repos")
        else:
            add(r1, "Check for updates", (INFO, "outline"),
                "dnf check-update || true", "check for updates")
        add(r1, "dnf upgrade --refresh", (SECONDARY, "outline"),
            "dnf upgrade --refresh -y", "dnf upgrade --refresh", reboot=True)
        add(r1, "Clean dnf cache", (SECONDARY, "outline"),
            "dnf clean all && dnf makecache", "clean + rebuild dnf cache")
        add(r1, "Fix Fedora GPG keys", (DANGER, "outline"),
            "rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-*-primary "
            "&& dnf clean all && dnf makecache",
            "import Fedora GPG keys + rebuild cache")

        # ---- Flatpak ----
        if have_flatpak:
            rf = section("Flatpak", INFO)
            add(rf, "Check updates", (INFO, "outline"),
                "flatpak remote-ls --updates || true", "check Flatpak updates")
            add(rf, "Update Flatpaks", SUCCESS, "flatpak update -y", "update Flatpaks")
            add(rf, "Remove unused runtimes", (SECONDARY, "outline"),
                "flatpak uninstall --unused -y", "remove unused Flatpak runtimes")

        # ---- Firmware ----
        if have_fwupd:
            rw = section("Firmware — fwupd", WARNING)
            add(rw, "Refresh metadata", (INFO, "outline"),
                "fwupdmgr refresh --force || true", "refresh firmware metadata")
            add(rw, "Show updates", (INFO, "outline"),
                "fwupdmgr get-updates || true", "list firmware updates")
            add(rw, "Apply firmware updates", (DANGER, "outline"),
                "fwupdmgr update -y", "apply firmware updates", reboot=True)

        tb.Label(
            frame, bootstyle=SECONDARY, wraplength=1100, justify="left",
            text="Tip: if an update aborts with a GPG signature error (Nobara ships some "
                 ".fc44 packages signed with a key that's on disk but not imported), run "
                 "“Fix Fedora GPG keys” then retry. Firmware updates can need the charger "
                 "plugged in and a reboot to complete.",
        ).pack(anchor="w", pady=(12, 0))

        self._refresh_update_count()

    def _refresh_update_count(self) -> None:
        """Count pending updates per package manager, off-thread. Result goes to
        _upd_count_var via _upd_count_q (drained in _poll_busy_queue).

        dnf is queried with --cacheonly so this never blocks on slow mirrors —
        the number is "as of the last metadata sync" and becomes exact right
        after any Check/Update action (which refreshes the cache; this then
        re-runs). '?' means the query failed or timed out."""
        self._upd_count_var.set("Updates available:  checking…")

        def sh(cmd: str, timeout: int) -> tuple[int, str]:
            try:
                p = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                   text=True, timeout=timeout)
                return p.returncode, p.stdout
            except Exception:  # noqa: BLE001
                return -1, ""

        def work():
            parts = []
            rc, out = sh("dnf -q --cacheonly check-update 2>/dev/null", 60)
            if rc in (0, 100):
                n = sum(1 for ln in out.splitlines()
                        if ln[:1].isalnum() and len(ln.split()) >= 3
                        and "." in ln.split()[0])
                parts.append(("dnf", str(n)))
            else:
                parts.append(("dnf", "?"))
            if shutil.which("flatpak"):
                rc, out = sh("flatpak remote-ls --updates --columns=application 2>/dev/null", 45)
                parts.append(("flatpak", str(sum(1 for ln in out.splitlines() if ln.strip()))
                              if rc == 0 else "?"))
            if shutil.which("fwupdmgr"):
                rc, out = sh("fwupdmgr get-updates -y 2>/dev/null", 45)
                parts.append(("firmware", str(out.count("New version:")) if rc in (0, 2) else "?"))
            total = sum(int(v) for _, v in parts if v.isdigit())
            detail = "  ·  ".join(f"{k} {v}" for k, v in parts)
            age = _dnf_metadata_age()
            stamp = f"   —   dnf list {age}" if age else ""
            self._upd_count_q.put(f"Updates available:  {total}   ({detail}){stamp}")

        threading.Thread(target=work, daemon=True).start()

    def _run_updates(self, cmd: str, desc: str, reboot: bool = False):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        self._begin_busy(f"Updates — {desc}", steps=0)
        self._progress(step=desc)
        self._log(f"[Updates] {desc} …")

        def work():
            rc, tail = -1, []
            try:
                proc = subprocess.Popen(
                    ["bash", "-c", cmd], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip()
                    self._log(line)
                    tail.append(line)
                    del tail[:-400]
                    ph = self._phase_from_line(line)
                    if ph:
                        self._progress(step=desc, phase=ph)
                rc = proc.wait()
            except Exception as exc:  # noqa: BLE001
                self._log(f"[Updates FAILED] {exc}")
                tail.append(f"[Updates FAILED] {exc}")
            finally:
                result = "done ✓" if rc == 0 else f"exit {rc}"
                self._log(f"[Updates] {desc} — {result}")
                # _poll_busy_queue (main thread) reads this after unlocking, to
                # pop a failure dialog or the reboot prompt — Tk isn't thread-safe.
                self._upd_last = {"ok": rc == 0, "rc": rc, "desc": desc,
                                  "reboot": reboot, "tail": tail}
                self._busy_queue.put(f"Updates: {desc} — {result}")

        threading.Thread(target=work, daemon=True).start()

    # ---------- About ----------

    def _open_url(self, url: str):
        """Open a link in the user's browser. The GUI runs as root, so hand it
        to the real user's session first; fall back to webbrowser."""
        try:
            uid = pwd.getpwnam(self.user).pw_uid
            r = subprocess.run(
                ["sudo", "-u", self.user, "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                 f"DISPLAY={os.environ.get('DISPLAY', ':0')}", "xdg-open", url],
                capture_output=True, timeout=8,
            )
            if r.returncode == 0:
                self._log(f"[About] opened {url}")
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            webbrowser.open(url)
            self._log(f"[About] opened {url}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[About] couldn't open a browser ({exc}) — copy the link instead")

    def _copy_text(self, text: str, what: str = "link"):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set(f"{what.capitalize()} copied.")
        except tk.TclError:
            pass

    def _toggle_about_features(self):
        self._about_open = not self._about_open
        if self._about_open:
            self._about_body.pack(fill="x")
            self._about_btn.configure(text="▾   What's inside  —  click to collapse")
        else:
            self._about_body.pack_forget()
            self._about_btn.configure(text="▸   What's inside  —  every section, expanded")

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

    # ---------- diagnostics / debug report ----------

    def _build_diagnostics_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Report a Bug", kind="support", spacer=True)

        # amber banner — this page is about GitHub issues / sending logs, not
        # changing the machine
        banner = tb.Frame(outer, style="SupportBanner.TFrame", padding=(16, 10))
        banner.pack(fill="x")
        tb.Label(
            banner, style="SupportBanner.TLabel", wraplength=1200, justify="left",
            text="⚑  Bug reports & logs.  This page only READS your system — it gathers "
                 "hardware + OS + toolkit state so you can attach it to a GitHub issue. "
                 "Nothing is uploaded automatically: you Copy or Save the report and paste "
                 "it into the issue yourself. Review it for username / hostname first.",
        ).pack(anchor="w")
        tb.Separator(outer).pack(fill="x")

        frame = tb.Frame(outer, padding=16)      # NOT _scroll_body — the report
        frame.pack(fill="both", expand=True)     # box scrolls itself and must be tall
        self._diag_q: queue.Queue = queue.Queue()
        self._diag_running = False
        self._diag_raw = ""                      # unwrapped report, for Save

        tb.Label(
            frame, wraplength=1200, justify="left", bootstyle=SECONDARY,
            text="Collected: kernel & DMI, "
                 "CPU/GPU, thermal/fan, the keyboard / hotkey / media-key evdev map "
                 "(/proc/bus/input/devices + capability bitmaps), OpenRGB, package "
                 "versions, filtered dmesg / journal. All read-only, hard-timed-out. "
                 "Run the toolkit with sudo for dmesg / RAPL. Review it for your "
                 "username / hostname before sharing.",
        ).pack(anchor="w", pady=(0, 10))

        row = tb.Frame(frame)
        row.pack(anchor="w", pady=(0, 8))
        self._diag_btn = tb.Button(row, text="Generate report", bootstyle=SUCCESS,
                                   command=self._gen_diag)
        self._diag_btn.pack(side="left", padx=(0, 6))
        tb.Button(row, text="⧉ Copy for GitHub issue", bootstyle=INFO,
                  command=lambda: self._to_clipboard(self._diag_text.get("1.0", "end-1c"))
                  ).pack(side="left", padx=4)
        tb.Button(row, text="Copy full issue (template + report)", bootstyle=(INFO, "outline"),
                  command=self._copy_full_issue).pack(side="left", padx=4)
        tb.Button(row, text="Save .txt…", bootstyle=(SECONDARY, "outline"),
                  command=self._save_diag).pack(side="left", padx=4)

        row2 = tb.Frame(frame)
        row2.pack(anchor="w", pady=(0, 8))
        self._bundle_btn = tb.Button(
            row2, text="⇩  Collect hardware bundle (.tar.gz)", bootstyle=(WARNING, "outline"),
            command=self._collect_bundle)
        self._bundle_btn.pack(side="left")
        tb.Label(row2, bootstyle=SECONDARY,
                 text="  — raw sysfs / DMI / evdev-keycaps / hwmon / PCI / OpenRGB dumps; "
                      "attach the file to a “new hardware support” issue").pack(side="left", padx=6)

        box = tb.Labelframe(
            frame, padding=10, bootstyle=WARNING,
            text="  ⧉  GITHUB ISSUE BLOCK — “Copy for GitHub issue” copies exactly what's "
                 "in here (a collapsible <details> block); paste it straight into the issue  ")
        box.pack(fill="both", expand=True, pady=(4, 0))
        self._diag_text = self._make_log_text(box)
        self._diag_text.configure(height=28)
        self._diag_text.pack(fill="both", expand=True)
        self._set_diag("Click “Generate report”.\n\nTerminal equivalent:\n"
                       "  sudo python3 /opt/tuxthrottle/tuxthrottle.py --debug\n")

    def _to_clipboard(self, text: str):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status_var.set("Copied to clipboard.")
        except tk.TclError:
            pass

    def _set_diag(self, text: str):
        self._diag_text.configure(state="normal")
        self._diag_text.delete("1.0", "end")
        self._diag_text.insert("end", text)
        self._diag_text.see("1.0")
        self._diag_text.configure(state="disabled")

    def _collect_bundle(self):
        if self._diag_running:
            return
        self._diag_running = True
        self._bundle_btn.configure(state="disabled", text="Collecting bundle…")
        self.status_var.set("Collecting hardware dump bundle…")

        def work():
            try:
                path = collect_hw_bundle()
                self._diag_q.put(("bundle", path))
            except Exception as exc:  # noqa: BLE001
                self._diag_q.put(("bundle", f"ERROR: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _copy_full_issue(self):
        if not self._diag_raw:
            self.status_var.set("Generate the report first.")
            return
        self._to_clipboard(GITHUB_ISSUE_TEMPLATE.replace(
            "PASTE THE DEBUG REPORT HERE",
            self._diag_raw.replace("```", "``​`").strip()))
        self.status_var.set("Full issue (template + report) copied — paste it on GitHub.")

    def _gen_diag(self):
        if self._diag_running:
            return
        self._diag_running = True
        self._diag_btn.configure(state="disabled", text="Collecting…")
        self._set_diag("Collecting hardware / OS / toolkit info — ~15–30 s…\n")
        items = list(self.items.values())

        def work():
            try:
                self._diag_raw = collect_debug_report(items, wrap=False)
                rep = wrap_issue_block(self._diag_raw)
            except Exception as exc:  # noqa: BLE001
                self._diag_raw = rep = f"debug report failed: {exc}"
            self._diag_q.put(rep)

        threading.Thread(target=work, daemon=True).start()

    def _save_diag(self):
        from tkinter import filedialog
        rep = self._diag_raw.strip()
        if not rep:
            self.status_var.set("Generate the report first.")
            return
        try:
            home = pwd.getpwnam(self.user).pw_dir
        except KeyError:
            home = os.path.expanduser("~")
        name = f"tuxthrottle-debug-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path = filedialog.asksaveasfilename(parent=self.root, initialdir=home,
                                            initialfile=name, defaultextension=".txt")
        if not path:
            return
        try:
            with open(path, "w") as f:
                f.write(rep + "\n")
            if os.geteuid() == 0:
                pw = pwd.getpwnam(self.user)
                os.chown(path, pw.pw_uid, pw.pw_gid)
            self.status_var.set(f"Saved {path}")
        except (OSError, KeyError) as exc:
            self.status_var.set(f"Save failed: {exc}")

    # ---------- dashboard loop ----------

    def _dashboard_loop(self):
        loop_n = 0
        stapm_limit = None       # refreshed every ~10 ticks — ryzenadj -i isn't free,
                                 # and the configured limit only changes when the user
                                 # touches Power & Limits, not every 2s
        while self.dash_running:
            if not getattr(self, "_dash_active", False):
                for _ in range(10):                 # idle ~1s, stay responsive
                    if not self.dash_running:
                        return
                    threading.Event().wait(0.1)
                continue
            cpu_temp = sensors.read_cpu_temp_c_value()
            cpu_freq = sensors.read_cpu_freq_ghz_value()
            cpu_power = sensors.read_cpu_power_watts()  # blocks ~0.1s, fine on this bg thread
            igpu_clock, igpu_temp = sensors.read_igpu_clock_temp_values()
            dgpu_clock, dgpu_temp, dgpu_util, dgpu_power = sensors.read_dgpu_values()
            rapl_ok = sensors.rapl_permissions_ok()
            gamemode = sensors.get_game_mode_state()
            if loop_n % 10 == 0:
                info = sensors.read_ryzenadj_info() if sensors.ryzenadj_available() else None
                stapm_limit = info.get("stapm_limit") if info else None
            loop_n += 1
            self.dash_queue.put((cpu_temp, cpu_freq, cpu_power, igpu_clock, igpu_temp,
                                  dgpu_clock, dgpu_temp, dgpu_util, dgpu_power, rapl_ok,
                                  gamemode, stapm_limit))
            for _ in range(19):  # ~2s poll total (0.1s already spent above), checkable for shutdown
                if not self.dash_running:
                    return
                threading.Event().wait(0.1)

    def _poll_dash_queue(self):
        if not getattr(self, "_dash_built", False):
            try:                                    # tab not built — just drain
                while True:
                    self.dash_queue.get_nowait()
            except queue.Empty:
                pass
            self.root.after(300, self._poll_dash_queue)
            return
        try:
            while True:
                (cpu_temp, cpu_freq, cpu_power, igpu_clock, igpu_temp,
                 dgpu_clock, dgpu_temp, dgpu_util, dgpu_power, rapl_ok,
                 gamemode, stapm_limit) = self.dash_queue.get_nowait()
                if not self._dash_first_data:
                    self._dash_reveal()
                self.meter_cpu_temp.set(cpu_temp)
                self.meter_cpu_freq.set(cpu_freq)
                self.meter_cpu_power.set(cpu_power)
                self.meter_igpu_freq.set(igpu_clock)
                self.meter_dgpu_temp.set(dgpu_temp)
                self.meter_dgpu_freq.set(dgpu_clock)
                self.meter_dgpu_util.set(dgpu_util)
                self.meter_dgpu_power.set(dgpu_power)
                cpu_power_txt = f", {cpu_power:.1f} W" if cpu_power is not None else ""
                stock = (sensors.model_profile().get("cpu", {}) or {}).get("stock_ppt_w")
                if stapm_limit is not None and stock:
                    delta = stapm_limit - stock[0]
                    sign = "+" if delta >= 0 else ""
                    cpu_power_txt += f"  (STAPM {stapm_limit:.0f}W, {sign}{delta:.0f}W vs stock {stock[0]}W)"
                self.dash_cpu_label.configure(text=f"CPU: {cpu_freq:.2f} GHz, {cpu_temp:.0f} C{cpu_power_txt}" if cpu_temp else "CPU: n/a")
                if igpu_clock is not None:
                    self.dash_igpu_label.configure(text=f"iGPU: {igpu_clock} MHz, {igpu_temp:.0f} C" if igpu_temp else f"iGPU: {igpu_clock} MHz")
                else:
                    self.dash_igpu_label.configure(text="iGPU: n/a")
                if dgpu_clock is not None:
                    dgpu_power_txt = f", {dgpu_power:.0f} W" if dgpu_power is not None else ""
                    self.dash_dgpu_label.configure(text=f"dGPU: {dgpu_clock} MHz, {dgpu_temp} C, {dgpu_util}% util{dgpu_power_txt}")
                else:
                    self.dash_dgpu_label.configure(text="dGPU: n/a (asleep or no nvidia-smi)")
                if not rapl_ok:
                    self.rapl_warning.configure(
                        text="⚠ CPU power reads 0/blank — Linux locks RAPL power counters to root by default. "
                             "Install the 'RaplPowerPermissions' tweak (Power tab) to fix this."
                    )
                else:
                    self.rapl_warning.configure(text="")
                self._suppress_gamemode_signal = True
                self.gamemode_var.set(gamemode)
                self._suppress_gamemode_signal = False

                for k, v in (("cpu_temp", cpu_temp), ("cpu_power", cpu_power),
                             ("dgpu_temp", dgpu_temp), ("dgpu_power", dgpu_power)):
                    ch = self._hist_charts.get(k)
                    if ch is not None and v is not None:
                        ch.push(v)
                if self._csv_writer is not None:
                    try:
                        self._csv_writer.writerow([
                            time.strftime("%Y-%m-%d %H:%M:%S"), cpu_temp, cpu_freq,
                            cpu_power, igpu_clock, igpu_temp, dgpu_clock, dgpu_temp,
                            dgpu_util, dgpu_power])
                        self._csv_file.flush()
                    except (OSError, ValueError):
                        pass
        except queue.Empty:
            pass
        self.root.after(300, self._poll_dash_queue)

    def _toggle_csv_log(self):
        if self._csv_logging.get():
            try:
                d = Path(pwd.getpwnam(self.user).pw_dir) / ".local/share/tuxthrottle/sessions"
                d.mkdir(parents=True, exist_ok=True)
                p = d / f"session-{time.strftime('%Y%m%d-%H%M%S')}.csv"
                self._csv_file = open(p, "w", newline="")
                self._csv_writer = csv.writer(self._csv_file)
                self._csv_writer.writerow(
                    ["timestamp", "cpu_temp_c", "cpu_freq_ghz", "cpu_power_w",
                     "igpu_clock_mhz", "igpu_temp_c", "dgpu_clock_mhz",
                     "dgpu_temp_c", "dgpu_util_pct", "dgpu_power_w"])
                if os.geteuid() == 0:
                    pw = pwd.getpwnam(self.user)
                    home = Path(pw.pw_dir)
                    for q in (p, d, d.parent, d.parent.parent):
                        try:
                            if q != home and str(q).startswith(str(home)):
                                os.chown(q, pw.pw_uid, pw.pw_gid)
                        except OSError:
                            pass
                self._csv_path_lbl.configure(text=str(p))
                self._log(f"[Dashboard] logging session to {p}")
            except OSError as exc:
                self._csv_logging.set(False)
                self._log(f"[Dashboard] CSV log failed: {exc}")
        else:
            self._close_csv_log()

    def _close_csv_log(self):
        if getattr(self, "_csv_file", None) is not None:
            try:
                self._csv_file.close()
            except OSError:
                pass
        self._csv_file = self._csv_writer = None
        if hasattr(self, "_csv_path_lbl"):
            try:
                self._csv_path_lbl.configure(text="(stopped)")
            except tk.TclError:                  # dashboard body torn down
                pass

    def _on_gamemode_toggle(self):
        if self._suppress_gamemode_signal:
            return
        enable = self.gamemode_var.get()
        threading.Thread(target=self._gamemode_worker, args=(enable,), daemon=True).start()

    def _gamemode_worker(self, enable: bool):
        ok, err = sensors.set_game_mode(enable)
        if not ok:
            self._log(f"[Game Mode FAILED] {err}")
        else:
            self._log(f"[Game Mode] {'ON' if enable else 'OFF'}")

    # ---------- status / logging ----------

    def _log(self, line: str):
        self.log_queue.put(line)

    def _poll_log_queue(self):
        new = []
        try:
            while True:
                new.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if new:
            self._log_lines.extend(new)
            del self._log_lines[:-4000]
            chunk = "\n".join(new) + "\n"
            for widget in (self.log_text, self._pop_text):
                if widget is None:
                    continue
                widget.configure(state="normal")
                widget.insert("end", chunk)
                widget.see("end")
                widget.configure(state="disabled")
        if hasattr(self, "_diag_q"):
            try:
                rep = self._diag_q.get_nowait()
                self._diag_running = False
                if isinstance(rep, tuple) and rep[0] == "bundle":
                    self._bundle_btn.configure(
                        state="normal", text="⇩  Collect hardware bundle (.tar.gz)")
                    if rep[1].startswith("ERROR"):
                        self.status_var.set(f"Bundle {rep[1]}")
                    else:
                        self.status_var.set(f"Hardware bundle saved: {rep[1]}")
                        messagebox.showinfo(
                            "Hardware bundle",
                            f"Saved:\n{rep[1]}\n\nSkim it for private strings, then attach "
                            "the .tar.gz to a “new hardware support” issue on GitHub.")
                else:
                    self._set_diag(rep)
                    self._diag_btn.configure(state="normal", text="Regenerate report")
                    self.status_var.set("Debug report ready — Copy for GitHub issue.")
            except queue.Empty:
                pass
        self.root.after(120, self._poll_log_queue)

    def _refresh_all_status(self):
        ledger = ledger_load()
        # Each check forks a privileged helper (sudo -u <user> kreadconfig6,
        # flatpak, rpm, systemctl). A wide pool of those at once starved
        # power-profiles-daemon badly enough to trip scx_lavd's stall watchdog,
        # so: warm the sudo/PAM path once, then run the batch NARROW. Results
        # still stream per-item so the pills fill progressively.
        run_cmd3(f"sudo -u {self.user} -H true", timeout=20)   # prime PAM/nss
        items = list(self.items.values())
        if items:
            with ThreadPoolExecutor(max_workers=min(6, len(items))) as ex:
                futs = {ex.submit(evaluate_item, it, ledger): it for it in items}
                for fut in as_completed(futs):
                    self.status_queue.put(futs[fut])
        # Tk is not thread-safe — hand back via the queue, never root.after()
        # from here (races the interpreter). `True` = the batch is done.
        self.status_queue.put(True)

    def _poll_status_queue(self):
        done = []
        try:
            while True:
                done.append(self.status_queue.get_nowait())
        except queue.Empty:
            pass
        if done:
            for x in done:
                if x is not True:
                    self._apply_one_status(x)
            self._recompute_status_summary()
            if any(x is True for x in done) and hasattr(self, "notebook"):
                # batch finished → the section-recommendations button may need
                # to hide (all applied) or update its count
                self._on_nav_page(self.notebook._header.cget("text"))  # noqa: SLF001
        self.root.after(200, self._poll_status_queue)

    def _apply_one_status(self, item):
        if item.status_label is None:
            return
        if not item.hw_supported:
            item.status_label.configure(text="unsupported", bootstyle=SECONDARY)
            return
        label, style = _STATE_UI.get(item.state, _STATE_UI["unknown"])
        if item.kind == "app":
            label = {"Applied": "Installed",
                     "Not applied": "Not installed"}.get(label, label)
        item.status_label.configure(text=label, bootstyle=style)
        if item.var is not None:
            item.var.set(item.done)

    def _recompute_status_summary(self):
        n_done = n_total = n_attention = 0
        for item in self.items.values():
            if item.status_label is None or not item.hw_supported:
                continue
            n_total += 1
            if item.done:
                n_done += 1
            if item.state in ("error", "drifted", "failed"):
                n_attention += 1
        msg = (f"{n_done} of {n_total} applied/installed — "
               f"{n_total - n_done} available.")
        if n_attention:
            msg += f"  ⚠ {n_attention} need attention (see Status report)."
        self.status_var.set(msg)

    def _apply_status_to_widgets(self):
        for item in self.items.values():
            self._apply_one_status(item)
        self._recompute_status_summary()

    def _on_refresh_click(self):
        self.status_var.set("Refreshing status…")
        threading.Thread(target=self._refresh_all_status, daemon=True).start()

    def _show_status_report(self):
        """Scrollable, copyable table of every item: state, the check that
        decided it (+ exit code), and the last thing the toolkit did to it."""
        win = tk.Toplevel(self.root)
        win.title("TuxThrottle — status report")
        win.geometry("1040x640")
        win.transient(self.root)
        tb.Label(win, text="Status report", font=("Sans", 11, "bold"),
                 padding=(12, 10)).pack(anchor="w")
        tb.Label(win, bootstyle=SECONDARY, padding=(12, 0), justify="left",
                 text="State = the item's own check command. “Reverted” = the toolkit "
                      "applied it but the check now fails; “Apply failed” = our last "
                      "attempt errored; “Check error” = the check couldn't run.").pack(anchor="w")
        txt = self._make_log_text(win)
        txt.pack(fill="both", expand=True, padx=12, pady=10)
        txt.configure(state="normal")
        txt.insert("end", format_status_report(self.items.values()))
        txt.configure(state="disabled")
        bar = tb.Frame(win); bar.pack(pady=(0, 12))
        tb.Button(bar, text="Re-check now", bootstyle=(INFO, "outline"),
                  command=lambda: (win.destroy(), self._on_refresh_click())).pack(side="left", padx=4)
        tb.Button(bar, text="Copy", bootstyle=(SECONDARY, "outline"),
                  command=lambda: (self.root.clipboard_clear(),
                                   self.root.clipboard_append(
                                       format_status_report(self.items.values())))
                  ).pack(side="left", padx=4)
        tb.Button(bar, text="Close", bootstyle=SECONDARY, command=win.destroy).pack(side="left", padx=4)

    # ---------- apply logic ----------

    def _stream_apply_cmd(self, cmd: str) -> bool:
        """Run one apply/undo command, streaming its output to the log and
        feeding phase hints (downloading / installing / …) to the overlay."""
        try:
            proc = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as exc:  # noqa: BLE001
            self._log(str(exc))
            return False
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self._log(line)
                ph = self._phase_from_line(line)
                if ph:
                    self._progress(phase=ph)
        try:
            return proc.wait(timeout=3600) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            self._log("[TIMEOUT] command ran over 60 min — killed")
            return False

    def _run_item_apply(self, item: Item):
        # Last-second collision guard: state might be stale (the user installed
        # this app another way since the last refresh). Re-run the (broadened)
        # check right before touching the system and bail if it's already here.
        if item.kind == "app" and item.check_cmd:
            ok, _rc, _out = run_cmd3(item.check_cmd, timeout=30)
            if ok:
                self._log(f"[skip, already present] {item.content} — nothing to install")
                ledger_record(item.id, "apply", True, "already present (another source); skipped")
                return True
        self._log(f"--- Applying: {item.content} ---")
        for n, cmd in enumerate(item.apply_cmds, 1):
            if not self._stream_apply_cmd(cmd):
                self._log(f"[FAILED] {cmd}")
                ledger_record(item.id, "apply", False,
                              f"failed at step {n}/{len(item.apply_cmds)}: {cmd}")
                return False
        self._log(f"[OK] {item.content}")
        ledger_record(item.id, "apply", True, f"{len(item.apply_cmds)} cmd(s) ok")
        return True

    def _run_item_undo(self, item: Item):
        self._log(f"--- Reverting: {item.content} ---")
        for n, cmd in enumerate(item.undo_cmds, 1):
            if not self._stream_apply_cmd(cmd):
                self._log(f"[FAILED] {cmd}")
                ledger_record(item.id, "undo", False,
                              f"failed at step {n}/{len(item.undo_cmds)}: {cmd}")
                return False
        self._log(f"[OK reverted] {item.content}")
        ledger_record(item.id, "undo", True, f"{len(item.undo_cmds)} cmd(s) ok")
        return True

    def _on_apply_click(self):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        selected_ids = [i.id for i in self.items.values() if i.var is not None and i.hw_supported]

        def _runnable(it):
            checked = it.var.get() if it.var else False
            if checked and not it.done:
                return True
            return bool(it.kind == "tweak" and not checked and it.applied and it.undo_cmds)

        n = sum(1 for iid in selected_ids if _runnable(self.items[iid]))
        self._begin_busy("Applying selected tweaks / apps", steps=max(1, n))
        threading.Thread(target=self._apply_worker, args=(selected_ids,), daemon=True).start()

    # ---------- pre-apply snapshot + confirm-or-auto-revert watchdog ----------

    def _pre_risky_snapshot(self, label: str) -> None:
        """Config snapshot (always) + best-effort Btrfs filesystem snapshot
        (only where the root is Btrfs with snapper configured — a no-op
        elsewhere, never fatal). Call this at the start of every worker that
        applies a batch of tweaks."""
        try:
            snap = tuxthrottle_profiles.snapshot(label=label)
            self._log(f"[snapshot] pre-apply rollback point: {snap.name}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[snapshot] couldn't capture a rollback point: {exc}")
        try:
            res = tuxthrottle_btrfs.create_snapshot(label)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[btrfs] snapshot attempt failed: {exc}")
            return
        if res["ok"]:
            self._log(f"[btrfs] {res['msg']} — {tuxthrottle_btrfs.rollback_hint(res['id'])}")
        else:
            self._log(f"[btrfs] {res['msg']}")

    def _arm_watchdog_if_risky(self, item_ids: list[str], seconds: int = 20) -> None:
        """If any item in this batch is risk == 'advanced', arm the
        confirm-or-auto-revert watchdog and pop a countdown dialog on the
        main thread. The watchdog itself is an independent systemd timer —
        it fires the rollback even if this GUI process locks up, which is
        the whole point (see tuxthrottle_watchdog.py docstring)."""
        risky = any(getattr(self.items.get(iid), "risk", "safe") == "advanced"
                   for iid in item_ids)
        if not risky:
            return
        try:
            unit = tuxthrottle_watchdog.arm(seconds, user=self.user)
        except RuntimeError as exc:
            self._log(f"[watchdog] couldn't arm auto-revert timer: {exc}")
            return
        self._log(f"[watchdog] armed — auto-revert in {seconds}s unless confirmed")
        self.root.after(0, self._show_revert_confirm, unit, seconds)

    def _show_revert_confirm(self, unit: str, seconds: int) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirm risky change")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)
        remaining = {"n": seconds}

        tb.Label(dlg, padding=16, wraplength=360, justify="left", text=(
            "A risky (ADVANCED) tweak was just applied. If the system looks "
            "fine, click Keep. If anything is wrong, click Revert Now — "
            "otherwise it reverts automatically when the countdown ends.")
                 ).pack()
        count_lbl = tb.Label(dlg, font=("Sans", 14, "bold"), bootstyle=WARNING,
                             text=f"Auto-revert in {remaining['n']}s")
        count_lbl.pack(pady=(0, 10))

        def _finish(disarm: bool):
            if disarm:
                try:
                    tuxthrottle_watchdog.disarm(unit)
                except Exception:  # noqa: BLE001
                    pass
            try:
                dlg.destroy()
            except tk.TclError:
                pass

        def _revert_now():
            try:
                tuxthrottle_profiles.rollback("last", user=self.user)
                self._log("[watchdog] user chose Revert Now — rolled back")
            except Exception as exc:  # noqa: BLE001
                self._log(f"[watchdog] revert-now failed: {exc}")
            _finish(disarm=True)

        def _keep():
            self._log("[watchdog] user confirmed — keeping the change")
            _finish(disarm=True)

        btn_row = tb.Frame(dlg, padding=(0, 0, 0, 12))
        btn_row.pack()
        tb.Button(btn_row, text="Keep", bootstyle=SUCCESS, command=_keep
                 ).pack(side="left", padx=8)
        tb.Button(btn_row, text="Revert Now", bootstyle=DANGER, command=_revert_now
                 ).pack(side="left", padx=8)

        def _tick():
            if not dlg.winfo_exists():
                return
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                count_lbl.configure(text="Reverting…")
                dlg.after(300, lambda: _finish(disarm=False))
                return
            count_lbl.configure(text=f"Auto-revert in {remaining['n']}s")
            dlg.after(1000, _tick)

        dlg.after(1000, _tick)

    def _apply_worker(self, item_ids: list[str]):
        # always leave a rollback point before a bulk change
        self._pre_risky_snapshot("pre-apply-selected")
        n_skipped = 0
        done = 0
        for item_id in item_ids:
            item = self.items[item_id]
            checked = item.var.get() if item.var else False
            if item.kind == "tweak":
                if checked and not item.done:
                    self._progress(overall=done, step=f"Applying {item.content}")
                    self._run_item_apply(item)
                    done += 1
                elif checked and item.done:
                    n_skipped += 1
                elif not checked and item.applied and item.undo_cmds:
                    self._progress(overall=done, step=f"Reverting {item.content}")
                    self._run_item_undo(item)
                    done += 1
            else:  # app: one-directional install only
                if checked and not item.done:
                    self._progress(overall=done, step=f"Installing {item.content}")
                    self._run_item_apply(item)
                    done += 1
                elif checked and item.done:
                    n_skipped += 1
        self._progress(overall=done)
        if n_skipped:
            self._log(f"[skipped {n_skipped} already-applied/installed item(s)]")
        self._log("=== Done. Click Refresh Status to confirm. ===")
        self._busy_queue.put("Done — refresh to confirm.")
        self._arm_watchdog_if_risky(item_ids)
        threading.Thread(target=self._refresh_all_status, daemon=True).start()

    # ---------- per-section "developer-recommended" apply ----------

    def _recommended_for(self, category: str, pending_only: bool = False) -> list["Item"]:
        out = []
        for it in self.items.values():
            if (it.recommended and it.category == category
                    and it.hw_supported and not it.hidden):
                if pending_only and it.done:
                    continue
                out.append(it)
        return out

    def _recommended_all(self, pending_only: bool = True) -> list["Item"]:
        out = []
        for it in self.items.values():
            if it.recommended and it.hw_supported and not it.hidden:
                if pending_only and it.done:
                    continue
                out.append(it)
        return out

    def _on_apply_all_recommended(self):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        pending = self._recommended_all()
        daemon = self.items.get("FanCurveDaemon")
        want_daemon = bool(daemon and daemon.hw_supported and not daemon.hidden
                           and not daemon.done)
        if not pending and not want_daemon:
            messagebox.showinfo("Nothing to do",
                                "All recommended items are already applied.")
            return
        lines = "\n".join(f"  •  {i.content}" for i in pending)
        if want_daemon:
            lines += "\n  •  Fan-curve + AC-switch daemon (enables the schedule)"
        reboot = any("cmdline" in i.id.lower()
                     or "grubby" in " ".join(i.apply_cmds).lower() for i in pending)
        total = len(pending) + (1 if want_daemon else 0)
        msg = (f"Apply the developer's recommended set — {total} item(s) across "
               f"every category?\n\n{lines}\n\nA snapshot is taken first so you "
               f"can roll back from the Profiles tab.")
        if reboot:
            msg += "\n\n⚠ Some of these change kernel boot params — reboot to finish."
        if not messagebox.askyesno("Apply all recommendations", msg):
            return
        ids = [i.id for i in pending] + (["FanCurveDaemon"] if want_daemon else [])
        self._begin_busy("Applying all recommendations", steps=max(1, len(ids)))
        threading.Thread(target=self._apply_ids_worker,
                         args=(ids, "recommended-all"), daemon=True).start()

    def _on_nav_page(self, page_text: str):
        """Lazy-load / unload the Dashboard on entry / exit, and show the
        'Apply section recommendations' button only on a category page that
        still has unapplied dev picks."""
        want_dash = (page_text == "Dashboard")
        if want_dash and not self._dash_shown:
            self._dash_shown = True
            self._dash_enter()
        elif not want_dash and self._dash_shown:
            self._dash_shown = False
            self._dash_leave()

        want_vram = (page_text == "VRAM")
        if want_vram and not getattr(self, "_vram_live", False):
            self._vram_live = True
            self._vram_poll()
        elif not want_vram:
            self._vram_live = False

        btn = getattr(self, "_rec_btn", None)
        if btn is None:
            return
        pending = self._recommended_for(page_text or "", pending_only=True)
        if pending:
            btn.configure(text=f"★  Apply the {len(pending)} recommended for {page_text}")
            if not btn.winfo_ismapped():
                btn.pack(side="right")
            self._rec_target = page_text
        elif btn.winfo_ismapped():
            btn.pack_forget()

    def _on_apply_recommended(self):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        cat = getattr(self, "_rec_target", None)
        pending = self._recommended_for(cat or "", pending_only=True)
        if not pending:
            messagebox.showinfo("Nothing to do",
                                f"The recommended items for {cat} are already applied.")
            return
        reboot = any("cmdline" in i.id.lower() or "grubby" in " ".join(i.apply_cmds).lower()
                     for i in pending)
        lines = "\n".join(f"  •  {i.content}" for i in pending)
        msg = (f"Apply the developer's recommended {len(pending)} item(s) for "
               f"“{cat}”?\n\n{lines}\n\nA snapshot is taken first so you can roll "
               f"back from the Profiles tab.")
        if reboot:
            msg += "\n\n⚠ Some of these change kernel boot params — reboot to finish."
        if not messagebox.askyesno("Apply section recommendations", msg):
            return
        ids = [i.id for i in pending]
        self._begin_busy(f"Applying recommended — {cat}", steps=max(1, len(ids)))
        threading.Thread(target=self._apply_ids_worker,
                         args=(ids, f"recommended-{cat}"), daemon=True).start()

    def _apply_ids_worker(self, item_ids: list[str], label: str):
        self._pre_risky_snapshot(f"pre-{label}")
        done = 0
        for item_id in item_ids:
            item = self.items.get(item_id)
            if not item or item.done or not item.hw_supported:
                continue
            self._progress(overall=done,
                           step=f"{'Installing' if item.kind == 'app' else 'Applying'} {item.content}")
            self._run_item_apply(item)
            done += 1
        self._progress(overall=done)
        self._log(f"=== Applied {done} recommended item(s). Refresh Status to confirm. ===")
        self._busy_queue.put(f"{label}: {done} applied — refresh to confirm.")
        self._arm_watchdog_if_risky(item_ids)
        threading.Thread(target=self._refresh_all_status, daemon=True).start()

    def _on_apply_preset(self, preset_id: str):
        if self._busy:
            messagebox.showinfo("Busy", "An operation is already running — check the log.")
            return
        preset = self.presets[preset_id]
        if not messagebox.askyesno(
            "Confirm preset",
            f"Apply preset '{preset['Content']}'?\n\n{len(preset.get('tweaks', []))} tweaks + "
            f"{len(preset.get('apps', []))} apps will be applied/installed.",
        ):
            return
        ids = list(preset.get("tweaks", [])) + list(preset.get("apps", []))
        n = sum(1 for i in ids
                if (it := self.items.get(i)) and it.hw_supported and not it.done)
        self._begin_busy(f"Applying preset — {preset['Content']}", steps=max(1, n))
        threading.Thread(target=self._preset_worker, args=(ids, preset_id), daemon=True).start()

    def _preset_worker(self, item_ids: list[str], preset_id: str = ""):
        self._pre_risky_snapshot(f"pre-preset-{preset_id}" if preset_id else "pre-preset")
        done = 0
        for item_id in item_ids:
            item = self.items.get(item_id)
            if not item or not item.hw_supported:
                continue
            if not item.done:
                verb = "Installing" if item.kind == "app" else "Applying"
                self._progress(overall=done, step=f"{verb} {item.content}")
                self._run_item_apply(item)
                done += 1
            else:
                state = "pending reboot" if item.pending else ("installed" if item.kind == "app" else "applied")
                self._log(f"[skip, already {state}] {item.content}")
        self._progress(overall=done)
        self._log("=== Preset done. Click Refresh Status to confirm. ===")
        self._busy_queue.put("Preset done — refresh to confirm.")
        self._arm_watchdog_if_risky(item_ids)
        threading.Thread(target=self._refresh_all_status, daemon=True).start()


def _dnf_metadata_age() -> str:
    """Human 'as of …' string for the newest dnf repo metadata on disk, so the
    update count reads as a snapshot, not a live number. '' if not found."""
    newest = 0.0
    for pat in ("/var/cache/dnf/*/repodata/repomd.xml",
                "/var/cache/libdnf5/*/repodata/repomd.xml"):
        for p in glob.glob(pat):
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                pass
    if not newest:
        return ""
    secs = max(0, time.time() - newest)
    if secs < 90:
        return "as of just now"
    if secs < 5400:
        return f"as of {round(secs / 60)} min ago"
    if secs < 172800:
        return f"as of {round(secs / 3600)} h ago"
    return f"as of {round(secs / 86400)} d ago"


def toolkit_version() -> str:
    """Human version string — date-based `YY.MM.DD` (Xylonic-style), keyed to
    the day of the last commit. Priority: the deploy stamp install.sh writes
    (`.version`, since /opt has no .git) → the last git commit date when running
    from a checkout → the committed `VERSION` file (source tarball, no git)
    → "unknown"."""
    # the deploy stamp wins, but only when this is NOT a git checkout — a stray
    # .version left in a source tree must never shadow the live commit date.
    if not (BASE_DIR / ".git").exists():
        try:
            v = (BASE_DIR / ".version").read_text().strip()
            if v:
                return v
        except OSError:
            pass
    dv = run_cmd3(f"git -C {BASE_DIR} log -1 --format=%cd --date=format:%y.%m.%d "
                  f"2>/dev/null")[2].strip()
    if dv:
        return dv
    try:
        v = (BASE_DIR / "VERSION").read_text().strip()
        if v:
            return v
    except OSError:
        pass
    return "unknown"


def _diag_fans() -> str:
    lines = []
    try:
        lines.append(f"platform_profile: {sensors.get_platform_profile()}  "
                     f"choices={sensors.platform_profile_choices()}")
        fans = sensors.read_fans()
        for f in fans or []:
            lines.append(f"  {f['label']}: {f['rpm']} rpm  (max {f['max']}, boost {f['boost']})")
        if not fans:
            lines.append("  (no alienware_wmi / dell_smm fan interface found)")
        lines.append(f"dell_smm pwm state (enable,value): {sensors.get_pwm_state()}")
        lines.append(f"dGPU awake: {sensors.dgpu_is_awake()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(fan probe failed: {exc})")
    return "\n".join(lines)


# Groups of (title, shell-command, max-lines). Kept read-only + quick; every
# command is best-effort. Inspired by the evtest / /proc/bus/input/devices /
# dmesg dumps used to bring this board up in the first place.
_DEBUG_CMDS = [
    ("── SYSTEM ──", None, 0),
    ("OS", "cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION|VARIANT|ID|BUILD)' ", 12),
    ("Kernel / cmdline", "uname -a; echo; cat /proc/cmdline", 6),
    ("Firmware / DMI", "for f in sys_vendor product_name product_sku board_name board_version "
     "bios_vendor bios_version bios_date chassis_type; do "
     "printf '%-16s %s\\n' \"$f\" \"$(cat /sys/class/dmi/id/$f 2>/dev/null)\"; done", 16),
    ("Desktop session", "for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do "
     "t=$(loginctl show-session \"$s\" -p Type --value 2>/dev/null); "
     "case \"$t\" in wayland|x11) loginctl show-session \"$s\" -p Name -p Type -p Desktop -p Active "
     "-p Remote 2>/dev/null; break;; esac; done; "
     "echo \"XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-} XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-}\"", 10),
    ("Uptime / load", "uptime", 3),
    ("── CPU / MEMORY ──", None, 0),
    ("CPU", "lscpu 2>/dev/null | grep -E 'Model name|^CPU\\(s\\)|Thread|Core|Socket|CPU max|Vendor'; "
     "echo \"governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null) "
     "epp: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)\"", 16),
    ("Memory / zram", "free -h; echo; zramctl 2>/dev/null; swapon --show 2>/dev/null", 14),
    ("── GPU ──", None, 0),
    ("PCI display devices", "lspci -nnk 2>/dev/null | grep -iA3 -E 'vga compatible|3d controller|display controller'", 24),
    ("NVIDIA", "nvidia-smi 2>/dev/null | head -18 || echo '(nvidia-smi unavailable — driver missing or dGPU runtime-suspended)'", 20),
    ("NVIDIA runtime PM", "for d in /sys/bus/pci/devices/*; do [ \"$(cat $d/vendor 2>/dev/null)\" = 0x10de ] && "
     "echo \"$(basename $d)  class=$(cat $d/class 2>/dev/null)  power=$(cat $d/power/runtime_status 2>/dev/null)\"; done", 6),
    ("AMD iGPU", "for c in /sys/class/drm/card[0-9]*/device; do [ \"$(cat $c/vendor 2>/dev/null)\" = 0x1002 ] && { "
     "echo \"$c\"; echo \" dpm: $(cat $c/power_dpm_force_performance_level 2>/dev/null)\"; "
     "cat $c/pp_dpm_sclk 2>/dev/null; }; done", 20),
    ("Mesa / GL", "glxinfo -B 2>/dev/null | grep -E 'OpenGL renderer|OpenGL version|Device:|Video memory' "
     "|| echo '(glxinfo not installed)'", 10),
    ("── THERMAL / POWER ──", None, 0),
    ("platform_profile", "echo \"current: $(cat /sys/firmware/acpi/platform_profile 2>/dev/null)\"; "
     "echo \"choices: $(cat /sys/firmware/acpi/platform_profile_choices 2>/dev/null)\"", 4),
    ("power-profiles-daemon", "powerprofilesctl get 2>/dev/null; echo '---'; powerprofilesctl 2>/dev/null | head -24", 26),
    ("Fans / hwmon", _diag_fans, None),
    ("sensors", "sensors 2>/dev/null || echo '(lm_sensors not installed)'", 45),
    ("RAPL (CPU power)", "ls -l /sys/class/powercap/*/energy_uj 2>/dev/null; "
     "(head -c1 /sys/class/powercap/intel-rapl:0/energy_uj >/dev/null 2>&1 && echo 'RAPL readable') "
     "|| echo 'RAPL NOT readable without root (kernel side-channel mitigation)'", 10),
    ("── KEYBOARD / HOTKEYS / MEDIA KEYS ──", None, 0),
    ("Loaded modules", "lsmod | grep -E '^(dell|alienware|i8k|sparse_keymap|hid_|nvidia|amdgpu)' | sort", 30),
    ("Alienware USB LED controller", "lsusb 2>/dev/null | grep -iE '187c:|alienware' || echo '(187c:0550 AW-ELC not seen on USB)'", 4),
    ("HID devices", "for h in /sys/bus/hid/devices/*; do [ -e \"$h\" ] || continue; "
     "printf '%-24s %s\\n' \"$(basename $h)\" \"$(cat $h/input/input*/name 2>/dev/null | head -1)\"; done", 20),
    ("input devices (evdev + KEY capability bitmaps)", "cat /proc/bus/input/devices", 140),
    ("event device names", "for e in /dev/input/event*; do "
     "printf '%-22s %s\\n' \"$e\" \"$(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null)\"; done", 30),
    ("Dell WMI / hotkey / media-key devices", "for e in /dev/input/event*; do "
     "n=$(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null); "
     "case \"$n\" in *WMI*|*wireless\\ hotkey*|*Wireless\\ hotkey*|*Translated\\ Set\\ 2*|*Video\\ Bus*) "
     "echo \"$e  $n\";; esac; done", 15),
    ("Fn-Lock / G-key note", "echo 'G-key = KEY_PERFORMANCE(701) on \"AT Translated Set 2 keyboard\" when Fn-Lock OFF, "
     "KEY_F9 when ON. Media keys (vol/mute) come via \"Dell WMI hotkeys\". Fn is an EC key and never reaches evdev.'", 4),
    ("input group membership", "u=$(logname 2>/dev/null || echo \"${SUDO_USER:-}\"); "
     "echo \"desktop user: $u\"; id \"$u\" 2>/dev/null; getent group input; "
     "id -nG \"$u\" 2>/dev/null | tr ' ' '\\n' | grep -qx input "
     "&& echo 'OK: user is in the input group' || echo 'WARN: user NOT in input group "
     "(the G-key HotkeyListener needs it)'", 8),
    ("── RGB KEYBOARD (OpenRGB) ──", None, 0),
    ("OpenRGB", "openrgb --version 2>/dev/null | head -1 || echo '(openrgb not installed)'", 4),
    ("OpenRGB devices", "openrgb --noautoconnect -l 2>/dev/null | grep -vE '<[a-z]|i2c|SMBus|help.openrgb' | head -40", 45),
    ("kbd services", "for s in tuxthrottle-openrgb.service tuxthrottle-kbd.service; do "
     "printf '%-26s enabled=%-9s active=%s\\n' \"$s\" "
     "\"$(systemctl is-enabled $s 2>/dev/null)\" \"$(systemctl is-active $s 2>/dev/null)\"; done", 6),
    ("kbd saved state", "u=$(logname 2>/dev/null || echo \"${SUDO_USER:-$USER}\"); "
     "h=$(getent passwd \"$u\" | cut -d: -f6); cat \"$h/.config/tuxthrottle/kbd.json\" 2>/dev/null "
     "|| echo '(no kbd.json — colour not saved / KbdBacklightFix not used)'", 24),
    ("── TWEAK SERVICES / SUDOERS ──", None, 0),
    ("tuxthrottle units", "systemctl list-unit-files 2>/dev/null | grep -E 'tuxthrottle|hotkey' ; "
     "systemctl --user list-unit-files 2>/dev/null | grep -E 'tuxthrottle|hotkey'", 12),
    ("sudoers drop-ins", "ls -l /etc/sudoers.d/ 2>/dev/null | grep -E 'tuxthrottle|gamemode|claude' || echo '(none)'", 8),
    ("── PACKAGES ──", None, 0),
    ("Kernels installed", "rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\\n' 2>/dev/null | sort -V", 10),
    ("NVIDIA packages", "rpm -qa 2>/dev/null | grep -iE 'nvidia|akmod-nvidia|cuda' | sort "
     "|| echo '(no NVIDIA packages — driver may be from a -NV image or missing)'", 12),
    ("Relevant packages", "rpm -q openrgb gamemode mangohud goverlay gamescope vkbasalt lm_sensors "
     "nobara-updater tlp auto-cpufreq 2>&1 | sed 's/ is not installed/  — NOT installed/'", 14),
    ("Update tooling", "dnf --version 2>/dev/null | head -1; command -v nobara-sync >/dev/null && echo 'nobara-sync: present'; "
     "command -v flatpak >/dev/null && flatpak --version; command -v fwupdmgr >/dev/null && echo 'fwupd: present'", 6),
    ("── LOGS ──", None, 0),
    ("dmesg (filtered, deduped)", "dmesg 2>/dev/null "
     "| grep -iE 'dell_|dell-|alienware|aw-elc|187c:0550|hid-generic 0003:187C|i8042|"
     "firmware bug|thermal (throttl|event)|MCE|hardware error|"
     "(nvidia|amdgpu|nouveau).*(error|fail|warn|timed? ?out|reset|hang|fault|Xid)|"
     "platform.?profile|pstate' "
     "| grep -viE 'Mode Validation Warning|Unknown Status failed|Console: switching|fbcon' "
     "| sed -E 's/^\\[[0-9. ]+\\] //' | awk '!seen[$0]++' | tail -40 "
     "|| echo '(dmesg not readable — run the toolkit with sudo, or kernel.dmesg_restrict=1)'", 42),
    ("journal errors (this boot)", "journalctl -b -p err --no-pager 2>/dev/null "
     "| grep -viE 'Module lib.*from rpm|^ *Module |drkonqi|KCrash|Stack trace|"
     "^ *#[0-9]+ +0x|libQt6|libKF6|libc\\.so|__libc_start' "
     "| awk '!seen[$0]++' | tail -35 || echo '(journalctl unavailable)'", 37),
    ("journal — kbd / fan / gpu units (this boot)", "journalctl -b --no-pager "
     "-u 'tuxthrottle-*' -u 'tuxthrottle-*.service' 2>/dev/null | tail -25; "
     "journalctl -b --no-pager 2>/dev/null | grep -iE "
     "'openrgb\\[|dell_smm|alienware_wmi|nvidia-persistenced|(nvidia|amdgpu).*(Xid|GPU has fallen|ring .* timeout)' "
     "| grep -viE 'audit\\[|sudo\\[|Mode Validation' | awk '!seen[$0]++' | tail -20 || echo '(none)'", 40),
]


def collect_debug_report(items=None, wrap: bool = False) -> str:
    """Assemble a hardware + OS + toolkit-state report for bug reports. All
    commands are read-only, hard-timed-out and best-effort. Run as root for
    the complete picture (dmesg, RAPL, privileged checks). `wrap=True` returns
    it inside a GitHub `<details>` + fenced block, ready to paste."""
    hdr = [
        "TuxThrottle — debug report",
        f"generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')}   toolkit {toolkit_version()}   "
        f"euid={os.geteuid()}",
        "REVIEW BEFORE PASTING — this contains your username, hostname and hardware IDs.",
        "=" * 92, "",
    ]
    body = []
    for title, cmd, maxlines in _DEBUG_CMDS:
        if cmd is None:                       # section divider
            body.append(f"\n{title}")
            continue
        try:
            if callable(cmd):
                out = cmd()
            else:  # hard cap via coreutils `timeout` so nothing can wedge
                out = run_cmd3(f"timeout -k 2 12 bash -lc {shlex.quote(cmd)}", timeout=16)[2]
        except Exception as exc:              # noqa: BLE001
            out = f"(error: {exc})"
        out = out.strip() or "(no output)"
        if maxlines:
            ls = out.splitlines()
            if len(ls) > maxlines:
                out = "\n".join(ls[:maxlines]) + f"\n… ({len(ls) - maxlines} more lines trimmed)"
        body.append(f"\n### {title}\n{out}")

    body.append("\n\n── TOOLKIT: KEYBOARD DRIVER ──")
    try:
        info = __import__("tuxthrottle_kbd").info()
        body.append("\n### tuxthrottle_kbd info\n" +
                    "\n".join(f"{k:16}: {v}" for k, v in info.items()))
    except Exception as exc:  # noqa: BLE001
        body.append(f"\n### tuxthrottle_kbd info\n(error: {exc})")

    body.append("\n\n── TOOLKIT: APPLY STATUS ──")
    if items is None:
        items = _load_all_items()
        led = ledger_load()
        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(lambda it: evaluate_item(it, led), items))
    body.append("\n" + format_status_report(items))

    body.append("\n── TOOLKIT: APPLY LEDGER (state.json) ──\n" +
                json.dumps(ledger_load(), indent=2, sort_keys=True))
    report = "\n".join(hdr) + "\n".join(body) + "\n"
    return wrap_issue_block(report) if wrap else report


def wrap_issue_block(report: str) -> str:
    """Wrap a raw report in a GitHub-ready collapsible fenced block."""
    return ("<details><summary>debug report — TuxThrottle</summary>\n\n"
            "```\n" + report.replace("```", "``​`").rstrip() + "\n```\n\n</details>\n")


GITHUB_ISSUE_TEMPLATE = """\
### What happened


### What you expected instead


### Where in the toolkit (which page / button / tweak)


### Steps to reproduce
1.
2.
3.

### Is your hardware the Dell G15 5515 Ryzen Edition on Nobara?
<!-- This tool is written for exactly that one machine. On anything else most
     checks/tweaks won't apply — say what you're on. -->
- [ ] yes, G15 5515 Ryzen + Nobara
- [ ] close (other G15 / other Dell hybrid) — details:
- [ ] no — details:

### Debug report
<!-- Toolkit → Report a Bug page → "Generate report" → "Copy report",
     or a terminal:  sudo python3 /opt/tuxthrottle/tuxthrottle.py --debug
     Review it for your username/hostname, then paste between the ``` fences. -->
<details><summary>debug report</summary>

```
PASTE THE DEBUG REPORT HERE
```

</details>

### Screenshot / log console output (if relevant)

"""


# ── new-hardware onboarding: a raw dump bundle to attach to a support issue ──

# linux/input-event-codes.h — the codes that matter for a laptop's function /
# media / hardware keys. Unknowns print as KEY_<n>.
_KEY_CODE_NAMES = {
    59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6", 65: "F7",
    66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
    99: "SYSRQ", 110: "INSERT", 111: "DELETE", 119: "PAUSE", 127: "MENU",
    113: "MUTE", 114: "VOLUMEDOWN", 115: "VOLUMEUP", 116: "POWER",
    128: "STOP", 140: "CALC", 142: "SLEEP", 143: "WAKEUP",
    148: "PROG1", 149: "PROG2", 150: "WWW", 152: "SCREENLOCK",
    158: "BACK", 159: "FORWARD", 161: "EJECTCD",
    163: "NEXTSONG", 164: "PLAYPAUSE", 165: "PREVIOUSSONG", 166: "STOPCD",
    172: "HOMEPAGE", 173: "REFRESH", 190: "PROG3", 191: "PROG4",
    202: "PAUSECD", 217: "SEARCH",
    224: "BRIGHTNESSDOWN", 225: "BRIGHTNESSUP", 226: "MEDIA",
    227: "SWITCHVIDEOMODE", 228: "KBDILLUMTOGGLE", 229: "KBDILLUMDOWN",
    230: "KBDILLUMUP", 236: "BATTERY", 238: "WLAN", 239: "UWB",
    240: "UNKNOWN", 241: "VIDEO_NEXT", 244: "BRIGHTNESS_AUTO",
    245: "DISPLAY_OFF", 246: "WWAN", 247: "RFKILL", 248: "MICMUTE",
    418: "SCALE", 431: "ASSISTANT", 464: "FN", 484: "FN_RIGHT_SHIFT",
    582: "MICMUTE", 701: "PERFORMANCE (the G-key / G-Mode)",
}
for _i in range(183, 195):                       # 183-194 -> F13..F24
    _KEY_CODE_NAMES[_i] = f"F{_i - 170}"


def _model_scaffold_json() -> str:
    """Run the model-profile scaffold generator (probes DMI / hwmon / PCI /
    OpenRGB / battery method — no writes) and return its JSON. This is the
    starting point for a new `models/<slug>.json`; the maintainer fills the
    `_todo` fields from the other bundle files."""
    try:
        import tuxthrottle_modelgen
        return json.dumps(tuxthrottle_modelgen.build_scaffold(), indent=2)
    except Exception as exc:  # noqa: BLE001
        return f"(model scaffold generation failed: {exc})"


def _decode_key_caps() -> str:
    """For each evdev device in /proc/bus/input/devices, decode its `B: KEY=`
    capability bitmap into KEY_ names — the fastest way to see what a new
    laptop's Fn / media / vendor keys can emit, without live evtest."""
    ok, _rc, blob = run_cmd3("cat /proc/bus/input/devices", timeout=8)
    if not ok:
        return "(could not read /proc/bus/input/devices)"
    out, name, keyline = [], "?", ""
    def flush():
        if not keyline:
            return
        words = keyline.split()
        codes = []
        for wi, w in enumerate(reversed(words)):
            try:
                val = int(w, 16)
            except ValueError:
                continue
            for bit in range(64):
                if val >> bit & 1:
                    codes.append(wi * 64 + bit)
        pretty = ", ".join(
            f"{c}:{_KEY_CODE_NAMES.get(c, 'KEY_' + str(c))}" for c in sorted(codes)
            if c >= 55 or c in _KEY_CODE_NAMES)          # skip the boring alnum block
        out.append(f"[{name}]\n  {pretty or '(only standard keys)'}\n")
    for ln in blob.splitlines():
        if ln.startswith("N: Name="):
            flush(); name = ln.split('"', 2)[1] if '"' in ln else ln[8:]; keyline = ""
        elif ln.startswith("B: KEY="):
            keyline = ln[7:].strip()
    flush()
    return "\n".join(out) or "(no KEY-capable devices found)"


def _collect_display_txt() -> str:
    """kscreen-doctor needs the real user's Wayland/D-Bus session — run bare
    as root (which every other bundle command here does, via plain shell) it
    SIGABRTs instead of erring cleanly, leaving a coredump behind every single
    time the bundle is collected. sensors._session_cmd() hops back to the
    real user's session the same way the Display tab's refresh-rate switcher
    already does."""
    out = ["# kscreen-doctor"]
    for args in (["kscreen-doctor", "-o"], ["kscreen-doctor", "-j"]):
        try:
            r = subprocess.run(sensors._session_cmd(args), capture_output=True,
                               text=True, timeout=6)
            out.append(r.stdout.strip())
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(f"(kscreen-doctor failed: {exc})")
        out.append("")
    out.append("# xrandr")
    try:
        r = subprocess.run(sensors._session_cmd(["xrandr", "--verbose"]),
                           capture_output=True, text=True, timeout=6)
        out.append("\n".join(r.stdout.splitlines()[:120]))
    except (OSError, subprocess.SubprocessError) as exc:
        out.append(f"(xrandr failed: {exc})")
    out.append("")
    out.append("# drm modes")
    for m in sorted(glob.glob("/sys/class/drm/*/modes")):
        out.append(f"{m}:")
        try:
            out.append(Path(m).read_text().strip())
        except OSError:
            pass
    out.append("")
    out.append("# vrr_capable")
    for m in sorted(glob.glob("/sys/class/drm/*/vrr_capable")):
        try:
            out.append(f"{m}:{Path(m).read_text().strip()}")
        except OSError:
            pass
    return "\n".join(out)


_HW_BUNDLE_FILES = [
    ("model-scaffold.json", _model_scaffold_json),
    ("dmi.txt", "grep -r . /sys/class/dmi/id/ 2>/dev/null | sed 's#/sys/class/dmi/id/##' "
     "| grep -viE 'uevid|modalias'; echo; echo '# dmidecode (root)'; "
     "dmidecode -t 0 -t 1 -t 2 -t 3 -t 11 2>/dev/null || echo '(dmidecode needs root)'"),
    ("kernel.txt", "uname -a; echo; echo '# cmdline'; cat /proc/cmdline; echo; "
     "echo '# os-release'; cat /etc/os-release; echo; echo '# virt'; systemd-detect-virt 2>/dev/null"),
    ("cpu.txt", "lscpu 2>/dev/null; echo; echo '# /proc/cpuinfo (cpu0)'; "
     "awk '/^$/{exit} {print}' /proc/cpuinfo; echo; echo '# amd_pstate'; "
     "grep -rH . /sys/devices/system/cpu/amd_pstate/ 2>/dev/null; echo; "
     "echo '# ryzenadj -i'; ryzenadj -i 2>/dev/null || echo '(ryzenadj not installed / not AMD)'"),
    ("lspci.txt", "lspci -nnvvv 2>/dev/null || lspci -nnk 2>/dev/null || echo '(lspci missing)'"),
    ("lsusb.txt", "lsusb -t 2>/dev/null; echo; lsusb 2>/dev/null; echo '=== verbose ==='; "
     "lsusb -v 2>/dev/null"),
    ("modules.txt", "lsmod; echo; for m in dell_laptop dell_wmi dell_smbios dell_smm_hwmon "
     "alienware_wmi hid_generic i8k sparse_keymap; do echo \"=== modinfo $m ===\"; "
     "modinfo $m 2>/dev/null | grep -E '^(filename|description|parm|alias):'; done"),
    ("input-devices.txt", "cat /proc/bus/input/devices"),
    ("key-capabilities.txt", _decode_key_caps),
    ("evdev-udev.txt", "for e in /dev/input/event*; do echo \"=== $e ===\"; "
     "udevadm info -q all -n $e 2>/dev/null; echo; done"),
    ("hwmon.txt", "for h in /sys/class/hwmon/hwmon*; do echo \"### $h  name=$(cat $h/name 2>/dev/null)\"; "
     "for f in $h/*; do [ -f \"$f\" ] || continue; printf '  %-26s %s\\n' \"$(basename $f)\" "
     "\"$(head -c 160 \"$f\" 2>/dev/null | tr -d '\\n')\"; done; echo; done"),
    ("thermal-power.txt", "echo '# platform_profile'; for f in /sys/firmware/acpi/platform_profile*; do "
     "echo \"$f = $(cat $f 2>/dev/null)\"; done; echo; echo '# powercap'; "
     "grep -rH . /sys/class/powercap/*/name /sys/class/powercap/*/*_range_uj 2>/dev/null; echo; "
     "echo '# power-profiles-daemon'; powerprofilesctl 2>/dev/null; echo; "
     "echo '# lm_sensors'; sensors 2>/dev/null; echo; sensors -j 2>/dev/null"),
    ("battery.txt", "echo '# power_supply sysfs'; "
     "grep -rH . /sys/class/power_supply/*/ 2>/dev/null | grep -viE 'uevent|modalias'; echo; "
     "echo '# upower'; timeout 4 upower -d 2>/dev/null; echo; "
     "echo '# smbios-battery-ctl'; timeout 4 smbios-battery-ctl --get-charging-cfg 2>/dev/null "
     "|| echo '(libsmbios not installed / not a Dell)'"),
    ("vendor-platform.txt", "echo '# /sys/devices/platform vendor interfaces'; "
     "for d in /sys/devices/platform/*wmi* /sys/devices/platform/*-laptop /sys/devices/platform/*_laptop "
     "/sys/devices/platform/alienware-wmi* /sys/devices/platform/dell-laptop; do "
     "[ -d \"$d\" ] || continue; echo \"### $d\"; grep -rH . \"$d\" 2>/dev/null "
     "| grep -viE 'uevent|modalias|power/' | head -80; echo; done; "
     "echo '# /sys/class/leds'; for l in /sys/class/leds/*; do echo \"$(basename $l): "
     "brightness=$(cat $l/brightness 2>/dev/null) max=$(cat $l/max_brightness 2>/dev/null)\"; done; echo; "
     "echo '# module parameters'; for m in dell_laptop dell_smm_hwmon alienware_wmi asus_nb_wmi "
     "hp_wmi ideapad_laptop thinkpad_acpi; do [ -d /sys/module/$m/parameters ] || continue; "
     "echo \"=== $m ===\"; grep -rH . /sys/module/$m/parameters/ 2>/dev/null; done"),
    ("firmware.txt", "echo '# fwupd devices'; timeout 6 fwupdmgr get-devices "
     "--no-authenticate-modules 2>/dev/null || timeout 6 fwupdmgr get-devices 2>/dev/null "
     "|| echo '(fwupd not installed / timed out reaching the daemon)'"),
    ("acpi.txt", "ls -l /sys/firmware/acpi/tables/ 2>/dev/null; echo; "
     "command -v acpidump >/dev/null && echo 'acpidump present — run: sudo acpidump -b (attach the DSDT.dat)'; "
     "command -v acpi_listen >/dev/null && echo 'acpi_listen present — run it and press Fn/media keys to capture ACPI events'"),
    ("dsdt.b64", "echo '# base64 of the ACPI DSDT + SSDTs — decode with:  base64 -d dsdt.b64 > acpi.bin ; "
     "iasl -d acpi.bin'; for t in /sys/firmware/acpi/tables/DSDT /sys/firmware/acpi/tables/SSDT*; do "
     "[ -r \"$t\" ] || continue; echo \"=== $(basename $t) ===\"; base64 \"$t\" 2>/dev/null; echo; done "
     "|| echo '(ACPI tables need root to read)'"),
    ("display.txt", _collect_display_txt),
    ("drm-gpu.txt", "for c in /sys/class/drm/card[0-9]*; do echo \"### $c\"; "
     "cat $c/device/uevent 2>/dev/null; echo \" runtime_status=$(cat $c/device/power/runtime_status 2>/dev/null)\"; "
     "echo; done; echo '=== nvidia-smi -q ==='; timeout 8 nvidia-smi -q 2>/dev/null; echo; "
     "echo '=== nvidia-smi -q -d SUPPORTED_CLOCKS ==='; "
     "timeout 8 nvidia-smi -q -d SUPPORTED_CLOCKS 2>/dev/null | head -50; echo; "
     "echo '=== glxinfo / vulkaninfo ==='; "
     "timeout 4 glxinfo -B 2>/dev/null | grep -E 'renderer|OpenGL version|Device'; "
     "timeout 4 vulkaninfo --summary 2>/dev/null | head -40"),
    ("openrgb.txt", "openrgb --version 2>/dev/null; echo; "
     "openrgb --noautoconnect -l --verbose 2>/dev/null | grep -vE 'i2c|SMBus|help.openrgb' "
     "|| openrgb --noautoconnect -l 2>/dev/null"),
    ("smbios-tokens.txt", "timeout 6 smbios-token-ctl 2>/dev/null | head -400 "
     "|| echo '(libsmbios / smbios-token-ctl not installed — needed for Dell battery / USB / thermal tokens)'"),
    ("dmesg-full.txt", "dmesg 2>/dev/null || echo '(dmesg needs root / kernel.dmesg_restrict=1)'"),
    ("journal-boot-tail.txt", "journalctl -b --no-pager 2>/dev/null | tail -3000 || echo '(journalctl unavailable)'"),
]


def collect_hw_bundle(dest_dir: str | None = None) -> str:
    """Write a folder of raw hardware dumps (+ the human report + a README) and
    tar it. Return the .tar.gz path. Everything needed to add a new laptop
    model to config/*.json and the sysfs paths — attach it to a
    'new hardware support' issue."""
    prod = run_cmd3("cat /sys/class/dmi/id/product_name 2>/dev/null")[2].strip() or "unknown"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", prod).strip("-").lower() or "laptop"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dirname = f"tuxthrottle-hwdump-{slug}-{stamp}"

    try:
        home = pwd.getpwnam(resolve_real_user()).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    dest_dir = dest_dir or home
    work = os.path.join(dest_dir, dirname)
    os.makedirs(work, exist_ok=True)

    for fname, cmd in _HW_BUNDLE_FILES:
        try:
            data = cmd() if callable(cmd) else run_cmd3(
                f"timeout -k 2 25 bash -lc {shlex.quote(cmd)}", timeout=30)[2]
        except Exception as exc:  # noqa: BLE001
            data = f"(error: {exc})"
        with open(os.path.join(work, fname), "w") as fh:
            fh.write((data or "(no output)").rstrip() + "\n")

    with open(os.path.join(work, "report.md"), "w") as fh:
        fh.write(collect_debug_report())
    with open(os.path.join(work, "README-attach-this.txt"), "w") as fh:
        fh.write(
            "TuxThrottle — hardware dump bundle\n"
            f"machine: {prod}   collected: {stamp}   euid={os.geteuid()}\n\n"
            "WHAT THIS IS\n"
            "  Raw sysfs / DMI / evdev / hwmon / PCI / OpenRGB / ACPI dumps + the\n"
            "  readable debug report, plus model-scaffold.json — an auto-generated\n"
            "  starting point for models/<slug>.json (probed fields filled, the\n"
            "  rest left under \"_todo\"). Together these are enough to add support\n"
            "  for this laptop: DMI strings to gate on, hwmon fan/pwm paths, the\n"
            "  platform_profile path + choices, evdev key codes for the Fn/media/\n"
            "  vendor keys, OpenRGB controller layout, battery method, GPU PCI ids,\n"
            "  panel modes, Dell/ASUS/Lenovo firmware tokens, and the decompilable\n"
            "  DSDT for reverse-engineering vendor WMI.\n\n"
            "HOW TO USE\n"
            "  1. Skim the files for anything private (hostname, serials in dmi.txt /\n"
            "     lsusb.txt / nvidia-smi). Redact if you care.\n"
            "  2. Open a 'new hardware support' issue and ATTACH this whole .tar.gz\n"
            "     (drag it onto the GitHub comment box).\n"
            "  3. Run this collector as root (sudo) if you can — dmidecode, the DSDT\n"
            "     and smbios-token-ctl need it. Re-run and re-attach if the first was\n"
            "     unprivileged.\n"
            "  4. If a Fn/media key doesn't work: run  sudo evtest  , pick the\n"
            "     keyboard / hotkey device, press the key, and paste those lines too.\n\n"
            "NEXT (maintainer): decode dsdt.b64 with  base64 -d dsdt.b64 > acpi.bin ;\n"
            "  iasl -d acpi.bin   ; finish model-scaffold.json's _todo fields; add\n"
            "  \"models\": [<slug>] gates to config/*.json entries that differ.\n\n"
            "FILES\n" + "".join(f"  {n}\n" for n, _ in _HW_BUNDLE_FILES) +
            "  report.md\n")

    tgz = os.path.join(dest_dir, dirname + ".tar.gz")
    run_cmd3(f"tar czf {shlex.quote(tgz)} -C {shlex.quote(dest_dir)} {shlex.quote(dirname)}",
             timeout=60)
    run_cmd3(f"rm -rf {shlex.quote(work)}", timeout=10)
    if os.geteuid() == 0:
        try:
            pw = pwd.getpwnam(resolve_real_user())
            os.chown(tgz, pw.pw_uid, pw.pw_gid)
        except (KeyError, OSError):
            pass
    return tgz


def cli_collect() -> int:
    """`--collect [dir]`: write the hardware dump bundle .tar.gz."""
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dest = args[0] if args else None
    try:
        path = collect_hw_bundle(dest)
        print(f"hardware bundle written:\n  {path}\nAttach it to a "
              f"'new hardware support' issue.")
        if os.geteuid() != 0:
            print("note: run with sudo for the full DSDT / dmesg / privileged dumps.",
                  file=sys.stderr)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"collect failed: {exc}", file=sys.stderr)
        return 1


def cli_report() -> int:
    """`--report`: print the status table, no GUI."""
    items = _load_all_items()
    ledger = ledger_load()
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(lambda it: evaluate_item(it, ledger), items))
    print(format_status_report(items))
    if os.geteuid() != 0:
        print("note: not running as root — privileged checks may read as "
              "'Not applied'/'Check error'. Re-run with sudo for accuracy.")
    return 0


def cli_debug() -> int:
    """`--debug`: print the full hardware/OS/toolkit debug report."""
    print(collect_debug_report())
    if os.geteuid() != 0:
        print("\nnote: run with sudo for dmesg / RAPL / privileged checks.", file=sys.stderr)
    return 0


def main():
    if "--report" in sys.argv:
        raise SystemExit(cli_report())
    if "--debug" in sys.argv or "--diag" in sys.argv:
        raise SystemExit(cli_debug())
    if "--collect" in sys.argv or "--hw-bundle" in sys.argv:
        raise SystemExit(cli_collect())
    self_elevate()
    root = tb.Window(themename=THEME)
    ToolkitApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
