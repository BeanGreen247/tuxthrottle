#!/usr/bin/env python3
"""Standalone GUI widgets and the BIOS dark theme — extracted from
tuxthrottle.py (second slice of the modular-refactor pass). Everything
here is genuinely self-contained: no ToolkitApp/self-instance coupling,
just tkinter/ttkbootstrap widget classes and pure color-math helpers.

_Tooltip, RingGauge, HistoryChart, SidebarNav (the left-rail nav that
replaces tb.Notebook), and the full BIOS-look theme (constants +
apply_bios_style + the WCAG-contrast color helpers it uses to keep text
legible against an arbitrary KDE accent color).
"""
from __future__ import annotations

import configparser
import os
import pwd
import re
import time
import tkinter as tk
from collections import deque

import ttkbootstrap as tb

from tuxthrottle_items import resolve_real_user


def _is_num(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


class _Tooltip:
    """Lightweight hover tooltip (ttkbootstrap 2.2.x has no ToolTip class).
    A borderless Toplevel below the widget after a short hover; hides on
    leave / click / destroy, auto-hides after a few seconds, and is placed
    well clear of the pointer so it can't cause an Enter/Leave flicker loop
    (a real source of "the GUI froze" on some Wayland setups)."""

    def __init__(self, widget, text: str, delay: int = 500):
        self.widget, self.text, self.delay = widget, text, delay
        self.tip = None
        self._after = self._autohide = None
        self._last_hide = 0.0
        for ev, fn in (("<Enter>", self._schedule), ("<Leave>", self._hide),
                       ("<ButtonPress>", self._hide), ("<Destroy>", self._hide)):
            widget.bind(ev, fn, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        # break a tight Enter/Leave flicker loop (tooltip landing under cursor)
        if time.monotonic() - self._last_hide < 0.2:
            return
        try:
            self._after = self.widget.after(self.delay, self._show)
        except tk.TclError:
            pass

    def _cancel(self):
        for h in ("_after", "_autohide"):
            hid = getattr(self, h)
            if hid:
                try:
                    self.widget.after_cancel(hid)
                except tk.TclError:
                    pass
                setattr(self, h, None)

    def _show(self):
        self._after = None
        if self.tip or not self.text:
            return
        try:
            if self.widget.winfo_toplevel().grab_current():
                return                       # a modal dialog / busy overlay is up
            x = self.widget.winfo_rootx() + 18
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 12
        except tk.TclError:
            return
        try:
            self.tip = tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            tk.Label(tw, text=self.text, justify="left", background="#0f1620",
                     foreground="#e6edf3", relief="solid", borderwidth=1,
                     wraplength=380, padx=8, pady=6, font=("Sans", 9)).pack()
            self._autohide = self.widget.after(5000, self._hide)
        except tk.TclError:
            self.tip = None

    def _hide(self, _e=None):
        self._cancel()
        self._last_hide = time.monotonic()
        if self.tip:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


# --------------------------------------------------------------------------- #
#  Look & feel — a "gaming BIOS" dark shell with the KDE accent colour.
# --------------------------------------------------------------------------- #

ACCENT_FALLBACK = "#3daee9"   # Breeze blue, if the desktop accent can't be read

# --- theme palettes ------------------------------------------------------- #
# A theme is a palette dict. The keys below are the whole surface language;
# every widget class + apply_bios_style() reads the module-level names that
# set_palette() rebinds from the chosen palette. "BIOS Dark" is the default
# and also the fallback for any key a palette omits.
#   panel   = every widget background      sunken  = troughs / window ground
#   panel_hi= hover / active row           fg / muted = primary / secondary text
#   border / border_hi = hairline / active-edge
#   card    = optional raised surface      chart_axis = sparkline labels
#   accent  = used when a theme pins its own accent instead of the KDE one
#             (None → follow the desktop accent)
_BASE_PALETTE = {
    "panel": "#141a21", "sunken": "#0b0e12", "panel_hi": "#212c38",
    "fg": "#e9eff5", "muted": "#b3bfcb", "border": "#3c4a5b",
    "border_hi": "#5a6b7e", "card": "#1b2531", "chart_axis": "#8b98a8",
    "success": "#3ddc97", "danger": "#ff7b70", "warning": "#f5b041",
    "info": "#57c4f2", "secondary": "#c3ccd6",
    "help_amber": "#e8a33d", "help_banner_bg": "#2a2314",
    "accent": None,
}
PALETTES: dict[str, dict] = {
    "BIOS Dark": {},  # the base as-is (follows the KDE accent)
    "Carbon": {       # near-monochrome, minimal colour, tighter contrast
        "panel": "#161616", "sunken": "#0b0b0b", "panel_hi": "#242424",
        "fg": "#ededed", "muted": "#a8a8a8", "border": "#3a3a3a",
        "border_hi": "#585858", "card": "#1d1d1d", "chart_axis": "#8a8a8a",
        "accent": "#7aa2f7",
    },
    "Nord": {         # the popular arctic palette
        "panel": "#2e3440", "sunken": "#242933", "panel_hi": "#3b4252",
        "fg": "#eceff4", "muted": "#c2cad8", "border": "#4c566a",
        "border_hi": "#5e6a86", "card": "#343c4c", "chart_axis": "#8b98b4",
        "success": "#a3be8c", "danger": "#bf616a", "warning": "#ebcb8b",
        "info": "#88c0d0", "secondary": "#d8dee9", "accent": "#88c0d0",
    },
    "Gruvbox": {      # warm retro dark
        "panel": "#282828", "sunken": "#1d2021", "panel_hi": "#3c3836",
        "fg": "#ebdbb2", "muted": "#c8b391", "border": "#504945",
        "border_hi": "#6b625b", "card": "#32302f", "chart_axis": "#a89984",
        "success": "#b8bb26", "danger": "#fb4934", "warning": "#fabd2f",
        "info": "#83a598", "secondary": "#d5c4a1", "accent": "#fabd2f",
    },
    "Slate Blue": {   # cooler, softer blue-grey than BIOS Dark
        "panel": "#1b2330", "sunken": "#111823", "panel_hi": "#2b3748",
        "fg": "#e8edf4", "muted": "#aebacb", "border": "#3d4b5f",
        "border_hi": "#5b6d84", "card": "#232d3d", "chart_axis": "#8fa0b6",
        "accent": "#5aa9e6",
    },
}

_P = dict(_BASE_PALETTE)          # active palette (mutated by set_palette)


def set_palette(name: str) -> str:
    """Rebind the module-level colour names from PALETTES[name] over the base.
    Returns the name actually applied (falls back to 'BIOS Dark')."""
    name = name if name in PALETTES else "BIOS Dark"
    _P.clear()
    _P.update(_BASE_PALETTE)
    _P.update(PALETTES[name])
    g = globals()
    g["BIOS_PANEL"] = _P["panel"]
    g["BIOS_SUNKEN"] = _P["sunken"]
    g["BIOS_BG"] = _P["sunken"]
    g["BIOS_PANEL_HI"] = _P["panel_hi"]
    g["BIOS_FG"] = _P["fg"]
    g["BIOS_MUTED"] = _P["muted"]
    g["BIOS_BORDER"] = _P["border"]
    g["BIOS_BORDER_HI"] = _P["border_hi"]
    g["BIOS_CARD"] = _P["card"]
    g["CHART_AXIS"] = _P["chart_axis"]
    g["SEM_SUCCESS"] = _P["success"]
    g["SEM_DANGER"] = _P["danger"]
    g["SEM_WARNING"] = _P["warning"]
    g["SEM_INFO"] = _P["info"]
    g["SEM_SECONDARY"] = _P["secondary"]
    g["HELP_AMBER"] = _P["help_amber"]
    g["HELP_BANNER_BG"] = _P["help_banner_bg"]
    return name


# module-level names (rebound by set_palette); initialised to the base here
BIOS_PANEL = _BASE_PALETTE["panel"]
BIOS_SUNKEN = _BASE_PALETTE["sunken"]
BIOS_BG = _BASE_PALETTE["sunken"]
BIOS_PANEL_HI = _BASE_PALETTE["panel_hi"]
BIOS_FG = _BASE_PALETTE["fg"]
BIOS_MUTED = _BASE_PALETTE["muted"]
BIOS_BORDER = _BASE_PALETTE["border"]
BIOS_BORDER_HI = _BASE_PALETTE["border_hi"]
BIOS_CARD = _BASE_PALETTE["card"]
CHART_AXIS = _BASE_PALETTE["chart_axis"]
SEM_SUCCESS = _BASE_PALETTE["success"]
SEM_DANGER = _BASE_PALETTE["danger"]
SEM_WARNING = _BASE_PALETTE["warning"]
SEM_INFO = _BASE_PALETTE["info"]
SEM_SECONDARY = _BASE_PALETTE["secondary"]
HELP_AMBER = _BASE_PALETTE["help_amber"]
HELP_BANNER_BG = _BASE_PALETTE["help_banner_bg"]


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


def apply_bios_style(style: tb.Style, accent: str, theme_name: str = "BIOS Dark") -> str:
    """Re-skin the ttkbootstrap 'darkly' base into the chosen palette. All
    wrapped defensively — a theming quirk must never take the app down.
    Returns the theme name actually applied. A palette that pins its own
    `accent` overrides the passed-in (desktop) accent."""
    theme_name = set_palette(theme_name)
    if _P.get("accent"):
        accent = _P["accent"]
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
        # Card = the page surface + a hairline border + a glyphed header. Same
        # background as everything else (BIOS_PANEL) so raw children — RingGauge
        # canvases, plain tb.Labels, Scales — sit on it seamlessly instead of
        # showing as darker rectangles (the "wrong gauge background" bug).
        "Card.TFrame": {"background": BIOS_PANEL, "bordercolor": BIOS_BORDER,
                        "darkcolor": BIOS_BORDER, "lightcolor": BIOS_BORDER,
                        "relief": "solid", "borderwidth": 1},
        # coloured-border variants — for a section that needs a semantic edge
        # (risky = danger, firmware = warning, …). Card(parent, t, border="danger")
        "CardDanger.TFrame": {"background": BIOS_PANEL, "bordercolor": SEM_DANGER,
                              "darkcolor": SEM_DANGER, "lightcolor": SEM_DANGER,
                              "relief": "solid", "borderwidth": 1},
        "CardWarning.TFrame": {"background": BIOS_PANEL, "bordercolor": SEM_WARNING,
                               "darkcolor": SEM_WARNING, "lightcolor": SEM_WARNING,
                               "relief": "solid", "borderwidth": 1},
        "CardSuccess.TFrame": {"background": BIOS_PANEL, "bordercolor": SEM_SUCCESS,
                               "darkcolor": SEM_SUCCESS, "lightcolor": SEM_SUCCESS,
                               "relief": "solid", "borderwidth": 1},
        "CardRow.TFrame": {"background": BIOS_PANEL, "borderwidth": 0, "relief": "flat"},
        "Card.TLabel": {"background": BIOS_PANEL, "foreground": BIOS_FG},
        "CardKey.TLabel": {"background": BIOS_PANEL, "foreground": accent_txt_hi,
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
    return theme_name


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
                 height=64, thresholds=None):
        super().__init__(master, height=height, bg=BIOS_SUNKEN,
                         highlightthickness=0, bd=0)
        self._buf = deque(maxlen=samples)
        self._color = color or ACCENT_FALLBACK
        self._caption = caption
        self._unit = unit
        # [(value, color, label)] — horizontal reference lines (throttle temp,
        # TDP cap, …). Mapped through the same base/span as the trace so they
        # only render once the value is inside the visible range.
        self._thresholds = list(thresholds or [])
        self.bind("<Configure>", lambda _e: self._draw())

    def set_thresholds(self, thresholds):
        self._thresholds = list(thresholds or [])
        self._draw()

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
        dlo, dhi = min(vals), max(vals)
        dspan = max(1e-6, dhi - dlo)
        # A threshold only widens the y-range (and only then renders) once the
        # trace gets within ~one span of it — otherwise a far-off 90°C throttle
        # line would flatten an idle 45-55°C trace against the floor.
        near = [(tv, tc, tl) for (tv, tc, tl) in self._thresholds
                if _is_num(tv) and dlo - dspan <= float(tv) <= dhi + dspan]
        lo, hi = dlo, dhi
        for tv, _c, _l in near:
            lo, hi = min(lo, float(tv)), max(hi, float(tv))
        if hi - lo < 1e-6:
            lo, hi = lo - 1, hi + 1
        span = (hi - lo) * 1.15
        base = lo - (hi - lo) * 0.075

        def _y(v):
            return h - pad - (v - base) / span * (h - 2 * pad - 10) - 2

        for tv, tc, tl in near:
            ty = _y(float(tv))
            self.create_line(pad, ty, w - pad, ty, fill=tc, width=1,
                             dash=(3, 3))
            if tl and 14 < ty < h - 10:      # room, and clear of the caption row
                self.create_text(w - pad - 2, ty - 5, text=tl, anchor="e",
                                 fill=tc, font=("Sans", 7))
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


# ---- shared layout primitives (the DAMX/LACT restyle) --------------------- #

# spacing scale — use instead of ad-hoc pixel pads so every tab lines up
PAD_XS, PAD_S, PAD_M, PAD_L = 4, 8, 14, 22
FONT_TITLE = ("Sans", 11, "bold")     # card / section heading
FONT_BODY = ("Sans", 10)              # normal copy
FONT_CAPTION = ("Sans", 9)            # secondary / hint text

# monochrome glyph per card title — same visual language as SidebarNav._NAV_ICONS
_CARD_ICONS = {"CPU": "▤", "GPU": "◈", "Details": "≣", "History": "∿",
               "Game Mode": "♞"}


class Card(tb.Frame):
    """A titled, hair-lined section — the DAMX/LACT card, and a drop-in for
    `tb.Labelframe(parent, text=…, padding=…)`. A glyphed header is packed at
    the top. **pack** your content straight into the Card; use `.body` (a
    zero-height-when-empty sub-frame) only when you need to `.grid()` children,
    since a container can't mix pack and grid. `icon` defaults to a glyph
    from the title; pass `icon=""` to suppress it."""

    _BORDER_STYLE = {"danger": "CardDanger.TFrame", "warning": "CardWarning.TFrame",
                     "success": "CardSuccess.TFrame"}

    def __init__(self, master, title="", *, icon=None, border=None, **kw):
        kw.setdefault("padding", PAD_M)
        super().__init__(master, style=self._BORDER_STYLE.get(border, "Card.TFrame"), **kw)
        if title:
            head = tb.Frame(self, style="CardRow.TFrame")
            head.pack(fill="x", pady=(0, PAD_S))
            glyph = icon if icon is not None else _CARD_ICONS.get(title, "")
            if glyph:
                tb.Label(head, text=glyph, style="CardKey.TLabel").pack(side="left",
                                                                        padx=(0, PAD_S))
            tb.Label(head, text=title, style="CardKey.TLabel",
                     font=FONT_TITLE, wraplength=1100, justify="left",
                     anchor="w").pack(side="left")
            self._head = head
        # for callers that .grid() their content (Dashboard gauge grid) — a
        # container can't mix pack and grid, so give them their own frame.
        self.body = tb.Frame(self, style="CardRow.TFrame")
        self.body.pack(fill="x")


class Segmented(tb.Frame):
    """A row of linked toggle buttons bound to one StringVar — replaces a
    cluster of Radiobuttons / an OptionMenu for picking one of a few modes.
    `options` is [(label, value), ...] or a plain list (label == value)."""

    def __init__(self, master, variable: tk.StringVar, options, *,
                 command=None, **kw):
        super().__init__(master, **kw)
        self._var = variable
        self._cmd = command
        self._btns = {}
        for opt in options:
            label, value = opt if isinstance(opt, (tuple, list)) else (opt, opt)
            b = tb.Button(self, text=label, takefocus=False,
                          command=lambda v=value: self._pick(v))
            b.pack(side="left", padx=(0, 3))
            self._btns[value] = b
        self._sync()

    def _pick(self, value):
        self._var.set(value)
        self._sync()
        if callable(self._cmd):
            self._cmd()

    def _sync(self):
        cur = self._var.get()
        for value, b in self._btns.items():
            # active = solid accent fill; inactive = quiet outline. A clear,
            # unmistakable selected state (the toolbutton style was too subtle
            # on the dark panels).
            b.configure(bootstyle="info" if value == cur else "secondary-outline")


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
        self._lazy: dict = {}       # str(frame) -> builder(frame); built on first select
        self._built: set = set()    # str(frame) of lazily-built pages already built

    def _nav_reflow(self):
        """Keep the scrollregion in sync and hide the scrollbar unless the
        button list actually overflows the rail."""
        self._nav_canvas.configure(scrollregion=self._nav_canvas.bbox("all"))
        need = self._nav_box.winfo_reqheight() > self._nav_canvas.winfo_height() + 1
        if need and not self._nav_vsb.winfo_ismapped():
            self._nav_vsb.pack(side="right", fill="y", before=self._nav_canvas)
        elif not need and self._nav_vsb.winfo_ismapped():
            self._nav_vsb.pack_forget()

    # A small monochrome-glyph icon per nav label — the one visual habit every
    # peer tool researched (TuxedoControlCenter, asusctl/rog-control-center,
    # LenovoLegionLinux/Legion-Linux-Toolkit, LACT) shares that this sidebar
    # didn't: icon + label, not label alone. Plain Unicode symbols, not color
    # emoji, matching the ★/↩/⇅ glyphs already used elsewhere in this GUI.
    _NAV_ICONS = {
        "Dashboard": "▦", "Keyboard": "⌨", "Touchpad": "▢", "Fans": "❄",
        "Battery": "⏻", "VRAM": "▥", "Power & Limits": "⚡", "Display": "▭",
        "Profiles": "❖", "Presets": "★", "Setup Games": "♦", "Game Tools": "⚙",
        "Updates": "⬇", "About": "ℹ", "Report a Bug": "⚠",
        # data-driven tweak categories (config/tweaks.json "category" values)
        "Performance": "▲", "GPU": "◈", "Power": "☉", "Stability": "▣",
        "Gaming": "♞", "KDE (Desktop GUI Tweaks)": "◧", "Software": "⬢",
    }

    def add(self, frame, text: str = "", *, kind: str = "normal",
            spacer: bool = False, pin: bool = False):
        """Register an already-built page (its widgets exist now)."""
        self._register(frame, text, kind=kind, spacer=spacer, pin=pin)
        if self._current is None:
            self.select(frame)

    def add_lazy(self, text: str, builder, *, kind: str = "normal",
                 spacer: bool = False, pin: bool = False):
        """Register a page whose widgets are built on first selection. `builder`
        is called once as `builder(frame)` the first time the nav entry is
        clicked. Returns the (empty for now) page frame."""
        frame = tb.Frame(self)
        self._register(frame, text, kind=kind, spacer=spacer, pin=pin)
        self._lazy[str(frame)] = builder
        return frame

    def _register(self, frame, text, *, kind="normal", spacer=False, pin=False):
        frame.master  # noqa: B018  (frame was created as tb.Frame(self); fine)
        pinned = pin or spacer or kind == "support"
        parent = self._rail_bottom if pinned else self._nav_box
        if pinned and self._rail_bottom_sep is None:
            self._rail_bottom_sep = tb.Separator(self._rail_bottom, orient="horizontal")
            self._rail_bottom_sep.pack(side="top", fill="x", padx=12, pady=(4, 2))
        base = "NavSupport.TButton" if kind == "support" else "Nav.TButton"
        icon = self._NAV_ICONS.get(text, "")
        label = f"{icon}  {text}" if icon else text
        btn = tb.Button(parent, text=label, style=base,
                        takefocus=False, command=lambda f=frame: self.select(f))
        btn.pack(side="top", fill="x", padx=0, pady=1)
        btn._nav_kind = kind  # noqa: SLF001
        self._pages.append((text, frame, btn))

    def select(self, frame=None):
        if frame is None:
            return self._current
        key = str(frame)
        if key in self._lazy and key not in self._built:
            self._built.add(key)          # mark first so a hard failure can't loop
            try:
                self._lazy[key](frame)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                # make the failure visible in the GUI, not just on stderr
                try:
                    for w in frame.winfo_children():
                        w.destroy()
                    tb.Label(frame, bootstyle="danger", justify="left",
                             wraplength=900, padding=16,
                             text=f"This tab failed to build:\n\n{exc}\n\n"
                                  "See the Report a Bug log / stderr for the "
                                  "traceback.").pack(anchor="w")
                except tk.TclError:
                    pass
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

