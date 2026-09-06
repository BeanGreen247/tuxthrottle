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
import csv
import os
import pwd
import queue
import shutil
import site
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import messagebox

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
ASSETS_DIR = BASE_DIR / "assets"

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
import tuxthrottle_profiles  # noqa: E402  (stdlib, imports sensors)
import tuxthrottle_watchdog  # noqa: E402  (stdlib, confirm-or-auto-revert timer)
from tuxthrottle_diag import (  # noqa: E402  (report builders — extracted)
    collect_debug_report,
    collect_hw_bundle,
)
from tuxthrottle_gui_widgets import (  # noqa: E402  (standalone widgets/theme — extracted)
    ACCENT_FALLBACK,
    BIOS_PANEL,
    SidebarNav,
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
from tuxthrottle_tab_about import AboutTabMixin  # noqa: E402
from tuxthrottle_tab_category import CategoryTabMixin  # noqa: E402
from tuxthrottle_tab_dashboard import DashboardTabMixin  # noqa: E402
from tuxthrottle_tab_diagnostics import DiagnosticsTabMixin  # noqa: E402
from tuxthrottle_tab_fans import FanTabMixin  # noqa: E402
from tuxthrottle_tab_games import GamesTabMixin  # noqa: E402
from tuxthrottle_tab_keyboard import KeyboardTabMixin  # noqa: E402
from tuxthrottle_tab_power_display import PowerDisplayTabMixin  # noqa: E402
from tuxthrottle_tab_profiles import ProfilesTabMixin  # noqa: E402
from tuxthrottle_tab_updates import UpdatesTabMixin  # noqa: E402
from tuxthrottle_tab_vram import VramTabMixin  # noqa: E402

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


class ToolkitApp(KeyboardTabMixin, FanTabMixin, VramTabMixin, ProfilesTabMixin,
                 UpdatesTabMixin, AboutTabMixin, DashboardTabMixin, PowerDisplayTabMixin,
                 CategoryTabMixin, GamesTabMixin, DiagnosticsTabMixin):
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
        # session-only GPU clock offset must not outlive the GUI
        if getattr(self, "_gpuoff_core", None) is not None and (
                self._gpuoff_core.get() or self._gpuoff_mem.get()):
            try:
                sensors.set_nvidia_clock_offset(0, 0)
            except Exception:  # noqa: BLE001
                pass
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

        # Dashboard is the landing page — build it eagerly. Every other tab is
        # registered as a lazy page: its widgets are constructed the first time
        # its nav entry is clicked (SidebarNav.add_lazy), which is what keeps
        # cold start off the ~2 s all-22-tabs build.
        self._build_dashboard_tab()
        self.notebook.add_lazy("Keyboard", self._build_keyboard_tab)
        self.notebook.add_lazy("Touchpad", self._build_touchpad_tab)
        self.notebook.add_lazy("Fans", self._build_fan_tab)
        self.notebook.add_lazy("Power & Limits", self._build_power_tab)
        self.notebook.add_lazy("Display", self._build_display_tab)
        self.notebook.add_lazy("Battery", self._build_battery_health_tab)
        self.notebook.add_lazy("VRAM", self._build_vram_tab)
        self.notebook.add_lazy("Profiles", self._build_profiles_tab)
        self.notebook.add_lazy("Presets", self._build_presets_tab)
        self.notebook.add_lazy("Updates", self._build_updates_tab)
        if self.games:
            self.notebook.add_lazy("Setup Games", self._build_games_tab)
        self.notebook.add_lazy("Game Tools", self._build_gametools_tab)

        categories = sorted(
            {item.category for item in self.items.values() if not item.hidden},
            key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99,
        )
        for cat in categories:
            self.notebook.add_lazy(cat, lambda f, c=cat: self._build_category_tab(f, c))

        # last, pinned to the foot of the rail (always visible, below the
        # scrollable list): About, then the "gather logs for a GitHub issue" page
        self.notebook.add_lazy("About", self._build_about_tab, pin=True)
        self.notebook.add_lazy("Report a Bug", self._build_diagnostics_tab,
                               kind="support", spacer=True)

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
        self._btn_apply = btn_apply   # relabelled with the pending count (LACT-style)
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
            self._refresh_pending_bar()
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

    def _pending_ids(self) -> list[str]:
        """Item ids whose tick disagrees with their applied state — the count
        the footer's Apply button shows. Only built category tabs have an
        `item.var`, which is exactly the set the user could have toggled."""
        out = []
        for item in self.items.values():
            var = getattr(item, "var", None)
            if var is None or item.hidden or not item.hw_supported:
                continue
            try:
                if bool(var.get()) != bool(item.done):
                    out.append(item.id)
            except tk.TclError:
                pass
        return out

    def _refresh_pending_bar(self):
        btn = getattr(self, "_btn_apply", None)
        if btn is None:
            return
        n = len(self._pending_ids())
        try:
            btn.configure(text=f"✓  Apply Selected ({n})" if n else "✓  Apply Selected")
        except tk.TclError:
            pass

    def _recompute_status_summary(self):
        n_done = n_total = n_attention = 0
        for item in self.items.values():
            # count by item substance, not widget presence — category tabs are
            # built lazily now, so status_label is often still None here
            if item.hidden or not item.hw_supported or item.state == "unknown":
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
        self._refresh_pending_bar()

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

    # per-tab live polling: only the tab currently on screen polls hardware.
    # {nav label: (live-flag attr, poll method name)}. The poll loops gate
    # their body on the flag and carry a generation token so re-entering a
    # tab can't stack duplicate loops. Dashboard keeps its own enter/leave.
    _TAB_LIVE = {
        "Fans": ("_fan_live", "_fan_poll"),
        "Power & Limits": ("_power_live", "_power_poll"),
        "Battery": ("_bath_live_on", "_bath_poll"),
        "VRAM": ("_vram_live", "_vram_poll"),
    }

    def _on_nav_page(self, page_text: str):
        """Start/stop the visible tab's live polling, and show the 'Apply
        section recommendations' button only on a category page that still has
        unapplied dev picks."""
        want_dash = (page_text == "Dashboard")
        if want_dash and not self._dash_shown:
            self._dash_shown = True
            self._dash_enter()
        elif not want_dash and self._dash_shown:
            self._dash_shown = False
            self._dash_leave()

        for label, (flag, poll) in self._TAB_LIVE.items():
            on = (page_text == label)
            if on and not getattr(self, flag, False):
                setattr(self, flag, True)
                getattr(self, poll)()          # fresh token → starts one loop
            elif not on and getattr(self, flag, False):
                setattr(self, flag, False)     # loop dies on its next tick

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

    @staticmethod
    def _fmt_snapshot_delta(before: dict, after: dict) -> list[str]:
        """Human 'X → Y (Δ)' lines for the fields that moved between two
        sensors.snapshot_light() readings."""
        rows = [
            ("CPU temp", "cpu_temp_c", "°C", 0),
            ("CPU clock", "cpu_freq_ghz", " GHz", 2),
            ("STAPM", "stapm_w", " W", 0),
            ("dGPU temp", "dgpu_temp_c", "°C", 0),
            ("dGPU clock", "dgpu_clock_mhz", " MHz", 0),
            ("dGPU power", "dgpu_power_w", " W", 0),
        ]
        out = []
        for label, key, unit, dp in rows:
            b, a = before.get(key), after.get(key)
            if b is None or a is None:
                continue
            try:
                d = float(a) - float(b)
            except (TypeError, ValueError):
                continue
            if abs(d) < (0.05 if dp else 1):
                continue
            sign = "+" if d >= 0 else "−"
            out.append(f"{label} {float(b):.{dp}f}{unit} → {float(a):.{dp}f}{unit} "
                       f"({sign}{abs(d):.{dp}f}{unit.strip()})")
        fb = [r for r in (before.get("fan_rpm") or []) if r]
        fa = [r for r in (after.get("fan_rpm") or []) if r]
        if fb and fa and abs(sum(fa) / len(fa) - sum(fb) / len(fb)) >= 100:
            out.append(f"fans ~{sum(fb) // len(fb)} → ~{sum(fa) // len(fa)} rpm avg")
        return out or ["no significant sensor change"]

    def _preset_delta_watch(self, before: dict, preset_id: str) -> None:
        time.sleep(30)
        after = sensors.snapshot_light()
        lines = self._fmt_snapshot_delta(before, after)
        self._last_preset_delta = (preset_id or "preset", lines, time.time())
        self._log("[Preset delta, 30 s after apply] " + "  ·  ".join(lines))
        if getattr(self, "_preset_delta_lbl", None) is not None:
            try:
                self._preset_delta_lbl.configure(
                    text=f"{preset_id or 'preset'} — " + "   ·   ".join(lines))
            except tk.TclError:
                pass

    def _preset_worker(self, item_ids: list[str], preset_id: str = ""):
        before = sensors.snapshot_light()
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
        threading.Thread(target=self._preset_delta_watch,
                         args=(before, preset_id), daemon=True).start()


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
