#!/usr/bin/env python3
"""Updates tab — extracted from tuxthrottle.py (module-split pass,
seventh slice)."""
import os
import pwd
import queue
import shlex
import shutil
import subprocess
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

from tuxthrottle_items import _dnf_metadata_age


class UpdatesTabMixin:
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

