#!/usr/bin/env python3
"""The tweaks/apps data layer: `Item`, config-file loading, the apply ledger,
and status evaluation — everything tuxthrottle.py's tweak checkboxes are
built from, with zero Tkinter/ttkbootstrap dependency. Extracted from
tuxthrottle.py (first slice of the modular-refactor pass) because this was
the one section confirmed to have no widget/GUI-state coupling at all: it's
pure data (config/*.json) in, `Item` objects with a `state` out.

Kept independent of ttkbootstrap on purpose — `_STATE_UI`'s bootstyle names
are plain lowercase strings (confirmed against ttkbootstrap.constants:
SUCCESS="success" etc.), so this module can be imported, tested, and used
by the CLI --report/--debug paths without pulling in the GUI toolkit.
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"


def resolve_real_user() -> str:
    for var in ("PKEXEC_UID", "SUDO_UID"):
        val = os.environ.get(var)
        if val:
            try:
                return pwd.getpwuid(int(val)).pw_name
            except (KeyError, ValueError):
                pass
    for var in ("SUDO_USER", "PKEXEC_USER"):
        val = os.environ.get(var)
        if val:
            return val
    return pwd.getpwuid(os.getuid()).pw_name


def load_json(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Item:
    """A tweak or app entry, normalized for the GUI."""

    def __init__(self, item_id: str, data: dict, kind: str, user: str):
        self.id = item_id
        self.kind = kind  # "tweak" or "app"
        self.content = data.get("Content", item_id)
        self.description = data.get("Description", "")
        self.category = data.get("category", "Other")
        self.risk = data.get("risk", "safe")
        self.recommended = bool(data.get("recommended"))  # dev's curated pick
        self.requires_vendor = data.get("requires_vendor")  # "nvidia" | "amd" | None
        # optional per-board gate: a list of models/<slug> ids this entry
        # applies to. Absent = applies on every board (current default).
        rm = data.get("models")
        self.requires_models = [str(x) for x in rm] if isinstance(rm, list) else None
        self.hw_supported = True  # set by ToolkitApp after GPU detection
        self.hidden = False       # set by _apply_vendor_gate for no-op-on-this-box items

        def sub(cmd: str) -> str:
            return cmd.replace("{USER}", user).replace("{TOOLKIT_DIR}", str(BASE_DIR))

        self.check_cmd = sub(data.get("check", ""))
        # Optional: a tweak whose real effect only lands after a reboot (kernel
        # cmdline) can declare `check_pending` — true once the change is staged
        # in the bootloader but not yet live. The UI shows "Pending reboot" and
        # Apply/Presets skip it, so the user doesn't re-select and re-run it.
        self.check_pending_cmd = sub(data.get("check_pending", ""))
        if kind == "tweak":
            self.apply_cmds = [sub(c) for c in data.get("apply", [])]
            self.undo_cmds = [sub(c) for c in data.get("undo", [])]
        else:
            manager = data.get("manager", "dnf")
            # An app counts as "already here" if ANY reasonable install of it is
            # present — not just the one this entry would use. Otherwise the row
            # shows "not installed" and Apply happily adds a second, colliding
            # copy (classic: dnf `steam` on top of the Flatpak). `provides` is a
            # list of extra shell probes OR-ed into the check; for a Flatpak
            # entry we also auto-detect a system *or* per-user install of its
            # app-id, and `binary` adds a `command -v` probe.
            alts: list[str] = [sub(p) for p in data.get("provides", [])]
            if data.get("binary"):
                alts.append(f"command -v {data['binary']} >/dev/null 2>&1")
            if manager == "flatpak":
                fid = data.get("package", item_id)
                alts.append(f"flatpak info {fid} >/dev/null 2>&1")
                alts.append(sub(f"sudo -u {{USER}} flatpak info {fid} >/dev/null 2>&1"))
            if alts:
                base = self.check_cmd.strip() or "false"
                self.check_cmd = " || ".join(f"({c})" for c in [base, *alts])
            if "install" in data:
                self.apply_cmds = [sub(c) for c in data["install"]]
            elif manager == "dnf":
                self.apply_cmds = [f"dnf install -y {data.get('package', item_id)}"]
            elif manager == "flatpak":
                pkg = data.get("package", item_id)
                self.apply_cmds = [
                    "dnf install -y flatpak",
                    "flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo",
                    f"flatpak install -y flathub {pkg}",
                ]
            else:
                self.apply_cmds = []
            self.undo_cmds = []
        # live status — set by _refresh_all_status. `state` is the single
        # source of truth; `applied`/`pending` are kept as derived bools so the
        # rest of the code doesn't change.
        #   applied      check exited 0
        #   not_applied  check exited non-zero, cleanly
        #   pending      check_pending exited 0 (staged, needs reboot)
        #   error        the check command couldn't run (not our no/yes answer)
        #   drifted      we applied it OK (per the ledger) but the check now fails
        #   failed       our last apply/undo of this item failed (per the ledger)
        self.state = "unknown"
        self.check_rc: int | None = None
        self.check_out = ""
        self.ledger: dict | None = None   # {"action","ok","ts","note"} or None
        self.var = None  # tk.BooleanVar, set when widget built
        self.status_label = None
        self.checkbutton = None

    @property
    def applied(self) -> bool:
        return self.state == "applied"

    @property
    def pending(self) -> bool:
        return self.state == "pending"

    @property
    def done(self) -> bool:
        """Already in the desired state — nothing to (re-)apply."""
        return self.state in ("applied", "pending")


def run_cmd(cmd: str) -> tuple[bool, str]:
    ok, _rc, out = run_cmd3(cmd)
    return ok, out


def run_cmd3(cmd: str, timeout: int = 1800) -> tuple[bool, int, str]:
    """(ok, returncode, combined-output). rc is -1 if the command itself
    couldn't be launched (used to tell "check says no" from "check broke")."""
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, r.returncode, out
    except Exception as exc:  # noqa: BLE001
        return False, -1, str(exc)


