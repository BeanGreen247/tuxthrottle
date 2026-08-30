#!/usr/bin/env python3
"""TuxThrottle profiles + snapshots — stdlib only, no GUI deps.

A **profile** is a named full-state bundle: platform_profile + CPU TDP
(ryzenadj) + battery charge limit + NVIDIA power limit + fan curve +
AC/battery auto-switch + (optionally) hybrid-GPU mode + keyboard colour.

A **snapshot** is the same shape, auto-captured *before* anything risky
(applying a profile, rolling back, or the GUI's "Apply Selected" tweak run)
so there is always something to roll back to. Snapshots live in
`~/.config/tuxthrottle/snapshots/` and are pruned to the newest `KEEP`.

CLI:
  tuxthrottle_profiles.py capture  <name>     # save current state as a profile
  tuxthrottle_profiles.py apply    <name>     # snapshot, then apply the profile
  tuxthrottle_profiles.py list
  tuxthrottle_profiles.py show     <name>
  tuxthrottle_profiles.py delete   <name>
  tuxthrottle_profiles.py snapshot [label]    # just capture a rollback point
  tuxthrottle_profiles.py snapshots           # list them, newest first
  tuxthrottle_profiles.py rollback [last|<file>]
  tuxthrottle_profiles.py reassert            # re-apply the last applied state
                                              # (used by the resume hook)

`apply` / `rollback` / `reassert` need root for the hardware writes.
`--user NAME` / $SUDO_USER locates the config dir in that user's home.
`--with-gpu-mode` also switches hybrid graphics (off by default — it needs
a logout and most callers don't want a profile switch to force one).
"""
import argparse
import json
import os
import pwd
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sensors  # noqa: E402

KEEP_SNAPSHOTS = 20


# --------------------------------------------------------------------------- #
#  paths
# --------------------------------------------------------------------------- #

