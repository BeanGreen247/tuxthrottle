"""Proton & Runtimes tab - Valve's own Steam tools (install / move to the OS
drive) and community Proton builds for every launcher (what ProtonUp-Qt
did). Backed by tuxthrottle_compat_tools.py, run as the real user."""
import json
import threading
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

import tuxthrottle_compat_tools as ct
from tuxthrottle_gui_widgets import Card
from tuxthrottle_items import run_cmd3


class ProtonTabMixin:
    def _ct_helper(self, args: str) -> str:
        return self._user_py("tuxthrottle_compat_tools.py", args)

    def _build_proton_tab(self, outer):
        frame = self._scroll_body(outer, pad=16)
        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY, text=(
            "Everything a Windows game needs to run: Valve's own Proton builds and "
            "Steam Linux Runtimes, and the community Proton builds for Steam, "
            "Heroic, Lutris and Bottles. Community builds always install under "
            "your home folder - the OS drive.")).pack(anchor="w", pady=(0, 12))

        vf = Card(frame, "Verify")
        vf.pack(fill="x", pady=6)
        vrow = tb.Frame(vf); vrow.pack(anchor="w", fill="x")
        self._tip(tb.Button(vrow, text="Verify everything", bootstyle=(INFO, "outline"),
                  command=self._ct_verify),
                  "Check every installed build and every Steam tool: the files a "
                  "launcher needs are there and executable, Steam reports the tool "
                  "as fully installed, and nothing sits on a drive that can't hold "
                  "it properly. Read-only."
                  ).pack(side="left")
        self._ct_verify_lbl = tb.Label(vrow, text="not checked yet", bootstyle=SECONDARY)
        self._ct_verify_lbl.pack(side="left", padx=(12, 0))
        tb.Label(vf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Steam and the other launchers find these on their own when they start, so "
            "anything installed or moved while one is open shows up after restarting it.")).pack(anchor="w", pady=(6, 0))
        self._ct_verify_box = tb.Frame(vf)
        self._ct_verify_box.pack(fill="x", pady=(4, 0))

        sv = Card(frame, "Steam's own tools")
        sv.pack(fill="x", pady=6)
        tb.Label(sv, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Install (queue) puts the tool straight into the Steam library on your OS "
            "drive and Steam downloads it at its next start, so queue what you want and "
            "restart Steam once. Install via Steam dialog needs no restart but you click "
            "through Steam. Move to OS drive needs Steam closed.")).pack(anchor="w")
        self._ct_steam_box = tb.Frame(sv)
        self._ct_steam_box.pack(fill="x", pady=(8, 0))

        dp = Card(frame, "Default Proton")
        dp.pack(fill="x", pady=6)
        tb.Label(dp, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Which Proton Steam uses when a game has no choice of its own. Steam's "
            "own setting only covers titles Valve has no recommendation for; for "
            "the rest Valve's pick wins, which is how a game ends up on Proton "
            "Hotfix. Tick the box to pin your choice on those games too.")).pack(anchor="w")
        drow = tb.Frame(dp); drow.pack(anchor="w", fill="x", pady=(8, 2))
        tb.Label(drow, text="Use:").pack(side="left")
        self._ct_def = tk.StringVar(value="Proton Experimental")
        self._ct_def_cb = tb.Combobox(drow, textvariable=self._ct_def, state="readonly",
                                      width=28, values=("Proton Experimental",))
        self._ct_def_cb.pack(side="left", padx=(4, 12))
        self._ct_def_all = tk.BooleanVar(value=True)
        self._tip(tb.Checkbutton(drow, text="also for installed games Steam picks a build for",
                  variable=self._ct_def_all, bootstyle="round-toggle"),
                  "Writes a per-game choice for every installed Windows game that "
                  "has none of its own or sits on Proton Hotfix. Native Linux games "
                  "and games you already forced to another build are left alone."
                  ).pack(side="left")
        self._tip(tb.Button(drow, text="Apply", bootstyle=SUCCESS, command=self._ct_set_default),
                  "Write this into Steam's own Compatibility settings. Steam has to "
                  "be closed for it; if it is running you are asked before it is "
                  "restarted.").pack(side="left", padx=(12, 0))
        self._ct_def_lbl = tb.Label(dp, text="", bootstyle=SECONDARY)
        self._ct_def_lbl.pack(anchor="w", pady=(2, 0))

        cb = Card(frame, "Community builds - Proton, Wine, DXVK, VKD3D")
        cb.pack(fill="x", pady=6)
        tb.Label(cb, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Downloaded from each project's GitHub releases, checked against the "
            "published sha512sum where the project provides one, and unpacked into "
            "the launcher's own tools folder. Steam takes Proton builds; Heroic, "
            "Lutris and Bottles also take Wine builds and the DXVK / vkd3d-proton "
            "layers.")).pack(anchor="w")
        row = tb.Frame(cb); row.pack(anchor="w", fill="x", pady=(8, 2))
        tb.Label(row, text="Launcher:").pack(side="left")
        self._ct_target = tk.StringVar(value="Steam")
        self._ct_target_cb = tb.Combobox(row, textvariable=self._ct_target, state="readonly",
                                         width=16, values=("Steam",))
        self._ct_target_cb.pack(side="left", padx=(4, 12))
        self._ct_target_cb.bind("<<ComboboxSelected>>", lambda _e: self._ct_target_changed())
        tb.Label(row, text="Build:").pack(side="left")
        self._ct_tool = tk.StringVar(value="GE-Proton")
        self._ct_tool_cb = tb.Combobox(row, textvariable=self._ct_tool, state="readonly",
                                       width=16, values=tuple(ct.TOOLS))
        self._ct_tool_cb.pack(side="left", padx=(4, 12))
        self._ct_tool_cb.bind("<<ComboboxSelected>>", lambda _e: self._ct_load_releases())
        tb.Label(row, text="Version:").pack(side="left")
        self._ct_tag = tk.StringVar(value="latest")
        self._ct_tag_cb = tb.Combobox(row, textvariable=self._ct_tag, state="readonly",
                                      width=30, values=("latest",))
        self._ct_tag_cb.pack(side="left", padx=(4, 12))
        self._tip(tb.Button(row, text="Install", bootstyle=SUCCESS, command=self._ct_install),
                  "Download, verify and unpack this build into the selected "
                  "launcher's tools folder.").pack(side="left")
        self._ct_about = tb.Label(cb, text="", bootstyle=SECONDARY, wraplength=1100,
                                  justify="left")
        self._ct_about.pack(anchor="w", pady=(2, 6))
        tb.Label(cb, text="Installed in this launcher:").pack(anchor="w")
        self._ct_inst_box = tb.Frame(cb)
        self._ct_inst_box.pack(fill="x", pady=(2, 0))

        self._ct_vmap: dict = {}          # (where, what) → verify result
        self._ct_state: dict = {}
        self._ct_rel_cache: dict = {}
        self.root.after(400, self._ct_reload)

    # ---------------------------------------------------------------- loading
    def _ct_reload(self):
        self._ct_result = None
        self._ct_tries = 0
        threading.Thread(target=self._ct_worker, daemon=True).start()
        self.root.after(300, self._ct_poll)

    def _ct_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._ct_helper("status --json"), timeout=30)
            self._ct_result = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, OSError):
            self._ct_result = {"error": True}

    def _ct_poll(self):
        r = getattr(self, "_ct_result", None)
        if r is None:
            self._ct_tries += 1
            if self._ct_tries < 120:
                self.root.after(300, self._ct_poll)
            return
        self._ct_state = r
        try:
            self._ct_render()
        except tk.TclError:
            return
        self._ct_load_releases()

    def _ct_render(self):
        for w in self._ct_steam_box.winfo_children():
            w.destroy()
        queued = [t for t in self._ct_state.get("steam_tools", [])
                  if t["installed"] and not t.get("ready", True)]
        if queued:
            bar = tb.Frame(self._ct_steam_box); bar.pack(fill="x", pady=(0, 6))
            running = self._ct_state.get("steam_running")
            tb.Label(bar, bootstyle=WARNING, text=(
                f"{len(queued)} tool(s) queued. Steam only reads its library when it "
                f"starts - " + ("restart it once to begin the downloads."
                                if running else "start it to begin the downloads."))
                ).pack(side="left")
            self._tip(tb.Button(bar, text="Restart Steam now" if running else "Start Steam",
                      bootstyle=WARNING, command=self._ct_restart_steam),
                      "Close Steam cleanly and start it again. Queue every tool you "
                      "want first - one restart picks them all up."
                      ).pack(side="left", padx=(10, 0))
        for t in self._ct_state.get("steam_tools", []):
            row = tb.Frame(self._ct_steam_box); row.pack(fill="x", pady=1)
            self._ct_mark(row, "Steam tool", t["name"], t["installed"])
            tb.Label(row, text=t["name"], width=36, anchor="w").pack(side="left")
            if not t["installed"]:
                tb.Label(row, text="not installed", width=44, anchor="w",
                         bootstyle=SECONDARY).pack(side="left")
                self._tip(tb.Button(row, text="Install (queue)", bootstyle=(SUCCESS, "outline"),
                          command=lambda a=t["appid"], n=t["name"]: self._ct_steam_install(a, n)),
                          "No dialog: queued straight into the Steam library on your OS "
                          "drive. Steam downloads it when it next starts - queue several, "
                          "then restart Steam once."
                          ).pack(side="left")
                self._tip(tb.Button(row, text="Install via Steam dialog",
                          bootstyle=(INFO, "outline"),
                          command=lambda a=t["appid"], n=t["name"]: self._ct_steam_dialog(a, n)),
                          "No restart: Steam opens its own install dialog right away "
                          "(starting Steam if it's closed). You pick the library there "
                          "- choose the one on your OS drive."
                          ).pack(side="left", padx=(6, 0))
            elif not t.get("ready", True):
                tb.Label(row, text="queued - downloads once Steam (re)starts", width=60,
                         anchor="w",
                         bootstyle=INFO).pack(side="left")
            elif t["on_os_drive"]:
                tb.Label(row, text="installed · OS drive", width=44, anchor="w",
                         bootstyle=SUCCESS).pack(side="left")
            else:
                tb.Label(row, text=f"installed · {t['library']}", width=44, anchor="w",
                         bootstyle=WARNING).pack(side="left")
                tb.Button(row, text="Move to OS drive", bootstyle=(WARNING, "outline"),
                          command=lambda a=t["appid"], n=t["name"], s=t["size"]:
                          self._ct_move(a, n, s)).pack(side="left")
        self._ct_def_names = {c["label"]: c["name"] for c in self._ct_state.get("compat_names", [])}
        labels = list(self._ct_def_names) or ["Proton Experimental"]
        self._ct_def_cb.configure(values=tuple(labels))
        if self._ct_def.get() not in labels:
            self._ct_def.set(labels[0])
        cur = self._ct_state.get("compat_default", "")
        cur_label = next((lb for lb, nm in self._ct_def_names.items() if nm == cur), cur)
        forced = self._ct_state.get("compat_forced", {})
        self._ct_def_lbl.configure(text=(
            f"Steam's default now: {cur_label or 'not set'}  -  {len(forced)} game(s) have "
            f"their own choice"))
        present = [t["name"] for t in self._ct_state.get("targets", []) if t["present"]]
        self._ct_target_cb.configure(values=tuple(present or ["Steam"]))
        if self._ct_target.get() not in present and present:
            self._ct_target.set(present[0])
        self._ct_target_changed(load=False)

    def _ct_mark(self, row, where: str, what: str, installed: bool):
        """Per-row verify badge: ✓ / ✗ from the last “Verify everything” run,
        · when that item hasn't been checked (or isn't installed)."""
        v = self._ct_vmap.get((where, what)) if installed else None
        if v is None:
            tb.Label(row, text="·", width=2, bootstyle=SECONDARY).pack(side="left")
        else:
            lbl = tb.Label(row, text="✓" if v["ok"] else "✗", width=2,
                           bootstyle=SUCCESS if v["ok"] else DANGER)
            lbl.pack(side="left")
            detail = "; ".join(v["problems"] + v["notes"])
            if detail:
                self._tip(lbl, detail)

    def _ct_cur_target(self) -> dict:
        return next((t for t in self._ct_state.get("targets", [])
                     if t["name"] == self._ct_target.get()), {})

    def _ct_target_changed(self, load: bool = True):
        tgt = self._ct_cur_target()
        tools = [k for k, v in self._ct_state.get("tools", {}).items()
                 if tgt.get("launcher") in v["launchers"]] or list(ct.TOOLS)
        self._ct_tool_cb.configure(values=tuple(tools))
        if self._ct_tool.get() not in tools:
            self._ct_tool.set(tools[0])
        for w in self._ct_inst_box.winfo_children():
            w.destroy()
        names = tgt.get("installed", [])
        if not names:
            tb.Label(self._ct_inst_box, text="nothing yet", bootstyle=SECONDARY).pack(anchor="w")
        for it in names:
            n, kind = it["name"], it["kind"]
            row = tb.Frame(self._ct_inst_box); row.pack(fill="x", pady=1)
            self._ct_mark(row, tgt.get("name", ""), n, True)
            tb.Label(row, text=n, width=44, anchor="w").pack(side="left")
            tb.Label(row, text=kind, width=8, anchor="w", bootstyle=SECONDARY).pack(side="left")
            tb.Button(row, text="Remove", bootstyle=(DANGER, "outline"),
                      command=lambda x=n, k=kind: self._ct_remove(x, k)).pack(side="left")
        if load:
            self._ct_load_releases()

    def _ct_load_releases(self):
        tool = self._ct_tool.get()
        about = self._ct_state.get("tools", {}).get(tool, {}).get("about", "")
        self._ct_about.configure(text=about)
        if tool in self._ct_rel_cache:
            self._ct_show_releases(tool)
            return
        self._ct_tag_cb.configure(values=("latest",))
        self._ct_tag.set("latest")
        self._ct_rel_pending = tool
        threading.Thread(target=self._ct_rel_worker, args=(tool,), daemon=True).start()
        self.root.after(400, self._ct_rel_poll)

    def _ct_rel_worker(self, tool: str):
        try:
            _ok, _rc, out = run_cmd3(self._ct_helper(f"releases {tool} --json"), timeout=40)
            self._ct_rel_cache[tool] = json.loads(out[out.index("["):out.rindex("]") + 1])
        except (ValueError, OSError):
            self._ct_rel_cache[tool] = []

    def _ct_rel_poll(self, tries: int = 0):
        tool = getattr(self, "_ct_rel_pending", "")
        if tool not in self._ct_rel_cache:
            if tries < 120:
                self.root.after(400, lambda: self._ct_rel_poll(tries + 1))
            return
        if tool == self._ct_tool.get():
            try:
                self._ct_show_releases(tool)
            except tk.TclError:
                pass

    def _ct_show_releases(self, tool: str):
        rels = self._ct_rel_cache.get(tool, [])
        vals = ["latest"] + [f"{r['tag']}   ({r['date']}, {r['size'] >> 20} MiB)" for r in rels]
        self._ct_tag_cb.configure(values=tuple(vals))
        if self._ct_tag.get() not in vals:
            self._ct_tag.set("latest")

    # ---------------------------------------------------------------- actions
    def _ct_run(self, desc: str, args: str):
        self._run_stream(desc, self._ct_helper(args), tag="Proton")
        self.root.after(3000, self._ct_reload)

    def _ct_install(self):
        tool, target = self._ct_tool.get(), self._ct_target.get()
        tag = self._ct_tag.get().split("   ", 1)[0]
        args = f"install {tool} --target '{target}'"
        if tag and tag != "latest":
            args += f" --tag {tag}"
        self._ct_run(f"Proton → install {tool} {tag} for {target}", args)

    def _ct_remove(self, name: str, kind: str = ""):
        target = self._ct_target.get()
        if not messagebox.askyesno(
                "Remove build",
                f"Delete {name} from {target}?\n\nA game still set to use it will "
                f"fall back to the launcher's default."):
            return
        self._ct_run(f"Proton → remove {name}",
                     f"remove '{target}' '{name}'" + (f" --kind {kind}" if kind else ""))

    def _ct_steam_install(self, appid: str, name: str):
        self._ct_run(f"Steam → install {name}", f"queue-install {appid}")

    def _ct_steam_dialog(self, appid: str, name: str):
        self._ct_run(f"Steam → install {name} (dialog)", f"steam-install {appid}")

    def _ct_move(self, appid: str, name: str, size: int):
        if not messagebox.askyesno(
                "Move to OS drive",
                f"Move {name} (~{size >> 20} MiB) into the Steam library on your OS "
                f"drive?\n\nIt is copied first and only then removed from the other "
                f"drive. Steam must be closed."):
            return
        self._ct_run(f"Steam → move {name} to the OS drive", f"move-to-os {appid}")

    # ----------------------------------------------------------------- verify
    def _ct_verify(self):
        self._ct_verify_lbl.configure(text="checking…", bootstyle=SECONDARY)
        self._ct_vres = None
        threading.Thread(target=self._ct_verify_worker, daemon=True).start()
        self.root.after(300, self._ct_verify_poll)

    def _ct_verify_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._ct_helper("verify --json"), timeout=60)
            self._ct_vres = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, OSError):
            self._ct_vres = {"items": [], "bad": -1}

    def _ct_verify_poll(self, tries: int = 0):
        r = getattr(self, "_ct_vres", None)
        if r is None:
            if tries < 240:
                self.root.after(300, lambda: self._ct_verify_poll(tries + 1))
            return
        try:
            for w in self._ct_verify_box.winfo_children():
                w.destroy()
            n = len(r["items"])
            if r["bad"] < 0:
                self._ct_verify_lbl.configure(text="could not run the check", bootstyle=DANGER)
                return
            self._ct_verify_lbl.configure(
                text=(f"all {n} OK" if not r["bad"] else
                      f"{r['bad']} of {n} have problems"),
                bootstyle=SUCCESS if not r["bad"] else DANGER)
            for i in r["items"]:
                if i["ok"] and not i["notes"]:
                    continue                    # only list what needs attention
                row = tb.Frame(self._ct_verify_box); row.pack(fill="x", pady=1)
                tb.Label(row, text=("✓ " if i["ok"] else "✗ ") + f"{i['what']}  ({i['where']})",
                         width=52, anchor="w",
                         bootstyle=SUCCESS if i["ok"] else DANGER).pack(side="left")
                tb.Label(row, text="; ".join(i["problems"] + i["notes"]), wraplength=620,
                         justify="left", bootstyle=SECONDARY).pack(side="left")
                if not i["ok"] and i.get("appid"):
                    tb.Button(row, text="Verify files in Steam", bootstyle=(WARNING, "outline"),
                              command=lambda a=i["appid"], w=i["what"]: self._ct_run(
                                  f"Steam → verify {w}", f"steam-validate {a}")
                              ).pack(side="left", padx=(8, 0))
            self._log(f"[Proton] verify: {n - r['bad']}/{n} OK")
            # refresh every row: re-read what is installed where, and badge
            # each Steam tool / build with its result
            self._ct_vmap = {(i["where"], i["what"]): i for i in r["items"]}
            self._ct_reload()
        except tk.TclError:
            pass

    def _ct_restart_steam(self):
        if self._ct_state.get("steam_running") and not messagebox.askyesno(
                "Restart Steam",
                "Close Steam and start it again now?\n\nA running game or an active "
                "download is interrupted. The queued tools start downloading as soon "
                "as Steam is back."):
            return
        self._run_stream("Steam → restart to pick up queued tools",
                         self._ct_helper("restart-steam"), tag="Proton")
        self.root.after(20000, self._ct_reload)

    def _ct_set_default(self):
        label = self._ct_def.get()
        tool = getattr(self, "_ct_def_names", {}).get(label)
        if not tool:
            messagebox.showinfo("Default Proton", f"{label} is not installed - install it "
                                                  f"above first.")
            return
        args = f"set-default {tool}" + (" --all-games" if self._ct_def_all.get() else "")
        if self._ct_state.get("steam_running"):
            if not messagebox.askyesno(
                    "Default Proton",
                    f"Make {label} the default"
                    + (" for every installed Windows game without its own choice"
                       if self._ct_def_all.get() else "") + "?\n\n"
                    "Steam is running and rewrites this setting when it exits, so it "
                    "has to be restarted for the change to stick. A running game or "
                    "download is interrupted. Restart Steam now?"):
                return
            args += " --restart-steam"
        self._run_stream(f"Proton - default {label}", self._ct_helper(args), tag="Proton")
        self.root.after(20000, self._ct_reload)
