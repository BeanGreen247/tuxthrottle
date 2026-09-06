#!/usr/bin/env python3
"""Power / Display / Touchpad / Battery-health tabs and all their helper
sections (TDP, Curve Optimizer, NVPL, GPU clock/mode, refresh-rate, VRR,
auto-switch, battery charge limits) — their build helpers are physically
interleaved in the original, so they share one mixin. Extracted from
tuxthrottle.py (module-split pass, eighth slice)."""
import shlex
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

import sensors
from tuxthrottle_items import BASE_DIR


class PowerDisplayTabMixin:
    def _build_battery_health_tab(self, outer):
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

        # live polling gated by _on_nav_page (ToolkitApp._TAB_LIVE).

    def _apply_charge_mode(self):
        m = self._chg_mode.get()
        ok, err = sensors.set_battery_charge_mode(m)
        self._log(f"[Battery] charging mode → {m}" + ("" if ok else f"  FAILED: {err}"))

    def _bath_poll(self, token=None):
        if token is None:
            self._bath_tok = getattr(self, "_bath_tok", 0) + 1
            token = self._bath_tok
        if not getattr(self, "_bath_live_on", False) or token != self._bath_tok:
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
        self.root.after(4000, lambda: self._bath_poll(token))

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

    def _build_power_tab(self, outer):
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
        # live polling gated by _on_nav_page (ToolkitApp._TAB_LIVE).

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

        self._build_gpuoffset_section(lf)

    def _build_gpuoffset_section(self, lf):
        """Core/mem clock OFFSET (overclock) — RISKY, session-only, needs
        Coolbits + an X session (usually absent on Wayland, so this commonly
        just shows why it's unavailable)."""
        tb.Separator(lf).pack(fill="x", pady=(10, 8))
        off = sensors.nvidia_clock_offset_info()
        head = tb.Frame(lf); head.pack(anchor="w")
        tb.Label(head, text="Clock offset (overclock)", font=("Sans", 10, "bold")
                 ).pack(side="left")
        tb.Label(head, text="RISKY", bootstyle=(DANGER, "inverse"),
                 font=("Sans", 7, "bold"), padding=(5, 1)).pack(side="left", padx=8)
        if not off.get("available"):
            tb.Label(lf, bootstyle=SECONDARY, wraplength=1000, justify="left",
                     text=f"Not available: {off.get('reason') or 'unsupported'}. "
                          "A GPU clock offset needs the NVIDIA 'Coolbits' option "
                          "and a reachable X display — a muxless laptop dGPU on a "
                          "Wayland session normally can't do this.").pack(anchor="w")
            return
        lo_r, hi_r = off.get("core_range") or (-200, 1000)
        self._gpuoff_core = tk.IntVar(value=int(off.get("core") or 0))
        self._gpuoff_mem = tk.IntVar(value=int(off.get("mem") or 0))
        for label, var, a, b in (("Core MHz", self._gpuoff_core, lo_r, hi_r),
                                 ("Mem MHz", self._gpuoff_mem, -500, 2000)):
            r = tb.Frame(lf); r.pack(fill="x", pady=3)
            tb.Label(r, text=label, width=20, anchor="w").pack(side="left")
            tb.Scale(r, from_=a, to=b, variable=var, orient="horizontal",
                     length=300).pack(side="left", fill="x", expand=True)
            tb.Label(r, textvariable=var, width=6).pack(side="left")
        br = tb.Frame(lf); br.pack(anchor="w", pady=(6, 0))
        tb.Button(br, text="Apply offset", bootstyle=(DANGER, "outline"),
                  command=self._gpuoff_apply).pack(side="left")
        tb.Button(br, text="Zero / revert", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._gpuoff_set(0, 0)).pack(side="left", padx=8)

    def _gpuoff_set(self, core, mem):
        if getattr(self, "_gpuoff_core", None) is not None:
            self._gpuoff_core.set(core); self._gpuoff_mem.set(mem)

        def work():
            ok, msg = sensors.set_nvidia_clock_offset(core, mem)
            self._log(f"[Power] GPU clock offset → {msg}"
                      + ("" if ok else f"  FAILED: {msg}"))
        threading.Thread(target=work, daemon=True).start()

    def _gpuoff_apply(self):
        core, mem = int(self._gpuoff_core.get()), int(self._gpuoff_mem.get())
        if core == 0 and mem == 0:
            self._gpuoff_set(0, 0)
            return
        self._gpuoff_set(core, mem)
        # session-only in-process keep/auto-revert guard (20 s)
        dlg = tk.Toplevel(self.root)
        dlg.title("Confirm GPU overclock")
        dlg.transient(self.root); dlg.grab_set(); dlg.resizable(False, False)
        left = {"n": 20}
        msg = tk.StringVar()
        tb.Label(dlg, padding=16, wraplength=380, justify="left", textvariable=msg
                 ).pack()
        row = tb.Frame(dlg, padding=(16, 0, 16, 16)); row.pack()

        def revert():
            dlg.destroy()
            self._gpuoff_set(0, 0)

        def keep():
            dlg.destroy()
            self._log("[Power] GPU clock offset kept by user")

        tb.Button(row, text="Keep", bootstyle=SUCCESS, command=keep).pack(side="left", padx=6)
        tb.Button(row, text="Revert now", bootstyle=DANGER, command=revert).pack(side="left", padx=6)

        def tick():
            if not dlg.winfo_exists():
                return
            msg.set(f"Applied core {core:+d} / mem {mem:+d} MHz.\n\nIf the screen "
                    f"is stable, click Keep. Auto-reverts to 0 in {left['n']} s "
                    f"(and always on app close).")
            left["n"] -= 1
            if left["n"] < 0:
                revert()
            else:
                dlg.after(1000, tick)
        tick()

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

    def _build_display_tab(self, outer):
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

    def _build_touchpad_tab(self, outer):
        info = self._probe("touchpad")
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

    def _power_poll(self, token=None):
        """Refresh the 'now:' readouts. The reads (ryzenadj -i, nvidia-smi)
        can each take ~1s, so they run on a worker and the label writes are
        marshalled back to the Tk thread."""
        if token is None:
            self._power_tok = getattr(self, "_power_tok", 0) + 1
            token = self._power_tok
        if not getattr(self, "_power_live", False) or self._power_tok != token:
            return
        threading.Thread(target=self._power_poll_worker, daemon=True).start()
        self.root.after(3000, lambda: self._power_poll(token))

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
