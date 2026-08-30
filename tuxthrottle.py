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
import configparser
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
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
ASSETS_DIR = BASE_DIR / "assets"
PROJECT_URL = "https://github.com/BeanGreen247/tuxthrottle"
PROJECT_ISSUES_URL = PROJECT_URL + "/issues"
sys.path.insert(0, str(BASE_DIR))

try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import SUCCESS, SECONDARY, WARNING, INFO, DANGER
except ImportError:
    print("ttkbootstrap not found. Install with: pip install --user ttkbootstrap")
    print("(not packaged in Fedora/Nobara's repos — pip is the only path)")
    sys.exit(1)

import sensors  # noqa: E402  (local module, no GUI deps)

try:
    import tuxthrottle_kbd  # noqa: E402  (AW-ELC RGB keyboard, stdlib-only)
except Exception:  # noqa: BLE001
    tuxthrottle_kbd = None

import tuxthrottle_profiles  # noqa: E402  (stdlib, imports sensors)

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


def resolve_real_user() -> str:
    for var in ("PKEXEC_UID", "SUDO_UID"):
        val = os.environ.get(var)
        if val:
            try:
                return pwd.getpwuid(int(val)).pw_name
            except (KeyError, ValueError):
                pass
    for var in ("SUDO_USER", "PKEXEC_USER"):
        val = os.environ.get(var)
        if val:
            return val
    return pwd.getpwuid(os.getuid()).pw_name


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


