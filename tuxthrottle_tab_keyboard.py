#!/usr/bin/env python3
"""Keyboard RGB tab — extracted from tuxthrottle.py (module-split pass,
third slice). A mixin: KeyboardTabMixin defines methods that only ever
run as part of a ToolkitApp instance (self.root, self.user, self._log,
self._tip, ... all still resolve normally — Python looks those up on
the instance/MRO at call time, not by which file defines the method)."""
import shutil
import subprocess
import threading
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import SECONDARY, SUCCESS, WARNING

import sensors

try:
    import tuxthrottle_kbd
except Exception:  # noqa: BLE001
    tuxthrottle_kbd = None


class KeyboardTabMixin:
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
