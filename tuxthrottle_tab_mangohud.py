"""MangoHud tab - everything MangoHud lives here: the session-wide switch
(kept OFF), per-game enable with a collapsible per-game settings panel, and
the overlay editor (moved from Game Tools). Backed by
tuxthrottle_mangohud_games.py, run as the real user."""
import base64
import json
import threading
import tkinter as tk
from tkinter import messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import DANGER, INFO, SECONDARY, SUCCESS, WARNING

import tuxthrottle_mangohud_games as mhg
from tuxthrottle_gui_widgets import Card
from tuxthrottle_items import BASE_DIR, run_cmd3

_DETAIL_LABELS = {"global": "same as the global layout", "fps_only": "FPS only",
                  "full": "everything (full)"}
_ROWS_SHOWN = 60          # widget rows built per filter - keeps the tab snappy


class MangoHudTabMixin:
    def _mhg_helper(self, args: str) -> str:
        return self._user_py("tuxthrottle_mangohud_games.py", args)

    def _build_mangohud_tab(self, outer):
        frame = self._scroll_body(outer, pad=16)
        tb.Label(frame, wraplength=1100, justify="left", bootstyle=SECONDARY, text=(
            "MangoHud is opt-in per game: nothing shows the overlay unless you "
            "switch it on for that game below. The session-wide enable "
            "(MANGOHUD=1 for every Vulkan game) stays off.")
            ).pack(anchor="w", pady=(0, 12))

        gl = Card(frame, "Session-wide overlay")
        gl.pack(fill="x", pady=6)
        grow = tb.Frame(gl); grow.pack(anchor="w", fill="x")
        self._mhg_global_lbl = tb.Label(grow, text="checking…", bootstyle=SECONDARY)
        self._mhg_global_lbl.pack(side="left")
        self._tip(tb.Button(grow, text="Turn off session-wide MangoHud",
                  bootstyle=(WARNING, "outline"), command=self._mhg_global_off),
                  "Remove MANGOHUD=1 (and any libMangoHud LD_PRELOAD) from your "
                  "~/.config/environment.d files, so only games you enable below "
                  "show the overlay. Takes full effect at next login."
                  ).pack(side="left", padx=(12, 0))

        gt = Card(frame, "Live per-game switch")
        gt.pack(fill="x", pady=6)
        tb.Label(gt, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "A gate at /usr/local/bin/mangohud that only loads the overlay for games "
            "switched on below. With it a switch applies at the game's next launch and "
            "Steam stays open; without it every change rewrites Steam's Launch Options.")
            ).pack(anchor="w")
        trow = tb.Frame(gt); trow.pack(anchor="w", fill="x", pady=(6, 0))
        self._mhg_gate_lbl = tb.Label(trow, text="checking…", bootstyle=SECONDARY)
        self._mhg_gate_lbl.pack(side="left")
        tb.Button(trow, text="Install", bootstyle=(SUCCESS, "outline"),
                  command=self._mhg_gate_install).pack(side="left", padx=(12, 0))
        tb.Button(trow, text="Remove", bootstyle=(SECONDARY, "outline"),
                  command=self._mhg_gate_remove).pack(side="left", padx=(6, 0))
        self._tip(tb.Button(trow, text="Add the hook to every game (one-time)",
                  bootstyle=(INFO, "outline"), command=self._mhg_hook_all),
                  "Put `mangohud` into every game's Launch Options once. Behind "
                  "the gate it does nothing until a game is switched on, and "
                  "afterwards no switch ever needs Steam closed again. This one "
                  "write does need Steam closed; every localconfig.vdf is backed up."
                  ).pack(side="left", padx=(16, 0))

        pg = Card(frame, "Per-game MangoHud")
        pg.pack(fill="x", pady=6)
        tb.Label(pg, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Every installed game from Steam, Heroic and Lutris. Flip as many switches as "
            "you like, then press Apply changes once. Settings opens a game's own profile, "
            "which overrides the global layout while it is on. A game with an anti-cheat "
            "is locked until you tick force on. “No hook” means Apply has to add "
            "`mangohud` to that game's Launch Options, which needs Steam closed once.")
            ).pack(anchor="w")
        top = tb.Frame(pg); top.pack(anchor="w", fill="x", pady=(8, 4))
        tb.Label(top, text="Filter:").pack(side="left")
        self._mhg_filter = tk.StringVar()
        fe = tb.Entry(top, textvariable=self._mhg_filter, width=28)
        fe.pack(side="left", padx=(4, 10))
        fe.bind("<KeyRelease>", lambda _e: self._mhg_render())
        self._mhg_only_on = tk.BooleanVar(value=False)
        tb.Checkbutton(top, text="only games with MangoHud on", variable=self._mhg_only_on,
                       bootstyle="round-toggle", command=self._mhg_render).pack(side="left")
        self._tip(tb.Button(top, text="↻ Reload", bootstyle=(INFO, "outline"),
                  command=self._mhg_reload),
                  "Re-read the installed games of every launcher and rescan for "
                  "anti-cheat."
                  ).pack(side="left", padx=(12, 0))
        self._tip(tb.Button(top, text="Remove MangoHud from every game",
                  bootstyle=(DANGER, "outline"), command=self._mhg_disable_all),
                  "Switch the overlay off in every game at once. With the live "
                  "switch installed this is instant; without it, it strips the "
                  "`mangohud` wrapper from every game's Launch Options (Steam "
                  "closed)."
                  ).pack(side="left", padx=(8, 0))
        arow = tb.Frame(pg); arow.pack(anchor="w", fill="x", pady=(2, 2))
        self._mhg_pending: dict = {}
        self._mhg_apply_btn = tb.Button(arow, text="Apply changes", bootstyle=SUCCESS,
                                        state="disabled", command=self._mhg_apply_pending)
        self._tip(self._mhg_apply_btn,
                  "Apply every switch and settings change you made below in one "
                  "go. Flip as many games as you like first - nothing is written "
                  "until you press this.").pack(side="left")
        self._mhg_discard_btn = tb.Button(arow, text="Discard", state="disabled",
                                          bootstyle=(SECONDARY, "outline"),
                                          command=self._mhg_discard)
        self._mhg_discard_btn.pack(side="left", padx=(8, 0))
        self._mhg_status = tb.Label(pg, text="", bootstyle=SECONDARY)
        self._mhg_status.pack(anchor="w", pady=(2, 4))
        self._mhg_list = tb.Frame(pg)
        self._mhg_list.pack(fill="x")
        self._mhg_rows: list = []
        self._mhg_games: list = []

        self._build_mangohud_box(frame)          # the overlay editor (global layout)

        self.root.after(400, self._mhg_reload)

    # ---------------------------------------------------------------- loading
    def _mhg_reload(self):
        self._mhg_status.configure(text="loading games…", bootstyle=SECONDARY)
        self._mhg_result = None
        self._mhg_tries = 0
        threading.Thread(target=self._mhg_worker, daemon=True).start()
        self.root.after(300, self._mhg_poll)

    def _mhg_worker(self):
        res = {"games": [], "global": [], "gate": mhg.gate_installed(),
               "gate_current": mhg.gate_current()}
        try:
            _ok, _rc, out = run_cmd3(self._mhg_helper("list --json"), timeout=30)
            res["games"] = json.loads(out[out.index("["):out.rindex("]") + 1])
        except (ValueError, OSError):
            res["error"] = "could not read the Steam library"
        try:
            _ok, rc, out = run_cmd3(self._mhg_helper("global-status"), timeout=15)
            res["global"] = [ln for ln in out.splitlines() if ln.startswith("/")] if rc else []
        except OSError:
            pass
        self._mhg_result = res

    def _mhg_poll(self):
        r = getattr(self, "_mhg_result", None)
        if r is None:
            self._mhg_tries += 1
            if self._mhg_tries < 120:
                self.root.after(300, self._mhg_poll)
                return
            r = {"games": [], "global": [], "error": "timed out"}
        self._mhg_games = r.get("games", [])
        try:
            if r.get("global"):
                self._mhg_global_lbl.configure(
                    text="ON for every game via " + ", ".join(r["global"]), bootstyle=DANGER)
            else:
                self._mhg_global_lbl.configure(
                    text="off - the overlay only shows in games enabled below",
                    bootstyle=SUCCESS)
        except tk.TclError:
            return
        self._mhg_gate = bool(r.get("gate"))
        self._mhg_gate_lbl.configure(
            text=("installed, but an older version - press Install to update it (the "
                  "dedicated-GPU switch on Game Tools needs the new one)"
                  if self._mhg_gate and not r.get("gate_current", True) else
                  "installed - changes apply live, Steam can stay open" if self._mhg_gate
                  else "not installed - every change needs Steam closed"),
            bootstyle=(WARNING if not self._mhg_gate or not r.get("gate_current", True)
                       else SUCCESS))
        self._mhg_error = r.get("error", "")
        self._mhg_render()

    def _mhg_render(self):
        for w in self._mhg_list.winfo_children():
            w.destroy()
        self._mhg_rows = []
        needle = self._mhg_filter.get().strip().lower()
        games = [g for g in self._mhg_games
                 if (not needle or needle in g["name"].lower() or needle == g["appid"]
                     or needle in g.get("source", "").lower()
                     or needle in g.get("anticheat", "").lower())
                 and (g["wanted"] or not self._mhg_only_on.get())]
        on = sum(1 for g in self._mhg_games if g["enabled"])
        locked = sum(1 for g in self._mhg_games if g.get("anticheat"))
        srcs = sorted({g.get("source", "Steam").split(" · ")[0] for g in self._mhg_games})
        msg = (f"{len(self._mhg_games)} installed games ({', '.join(srcs) or 'none found'}), "
               f"MangoHud on in {on}, {locked} locked by anti-cheat"
               + (f" - showing the first {_ROWS_SHOWN} of {len(games)}; type in the "
                  f"filter to narrow down" if len(games) > _ROWS_SHOWN else ""))
        if getattr(self, "_mhg_error", ""):
            msg = self._mhg_error
        self._mhg_status.configure(text=msg, bootstyle=SECONDARY)
        for g in games[:_ROWS_SHOWN]:
            self._mhg_build_row(g)

    # ------------------------------------------------------------------- rows
    # Nothing here acts on its own: a switch or a settings edit only records a
    # pending change in self._mhg_pending; “Apply changes” sends the whole
    # batch in one call (so at most one Steam config write, ever).
    def _mhg_build_row(self, g: dict):
        box = tb.Frame(self._mhg_list)
        box.pack(fill="x", pady=1)
        head = tb.Frame(box); head.pack(fill="x")
        pend = self._mhg_pending.get(g["id"])
        ac = g.get("anticheat", "")
        st = {"game": g, "open": False, "panel": None,
              "enabled": tk.BooleanVar(value=pend["on"] if pend else g["wanted"]),
              "force": tk.BooleanVar(value=pend["force"] if pend else g.get("forced", False))}
        # anti-cheat lock: the switch is greyed out until “force” is ticked
        st["switch"] = tb.Checkbutton(head, variable=st["enabled"], bootstyle="round-toggle",
                                      command=lambda s=st: self._mhg_mark(s))
        st["switch"].pack(side="left")
        if ac and not st["force"].get():
            st["switch"].configure(state="disabled")
        tb.Label(head, text=g["name"], width=40, anchor="w").pack(side="left", padx=(4, 0))
        tb.Label(head, text=f"{g.get('source', 'Steam')} · {g['appid']}", width=26, anchor="w",
                 bootstyle=SECONDARY).pack(side="left")
        st["btn"] = tb.Button(head, text="Settings ▾", bootstyle=(SECONDARY, "link"),
                              command=lambda s=st, b=box: self._mhg_toggle_panel(s, b))
        if g["id"].startswith("steam:"):
            st["btn"].pack(side="left")
        if ac:
            tb.Label(head, text=f"⚠ {ac}", bootstyle=DANGER).pack(side="left", padx=(6, 0))
            self._tip(tb.Checkbutton(head, text="force on", variable=st["force"],
                      bootstyle="round-toggle",
                      command=lambda s=st: self._mhg_force(s)),
                      f"{ac} was found in this game's folder. An overlay injected "
                      "into an anti-cheat game can get the account flagged or "
                      "banned, so the switch is locked. Tick this to unlock it "
                      "anyway - at your own risk.").pack(side="left", padx=(8, 0))
        if g.get("profile") == "on":
            tb.Label(head, text="own profile", bootstyle=INFO).pack(side="left", padx=(6, 0))
        elif g.get("profile") == "off":
            tb.Label(head, text="profile off", bootstyle=SECONDARY).pack(side="left", padx=(6, 0))
        if g["id"].startswith("steam:"):
            if g["wanted"] and not g.get("hook"):
                tb.Label(head, text="waiting for hook - close Steam once, then Apply",
                         bootstyle=WARNING).pack(side="left", padx=(6, 0))
            elif not g.get("hook"):
                tb.Label(head, text="no hook", bootstyle=SECONDARY).pack(side="left", padx=(6, 0))
        st["pend_lbl"] = tb.Label(head, text="● pending" if pend else "", bootstyle=WARNING)
        st["pend_lbl"].pack(side="left", padx=(6, 0))
        self._mhg_rows.append(st)

    def _mhg_force(self, st: dict):
        """Unlock / re-lock an anti-cheat game's switch."""
        if st["force"].get():
            st["switch"].configure(state="normal")
        else:
            st["enabled"].set(False)
            st["switch"].configure(state="disabled")
        self._mhg_mark(st)

    def _mhg_toggle_panel(self, st: dict, box):
        if st["open"]:
            st["panel"].pack_forget()
            st["open"] = False
            st["btn"].configure(text="Settings ▾")
            return
        if st["panel"] is None:
            st["panel"] = self._mhg_build_panel(st, box)
        st["panel"].pack(fill="x", padx=(34, 0), pady=(2, 8))
        st["open"] = True
        st["btn"].configure(text="Settings ▴")

    def _mhg_build_panel(self, st: dict, box):
        g = st["game"]
        pend = self._mhg_pending.get(g["id"]) or {}
        cur = pend.get("settings") or g.get("settings") or {}
        own = pend["settings"].get("own") if pend.get("settings") else g["own_settings"]
        p = tb.Frame(box)
        st["own"] = tk.BooleanVar(value=bool(own))
        st["font_size"] = tk.StringVar(value=cur.get("font_size", ""))
        st["fps_limit"] = tk.StringVar(value=cur.get("fps_limit", ""))
        r1 = tb.Frame(p); r1.pack(anchor="w", fill="x")
        self._tip(tb.Checkbutton(r1, text="use this game's own profile (overrides the "
                                          "global one)",
                  variable=st["own"], bootstyle="round-toggle",
                  command=lambda s=st: self._mhg_mark(s, settings=True)),
                  "On: this game uses its own overlay profile "
                  f"(~/.config/MangoHud/tuxthrottle-{g['appid']}.conf), which takes "
                  "priority over the global layout. Off: the profile is kept but "
                  "parked, and the game uses the global layout."
                  ).pack(side="left")
        state = {"on": "profile in force", "off": "profile saved, switched off",
                 "none": "no profile yet - starts as a copy of the global one"
                 }.get(g.get("profile", "none"), "")
        tb.Label(r1, text=f"   AppID {g['appid']}  ·  {state}", bootstyle=SECONDARY).pack(side="left")
        r2 = tb.Frame(p); r2.pack(anchor="w", fill="x", pady=(4, 0))
        self._tip(tb.Button(r2, text="Edit layout & placement ▸", bootstyle=(INFO, "outline"),
                  command=lambda s=st: self._mhg_edit_profile(s)),
                  "Open this game's profile in the overlay editor below: the same "
                  "CPU / GPU / memory detail switches, frametime graph, GPU clocks, "
                  "names and the on-screen placement picker as the global layout - "
                  "saved for this game only."
                  ).pack(side="left")
        tb.Label(r2, text="   Font size:").pack(side="left")
        ef = tb.Entry(r2, textvariable=st["font_size"], width=5)
        ef.pack(side="left", padx=(4, 12))
        tb.Label(r2, text="FPS limit:").pack(side="left")
        el = tb.Entry(r2, textvariable=st["fps_limit"], width=5)
        el.pack(side="left", padx=(4, 12))
        for w in (ef, el):
            w.bind("<KeyRelease>", lambda _e, s=st: self._mhg_mark(s, settings=True))
        tb.Label(p, bootstyle=SECONDARY, wraplength=1000, justify="left",
                 text="Launch options now: " + (g["options"] or "-")).pack(anchor="w", pady=(4, 0))
        return p

    def _mhg_edit_profile(self, st: dict):
        g = st["game"]
        st["own"].set(True)
        self._mhg_mark(st, settings=True)
        self._mh_select_profile(g["appid"], g["name"])
        self._log(f"[MangoHud] editing the profile of {g['name']} (AppID {g['appid']}) - "
                  f"set it up in the overlay editor and press Write, then Apply changes")

    # ---------------------------------------------------------------- actions
    def _mhg_settings(self, st: dict) -> dict:
        return {"own": bool(st["own"].get()),
                "font_size": st["font_size"].get().strip(),
                "fps_limit": st["fps_limit"].get().strip()}

    def _mhg_mark(self, st: dict, settings: bool = False):
        """Record this row's state as a pending change (nothing is written)."""
        aid = st["game"]["id"]
        if settings:
            st["enabled"].set(True)        # editing a game's settings implies on
        pend = self._mhg_pending.setdefault(
            aid, {"on": st["enabled"].get(), "settings": None, "force": False})
        pend["on"] = st["enabled"].get()
        pend["force"] = st["force"].get()
        if settings or (pend["settings"] is not None and "own" in st):
            pend["settings"] = self._mhg_settings(st)
        try:
            st["pend_lbl"].configure(text="● pending")
        except tk.TclError:
            pass
        self._mhg_update_apply()

    def _mhg_update_apply(self):
        n = len(self._mhg_pending)
        self._mhg_apply_btn.configure(
            text=f"Apply changes ({n})" if n else "Apply changes",
            state="normal" if n else "disabled")
        self._mhg_discard_btn.configure(state="normal" if n else "disabled")

    def _mhg_discard(self):
        self._mhg_pending.clear()
        self._mhg_update_apply()
        self._mhg_render()

    def _mhg_apply_pending(self):
        if not self._mhg_pending:
            return
        plan = {"on": {a: p["settings"] for a, p in self._mhg_pending.items() if p["on"]},
                "off": [a for a, p in self._mhg_pending.items() if not p["on"]],
                "force": [a for a, p in self._mhg_pending.items() if p["on"] and p["force"]]}
        blob = base64.b64encode(json.dumps(plan).encode()).decode()
        n = len(self._mhg_pending)
        self._mhg_pending.clear()
        self._mhg_update_apply()
        self._mhg_run(f"MangoHud → apply {n} game change(s)", f"apply --plan {blob}")

    def _mhg_run(self, desc: str, args: str):
        self._run_stream(desc, self._mhg_helper(args), tag="MangoHud")
        self.root.after(2500, self._mhg_reload)

    def _mhg_disable_all(self):
        if not messagebox.askyesno(
                "Remove MangoHud from every game",
                "Switch the MangoHud overlay off in every Steam game?"
                + ("" if getattr(self, "_mhg_gate", False) else
                   "\n\nThe live switch isn't installed, so this strips the "
                   "`mangohud` wrapper from every game's Launch Options - Steam "
                   "must be closed first. Every localconfig.vdf is backed up.")):
            return
        self._mhg_run("MangoHud → remove from every game", "disable-all")

    def _mhg_global_off(self):
        self._mhg_run("MangoHud → session-wide off", "global-off")

    def _mhg_gate_install(self):
        cmd = (f"python3 {BASE_DIR}/tuxthrottle_mangohud_games.py gate-script "
               f"> {mhg.GATE_PATH}.new && chmod 755 {mhg.GATE_PATH}.new && "
               f"mv -f {mhg.GATE_PATH}.new {mhg.GATE_PATH} && echo 'gate installed'")
        self._run_stream("MangoHud → install live per-game switch", cmd, tag="MangoHud")
        self.root.after(2500, self._mhg_reload)

    def _mhg_gate_remove(self):
        cmd = (f"if grep -q {mhg.GATE_MARK} {mhg.GATE_PATH} 2>/dev/null; then "
               f"rm -f {mhg.GATE_PATH} && echo 'gate removed'; "
               f"else echo 'no TuxThrottle gate at {mhg.GATE_PATH}'; fi")
        self._run_stream("MangoHud → remove live per-game switch", cmd, tag="MangoHud")
        self.root.after(2500, self._mhg_reload)

    def _mhg_hook_all(self):
        if not getattr(self, "_mhg_gate", False):
            messagebox.showinfo("Install the live switch first",
                                "Without the gate, `mangohud` in a game's Launch "
                                "Options turns the overlay on. Press Install first.")
            return
        if not messagebox.askyesno(
                "Add the hook to every game",
                "Add `mangohud` to every Steam game's Launch Options?\n\nBehind "
                "the gate it stays off until you switch a game on, and from then "
                "on switches apply without closing Steam. Steam must be closed "
                "for this one write; every localconfig.vdf is backed up."):
            return
        self._mhg_run("MangoHud → add the hook to every game", "hook-all")
