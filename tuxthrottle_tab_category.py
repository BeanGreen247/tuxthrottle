#!/usr/bin/env python3
"""The data-driven category tab renderer (Performance/GPU/Power/Stability/
Gaming/KDE/Software nav pages) and the Presets tab — extracted from
tuxthrottle.py (module-split pass, ninth slice)."""
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import SECONDARY, SUCCESS, WARNING


class CategoryTabMixin:
    def _build_category_tab(self, outer, category: str):
        inner = self._scroll_body(outer)

        for item in self.items.values():
            if item.category != category or item.hidden:
                continue
            row = tb.Frame(inner, padding=16, bootstyle="dark")
            row.pack(fill="x", padx=2, pady=4)

            item.var = tk.BooleanVar(value=False)
            cb = tb.Checkbutton(row, variable=item.var, bootstyle="round-toggle",
                                command=self._refresh_pending_bar)
            cb.pack(side="left", anchor="n", padx=(0, 14))
            item.checkbutton = cb
            if not item.hw_supported:
                item.var.set(False)
                cb.configure(state="disabled")

            item.status_label = tb.Label(row, text="checking…", width=15, anchor="e",
                                         font=("Sans", 9, "bold"), bootstyle=SECONDARY)
            item.status_label.pack(side="right", anchor="n", padx=(14, 0))

            text_frame = tb.Frame(row, bootstyle="dark")
            text_frame.pack(side="left", fill="both", expand=True)
            title_row = tb.Frame(text_frame, bootstyle="dark")
            title_row.pack(anchor="w", fill="x")
            tb.Label(title_row, text=item.content, font=("Sans", 11, "bold"),
                     bootstyle="inverse-dark").pack(side="left")
            if item.risk == "advanced":
                tb.Label(title_row, text="ADVANCED", bootstyle=(WARNING, "inverse"),
                         font=("Sans", 7, "bold"), padding=(5, 1)).pack(side="left", padx=8)
            tb.Label(text_frame, text=item.description, wraplength=1250,
                     bootstyle="inverse-dark", justify="left").pack(anchor="w", pady=(4, 0))

        # This tab may be built lazily, after the first status sweep already
        # ran — paint the rows with whatever state we already know so they
        # don't sit on "checking…" until the next refresh.
        for item in self.items.values():
            if item.category == category and not item.hidden and item.state != "unknown":
                self._apply_one_status(item)
                item.var.set(item.done)
        self._refresh_pending_bar()

    def _build_presets_tab(self, outer):
        frame = self._scroll_body(outer, pad=14)
        tb.Label(frame, text="One click applies a curated bundle of tweaks + installs apps.", bootstyle=SECONDARY).pack(anchor="w", pady=(0, 12))

        recs = self._recommended_all()
        rb = tb.Labelframe(frame, text="Developer recommendations", padding=14)
        rb.pack(fill="x", pady=(0, 10))
        tb.Label(rb, wraplength=900, bootstyle=SECONDARY, text=(
            "Applies every item the developer marked ★ recommended — across all "
            "categories — in one pass, and offers to turn on the background "
            "daemon that powers the fan curve, AC/battery auto-switch and the "
            "time schedule. A snapshot is taken first so you can roll back from "
            "the Profiles tab.")).pack(anchor="w", pady=(0, 8))
        self._tip(tb.Button(rb, text="★  Apply all recommendations", bootstyle=SUCCESS,
                  command=self._on_apply_all_recommended),
                  "One-click sensible setup: applies every ★ recommended tweak "
                  "across all categories and offers to enable the background "
                  "daemon. Snapshot taken first; roll back from the Profiles tab."
                  ).pack(anchor="w")
        self._rec_all_lbl = tb.Label(
            rb, bootstyle=SECONDARY,
            text=(f"{len(recs)} not yet applied" if recs else "all applied ✓"))
        self._rec_all_lbl.pack(anchor="w", pady=(4, 0))

        for preset_id, data in self.presets.items():
            box = tb.Frame(frame, padding=14, bootstyle="secondary")
            box.pack(fill="x", pady=6)
            tb.Label(box, text=data["Content"], font=("Sans", 12, "bold")).pack(anchor="w")
            tb.Label(box, text=data["Description"], wraplength=900, bootstyle=SECONDARY).pack(anchor="w", pady=(2, 8))
            self._tip(tb.Button(
                box, text="Apply This Preset", bootstyle=SUCCESS,
                command=lambda pid=preset_id: self._on_apply_preset(pid)),
                "Apply this whole bundle of tweaks + app installs at once "
                "(snapshot taken first).").pack(anchor="e")
