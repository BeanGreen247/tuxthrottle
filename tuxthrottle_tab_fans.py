#!/usr/bin/env python3
"""Fans tab (thermal profile, fan boost, the custom fan-curve editor with
its live position dot, the 60s boost quick action, keyboard-color-tied-to-
profile) — extracted from tuxthrottle.py (module-split pass, fourth slice)."""
import json
import os
import pwd
import shlex
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

import sensors
import tuxthrottle_fixlog as fixlog

try:
    import tuxthrottle_kbd
except Exception:  # noqa: BLE001
    tuxthrottle_kbd = None

try:
    from tuxthrottle_powerd import interp as fancurve_interp
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

from tuxthrottle_gui_widgets import CHART_AXIS  # noqa: E402
from tuxthrottle_items import BASE_DIR  # noqa: E402

FAN_CURVE_POINTS = 10


class FanTabMixin:
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
            return [list(p) for p in FanTabMixin._FANCURVE_DEFAULT[:n]]

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
