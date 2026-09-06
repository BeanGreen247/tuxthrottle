#!/usr/bin/env python3
"""Dashboard tab (RingGauge/HistoryChart wiring, the reveal animation, and the
background polling loop + queue drain) — extracted from tuxthrottle.py
(module-split pass, seventh slice)."""
import queue
import threading
import time
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import SECONDARY, WARNING

import sensors
from tuxthrottle_gui_widgets import (
    ACCENT_FALLBACK,
    PAD_M,
    PAD_S,
    Card,
    HistoryChart,
    RingGauge,
)

# history-chart reference lines (DAMX/CoreCtrl); G15 5515 Ryzen + RTX 3050 Ti
_CPU_THROTTLE_C = 90
_DGPU_THROTTLE_C = 87


class DashboardTabMixin:
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

        acc = getattr(self, "accent", ACCENT_FALLBACK)
        # DAMX-style: CPU and GPU each get their own card, gauges in a 2x2 grid.
        cpu_specs = [
            ("meter_cpu_temp",  "CPU temp",   "°C",  100, acc,       "{:.0f}"),
            ("meter_cpu_freq",  "CPU clock",  "GHz", 5.0, "#3fb950", "{:.2f}"),
            ("meter_cpu_power", "CPU power",  "W",    65, acc,       "{:.0f}"),
            ("meter_igpu_freq", "iGPU clock", "MHz", 2000, "#3fb950", "{:.0f}"),
        ]
        gpu_specs = [
            ("meter_dgpu_temp", "dGPU temp",  "°C",  100, "#d29922", "{:.0f}"),
            ("meter_dgpu_freq", "dGPU clock", "MHz", 2100, "#d29922", "{:.0f}"),
            ("meter_dgpu_util", "dGPU util",  "%",   100, "#f85149", "{:.0f}"),
            ("meter_dgpu_power","dGPU power", "W",    80, "#d29922", "{:.0f}"),
        ]
        cards = tb.Frame(frame)
        cards.pack(fill="x", pady=(0, PAD_M))
        for col, (title, specs) in enumerate((("CPU", cpu_specs), ("GPU", gpu_specs))):
            card = Card(cards, title)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, PAD_S) if col == 0 else 0)
            cards.columnconfigure(col, weight=1)
            for i, (attr, cap, unit, mx, gcol, fmt) in enumerate(specs):
                g = RingGauge(card.body, caption=cap, unit=unit, maximum=mx,
                              color=gcol, fmt=fmt, size=124)
                g.grid(row=i // 2, column=i % 2, padx=PAD_S, pady=PAD_S, sticky="n")
                card.body.columnconfigure(i % 2, weight=1)
                setattr(self, attr, g)

        self.rapl_warning = tb.Label(
            frame, text="", bootstyle=WARNING, wraplength=900,
        )
        self.rapl_warning.pack(anchor="w", pady=(0, 12))

        det_card = Card(frame, "Details")
        det_card.pack(fill="x", pady=(0, PAD_M))
        details = det_card.body
        self.dash_cpu_label = tb.Label(details, text="CPU: …", font=("Monospace", 10))
        self.dash_cpu_label.pack(anchor="w")
        self.dash_igpu_label = tb.Label(details, text="iGPU: …", font=("Monospace", 10))
        self.dash_igpu_label.pack(anchor="w")
        self.dash_dgpu_label = tb.Label(details, text="dGPU: …", font=("Monospace", 10))
        self.dash_dgpu_label.pack(anchor="w")

        # rolling history strip
        hist_card = Card(frame, "History", icon="∿")
        hist_card.pack(fill="x", pady=(0, PAD_M))
        tb.Label(hist_card.body, text="rolling ~3 min", bootstyle=SECONDARY,
                 font=("Sans", 9)).pack(anchor="w", pady=(0, PAD_S))
        hgrid = tb.Frame(hist_card.body)
        hgrid.pack(fill="x")
        self._hist_charts = {}
        for i, (key, cap, unit, col, thr) in enumerate([
            ("cpu_temp",  "CPU °C",   "",  acc,       [(_CPU_THROTTLE_C, "#f85149", "throttle")]),
            ("cpu_power", "CPU W",    "",  acc,       []),
            ("dgpu_temp", "dGPU °C",  "",  "#d29922", [(_DGPU_THROTTLE_C, "#f85149", "throttle")]),
            ("dgpu_power","dGPU W",   "",  "#d29922", []),
        ]):
            c = HistoryChart(hgrid, caption=cap, unit=unit, color=col, samples=90,
                             thresholds=thr)
            c.grid(row=i // 2, column=i % 2, sticky="ew", padx=6, pady=4)
            hgrid.columnconfigure(i % 2, weight=1)
            self._hist_charts[key] = c
        logrow = tb.Frame(hist_card.body)
        logrow.pack(anchor="w", pady=(6, 0))
        tb.Checkbutton(logrow, text="Log this session to CSV",
                       variable=self._csv_logging, bootstyle="round-toggle",
                       command=self._toggle_csv_log).pack(side="left")
        self._csv_path_lbl = tb.Label(logrow, text="", bootstyle=SECONDARY,
                                      font=("Monospace", 8))
        self._csv_path_lbl.pack(side="left", padx=10)

        gm_card = Card(frame, "Game Mode", icon="♞")
        gm_card.pack(fill="x")
        toggle_frame = gm_card.body
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
                ch = self._hist_charts.get("cpu_power")
                if ch is not None and stapm_limit != getattr(self, "_stapm_shown", None):
                    self._stapm_shown = stapm_limit
                    ch.set_thresholds([(stapm_limit, "#d29922", "STAPM cap")]
                                      if stapm_limit else [])
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