def _real_user(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for env in ("SUDO_USER", "PKEXEC_USER"):
        if os.environ.get(env):
            return os.environ[env]
    for env in ("PKEXEC_UID", "SUDO_UID"):
        if os.environ.get(env):
            try:
                return pwd.getpwuid(int(os.environ[env])).pw_name
            except (KeyError, ValueError):
                pass
    return pwd.getpwuid(os.getuid()).pw_name


def _config_dir(user: str | None) -> Path:
    try:
        home = Path(pwd.getpwnam(_real_user(user)).pw_dir)
    except KeyError:
        home = Path.home()
    return home / ".config" / "tuxthrottle"


def profiles_dir(user=None) -> Path:
    return _config_dir(user) / "profiles"


def snapshots_dir(user=None) -> Path:
    return _config_dir(user) / "snapshots"


def _chown_tree(path: Path, user: str | None) -> None:
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(_real_user(user))
    except KeyError:
        return
    for p in (path, *path.parents):
        try:
            if str(p).startswith(str(_config_dir(user).parent)):
                os.chown(p, pw.pw_uid, pw.pw_gid)
        except OSError:
            break


def _write_json(path: Path, data: dict, user: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    _chown_tree(path, user)


def _read_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
#  capture / apply
# --------------------------------------------------------------------------- #

def capture_state(user=None) -> dict:
    """Read every live setting into a profile dict. Missing/unsupported bits
    are simply left out."""
    st: dict = {"captured": time.strftime("%Y-%m-%d %H:%M:%S")}

    pp = sensors.get_platform_profile()
    if pp:
        st["platform_profile"] = pp

    tdp = sensors.read_ryzenadj_info() or {}
    if tdp.get("stapm_limit"):
        st["tdp"] = {"stapm": round(tdp["stapm_limit"]),
                     "fast": round(tdp.get("fast_limit", tdp["stapm_limit"])),
                     "slow": round(tdp.get("slow_limit", tdp["stapm_limit"]))}

    bat = sensors.battery_charge_limit_info()
    if bat.get("current") is not None:
        st["battery"] = {"percent": int(bat["current"])}

    nvpl = sensors.nvidia_power_limit_info()
    if nvpl and nvpl.get("supported") and nvpl.get("current"):
        st["nvpl"] = {"watts": int(nvpl["current"])}

    gm = sensors.gpu_mode_get()
    if gm:
        st["gpu_mode"] = gm

    powerd = _read_json(_config_dir(user) / "powerd.json")
    for k in ("fan_curve", "autoswitch", "game_profiles"):
        if isinstance(powerd.get(k), dict):
            st[k] = powerd[k]

    kbd = _read_json(_config_dir(user) / "kbd.json")
    if kbd:
        st["kbd"] = kbd

    return st


def apply_state(state: dict, user=None, with_gpu_mode: bool = False) -> list[dict]:
    """Apply each setting present in `state`. Returns one result per key:
    {'key', 'ok', 'msg'}."""
    results: list[dict] = []

    def rec(key, ok, msg=""):
        results.append({"key": key, "ok": bool(ok), "msg": msg})

    if "platform_profile" in state:
        ok, err = sensors.set_platform_profile(state["platform_profile"])
        rec("platform_profile", ok, err)

    if "tdp" in state:
        t = state["tdp"]
        ok, err = sensors.set_ryzenadj_limits(
            fast_w=t.get("fast"), slow_w=t.get("slow"), stapm_w=t.get("stapm"))
        rec("tdp", ok, err)

    if "battery" in state:
        ok, err = sensors.set_battery_charge_limit(state["battery"]["percent"])
        rec("battery", ok, err)

    if "nvpl" in state:
        info = sensors.nvidia_power_limit_info()
        if info and not info.get("supported"):
            rec("nvpl", True, "skipped — firmware-locked on this GPU")
        else:
            ok, err = sensors.set_nvidia_power_limit(state["nvpl"]["watts"])
            rec("nvpl", ok, err)

    if with_gpu_mode and state.get("gpu_mode") and state["gpu_mode"] != sensors.gpu_mode_get():
        ok, err = sensors.gpu_mode_set(state["gpu_mode"])
        rec("gpu_mode", ok, err + " (log out to apply)" if ok else err)

    # fan curve / auto-switch / game-profile map are owned by the daemon —
    # merge them into powerd.json and let it re-read on mtime change.
    powerd_keys = {k: state[k] for k in ("fan_curve", "autoswitch", "game_profiles")
                   if k in state}
    if powerd_keys:
        pd = _read_json(_config_dir(user) / "powerd.json")
        pd.update(powerd_keys)
        _write_json(_config_dir(user) / "powerd.json", pd, user)
        rec("powerd", True, ", ".join(powerd_keys))

    if "kbd" in state:
        _write_json(_config_dir(user) / "kbd.json", state["kbd"], user)
        rec("kbd", True, "saved (apply via the Keyboard tab / boot service)")

    # remember what we applied so `reassert` (resume hook) can redo it
    _write_json(_config_dir(user) / "active_state.json",
                {**state, "applied": time.strftime("%Y-%m-%d %H:%M:%S")}, user)
    return results


# --------------------------------------------------------------------------- #
#  profiles
# --------------------------------------------------------------------------- #

def _safe_name(name: str) -> str:
    # no '.', '/' or '\' — the result is interpolated straight into a file path,
    # so this also blocks '..' traversal from a crafted profile name.
    cleaned = "".join(c for c in name if c.isalnum() or c in "-_ ").strip()
    return cleaned or "unnamed"


def list_profiles(user=None) -> list[str]:
    d = profiles_dir(user)
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def load_profile(name: str, user=None) -> dict:
    return _read_json(profiles_dir(user) / f"{_safe_name(name)}.json")


def save_profile(name: str, state: dict, user=None) -> Path:
    p = profiles_dir(user) / f"{_safe_name(name)}.json"
    _write_json(p, state, user)
    return p


def delete_profile(name: str, user=None) -> bool:
    p = profiles_dir(user) / f"{_safe_name(name)}.json"
    try:
        p.unlink()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
#  snapshots
# --------------------------------------------------------------------------- #

def snapshot(user=None, label: str = "auto") -> Path:
    d = snapshots_dir(user)
    ts = time.strftime("%Y%m%d-%H%M%S")
    p = d / f"{ts}_{_safe_name(label).replace(' ', '-')}.json"
    _write_json(p, {**capture_state(user), "label": label}, user)
    _prune_snapshots(user)
    return p


def _prune_snapshots(user=None, keep: int | None = None) -> None:
    keep = KEEP_SNAPSHOTS if keep is None else keep   # read at call time (tests patch it)
    snaps = sorted(snapshots_dir(user).glob("*.json"))
    for old in snaps[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def list_snapshots(user=None) -> list[dict]:
    out = []
    for p in sorted(snapshots_dir(user).glob("*.json"), reverse=True):
        d = _read_json(p)
        out.append({"path": str(p), "name": p.name,
                    "label": d.get("label", "?"),
                    "captured": d.get("captured", "?")})
    return out


def rollback(target: str, user=None, with_gpu_mode: bool = False) -> list[dict]:
    if target in ("", "last", "latest"):
        snaps = sorted(snapshots_dir(user).glob("*.json"))
        if not snaps:
            return [{"key": "-", "ok": False, "msg": "no snapshots to roll back to"}]
        path = snaps[-1]
    else:
        path = Path(target)
        if not path.is_absolute():
            path = snapshots_dir(user) / target
    state = _read_json(path)
    if not state:
        return [{"key": "-", "ok": False, "msg": f"unreadable snapshot: {path}"}]
    # snapshot the *current* state first, so a rollback is itself undoable
    snapshot(user, label="pre-rollback")
    return apply_state(state, user, with_gpu_mode=with_gpu_mode)


def reassert(user=None) -> list[dict]:
    """Re-apply the last state we applied — for the systemd-sleep resume hook.
    No-op (not an error) if nothing has been applied yet."""
    st = _read_json(_config_dir(user) / "active_state.json")
    if not st:
        return [{"key": "-", "ok": True, "msg": "nothing to reassert"}]
    return apply_state(st, user)


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #

def _print_results(rows: list[dict]) -> int:
    bad = 0
    for r in rows:
        mark = "ok " if r["ok"] else "ERR"
        if not r["ok"]:
            bad += 1
        print(f"  [{mark}] {r['key']}" + (f" — {r['msg']}" if r["msg"] else ""))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tuxthrottle_profiles", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user")
    ap.add_argument("--with-gpu-mode", action="store_true")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("capture", "apply", "show", "delete"):
        sp = sub.add_parser(name)
        sp.add_argument("name")
    sub.add_parser("list")
    sp = sub.add_parser("snapshot")
    sp.add_argument("label", nargs="?", default="manual")
    sub.add_parser("snapshots")
    sp = sub.add_parser("rollback")
    sp.add_argument("target", nargs="?", default="last")
    sub.add_parser("reassert")

    a = ap.parse_args()
    u = a.user

    if a.cmd == "list":
        names = list_profiles(u)
        print(json.dumps(names) if a.json else ("\n".join(names) or "(no profiles)"))
        return 0
    if a.cmd == "capture":
        st = capture_state(u)
        save_profile(a.name, st, u)
        print(json.dumps(st, indent=2) if a.json else f"saved profile '{a.name}':")
        if not a.json:
            _print_results([{"key": k, "ok": True, "msg": str(v)}
                            for k, v in st.items() if k != "captured"])
        return 0
    if a.cmd == "show":
        st = load_profile(a.name, u)
        print(json.dumps(st, indent=2, sort_keys=True) if st else "(no such profile)")
        return 0 if st else 1
    if a.cmd == "delete":
        return 0 if delete_profile(a.name, u) else 1
    if a.cmd == "apply":
        st = load_profile(a.name, u)
        if not st:
            print(f"no such profile: {a.name}", file=sys.stderr)
            return 1
        snapshot(u, label=f"pre-apply-{a.name}")
        rows = apply_state(st, u, with_gpu_mode=a.with_gpu_mode)
        if a.json:
            print(json.dumps(rows, indent=2))
            return 1 if any(not r["ok"] for r in rows) else 0
        print(f"applied profile '{a.name}':")
        return _print_results(rows)
    if a.cmd == "snapshot":
        p = snapshot(u, label=a.label)
        print(str(p))
        return 0
    if a.cmd == "snapshots":
        snaps = list_snapshots(u)
        print(json.dumps(snaps, indent=2) if a.json else
              ("\n".join(f"{s['captured']}  {s['label']:16}  {s['name']}"
                         for s in snaps) or "(no snapshots)"))
        return 0
    if a.cmd == "rollback":
        rows = rollback(a.target, u, with_gpu_mode=a.with_gpu_mode)
        if a.json:
            print(json.dumps(rows, indent=2))
            return 1 if any(not r["ok"] for r in rows) else 0
        print(f"rolled back to '{a.target}':")
        return _print_results(rows)
    if a.cmd == "reassert":
        return _print_results(reassert(u))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
