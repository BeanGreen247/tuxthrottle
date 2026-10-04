"""Drives tab - SMART, filesystem, kernel-log and Steam-library health with a
few safe maintenance actions. Backed by tuxthrottle_drivehealth.py, which the
(already elevated) GUI runs directly as root."""
import json
import threading
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

from tuxthrottle_gui_widgets import Card
from tuxthrottle_items import BASE_DIR, run_cmd3

_STYLE = {"ok": SUCCESS, "note": SECONDARY, "warn": WARNING, "bad": DANGER}
_MARK = {"ok": "✓", "note": "·", "warn": "⚠", "bad": "✗"}


def _size(n: int) -> str:
    return f"{n / (1 << 30):.0f} GiB" if n >= 10 << 30 else \
        f"{n / (1 << 30):.1f} GiB" if n >= 1 << 30 else f"{n >> 20} MiB"


class DrivesTabMixin:
    def _dh_cmd(self, args: str) -> str:
        return f"python3 {BASE_DIR}/tuxthrottle_drivehealth.py {args}"

    def _build_drives_tab(self, outer):
        frame = self._scroll_body(outer, pad=16)
        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY, text=(
            "SMART, filesystems, this boot's kernel log and the Steam libraries. "
            "Checking only reads; the buttons are what change things."
            )).pack(anchor="w", pady=(0, 12))
        top = tb.Frame(frame); top.pack(anchor="w", fill="x")
        self._tip(tb.Button(top, text="Check drives", bootstyle=(INFO, "outline"),
                  command=self._dh_check),
                  "Run every check again. Read-only.").pack(side="left")
        self._dh_fix_btn = tb.Button(top, text="Fix issues", bootstyle=SUCCESS,
                                     state="disabled", command=self._dh_fix_all)
        self._tip(self._dh_fix_btn,
                  "Run every safe automatic fix for all drives at once: scrub a "
                  "Btrfs filesystem that needs it, repair a dirty NTFS volume, mend "
                  "Steam's shadercache link, TRIM the SSDs. Asks first and lists "
                  "exactly what it will do."
                  ).pack(side="left", padx=(8, 0))
        self._tip(tb.Button(top, text="TRIM all SSDs", bootstyle=(SECONDARY, "outline"),
                  command=lambda: self._dh_run("Drives → TRIM", "trim")),
                  "fstrim every mounted filesystem that supports it - tells the SSD "
                  "which blocks are free. Safe; normally a weekly timer does it."
                  ).pack(side="left", padx=(8, 0))
        self._dh_summary = tb.Label(top, text="not checked yet", bootstyle=SECONDARY)
        self._dh_summary.pack(side="left", padx=(12, 0))
        self._dh_body = tb.Frame(frame)
        self._dh_body.pack(fill="x", pady=(10, 0))
        self.root.after(600, self._dh_check)

    def _dh_check(self):
        self._dh_summary.configure(text="checking…", bootstyle=SECONDARY)
        self._dh_res = None
        threading.Thread(target=self._dh_worker, daemon=True).start()
        self.root.after(400, self._dh_poll)

    def _dh_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._dh_cmd(f"report --json --user {self.user}"),
                                     timeout=120)
            self._dh_res = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, OSError):
            self._dh_res = {"error": True}

    def _dh_poll(self, tries: int = 0):
        r = getattr(self, "_dh_res", None)
        if r is None:
            if tries < 400:
                self.root.after(400, lambda: self._dh_poll(tries + 1))
            return
        try:
            self._dh_render(r)
        except tk.TclError:
            pass

    def _dh_findings(self, parent, findings):
        for f in findings:
            tb.Label(parent, text=f"{_MARK[f['level']]}  {f['text']}", wraplength=1000,
                     justify="left", bootstyle=_STYLE[f["level"]]).pack(anchor="w")

    def _dh_render(self, r: dict):
        for w in self._dh_body.winfo_children():
            w.destroy()
        if r.get("error"):
            self._dh_summary.configure(text="the check could not run", bootstyle=DANGER)
            self._dh_fix_btn.configure(state="disabled", text="Fix issues")
            return
        self._dh_plan = r.get("fix_plan", [])
        auto = [p for p in self._dh_plan if p["action"] != "manual"]
        self._dh_fix_btn.configure(state="normal" if auto else "disabled",
                                   text=f"Fix issues ({len(auto)})" if auto else "Fix issues")
        self._dh_summary.configure(
            text=("no problems found" if not (r["bad"] or r["warn"]) else
                  f"{r['bad']} problem(s), {r['warn']} warning(s)"),
            bootstyle=DANGER if r["bad"] else WARNING if r["warn"] else SUCCESS)
        for d in r["drives"]:
            c = Card(self._dh_body, f"{d['dev']} - {d['model'] or 'drive'}")
            c.pack(fill="x", pady=6)
            s = d["smart"]
            bits = [f"{d['size'] >> 30} GiB", d["tran"].upper() or "",
                    "HDD" if d["rotational"] else "SSD"]
            for key, fmt in (("temperature_c", "{} °C"), ("wear_percent", "{}% worn"),
                             ("spare_percent", "{}% spare"), ("media_errors", "{} media errors"),
                             ("power_on_hours", "{} h powered on")):
                if s.get(key) is not None:
                    bits.append(fmt.format(s[key]))
            tb.Label(c, text="   ·   ".join(b for b in bits if b)).pack(anchor="w")
            self._dh_findings(c, d["findings"])
            row = tb.Frame(c); row.pack(anchor="w", pady=(6, 0))
            self._tip(tb.Button(row, text="Short self-test", bootstyle=(SECONDARY, "outline"),
                      command=lambda dev=d["dev"]: self._dh_run(
                          f"Drives → self-test {dev}", f"selftest {dev}")),
                      "Start the drive's own short self-test (about two minutes, "
                      "the drive stays usable). The result shows in the SMART data "
                      "at the next check.").pack(side="left")
        fs = Card(self._dh_body, "Filesystems")
        fs.pack(fill="x", pady=6)
        for m in r["filesystems"]:
            box = tb.Frame(fs); box.pack(fill="x", pady=(4, 2))
            head = tb.Frame(box); head.pack(fill="x")
            tb.Label(head, text=m["mount"], width=22, anchor="w",
                     font=("Sans", 10, "bold")).pack(side="left")
            tb.Label(head, text=f"{m['fstype']} · {m['dev']} · {m['used_pct']}% used · "
                                f"{_size(m['free'])} free", bootstyle=SECONDARY).pack(side="left")
            if m["fstype"] == "btrfs":
                self._tip(tb.Button(head, text="Start scrub", bootstyle=(INFO, "outline"),
                          command=lambda mt=m["mount"]: self._dh_run(
                              f"Drives → scrub {mt}", f"scrub '{mt}'")),
                          "Read every block and verify its checksum, in the "
                          "background while the system stays usable."
                          ).pack(side="left", padx=(10, 0))
            if m["fstype"] in ("ntfs", "ntfs3", "fuseblk"):
                self._tip(tb.Button(head, text="Repair NTFS", bootstyle=(WARNING, "outline"),
                          command=lambda dev=m["dev"], mt=m["mount"]: self._dh_ntfs(dev, mt)),
                          "Unmount, run ntfsfix (clears the dirty flag, fixes basic "
                          "inconsistencies), mount again. Close Steam and anything "
                          "using the drive first."
                          ).pack(side="left", padx=(10, 0))
            self._dh_findings(box, m["findings"])
        for n in r["ntfs_unmounted"]:
            box = tb.Frame(fs); box.pack(fill="x", pady=(4, 2))
            head = tb.Frame(box); head.pack(fill="x")
            tb.Label(head, text=f"{n['dev']}  {n['label']}", width=22, anchor="w",
                     font=("Sans", 10, "bold")).pack(side="left")
            tb.Label(head, text="NTFS · not mounted", bootstyle=SECONDARY).pack(side="left")
            tb.Button(head, text="Repair NTFS", bootstyle=(WARNING, "outline"),
                      command=lambda dev=n["dev"]: self._dh_ntfs(dev, "")
                      ).pack(side="left", padx=(10, 0))
            self._dh_findings(box, n["findings"])
        kl = Card(self._dh_body, "Kernel log (this boot)")
        kl.pack(fill="x", pady=6)
        if not r["kernel_log"]:
            tb.Label(kl, text="✓  no I/O or filesystem errors logged since boot",
                     bootstyle=SUCCESS).pack(anchor="w")
        for k in r["kernel_log"]:
            tb.Label(kl, text=f"{_MARK[k['level']]}  {k['label']} - {k['count']} line(s)",
                     bootstyle=_STYLE[k["level"]]).pack(anchor="w")
            for ln in k["last"]:
                tb.Label(kl, text="      " + ln[:170], bootstyle=SECONDARY).pack(anchor="w")
        if r["steam"]:
            sl = Card(self._dh_body, "Steam libraries")
            sl.pack(fill="x", pady=6)
            for s in r["steam"]:
                tb.Label(sl, text=s["path"], font=("Sans", 10, "bold")).pack(anchor="w")
                self._dh_findings(sl, s["findings"])

    def _dh_run(self, desc: str, args: str):
        self._run_stream(desc, self._dh_cmd(args), tag="Drives")
        self.root.after(4000, self._dh_check)

    def _dh_fix_all(self):
        plan = getattr(self, "_dh_plan", [])
        auto = [p["text"] for p in plan if p["action"] != "manual"]
        manual = [p["text"] for p in plan if p["action"] == "manual"]
        if not auto:
            return
        msg = "This will:\n\n" + "\n".join(f"  - {t}" for t in auto)
        if manual:
            msg += "\n\nNot fixable from here:\n\n" + "\n".join(f"  - {t}" for t in manual)
        if any(p["action"] == "ntfs-repair" for p in plan):
            msg += ("\n\nAn NTFS repair unmounts that drive for a moment and is skipped "
                    "if something is using it.")
        msg += "\n\nGo ahead?"
        if not messagebox.askyesno("Fix issues on all drives", msg):
            return
        self._dh_run("Drives - fix issues on all drives", f"fix-all --user {self.user}")

    def _dh_ntfs(self, dev: str, mount: str):
        if not messagebox.askyesno(
                "Repair NTFS",
                f"Run ntfsfix on {dev}?\n\n"
                + (f"{mount} is unmounted first and mounted again afterwards - close "
                   f"Steam, games and file managers using it, or the unmount is "
                   f"refused.\n\n" if mount else "")
                + "ntfsfix clears the dirty flag and fixes basic inconsistencies. It "
                  "is not a full check: for real damage run chkdsk /f from Windows."):
            return
        self._dh_run(f"Drives → repair NTFS {dev}", f"ntfs-repair {dev}")
