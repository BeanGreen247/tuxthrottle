#!/usr/bin/env python3
"""Profiles tab (snapshots, named profiles, export/import, rollback) —
extracted from tuxthrottle.py (module-split pass, sixth slice)."""
import os
import pwd
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

import tuxthrottle_profiles


class ProfilesTabMixin:
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

