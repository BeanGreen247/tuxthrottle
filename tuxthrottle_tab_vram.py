#!/usr/bin/env python3
"""VRAM tab (tier presets, free-VRAM, compositor-GPU selector) — extracted
from tuxthrottle.py (module-split pass, fifth slice)."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import INFO, SECONDARY, WARNING

import sensors
from tuxthrottle_items import BASE_DIR

try:
    import tuxthrottle_vram
except Exception:  # noqa: BLE001
    tuxthrottle_vram = None


class VramTabMixin:
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