def load_json(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
#  Look & feel — a "gaming BIOS" dark shell with the KDE accent colour.
# --------------------------------------------------------------------------- #

ACCENT_FALLBACK = "#3daee9"   # Breeze blue, if the desktop accent can't be read
# One flat surface for every widget background (frames, labels, labelframes,
# scales, nav rail) — a per-widget mismatch here is what showed up as "black
# boxes" behind labels/sliders. BIOS_SUNKEN is only used for scale/progress
# troughs and the window ground behind everything.
BIOS_PANEL = "#141a21"        # the surface — all widget backgrounds
BIOS_SUNKEN = "#0b0e12"       # troughs / window ground (darker, so troughs read)
BIOS_BG = BIOS_SUNKEN         # back-compat alias (busy overlay etc.)
BIOS_PANEL_HI = "#212c38"     # hover / selected nav row / disclosure headers
BIOS_FG = "#e9eff5"
BIOS_MUTED = "#b3bfcb"        # secondary text — >= 7:1 on every panel surface
BIOS_BORDER = "#3c4a5b"       # card / labelframe hairline — clearly visible, not invisible
BIOS_BORDER_HI = "#5a6b7e"    # stronger edge for the active / hovered card
BIOS_CARD = "#1b2531"         # a subtle lift above BIOS_PANEL for raised panels / rows
CHART_AXIS = "#8b98a8"        # sparkline axis / point labels — readable, not invisible
# semantic status colours, re-picked so each clears ~6:1 on the dark panels
# (darkly's defaults — esp. danger/info — drop below AA on the card / hover bg)
SEM_SUCCESS = "#3ddc97"
SEM_DANGER = "#ff7b70"
SEM_WARNING = "#f5b041"
SEM_INFO = "#57c4f2"
SEM_SECONDARY = "#c3ccd6"
HELP_AMBER = "#e8a33d"         # the "support / bug report" accent (warm, != KDE accent)
HELP_BANNER_BG = "#2a2314"     # dark amber tint behind the bug-report banner


def _rgb_str_to_hex(s: str) -> str | None:
    nums = [int(n) for n in re.findall(r"\d+", s)][:3]
    return "#%02x%02x%02x" % tuple(nums) if len(nums) == 3 else None


def read_desktop_accent(default: str = ACCENT_FALLBACK) -> str:
    """Best-effort read of the Plasma accent colour from ~/.config/kdeglobals
    (of the *invoking* user, since we run elevated). Falls back to a preset."""
    user = resolve_real_user()
    try:
        home = pwd.getpwnam(user).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read(os.path.join(home, ".config", "kdeglobals"))
    except (configparser.Error, OSError):
        return default
    for sec, key in (("General", "AccentColor"),
                     ("Colors:Selection", "DecorationFocus"),
                     ("Colors:Selection", "BackgroundNormal")):
        if cp.has_option(sec, key):
            hexv = _rgb_str_to_hex(cp.get(sec, key))
            if hexv:
                return hexv
    return default


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _rel_luminance(hex_c: str) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(int(hex_c[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg: str, bg: str) -> float:
    a, b = _rel_luminance(fg), _rel_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def readable_on(fg: str, bg: str, target: float = 4.5) -> str:
    """Return `fg`, or a lightened/darkened version of it, so it clears the
    WCAG contrast `target` against `bg`. Used so a dark desktop accent colour
    doesn't become unreadable text on the dark panels."""
    if _contrast_ratio(fg, bg) >= target:
        return fg
    toward = "#ffffff" if _rel_luminance(bg) < 0.4 else "#000000"
    cand = fg
    for i in range(1, 21):
        cand = _mix(fg, toward, i / 20.0)
        if _contrast_ratio(cand, bg) >= target:
            return cand
    return cand


def apply_bios_style(style: "tb.Style", accent: str) -> None:
    """Re-skin the ttkbootstrap 'darkly' base into the BIOS look. All wrapped
    defensively — a theming quirk must never take the app down."""
    # accent as *text* on the dark panels must stay legible whatever the
    # desktop accent is
    # headings/accents: aim past AA (>=6:1) so they read easily even when the
    # user's desktop accent is dark
    accent_txt = readable_on(accent, BIOS_PANEL, 6.0)
    accent_txt_hi = readable_on(accent, BIOS_PANEL_HI, 6.0)
    try:
        c = style.colors
        c.primary = accent
        c.info = accent
        c.selectbg = accent
        c.bg = BIOS_PANEL
        c.fg = BIOS_FG
        c.dark = BIOS_PANEL
        c.light = BIOS_PANEL
        c.border = BIOS_BORDER
        c.active = BIOS_PANEL_HI
        c.inputbg = BIOS_SUNKEN
        # re-pick the semantic colours so status text/outlines clear AA on the
        # darker card / hover surfaces, not just on the base panel
        c.secondary = SEM_SECONDARY
        c.success = SEM_SUCCESS
        c.danger = SEM_DANGER
        c.warning = SEM_WARNING
    except Exception:  # noqa: BLE001
        pass

    specs = {
        ".": {"background": BIOS_PANEL, "foreground": BIOS_FG,
              "fieldbackground": BIOS_SUNKEN, "troughcolor": BIOS_SUNKEN,
              "bordercolor": BIOS_BORDER, "lightcolor": BIOS_PANEL,
              "darkcolor": BIOS_PANEL},
        "TFrame": {"background": BIOS_PANEL},
        "TLabel": {"background": BIOS_PANEL, "foreground": BIOS_FG},
        "TLabelframe": {"background": BIOS_PANEL, "bordercolor": BIOS_BORDER,
                        "darkcolor": BIOS_BORDER, "lightcolor": BIOS_BORDER,
                        "relief": "solid", "borderwidth": 1},
        "TLabelframe.Label": {"background": BIOS_PANEL, "foreground": accent_txt,
                              "font": ("Sans", 10, "bold")},
        # a visible click-to-expand header bar (About "What's inside", etc.)
        "Disclosure.TButton": {"background": BIOS_PANEL_HI, "foreground": accent_txt_hi,
                               "bordercolor": BIOS_BORDER_HI, "focuscolor": "",
                               "font": ("Sans", 10, "bold"), "anchor": "w",
                               "relief": "solid", "borderwidth": 1,
                               "padding": (12, 9)},
        # a slightly raised surface for panels / rows that should stand off the page
        "Card.TFrame": {"background": BIOS_CARD, "bordercolor": BIOS_BORDER,
                        "darkcolor": BIOS_BORDER, "lightcolor": BIOS_BORDER,
                        "relief": "solid", "borderwidth": 1},
        "CardRow.TFrame": {"background": BIOS_CARD, "borderwidth": 0, "relief": "flat"},
        "Card.TLabel": {"background": BIOS_CARD, "foreground": BIOS_FG},
        "CardKey.TLabel": {"background": BIOS_CARD, "foreground": accent_txt_hi,
                           "font": ("Sans", 10, "bold")},
        "TCheckbutton": {"background": BIOS_PANEL, "foreground": BIOS_FG},
        "TRadiobutton": {"background": BIOS_PANEL, "foreground": BIOS_FG},
        "TSeparator": {"background": BIOS_BORDER},
        "Nav.TFrame": {"background": BIOS_PANEL},
        "Header.TLabel": {"background": BIOS_PANEL, "foreground": BIOS_FG,
                          "font": ("Sans", 18, "bold")},
        "Horizontal.TProgressbar": {"background": accent, "troughcolor": BIOS_SUNKEN,
                                    "bordercolor": BIOS_SUNKEN, "lightcolor": accent,
                                    "darkcolor": accent},
        "Horizontal.TScale": {"background": BIOS_PANEL, "troughcolor": BIOS_SUNKEN},
        "TScale": {"background": BIOS_PANEL, "troughcolor": BIOS_SUNKEN},
        "Nav.TButton": {"background": BIOS_PANEL, "foreground": BIOS_MUTED,
                        "bordercolor": BIOS_PANEL, "focuscolor": "",
                        "font": ("Sans", 10, "bold"), "anchor": "w",
                        "padding": (16, 11), "relief": "flat"},
        "NavActive.TButton": {"background": BIOS_PANEL_HI, "foreground": accent_txt_hi,
                              "bordercolor": accent, "focuscolor": "",
                              "font": ("Sans", 10, "bold"), "anchor": "w",
                              "padding": (16, 11), "relief": "flat"},
        # the odd one out: the Bug Report / Logs page — warm amber, not the
        # KDE accent, so it reads as "support / external", not a hardware tab
        "NavSupport.TButton": {"background": BIOS_PANEL,
                               "foreground": readable_on(HELP_AMBER, BIOS_PANEL, 6.0),
                               "bordercolor": BIOS_PANEL, "focuscolor": "",
                               "font": ("Sans", 10, "bold"), "anchor": "w",
                               "padding": (16, 11), "relief": "flat"},
        "NavSupportActive.TButton": {"background": BIOS_PANEL_HI,
                                     "foreground": readable_on(HELP_AMBER, BIOS_PANEL_HI, 6.0),
                                     "bordercolor": HELP_AMBER, "focuscolor": "",
                                     "font": ("Sans", 10, "bold"), "anchor": "w",
                                     "padding": (16, 11), "relief": "flat"},
        # top tab-strip used inside the Setup Games page (a real tb.Notebook,
        # unlike the left rail which is SidebarNav)
        "TNotebook": {"background": BIOS_PANEL, "bordercolor": BIOS_BORDER,
                      "darkcolor": BIOS_PANEL, "lightcolor": BIOS_PANEL,
                      "tabmargins": (2, 4, 2, 0)},
        "TNotebook.Tab": {"background": BIOS_PANEL, "foreground": BIOS_MUTED,
                          "bordercolor": BIOS_BORDER, "focuscolor": "",
                          "font": ("Sans", 10, "bold"), "padding": (16, 8)},
        "SupportBanner.TFrame": {"background": HELP_BANNER_BG},
        "SupportBanner.TLabel": {"background": HELP_BANNER_BG,
                                 "foreground": readable_on(HELP_AMBER, HELP_BANNER_BG, 6.0),
                                 "font": ("Sans", 10, "bold")},
    }
    for name, opts in specs.items():
        try:
            style.configure(name, **opts)
        except Exception:  # noqa: BLE001
            pass
    hover = readable_on(_mix(accent, "#ffffff", 0.22), BIOS_PANEL_HI, 5.0)
    amber_hover = readable_on(_mix(HELP_AMBER, "#ffffff", 0.22), BIOS_PANEL_HI, 5.0)
    for name in ("NavSupport.TButton", "NavSupportActive.TButton"):
        try:
            style.map(name, background=[("active", BIOS_PANEL_HI)],
                      foreground=[("active", amber_hover)])
        except Exception:  # noqa: BLE001
            pass
    for name in ("Nav.TButton", "NavActive.TButton"):
        try:
            style.map(name, background=[("active", BIOS_PANEL_HI)],
                      foreground=[("active", hover)])
        except Exception:  # noqa: BLE001
            pass
    try:
        style.map("Disclosure.TButton",
                  background=[("active", _mix(BIOS_PANEL_HI, "#ffffff", 0.06))],
                  bordercolor=[("active", accent)],
                  foreground=[("active", hover)])
    except Exception:  # noqa: BLE001
        pass
    try:
        style.map("TNotebook.Tab",
                  background=[("selected", BIOS_PANEL_HI), ("active", BIOS_PANEL_HI)],
                  foreground=[("selected", accent_txt_hi), ("active", hover)])
    except Exception:  # noqa: BLE001
        pass


class RingGauge(tk.Canvas):
    """A self-drawn 270° ring gauge — replaces ttkbootstrap's Meter, which
    doesn't re-colour cleanly under the custom theme and rendered thin/odd.
    `set(value)` redraws; `size` scales the whole thing."""

    def __init__(self, master, *, caption="", unit="", maximum=100.0,
                 color=None, size=150, fmt="{:.0f}"):
        super().__init__(master, width=size, height=size + 20,
                         bg=BIOS_PANEL, highlightthickness=0, bd=0)
        self._max = float(maximum) or 1.0
        self._color = color or ACCENT_FALLBACK
        self._size = size
        self._unit = unit
        self._caption = caption
        self._fmt = fmt
        self._ring = max(8, size // 11)       # ring thickness
        self._value = 0.0
        self._draw()

    def _draw(self):
        self.delete("all")
        s, w = self._size, self._ring
        pad = w // 2 + 3
        box = (pad, pad, s - pad, s - pad)
        frac = max(0.0, min(1.0, self._value / self._max))
        # track + value arc — 270° sweep with a symmetric gap at the bottom
        self.create_arc(*box, start=225, extent=-270, style="arc",
                        outline=BIOS_SUNKEN, width=w)
        if frac > 0.001:
            self.create_arc(*box, start=225, extent=-270 * frac, style="arc",
                            outline=self._color, width=w)
        self.create_text(s / 2, s / 2 - 4, text=self._fmt.format(self._value),
                         fill=BIOS_FG, font=("Sans", int(s * 0.19), "bold"))
        if self._unit:
            self.create_text(s / 2, s / 2 + int(s * 0.16), text=self._unit,
                             fill=BIOS_MUTED, font=("Sans", int(s * 0.09)))
        self.create_text(s / 2, s + 8, text=self._caption, fill=BIOS_MUTED,
                         font=("Sans", 9))

    def set(self, value):
        try:
            self._value = float(value or 0.0)
        except (TypeError, ValueError):
            self._value = 0.0
        self._draw()


class HistoryChart(tk.Canvas):
    """A rolling sparkline — `push(value)` appends and redraws. Keeps the last
    `samples` points; auto-scales Y with a small headroom. Used for the
    Dashboard history strip."""

    def __init__(self, master, *, caption="", unit="", samples=90, color=None,
                 height=64):
        super().__init__(master, height=height, bg="#0e1116",
                         highlightthickness=0, bd=0)
        self._buf = deque(maxlen=samples)
        self._color = color or ACCENT_FALLBACK
        self._caption = caption
        self._unit = unit
        self.bind("<Configure>", lambda _e: self._draw())

    def push(self, value):
        try:
            self._buf.append(float(value))
        except (TypeError, ValueError):
            self._buf.append(0.0)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width() or 300
        h = int(self["height"])
        pad = 4
        vals = list(self._buf)
        cap = self._caption + (f"  {vals[-1]:.0f}{self._unit}" if vals else "")
        self.create_text(6, 8, text=cap, anchor="w", fill=BIOS_MUTED,
                         font=("Sans", 8))
        if len(vals) < 2:
            return
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-6:
            lo, hi = lo - 1, hi + 1
        span = (hi - lo) * 1.15
        base = lo - (hi - lo) * 0.075
        n = len(vals)
        step = (w - 2 * pad) / max(1, self._buf.maxlen - 1)
        x0 = w - pad - (n - 1) * step
        pts = []
        for i, v in enumerate(vals):
            x = x0 + i * step
            y = h - pad - (v - base) / span * (h - 2 * pad - 10) - 2
            pts += [x, y]
        self.create_line(*pts, fill=self._color, width=1.6, smooth=False)
        self.create_line(pts[0], h - pad, *pts, pts[-2], h - pad,
                         fill=self._color, width=0, stipple="gray12")
        self.create_text(w - 6, h - 6, text=f"{lo:.0f}", anchor="se",
                         fill=CHART_AXIS, font=("Sans", 7))
        self.create_text(w - 6, 8, text=f"{hi:.0f}", anchor="ne",
                         fill=CHART_AXIS, font=("Sans", 7))


class SidebarNav(tb.Frame):
    """Minimal drop-in for tb.Notebook that renders a left nav rail + a single
    swapped content pane and a big page header (gaming-BIOS layout).

    Pages are still created as `tb.Frame(self)` and registered with
    `.add(frame, text=...)`; `.tabs()` / `.tab()` / `.select()` keep the few
    Notebook call-sites (and the smoke tests) working."""

    RAIL_WIDTH = 256

    def __init__(self, master):
        super().__init__(master)
        self.rail = tb.Frame(self, width=self.RAIL_WIDTH, style="Nav.TFrame")
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)
        tb.Separator(self, orient="vertical").pack(side="left", fill="y")

        # Pinned area at the foot of the rail — packed first (side=bottom) so it
        # always reserves its height; About + Report a Bug live here and stay
        # visible no matter how far the scrollable list above is scrolled.
        self._rail_bottom = tb.Frame(self.rail, style="Nav.TFrame")
        self._rail_bottom.pack(side="bottom", fill="x")
        self._rail_bottom_sep = None

        # Scrollable list of the normal nav buttons.
        self._nav_canvas = tk.Canvas(self.rail, bg=BIOS_PANEL, highlightthickness=0,
                                     bd=0, width=self.RAIL_WIDTH)
        self._nav_vsb = tb.Scrollbar(self.rail, orient="vertical",
                                     command=self._nav_canvas.yview)
        self._nav_canvas.configure(yscrollcommand=self._nav_vsb.set)
        self._nav_canvas.pack(side="left", fill="both", expand=True)
        self._nav_box = tb.Frame(self._nav_canvas, style="Nav.TFrame")
        self._nav_win = self._nav_canvas.create_window((0, 0), window=self._nav_box,
                                                       anchor="nw")
        self._nav_box.bind("<Configure>", lambda _e: self._nav_reflow())
        self._nav_canvas.bind("<Configure>", lambda e: (
            self._nav_canvas.itemconfigure(self._nav_win, width=e.width),
            self._nav_reflow()))

        right = tb.Frame(self)
        right.pack(side="left", fill="both", expand=True)
        header_row = tb.Frame(right)
        header_row.pack(fill="x")
        self._header = tb.Label(header_row, text="", style="Header.TLabel",
                                anchor="w", padding=(24, 18, 12, 14))
        self._header.pack(side="left")
        # right-hand slot for a per-page action (ToolkitApp drops the
        # "Apply section recommendations" button here) — kept well clear of the
        # title so it can't be fat-fingered instead of a nav click
        self._header_actions = tb.Frame(header_row)
        self._header_actions.pack(side="right", padx=(0, 20))
        tb.Separator(right).pack(fill="x")
        self._stack = tb.Frame(right)
        self._stack.pack(fill="both", expand=True)

        self.on_select = None       # ToolkitApp callback: fn(page_text)
        self._pages: list = []      # (text, frame, button)
        self._current = None

    def _nav_reflow(self):
        """Keep the scrollregion in sync and hide the scrollbar unless the
        button list actually overflows the rail."""
        self._nav_canvas.configure(scrollregion=self._nav_canvas.bbox("all"))
        need = self._nav_box.winfo_reqheight() > self._nav_canvas.winfo_height() + 1
        if need and not self._nav_vsb.winfo_ismapped():
            self._nav_vsb.pack(side="right", fill="y", before=self._nav_canvas)
        elif not need and self._nav_vsb.winfo_ismapped():
            self._nav_vsb.pack_forget()

    def add(self, frame, text: str = "", *, kind: str = "normal",
            spacer: bool = False, pin: bool = False):
        frame.master  # noqa: B018  (frame was created as tb.Frame(self); fine)
        pinned = pin or spacer or kind == "support"
        parent = self._rail_bottom if pinned else self._nav_box
        if pinned and self._rail_bottom_sep is None:
            self._rail_bottom_sep = tb.Separator(self._rail_bottom, orient="horizontal")
            self._rail_bottom_sep.pack(side="top", fill="x", padx=12, pady=(4, 2))
        base = "NavSupport.TButton" if kind == "support" else "Nav.TButton"
        btn = tb.Button(parent, text=text, style=base,
                        takefocus=False, command=lambda f=frame: self.select(f))
        btn.pack(side="top", fill="x", padx=0, pady=1)
        btn._nav_kind = kind  # noqa: SLF001
        self._pages.append((text, frame, btn))
        if self._current is None:
            self.select(frame)

    def select(self, frame=None):
        if frame is None:
            return self._current
        for text, f, b in self._pages:
            on = f is frame
            support = getattr(b, "_nav_kind", "normal") == "support"
            if support:
                sty = "NavSupportActive.TButton" if on else "NavSupport.TButton"
            else:
                sty = "NavActive.TButton" if on else "Nav.TButton"
            try:
                b.configure(style=sty)
            except tk.TclError:
                pass
            if on:
                f.pack(in_=self._stack, fill="both", expand=True)
                self._header.configure(text=text)
                self._reveal(b)
            else:
                f.pack_forget()
        self._current = frame
        if callable(self.on_select):
            try:
                self.on_select(self._header.cget("text"))
            except Exception:  # noqa: BLE001
                pass

    def _reveal(self, btn):
        """If the selected button lives in the scrollable list and is off-screen,
        scroll it into view."""
        if btn.master is not self._nav_box:
            return
        try:
            self._nav_canvas.update_idletasks()
            top = btn.winfo_y()
            bot = top + btn.winfo_height()
            view_h = self._nav_canvas.winfo_height()
            y0 = self._nav_canvas.canvasy(0)
            total = max(1, self._nav_box.winfo_reqheight())
            if top < y0:
                self._nav_canvas.yview_moveto(top / total)
            elif bot > y0 + view_h:
                self._nav_canvas.yview_moveto((bot - view_h) / total)
        except (tk.TclError, ZeroDivisionError):
            pass

    # ---- tb.Notebook compatibility ----
    def tabs(self):
        return [str(f) for _, f, _ in self._pages]

    def tab(self, ref, option="text"):
        if isinstance(ref, int):
            return self._pages[ref][0]
        for text, f, _ in self._pages:
            if f is ref or str(f) == str(ref):
                return text
        return ""


class Item:
    """A tweak or app entry, normalized for the GUI."""

    def __init__(self, item_id: str, data: dict, kind: str, user: str):
        self.id = item_id
        self.kind = kind  # "tweak" or "app"
        self.content = data.get("Content", item_id)
        self.description = data.get("Description", "")
        self.category = data.get("category", "Other")
        self.risk = data.get("risk", "safe")
        self.recommended = bool(data.get("recommended"))  # dev's curated pick
        self.requires_vendor = data.get("requires_vendor")  # "nvidia" | "amd" | None
        self.hw_supported = True  # set by ToolkitApp after GPU detection
        self.hidden = False       # set by _apply_vendor_gate for no-op-on-this-box items

        def sub(cmd: str) -> str:
            return cmd.replace("{USER}", user).replace("{TOOLKIT_DIR}", str(BASE_DIR))

        self.check_cmd = sub(data.get("check", ""))
        # Optional: a tweak whose real effect only lands after a reboot (kernel
        # cmdline) can declare `check_pending` — true once the change is staged
        # in the bootloader but not yet live. The UI shows "Pending reboot" and
        # Apply/Presets skip it, so the user doesn't re-select and re-run it.
        self.check_pending_cmd = sub(data.get("check_pending", ""))
        if kind == "tweak":
            self.apply_cmds = [sub(c) for c in data.get("apply", [])]
            self.undo_cmds = [sub(c) for c in data.get("undo", [])]
        else:
            manager = data.get("manager", "dnf")
            # An app counts as "already here" if ANY reasonable install of it is
            # present — not just the one this entry would use. Otherwise the row
            # shows "not installed" and Apply happily adds a second, colliding
            # copy (classic: dnf `steam` on top of the Flatpak). `provides` is a
            # list of extra shell probes OR-ed into the check; for a Flatpak
            # entry we also auto-detect a system *or* per-user install of its
            # app-id, and `binary` adds a `command -v` probe.
            alts: list[str] = [sub(p) for p in data.get("provides", [])]
            if data.get("binary"):
                alts.append(f"command -v {data['binary']} >/dev/null 2>&1")
            if manager == "flatpak":
                fid = data.get("package", item_id)
                alts.append(f"flatpak info {fid} >/dev/null 2>&1")
                alts.append(sub(f"sudo -u {{USER}} flatpak info {fid} >/dev/null 2>&1"))
            if alts:
                base = self.check_cmd.strip() or "false"
                self.check_cmd = " || ".join(f"({c})" for c in [base, *alts])
            if "install" in data:
                self.apply_cmds = [sub(c) for c in data["install"]]
            elif manager == "dnf":
                self.apply_cmds = [f"dnf install -y {data.get('package', item_id)}"]
            elif manager == "flatpak":
                pkg = data.get("package", item_id)
                self.apply_cmds = [
                    "dnf install -y flatpak",
                    "flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo",
                    f"flatpak install -y flathub {pkg}",
                ]
            else:
                self.apply_cmds = []
            self.undo_cmds = []
        # live status — set by _refresh_all_status. `state` is the single
        # source of truth; `applied`/`pending` are kept as derived bools so the
        # rest of the code doesn't change.
        #   applied      check exited 0
        #   not_applied  check exited non-zero, cleanly
        #   pending      check_pending exited 0 (staged, needs reboot)
        #   error        the check command couldn't run (not our no/yes answer)
        #   drifted      we applied it OK (per the ledger) but the check now fails
        #   failed       our last apply/undo of this item failed (per the ledger)
        self.state = "unknown"
        self.check_rc: int | None = None
        self.check_out = ""
        self.ledger: dict | None = None   # {"action","ok","ts","note"} or None
        self.var = None  # tk.BooleanVar, set when widget built
        self.status_label = None
        self.checkbutton = None

    @property
    def applied(self) -> bool:
        return self.state == "applied"

    @property
    def pending(self) -> bool:
        return self.state == "pending"

    @property
    def done(self) -> bool:
        """Already in the desired state — nothing to (re-)apply."""
        return self.state in ("applied", "pending")


def run_cmd(cmd: str) -> tuple[bool, str]:
    ok, _rc, out = run_cmd3(cmd)
    return ok, out


def run_cmd3(cmd: str, timeout: int = 1800) -> tuple[bool, int, str]:
    """(ok, returncode, combined-output). rc is -1 if the command itself
    couldn't be launched (used to tell "check says no" from "check broke")."""
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, r.returncode, out
    except Exception as exc:  # noqa: BLE001
        return False, -1, str(exc)


# --------------------------------------------------------------------------- #
#  Apply ledger — what the toolkit itself has applied/undone, and how it went.
#  The per-tweak `check` command is still the source of truth for the *current*
#  state; the ledger adds "...and we're the ones who set it" / "...and our last
#  attempt failed", which is how "Reverted" and "Apply failed" are told apart
#  from a plain "Not applied".
# --------------------------------------------------------------------------- #

def _ledger_path() -> Path:
    try:
        home = Path(pwd.getpwnam(resolve_real_user()).pw_dir)
    except (KeyError, Exception):  # noqa: BLE001
        home = Path.home()
    return home / ".config" / "tuxthrottle" / "state.json"


def ledger_load() -> dict:
    try:
        d = json.loads(_ledger_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ledger_record(item_id: str, action: str, ok: bool, note: str = "") -> None:
    p = _ledger_path()
    data = ledger_load()
    data[item_id] = {"action": action, "ok": bool(ok),
                     "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note[:200]}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, sort_keys=True))
        if os.geteuid() == 0:
            pw = pwd.getpwnam(resolve_real_user())
            os.chown(p, pw.pw_uid, pw.pw_gid)
            os.chown(p.parent, pw.pw_uid, pw.pw_gid)
    except (OSError, KeyError):
        pass


# state key → (short label, ttkbootstrap style). App items relabel
# applied/not_applied to installed/not installed at render time.
_STATE_UI = {
    "applied":     ("Applied", SUCCESS),
    "pending":     ("Pending reboot", INFO),
    "not_applied": ("Not applied", SECONDARY),
    "error":       ("Check error", WARNING),
    "drifted":     ("Reverted", WARNING),
    "failed":      ("Apply failed", DANGER),
    "unsupported": ("unsupported", SECONDARY),
    "unknown":     ("checking…", SECONDARY),
}


def format_status_report(items) -> str:
    """Plain-text table of every item's state + the check that decided it +
    the toolkit's last action. Used by the GUI dialog and `--report`."""
    rows = sorted(items, key=lambda i: (i.category, i.kind, i.content.lower()))
    out = [f"TuxThrottle — status report   {time.strftime('%Y-%m-%d %H:%M:%S')}",
           "=" * 100]
    cur = None
    counts: dict[str, int] = {}
    for it in rows:
        counts[it.state] = counts.get(it.state, 0) + 1
        if it.category != cur:
            cur = it.category
            out.append(f"\n[{cur}]")
        led = it.ledger
        led_s = (f"{led['action']} {'ok' if led['ok'] else 'FAILED'} {led['ts']}"
                 + (f" — {led['note']}" if led.get("note") else "")) if led else "—"
        rc = "" if it.check_rc in (None, 0) else f" (rc {it.check_rc})"
        out.append(f"  {it.state.upper():<12} {it.content[:44]:<44} "
                   f"check{rc}: {it.check_cmd[:60] or '(none)'}")
        if it.check_out and it.state in ("error", "drifted", "failed", "not_applied"):
            out.append(f"    ↳ check said: {it.check_out.splitlines()[-1][:88]}")
        if led:
            out.append(f"    ↳ toolkit:    {led_s}")
    out.append("\n" + "-" * 100)
    out.append("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "\n".join(out) + "\n"


def evaluate_item(item: "Item", ledger: dict) -> None:
    """Run `item`'s check(s), consult the ledger, and set item.state /
    check_rc / check_out / ledger."""
    item.check_rc, item.check_out = None, ""
    item.ledger = ledger.get(item.id)
    if not item.hw_supported:
        item.state = "unsupported"
        return
    if not item.check_cmd:
        item.state = "not_applied"
        return
    ok, rc, out = run_cmd3(item.check_cmd)
    item.check_rc, item.check_out = rc, out[:600]
    if ok:
        item.state = "applied"
        return
    led = item.ledger
    if rc in (-1, 127):
        item.state = "error"
    elif item.check_pending_cmd and run_cmd3(item.check_pending_cmd)[0]:
        item.state = "pending"
    elif led and led.get("action") == "apply" and led.get("ok"):
        item.state = "drifted"        # we applied it OK; something undid it
    elif led and not led.get("ok"):
        item.state = "failed"         # our last apply/undo of it errored
    else:
        item.state = "not_applied"


class ToolkitApp:
    def __init__(self, root: "tb.Window"):
        self.root = root
        self.user = resolve_real_user()
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
        threading.Thread(target=self._refresh_all_status, daemon=True).start()

        self.dash_running = True
        threading.Thread(target=self._dashboard_loop, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        tb.Label(titlebox,
                 text="Nobara Linux · Ryzen 7 5800H + RTX 3050 Ti — this board only",
                 font=("Sans", 9), bootstyle=SECONDARY).pack(anchor="w")
        tb.Label(header, text=f"elevated · {self.user}",
                 bootstyle=(SECONDARY, "inverse"), font=("Sans", 8, "bold"),
                 padding=(8, 3)).pack(side="right")

        # actual DMI identity of the machine this is running on
        m = sensors.detect_model()
        if m["is_target"]:
            txt = f"✓  Detected: {m['vendor']} {m['product']}" + (f" (board {m['board']})" if m['board'] else "") + \
                  f", BIOS {m['bios']} — matches the target platform."
            style = SUCCESS
        elif m["is_close"]:
            txt = (f"⚠  Detected: {m['vendor']} {m['product']} — a G15 5515 variant, but not the exact "
                   f"unit this was built against. Most things should work; some sysfs paths may differ.")
            style = WARNING
        else:
            txt = (f"⚠  Detected: {m['vendor']} {m['product']} — this is NOT a Dell G15 5515. "
                   f"The Toolkit's checks and tweaks are written for that board; expect breakage.")
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
        self._build_fan_tab()
        self._build_power_tab()
        self._build_profiles_tab()
        self._build_presets_tab()
        self._build_updates_tab()
        if self.games:
            self._build_games_tab()

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
        self.notebook.on_select = self._on_nav_page
        self._on_nav_page(self.notebook.tab(0))

        # ---- footer: actions + status ----
        tb.Separator(self.root).pack(fill="x", padx=16)
        btn_bar = tb.Frame(self.root, padding=(16, 10))
        btn_bar.pack(fill="x")
        btn_refresh = tb.Button(btn_bar, text="↻  Refresh Status", bootstyle=(INFO, "outline"),
                                command=self._on_refresh_click)
        btn_refresh.pack(side="left")
        btn_apply = tb.Button(btn_bar, text="✓  Apply Selected", bootstyle=SUCCESS,
                              command=self._on_apply_click)
        btn_apply.pack(side="left", padx=8)
        btn_report = tb.Button(btn_bar, text="≣  Status report", bootstyle=(SECONDARY, "outline"),
                               command=self._show_status_report)
        btn_report.pack(side="left")
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

    def _build_dashboard_tab(self):
        outer = tb.Frame(self.notebook)
        self.notebook.add(outer, text="Dashboard")
        frame = self._scroll_body(outer, pad=16)

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
        self._csv_logging = tk.BooleanVar(value=False)
        tb.Checkbutton(logrow, text="Log this session to CSV",
                       variable=self._csv_logging, bootstyle="round-toggle",
                       command=self._toggle_csv_log).pack(side="left")
        self._csv_path_lbl = tb.Label(logrow, text="", bootstyle=SECONDARY,
                                      font=("Monospace", 8))
        self._csv_path_lbl.pack(side="left", padx=10)
        self._csv_file = None
        self._csv_writer = None

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
        self.kbd_speed.set(tuxthrottle_kbd.load_meta().get("speed", 50))

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
        return {z: rgb for z in range(tuxthrottle_kbd.ZONE_COUNT)}

    def _kbd_apply_brightness(self):
        b = self.kbd_brightness.get()
        hx = self._safe_hex(self.kbd_all_hex.get())
        colors = self._kbd_all_colors()
        self._kbd_run(lambda kb: (kb.set_all(hx, b),
                                  tuxthrottle_kbd.save_state(colors, b, mode="zones")),
                      f"brightness {b}%")

    def _kbd_apply_all(self):
        hx = self._safe_hex(self.kbd_all_hex.get())
        b = self.kbd_brightness.get()
        colors = self._kbd_all_colors()
        self._kbd_run(lambda kb: (kb.set_all(hx, b),
                                  tuxthrottle_kbd.save_state(colors, b, mode="zones")),
                      f"colour {hx} @ {b}%")

    def _kbd_apply_effect(self, key: str):
        b = self.kbd_brightness.get()
        sp = self.kbd_speed.get()
        colors = self._kbd_all_colors()
        self._kbd_run(lambda kb: (kb.set_effect(key, sp, b),
                                  tuxthrottle_kbd.save_state(colors, b, mode=key, speed=sp)),
                      f"effect {key} @ speed {sp}, {b}%")

    def _kbd_reset(self):
        self._kbd_run(lambda kb: kb.reset(), "reset backlight (restart OpenRGB + re-apply)")

    def _kbd_off(self):
        self.kbd_brightness.set(0)
        self._kbd_run(lambda kb: kb.off(), "backlight off")

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

    _FANCURVE_DEFAULT = [[45, 0], [60, 25], [72, 55], [82, 85], [90, 100]]

    def _build_fancurve_section(self, parent):
        cfg = self._read_power_state("powerd.json") or {}
        fc = cfg.get("fan_curve", {})
        pts = fc.get("points") or self._FANCURVE_DEFAULT

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

        grid = tb.Frame(lf); grid.pack(anchor="w", pady=(10, 4))
        tb.Label(grid, text="Temp °C", width=10, bootstyle=SECONDARY).grid(row=0, column=0)
        tb.Label(grid, text="Boost %", width=10, bootstyle=SECONDARY).grid(row=0, column=1)
        self._fc_rows = []
        for r in range(5):
            t, b = (pts[r] if r < len(pts) else self._FANCURVE_DEFAULT[r])
            tv = tk.IntVar(value=int(t)); bv = tk.IntVar(value=int(b))
            tb.Spinbox(grid, from_=30, to=100, textvariable=tv, width=8,
                       command=self._fc_redraw).grid(row=r + 1, column=0, padx=4, pady=2)
            tb.Spinbox(grid, from_=0, to=100, textvariable=bv, width=8,
                       command=self._fc_redraw).grid(row=r + 1, column=1, padx=4, pady=2)
            self._fc_rows.append((tv, bv))

        self._fc_canvas = tk.Canvas(lf, height=120, bg="#0e1116", highlightthickness=0)
        self._fc_canvas.pack(fill="x", pady=(6, 6))
        self._fc_canvas.bind("<Configure>", lambda _e: self._fc_redraw())

        hr = tb.Frame(lf); hr.pack(anchor="w")
        tb.Label(hr, text="Cool-down hysteresis").pack(side="left")
        self._fc_hys = tk.IntVar(value=int(fc.get("hysteresis_c", 3)))
        tb.Spinbox(hr, from_=0, to=10, textvariable=self._fc_hys, width=6).pack(side="left", padx=6)
        tb.Label(hr, text="°C").pack(side="left")
        tb.Button(hr, text="Save curve", bootstyle=SUCCESS,
                  command=self._fc_save).pack(side="left", padx=(16, 0))
        self._fc_live = tb.Label(hr, text="", bootstyle=SECONDARY)
        self._fc_live.pack(side="left", padx=12)
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

    def _fan_set_boost(self, index: int):
        pct = self._fan_boost_vars[index].get()
        ok, err = sensors.set_fan_boost(index, round(pct * 255 / 100))
        self._log(f"[Fans] fan {index} boost → {pct}%" + ("" if ok else f"  FAILED: {err}"))

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
        self._build_nvpl_section(frame)
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
        if not sensors.ryzenadj_available():
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

    # --- NVIDIA board power limit ---

    def _build_nvpl_section(self, parent):
        if not self.has_nvidia:
            return
        lf = tb.Labelframe(parent, text="NVIDIA board power limit — RTX 3050 Ti",
                           padding=12)
        lf.pack(fill="x", pady=6)
        info = sensors.nvidia_power_limit_info()
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

    # --- Battery charge limit ---

    def _build_battery_section(self, parent):
        info = sensors.battery_charge_limit_info()
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
        r = tb.Frame(lf); r.pack(fill="x", pady=4)
        tb.Label(r, text="Stop charging at", width=20, anchor="w").pack(side="left")
        self._bat_var = tk.IntVar(value=info["current"] or 100)
        sc = tb.Scale(r, from_=50, to=100, variable=self._bat_var,
                      orient="horizontal", length=300,
                      command=lambda _v: self._bat_var.set(round(self._bat_var.get() / 5) * 5))
        sc.pack(side="left", fill="x", expand=True)
        sc.bind("<ButtonRelease-1>", lambda _e: self._bat_apply())
        tb.Label(r, textvariable=self._bat_var, width=3).pack(side="left")
        tb.Label(r, text="%").pack(side="left", padx=(0, 8))
        self._bat_live = tb.Label(r, text="now: — %", width=12, bootstyle=SECONDARY)
        self._bat_live.pack(side="left")
        br = tb.Frame(lf); br.pack(anchor="w", pady=(8, 0))
        for lbl, pct in (("60 %", 60), ("80 %", 80), ("Full (100 %)", 100)):
            tb.Button(br, text=lbl, bootstyle=(SECONDARY, "outline"),
                      command=lambda p=pct: (self._bat_var.set(p), self._bat_apply())
                      ).pack(side="left", padx=3)

    def _bat_apply(self):
        p = self._bat_var.get()
        self._write_power_state("battery.json", {"percent": p})
        ok, err = sensors.set_battery_charge_limit(p)
        self._log(f"[Power] battery charge limit → {p}%" + ("" if ok else f"  FAILED: {err}"))

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
        cur = sensors.gpu_mode_get()
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
        tb.Button(lf, text="Save auto-switch", bootstyle=SUCCESS,
                  command=self._aw_save).pack(anchor="w", pady=(10, 0))

    def _aw_save(self):
        merged = self._read_power_state("powerd.json") or {}
        merged["autoswitch"] = {
            "enabled": bool(self._aw_enabled.get()),
            "on_ac": self._aw_on_ac.get(),
            "on_battery": self._aw_on_bat.get(),
        }
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
        try:
            self.root.after(0, lambda: self._power_poll_apply(tdp, nvpl, bat))
        except (RuntimeError, tk.TclError):
            pass  # window torn down while this worker was in flight

    def _power_poll_apply(self, tdp, nvpl, bat):
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

        self._profiles_refresh()

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
        for _pv, _cv, cb in getattr(self, "_gp_rows", []):
            cb.configure(values=[""] + names)
        if getattr(self, "_gp_default_cb", None) is not None:
            self._gp_default_cb.configure(values=[""] + names)
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
        for preset_id, data in self.presets.items():
            box = tb.Frame(frame, padding=14, bootstyle="secondary")
            box.pack(fill="x", pady=6)
            tb.Label(box, text=data["Content"], font=("Sans", 12, "bold")).pack(anchor="w")
            tb.Label(box, text=data["Description"], wraplength=900, bootstyle=SECONDARY).pack(anchor="w", pady=(2, 8))
            tb.Button(
                box, text="Apply This Preset", bootstyle=SUCCESS,
                command=lambda pid=preset_id: self._on_apply_preset(pid)
            ).pack(anchor="e")

    # ---------- Setup Games ----------

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
                      "you've done them.").pack(anchor="w", pady=(2, 0))

        # general Proton-prefix + save-file relocation — useful for ANY Steam
        # game, not just the ones with a walkthrough below
        pf = tb.Labelframe(outer, text="Proton prefix & save-file tools", padding=10)
        pf.pack(fill="x", padx=16, pady=(6, 8))
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
        tb.Button(row, text="Scan Steam prefixes", bootstyle=(INFO, "outline"),
                  command=self._prefix_scan).pack(side="left")
        tb.Button(row, text="Migrate all at-risk prefixes", bootstyle=(WARNING, "outline"),
                  command=self._prefix_migrate_all).pack(side="left", padx=6)
        tb.Label(row, text="   or one — AppID:").pack(side="left")
        self._prefix_appid_var = tk.StringVar()
        tb.Entry(row, textvariable=self._prefix_appid_var, width=12).pack(side="left", padx=(2, 6))
        tb.Button(row, text="Relocate this prefix", bootstyle=(WARNING, "outline"),
                  command=self._prefix_relocate_entry).pack(side="left")
        row2 = tb.Frame(pf); row2.pack(anchor="w", fill="x", pady=(2, 0))
        tb.Button(row2, text="Scan for saves on another drive", bootstyle=(INFO, "outline"),
                  command=self._saves_scan).pack(side="left")
        tb.Button(row2, text="Move all stray saves into their prefixes",
                  bootstyle=(WARNING, "outline"),
                  command=self._saves_move_all).pack(side="left", padx=6)
        tb.Button(row2, text="Import loose saves for AppID above",
                  bootstyle=(WARNING, "outline"),
                  command=self._saves_import_entry).pack(side="left")

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
        tb.Button(vrow, text="Browse…", bootstyle=(SECONDARY, "outline"),
                  command=self._vault_browse).pack(side="left")
        vrow2 = tb.Frame(pf); vrow2.pack(anchor="w", fill="x", pady=(2, 0))
        tb.Button(vrow2, text="List vault", bootstyle=(INFO, "outline"),
                  command=lambda: self._vault_cmd("list")).pack(side="left")
        tb.Button(vrow2, text="Export saves → vault", bootstyle=(WARNING, "outline"),
                  command=lambda: self._vault_cmd("export")).pack(side="left", padx=6)
        tb.Button(vrow2, text="Import saves ← vault", bootstyle=(WARNING, "outline"),
                  command=lambda: self._vault_cmd("import")).pack(side="left")

        tb.Separator(outer).pack(fill="x")

        gnb = tb.Notebook(outer)
        gnb.pack(fill="both", expand=True, padx=8, pady=8)

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

    def _game_subst(self, gid: str, s: str) -> str:
        return (s.replace("{USER}", self.user)
                 .replace("{TOOLKIT_DIR}", str(BASE_DIR))
                 .replace("{APPID}", str(self.games.get(gid, {}).get("appid", ""))))

    def _game_step_card(self, parent, gid: str, step: dict):
        card = tb.Frame(parent, padding=14, bootstyle="dark")
        card.pack(fill="x", padx=2, pady=5)

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
        rec = {"gid": gid, "step": step, "status": status}

        if step.get("run"):
            tb.Button(row, text="▶  Run step", bootstyle=SUCCESS,
                      command=lambda: self._run_game_step(gid, step)).pack(side="left")
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

    @staticmethod
    def _apply_game_state(rec: dict, state: str) -> None:
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
            ("Dashboard", "live CPU / iGPU / dGPU clocks, temps, power; rolling history sparklines; session CSV log; Game Mode toggle"),
            ("Keyboard", "AW-ELC RGB — whole-keyboard solid colour, brightness, firmware Spectrum Cycle"),
            ("Fans", "thermal profile, additive fan boost + presets, manual PWM (guarded), closed-loop custom fan curve"),
            ("Power & Limits", "CPU TDP (ryzenadj STAPM/fast/slow), NVIDIA power limit where the GPU allows, hybrid-graphics mode (EnvyControl), battery charge limit (sysfs / libsmbios), AC↔battery auto-switch"),
            ("Profiles", "named full-state bundles; automatic snapshot before every apply; one-click rollback; per-game auto-profiles"),
            ("Presets", "one-click curated bundles of tweaks + app installs"),
            ("Updates", "nobara-sync wrapper + per-manager dnf / Flatpak / fwupd; pending count tagged with the metadata age"),
            ("Setup Games", "per-game click-through walkthroughs (GTA V Online first) + Proton prefix / save-file tools"),
            ("Tweaks & Apps", "reversible system tweaks by category — Gaming, GPU, Power, Performance, KDE (Desktop GUI Tweaks), Stability — plus one-directional app installs"),
            ("tuxthrottlectl", "headless CLI (status / get / set / profile / snapshot / rollback / gamemode, --json) for scripts, keybinds and ssh"),
            ("Report a Bug", "read-only hardware / OS dump for GitHub issues"),
        ):
            row = tb.Frame(self._about_body, style="CardRow.TFrame")
            row.pack(anchor="w", fill="x", pady=2)
            tb.Label(row, text=f"▸  {name}", font=("Sans", 10, "bold"),
                     width=16, anchor="w", style="CardKey.TLabel").pack(side="left", anchor="n")
            tb.Label(row, text=desc, wraplength=900, justify="left",
                     style="Card.TLabel").pack(side="left", anchor="n")

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

        meta = tb.Labelframe(frame, text="Details", padding=12)
        meta.pack(fill="x", pady=6)
        m = sensors.detect_model()
        for k, v in (
            ("Target hardware", "Dell G15 5515 Ryzen Edition (0R3CDX)"),
            ("This machine", f"{m['vendor']} {m['product']}"
                             + (f", BIOS {m['bios']}" if m['bios'] else "")),
            ("Distro target", "Nobara Linux (Fedora 43 base, KDE Plasma / Wayland)"),
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
        while self.dash_running:
            cpu_temp = sensors.read_cpu_temp_c_value()
            cpu_freq = sensors.read_cpu_freq_ghz_value()
            cpu_power = sensors.read_cpu_power_watts()  # blocks ~0.1s, fine on this bg thread
            igpu_clock, igpu_temp = sensors.read_igpu_clock_temp_values()
            dgpu_clock, dgpu_temp, dgpu_util, dgpu_power = sensors.read_dgpu_values()
            rapl_ok = sensors.rapl_permissions_ok()
            gamemode = sensors.get_game_mode_state()
            self.dash_queue.put((cpu_temp, cpu_freq, cpu_power, igpu_clock, igpu_temp,
                                  dgpu_clock, dgpu_temp, dgpu_util, dgpu_power, rapl_ok, gamemode))
            for _ in range(19):  # ~2s poll total (0.1s already spent above), checkable for shutdown
                if not self.dash_running:
                    return
                threading.Event().wait(0.1)

    def _poll_dash_queue(self):
        try:
            while True:
                (cpu_temp, cpu_freq, cpu_power, igpu_clock, igpu_temp,
                 dgpu_clock, dgpu_temp, dgpu_util, dgpu_power, rapl_ok, gamemode) = self.dash_queue.get_nowait()
                self.meter_cpu_temp.set(cpu_temp)
                self.meter_cpu_freq.set(cpu_freq)
                self.meter_cpu_power.set(cpu_power)
                self.meter_igpu_freq.set(igpu_clock)
                self.meter_dgpu_temp.set(dgpu_temp)
                self.meter_dgpu_freq.set(dgpu_clock)
                self.meter_dgpu_util.set(dgpu_util)
                self.meter_dgpu_power.set(dgpu_power)
                cpu_power_txt = f", {cpu_power:.1f} W" if cpu_power is not None else ""
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
            self._csv_path_lbl.configure(text="(stopped)")

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
        # Each check spawns a shell; running them serially made startup crawl.
        # They're independent and I/O-bound, so fan them out over a pool.
        items = list(self.items.values())
        if items:
            with ThreadPoolExecutor(max_workers=min(12, len(items))) as ex:
                list(ex.map(lambda it: evaluate_item(it, ledger), items))
        # Hand back to the main thread via a queue — Tk is not thread-safe and
        # calling root.after() from here races the interpreter (crashes as
        # "main thread is not in main loop"). Mirrors the log/dash queues.
        self.status_queue.put(True)

    def _poll_status_queue(self):
        drained = False
        try:
            while True:
                self.status_queue.get_nowait()
                drained = True
        except queue.Empty:
            pass
        if drained:
            self._apply_status_to_widgets()
            # states changed → the section-recommendations button may need to
            # hide (all applied) or update its count
            if hasattr(self, "notebook"):
                self._on_nav_page(self.notebook._header.cget("text"))  # noqa: SLF001
        self.root.after(200, self._poll_status_queue)

    def _apply_status_to_widgets(self):
        n_done = n_total = n_attention = 0
        for item in self.items.values():
            if item.status_label is None:
                continue
            if not item.hw_supported:
                item.status_label.configure(text="unsupported", bootstyle=SECONDARY)
                continue
            n_total += 1
            label, style = _STATE_UI.get(item.state, _STATE_UI["unknown"])
            if item.kind == "app":
                label = {"Applied": "Installed", "Not applied": "Not installed"}.get(label, label)
            if item.done:
                n_done += 1
            if item.state in ("error", "drifted", "failed"):
                n_attention += 1
            item.status_label.configure(text=label, bootstyle=style)
            if item.var is not None:
                item.var.set(item.done)
        msg = (f"{n_done} of {n_total} applied/installed — "
               f"{n_total - n_done} available.")
        if n_attention:
            msg += f"  ⚠ {n_attention} need attention (see Status report)."
        self.status_var.set(msg)

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

    def _apply_worker(self, item_ids: list[str]):
        # always leave a rollback point before a bulk change
        try:
            snap = tuxthrottle_profiles.snapshot(label="pre-apply-selected")
            self._log(f"[snapshot] pre-apply rollback point: {snap.name}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[snapshot] couldn't capture a rollback point: {exc}")
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

    def _on_nav_page(self, page_text: str):
        """Show the 'Apply section recommendations' button only on a category
        page that still has unapplied dev picks."""
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
        try:
            snap = tuxthrottle_profiles.snapshot(label=f"pre-{label}")
            self._log(f"[snapshot] pre-apply rollback point: {snap.name}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[snapshot] couldn't capture a rollback point: {exc}")
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
        threading.Thread(target=self._preset_worker, args=(ids,), daemon=True).start()

    def _preset_worker(self, item_ids: list[str]):
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
        threading.Thread(target=self._refresh_all_status, daemon=True).start()


def _load_all_items() -> list:
    """Build the Item list (with vendor gating) without a ToolkitApp — shared
    by the --report / --debug CLI paths."""
    user = resolve_real_user()
    has_nv, has_amd = sensors.has_nvidia_gpu(), sensors.has_amd_gpu()
    items = []
    for kind, fn in (("tweak", "tweaks.json"), ("app", "apps.json")):
        for iid, data in load_json(fn).items():
            it = Item(iid, data, kind, user)
            if it.requires_vendor == "nvidia" and not has_nv:
                it.hw_supported = False
            elif it.requires_vendor == "amd" and not has_amd:
                it.hw_supported = False
            items.append(it)
    return items


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
    for p in (BASE_DIR / ".version",):
        try:
            v = p.read_text().strip()
            if v:
                return v
        except OSError:
            pass
    ok, _rc, out = run_cmd3(f"git -C {BASE_DIR} describe --tags --always --dirty 2>/dev/null "
                            f"|| git -C {BASE_DIR} rev-parse --short HEAD 2>/dev/null")
    return out.strip() or "unknown"


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


_HW_BUNDLE_FILES = [
    ("dmi.txt", "grep -r . /sys/class/dmi/id/ 2>/dev/null | sed 's#/sys/class/dmi/id/##' "
     "| grep -viE 'uevid|modalias'"),
    ("kernel.txt", "uname -a; echo; echo '# cmdline'; cat /proc/cmdline; echo; "
     "echo '# os-release'; cat /etc/os-release"),
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
     "echo '# power-profiles-daemon'; powerprofilesctl 2>/dev/null"),
    ("acpi.txt", "ls -l /sys/firmware/acpi/tables/ 2>/dev/null; echo; "
     "command -v acpidump >/dev/null && echo 'acpidump present — run: sudo acpidump -b (attach the DSDT.dat)'; "
     "command -v acpi_listen >/dev/null && echo 'acpi_listen present — run it and press Fn/media keys to capture ACPI events'"),
    ("drm-gpu.txt", "for c in /sys/class/drm/card[0-9]*; do echo \"### $c\"; "
     "cat $c/device/uevent 2>/dev/null; echo \" runtime_status=$(cat $c/device/power/runtime_status 2>/dev/null)\"; "
     "echo; done; echo '=== nvidia-smi -q ==='; nvidia-smi -q 2>/dev/null | head -90"),
    ("openrgb.txt", "openrgb --version 2>/dev/null; echo; "
     "openrgb --noautoconnect -l --verbose 2>/dev/null | grep -vE 'i2c|SMBus|help.openrgb' "
     "|| openrgb --noautoconnect -l 2>/dev/null"),
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
            "  Raw sysfs / DMI / evdev / hwmon / PCI / OpenRGB dumps + the readable\n"
            "  debug report. Enough to add support for this laptop model: the DMI\n"
            "  strings to gate on, the hwmon fan/pwm paths, the evdev key codes for\n"
            "  the Fn/media/vendor keys, the OpenRGB controller layout, etc.\n\n"
            "HOW TO USE\n"
            "  1. Skim the files for anything private (hostname, serials in dmi.txt /\n"
            "     lsusb.txt / nvidia-smi). Redact if you care.\n"
            "  2. Open a 'new hardware support' issue and ATTACH this whole .tar.gz\n"
            "     (drag it onto the GitHub comment box).\n"
            "  3. If a Fn/media key doesn't work: run  sudo evtest  , pick the\n"
            "     keyboard / hotkey device, press the key, and paste those lines too.\n\n"
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
