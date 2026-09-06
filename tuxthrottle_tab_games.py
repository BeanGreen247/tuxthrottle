#!/usr/bin/env python3
"""Setup Games + Game Tools — per-game step cards with ProtonDB badges, and
the shadercache / steamperf / launch-options / MangoHud / save-vault /
prefix-relocate / Fixes boxes. The single biggest slice. Extracted from
tuxthrottle.py (module-split pass, tenth slice)."""
import base64
import json
import os
import pwd
import queue
import re
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
import tuxthrottle_mangohud_status as mangohud_status
import tuxthrottle_protondb as protondb
from tuxthrottle_gui_widgets import BIOS_PANEL, BIOS_SUNKEN, _human_bytes
from tuxthrottle_items import BASE_DIR, run_cmd3


class GamesTabMixin:
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

    def _build_games_tab(self, outer):
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

    def _build_gametools_tab(self, outer):
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