# --------------------------------------------------------------------------- #
#  Apply ledger — what the toolkit itself has applied/undone, and how it went.
#  The per-tweak `check` command is still the source of truth for the *current*
#  state; the ledger adds "...and we're the ones who set it" / "...and our last
#  attempt failed", which is how "Reverted" and "Apply failed" are told apart
#  from a plain "Not applied".
# --------------------------------------------------------------------------- #

def _ledger_path() -> Path:
    try:
        home = Path(pwd.getpwnam(resolve_real_user()).pw_dir)
    except (KeyError, Exception):  # noqa: BLE001
        home = Path.home()
    return home / ".config" / "tuxthrottle" / "state.json"


def ledger_load() -> dict:
    try:
        d = json.loads(_ledger_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ledger_record(item_id: str, action: str, ok: bool, note: str = "") -> None:
    p = _ledger_path()
    data = ledger_load()
    data[item_id] = {"action": action, "ok": bool(ok),
                     "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note[:200]}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, sort_keys=True))
        if os.geteuid() == 0:
            pw = pwd.getpwnam(resolve_real_user())
            os.chown(p, pw.pw_uid, pw.pw_gid)
            os.chown(p.parent, pw.pw_uid, pw.pw_gid)
    except (OSError, KeyError):
        pass


# state key -> (short label, ttkbootstrap bootstyle name — a plain lowercase
# string, e.g. "success"; see module docstring for why this isn't imported
# from ttkbootstrap.constants). App items relabel applied/not_applied to
# installed/not installed at render time (that relabeling stays in the GUI).
_STATE_UI = {
    "applied":     ("Applied", "success"),
    "pending":     ("Pending reboot", "info"),
    "not_applied": ("Not applied", "secondary"),
    "error":       ("Check error", "warning"),
    "drifted":     ("Reverted", "warning"),
    "failed":      ("Apply failed", "danger"),
    "unsupported": ("unsupported", "secondary"),
    "unknown":     ("checking…", "secondary"),
}


def format_status_report(items) -> str:
    """Plain-text table of every item's state + the check that decided it +
    the toolkit's last action. Used by the GUI dialog and `--report`."""
    rows = sorted(items, key=lambda i: (i.category, i.kind, i.content.lower()))
    out = [f"TuxThrottle — status report   {time.strftime('%Y-%m-%d %H:%M:%S')}",
           "=" * 100]
    cur = None
    counts: dict[str, int] = {}
    for it in rows:
        counts[it.state] = counts.get(it.state, 0) + 1
        if it.category != cur:
            cur = it.category
            out.append(f"\n[{cur}]")
        led = it.ledger
        led_s = (f"{led['action']} {'ok' if led['ok'] else 'FAILED'} {led['ts']}"
                 + (f" — {led['note']}" if led.get("note") else "")) if led else "—"
        rc = "" if it.check_rc in (None, 0) else f" (rc {it.check_rc})"
        out.append(f"  {it.state.upper():<12} {it.content[:44]:<44} "
                   f"check{rc}: {it.check_cmd[:60] or '(none)'}")
        if it.check_out and it.state in ("error", "drifted", "failed", "not_applied"):
            out.append(f"    ↳ check said: {it.check_out.splitlines()[-1][:88]}")
        if led:
            out.append(f"    ↳ toolkit:    {led_s}")
    out.append("\n" + "-" * 100)
    out.append("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return "\n".join(out) + "\n"


def evaluate_item(item: Item, ledger: dict) -> None:
    """Run `item`'s check(s), consult the ledger, and set item.state /
    check_rc / check_out / ledger."""
    item.check_rc, item.check_out = None, ""
    item.ledger = ledger.get(item.id)
    if not item.hw_supported:
        item.state = "unsupported"
        return
    if not item.check_cmd:
        item.state = "not_applied"
        return
    ok, rc, out = run_cmd3(item.check_cmd)
    item.check_rc, item.check_out = rc, out[:600]
    if ok:
        item.state = "applied"
        return
    led = item.ledger
    if rc in (-1, 127):
        item.state = "error"
    elif item.check_pending_cmd and run_cmd3(item.check_pending_cmd)[0]:
        item.state = "pending"
    elif led and led.get("action") == "apply" and led.get("ok"):
        item.state = "drifted"        # we applied it OK; something undid it
    elif led and not led.get("ok"):
        item.state = "failed"         # our last apply/undo of it errored
    else:
        item.state = "not_applied"


def load_all_items() -> list:
    """Build the Item list (with vendor gating) without a ToolkitApp — shared
    by the --report / --debug CLI paths."""
    import sensors  # local import: keeps this module importable even in a
                    # context where sensors.py's DMI probing isn't wanted
    user = resolve_real_user()
    has_nv, has_amd = sensors.has_nvidia_gpu(), sensors.has_amd_gpu()
    items = []
    for kind, fn in (("tweak", "tweaks.json"), ("app", "apps.json")):
        for iid, data in load_json(fn).items():
            it = Item(iid, data, kind, user)
            if it.requires_vendor == "nvidia" and not has_nv or it.requires_vendor == "amd" and not has_amd:
                it.hw_supported = False
            items.append(it)
    return items
