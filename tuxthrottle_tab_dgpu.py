"""Game Tools box: force individual Steam games onto the dedicated GPU, on
top of whatever their Launch Options say. Backed by
tuxthrottle_mangohud_games.py (`apply --plan` with dgpu_on / dgpu_off)."""
import base64
import json
import threading
import tkinter as tk

import ttkbootstrap as tb
from ttkbootstrap.constants import INFO, SECONDARY, SUCCESS, WARNING

from tuxthrottle_gui_widgets import Card
from tuxthrottle_items import run_cmd3

_ROWS_SHOWN = 40


class DgpuBoxMixin:
    def _dg_helper(self, args: str) -> str:
        return self._user_py("tuxthrottle_mangohud_games.py", args)

    def _build_dgpu_box(self, parent):
        lf = Card(parent, "Dedicated GPU per game")
        lf.pack(fill="x", pady=6)
        tb.Label(lf, bootstyle=SECONDARY, wraplength=1100, justify="left", text=(
            "Force single Steam games onto the dedicated GPU, whatever their Launch "
            "Options say. Flip as many as you like, then press Apply once. A game "
            "with the `mangohud` hook picks the change up at its next launch; for "
            "the others Apply edits their Launch Options, which needs Steam closed "
            "once.")).pack(anchor="w")
        top = tb.Frame(lf); top.pack(anchor="w", fill="x", pady=(8, 4))
        tb.Label(top, text="Filter:").pack(side="left")
        self._dg_filter = tk.StringVar()
        fe = tb.Entry(top, textvariable=self._dg_filter, width=28)
        fe.pack(side="left", padx=(4, 10))
        fe.bind("<KeyRelease>", lambda _e: self._dg_render())
        self._dg_only_off = tk.BooleanVar(value=False)
        tb.Checkbutton(top, text="only games not on the dedicated GPU",
                       variable=self._dg_only_off, bootstyle="round-toggle",
                       command=self._dg_render).pack(side="left")
        self._tip(tb.Button(top, text="↻ Reload", bootstyle=(INFO, "outline"),
                  command=self._dg_reload),
                  "Re-read the installed Steam games and their Launch Options."
                  ).pack(side="left", padx=(12, 0))
        arow = tb.Frame(lf); arow.pack(anchor="w", fill="x", pady=(2, 2))
        self._dg_pending: dict = {}
        self._dg_apply_btn = tb.Button(arow, text="Apply changes", bootstyle=SUCCESS,
                                       state="disabled", command=self._dg_apply)
        self._dg_apply_btn.pack(side="left")
        self._dg_discard_btn = tb.Button(arow, text="Discard", state="disabled",
                                         bootstyle=(SECONDARY, "outline"),
                                         command=self._dg_discard)
        self._dg_discard_btn.pack(side="left", padx=(8, 0))
        self._dg_status = tb.Label(lf, text="", bootstyle=SECONDARY)
        self._dg_status.pack(anchor="w", pady=(2, 4))
        self._dg_list = tb.Frame(lf)
        self._dg_list.pack(fill="x")
        self._dg_games: list = []
        self.root.after(5400, self._dg_reload)       # after the other Game Tools pollers

    def _dg_reload(self):
        self._dg_status.configure(text="loading games...", bootstyle=SECONDARY)
        self._dg_result = None
        threading.Thread(target=self._dg_worker, daemon=True).start()
        self.root.after(300, self._dg_poll)

    def _dg_worker(self):
        try:
            _ok, _rc, out = run_cmd3(self._dg_helper("list --json"), timeout=40)
            rows = json.loads(out[out.index("["):out.rindex("]") + 1])
            self._dg_result = [r for r in rows if r["id"].startswith("steam:")]
        except (ValueError, OSError):
            self._dg_result = "error"

    def _dg_poll(self, tries: int = 0):
        r = getattr(self, "_dg_result", None)
        if r is None:
            if tries < 150:
                self.root.after(300, lambda: self._dg_poll(tries + 1))
            return
        try:
            if r == "error":
                self._dg_status.configure(text="could not read the Steam library",
                                          bootstyle=WARNING)
                return
            self._dg_games = r
            self._dg_render()
        except tk.TclError:
            pass

    def _dg_render(self):
        for w in self._dg_list.winfo_children():
            w.destroy()
        needle = self._dg_filter.get().strip().lower()
        games = [g for g in self._dg_games
                 if (not needle or needle in g["name"].lower() or needle == g["appid"])
                 and (not g["dgpu"] or not self._dg_only_off.get())]
        on = sum(1 for g in self._dg_games if g["dgpu"])
        waiting = sum(1 for g in self._dg_games if g["dgpu"] != g.get("dgpu_active", g["dgpu"]))
        msg = f"{len(self._dg_games)} Steam games, {on} on the dedicated GPU"
        if waiting:
            msg += f", {waiting} waiting for a Steam-closed Apply"
        if not self._dg_games:
            msg = "no installed Steam games found"
        elif len(games) > _ROWS_SHOWN:
            msg += f" - showing the first {_ROWS_SHOWN} of {len(games)}; type in the filter"
        self._dg_status.configure(text=msg, bootstyle=SECONDARY)
        for g in games[:_ROWS_SHOWN]:
            row = tb.Frame(self._dg_list); row.pack(fill="x", pady=1)
            want = self._dg_pending.get(g["appid"], g["dgpu"])
            var = tk.BooleanVar(value=want)
            tb.Checkbutton(row, variable=var, bootstyle="round-toggle",
                           command=lambda a=g["appid"], v=var, cur=g["dgpu"]:
                           self._dg_mark(a, v.get(), cur)).pack(side="left")
            tb.Label(row, text=g["name"], width=44, anchor="w").pack(side="left", padx=(4, 0))
            tb.Label(row, text=g["appid"], width=10, anchor="w",
                     bootstyle=SECONDARY).pack(side="left")
            if g["appid"] in self._dg_pending:
                tb.Label(row, text="● pending", bootstyle=WARNING).pack(side="left")
            elif g["dgpu"] != g.get("dgpu_active", g["dgpu"]):
                tb.Label(row, text="waiting - close Steam once, then Apply",
                         bootstyle=WARNING).pack(side="left")
            elif g["dgpu"] and g.get("hook"):
                tb.Label(row, text="live", bootstyle=SECONDARY).pack(side="left")

    def _dg_mark(self, appid: str, want: bool, current: bool):
        if want == current:
            self._dg_pending.pop(appid, None)
        else:
            self._dg_pending[appid] = want
        self._dg_update()
        self._dg_render()

    def _dg_update(self):
        n = len(self._dg_pending)
        self._dg_apply_btn.configure(text=f"Apply changes ({n})" if n else "Apply changes",
                                     state="normal" if n else "disabled")
        self._dg_discard_btn.configure(state="normal" if n else "disabled")

    def _dg_discard(self):
        self._dg_pending.clear()
        self._dg_update()
        self._dg_render()

    def _dg_apply(self):
        if not self._dg_pending:
            return
        plan = {"on": {}, "off": [],
                "dgpu_on": [a for a, v in self._dg_pending.items() if v],
                "dgpu_off": [a for a, v in self._dg_pending.items() if not v]}
        blob = base64.b64encode(json.dumps(plan).encode()).decode()
        n = len(self._dg_pending)
        self._dg_pending.clear()
        self._dg_update()
        self._run_stream(f"Dedicated GPU - apply {n} game change(s)",
                         self._dg_helper(f"apply --plan {blob}"), tag="Game Tools")
        self.root.after(2500, self._dg_reload)
