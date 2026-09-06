#!/usr/bin/env python3
"""Diagnostics tab — the hardware-bundle collector UI and the "copy a full
GitHub issue" flow. The heavy report-building functions live in
tuxthrottle_diag. Extracted from tuxthrottle.py (module-split pass, 11th
slice)."""
import os
import pwd
import queue
import threading
import time
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import INFO, SECONDARY, SUCCESS, WARNING

from tuxthrottle_diag import (
    GITHUB_ISSUE_TEMPLATE,
    collect_debug_report,
    collect_hw_bundle,
    wrap_issue_block,
)


class DiagnosticsTabMixin:
    def _build_diagnostics_tab(self, outer):
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

