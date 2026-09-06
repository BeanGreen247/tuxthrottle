#!/usr/bin/env python3
"""Bulk-set Steam per-game **Launch Options** from one string.

Steam has no global launch-options field — every game keeps its own in
`userdata/<id>/config/localconfig.vdf` under
`Software / Valve / Steam / apps / <appid> / LaunchOptions`. This writes the
same string into every installed game at once (the TuxThrottle launch-options
builder's "Apply to every game" button, or standalone).

Usage:
    python3 tuxthrottle_launchopts.py list
    python3 tuxthrottle_launchopts.py set-all  '<string>'  [--only-empty] [--dry-run]
    python3 tuxthrottle_launchopts.py set-all  --b64 <base64-of-string> [--only-empty] [--dry-run]
    python3 tuxthrottle_launchopts.py clear-all [--dry-run]
    python3 tuxthrottle_launchopts.py remove-token '<token>' [--dry-run]
    python3 tuxthrottle_launchopts.py remove-token --b64 <base64-of-token> [--dry-run]

`remove-token` strips one token (e.g. a flag you bulk-applied earlier and
have since decided against) out of *whichever* game's LaunchOptions contains
it, leaving the rest of that game's string intact — unlike `set-all`, which
replaces the whole string everywhere.

Rules:
  * Steam must be **closed** — it rewrites localconfig.vdf on exit and would
    clobber the change (the script refuses while `steam` is running).
  * every localconfig.vdf is copied to `*.tuxthrottle-bak-<epoch>` first.
  * `--only-empty` skips games that already have a non-empty LaunchOptions.
  * run as your normal user, not root.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APPS_PATH = ("Software", "Valve", "Steam", "apps")  # under the file's root key


# --------------------------------------------------------------------------- #
#  VDF load/dump — use the `vdf` module when present, else a small parser
# --------------------------------------------------------------------------- #
def _load(path: Path) -> dict:
    try:
        import vdf  # type: ignore
        with path.open(encoding="utf-8", errors="replace") as fh:
            return vdf.load(fh, mapper=dict)
    except ImportError:
        return _mini_load(path.read_text(encoding="utf-8", errors="replace"))


def _dump(obj: dict, path: Path) -> None:
    try:
        import vdf  # type: ignore
        with path.open("w", encoding="utf-8") as fh:
            vdf.dump(obj, fh, pretty=True)
    except ImportError:
        path.write_text(_mini_dump(obj), encoding="utf-8")


_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t"}
_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t"}


def _mini_load(text: str) -> dict:
    """Parse Valve KeyValues into nested dicts. Handles quoted tokens with
    \\" \\\\ \\n \\t escapes and // line comments. No macro/conditional support
    (localconfig.vdf has none)."""
    i, n = 0, len(text)

    def skip_ws() -> None:
        nonlocal i
        while i < n:
            c = text[i]
            if c in " \t\r\n":
                i += 1
            elif c == "/" and i + 1 < n and text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
            else:
                break

    def token() -> str:
        nonlocal i
        skip_ws()
        if i >= n:
            raise ValueError("unexpected end of VDF")
        if text[i] == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(_UNESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1  # closing quote
            return "".join(buf)
        start = i
        while i < n and text[i] not in ' \t\r\n"{}':
            i += 1
        return text[start:i]

    def parse_block() -> dict:
        nonlocal i
        out: dict = {}
        while True:
            skip_ws()
            if i >= n or text[i] == "}":
                i += 1
                return out
            key = token()
            skip_ws()
            if i < n and text[i] == "{":
                i += 1
                out[key] = parse_block()
            else:
                out[key] = token()

    skip_ws()
    root: dict = {}
    while i < n:
        key = token()
        skip_ws()
        if i < n and text[i] == "{":
            i += 1
            root[key] = parse_block()
        elif key:
            root[key] = token()
        skip_ws()
    return root


def _esc(s: str) -> str:
    return "".join(_ESCAPES.get(c, c) for c in s)


def _mini_dump(obj: dict, depth: int = 0) -> str:
    pad = "\t" * depth
    lines = []
    for k, v in obj.items():
        if isinstance(v, dict):
            lines.append(f'{pad}"{_esc(str(k))}"')
            lines.append(f"{pad}{{")
            lines.append(_mini_dump(v, depth + 1).rstrip("\n"))
            lines.append(f"{pad}}}")
        else:
            lines.append(f'{pad}"{_esc(str(k))}"\t\t"{_esc(str(v))}"')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _steam_roots() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    cands = [home / ".steam" / "steam", home / ".steam" / "root",
             home / ".local" / "share" / "Steam",
             home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam"]
    out, seen = [], set()
    for c in cands:
        try:
            rp = c.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def find_localconfigs() -> list[Path]:
    out, seen = [], set()
    for root in _steam_roots():
        for lc in sorted(root.glob("userdata/*/config/localconfig.vdf")):
            try:
                rp = lc.resolve()
            except OSError:
                continue
            if rp not in seen:
                seen.add(rp)
                out.append(lc)
    return out


def steam_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "steam"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def _ci_get(d: dict, key: str):
    """dict.get, case-insensitively (Steam mixes 'Valve'/'valve')."""
    if key in d:
        return d[key]
    kl = key.lower()
    for k, v in d.items():
        if k.lower() == kl:
            return v
    return None


def _descend(node: dict, path) -> dict | None:
    for part in path:
        if not isinstance(node, dict):
            return None
        node = _ci_get(node, part)
    return node if isinstance(node, dict) else None


def _apps_dict(cfg: dict) -> dict | None:
    """Steam wraps the tree in a single root key ('UserLocalConfigStore'); some
    older files don't. Try under the root key, then the raw top level."""
    if isinstance(cfg, dict):
        for root in list(cfg.values()):
            got = _descend(root, APPS_PATH) if isinstance(root, dict) else None
            if got is not None:
                return got
    return _descend(cfg, APPS_PATH)


def _iter_games(apps: dict):
    for appid, entry in apps.items():
        if appid.isdigit() and isinstance(entry, dict):
            yield appid, entry


# --------------------------------------------------------------------------- #
#  commands
# --------------------------------------------------------------------------- #
def do_list() -> int:
    files = find_localconfigs()
    if not files:
        print("no localconfig.vdf found — is Steam installed for this user?")
        return 1
    for lc in files:
        cfg = _load(lc)
        apps = _apps_dict(cfg)
        acct = lc.parent.parent.name
        if not apps:
            print(f"[{acct}] no apps block in {lc}")
            continue
        rows = [(aid, _ci_get(e, "LaunchOptions") or "") for aid, e in _iter_games(apps)]
        print(f"[{acct}] {len(rows)} games in {lc}")
        for aid, opt in sorted(rows, key=lambda r: int(r[0])):
            print(f"  {aid:<8}  {opt}" if opt else f"  {aid:<8}  —")
    return 0


def _write_all(value: str, only_empty: bool, dry: bool) -> int:
    if steam_running():
        print("Steam is running — quit Steam completely first (it overwrites "
              "localconfig.vdf on exit).")
        return 2
    files = find_localconfigs()
    if not files:
        print("no localconfig.vdf found — is Steam installed for this user?")
        return 1
    total = 0
    for lc in files:
        cfg = _load(lc)
        apps = _apps_dict(cfg)
        if not apps:
            print(f"  skip {lc} (no apps block)")
            continue
        changed = 0
        for _aid, entry in _iter_games(apps):
            cur = _ci_get(entry, "LaunchOptions")
            if only_empty and cur:
                continue
            if cur == value:
                continue
            # normalise the key name to 'LaunchOptions'
            for k in list(entry):
                if k.lower() == "launchoptions" and k != "LaunchOptions":
                    del entry[k]
            entry["LaunchOptions"] = value
            changed += 1
        if not changed:
            print(f"  {lc.parent.parent.name}: nothing to change")
            continue
        if dry:
            print(f"  {lc.parent.parent.name}: would set {changed} game(s)")
        else:
            bak = lc.with_name(lc.name + f".tuxthrottle-bak-{int(time.time())}")
            shutil.copy2(lc, bak)
            _dump(cfg, lc)
            print(f"  {lc.parent.parent.name}: set {changed} game(s)  "
                  f"(backup {bak.name})")
        total += changed
    verb = "would set" if dry else "set"
    print(f"{verb} LaunchOptions on {total} game(s)"
          + ("" if dry else " — restart Steam to pick it up"))
    return 0


def _remove_token(token: str, dry: bool) -> int:
    if not token.strip():
        print("empty token — nothing to remove")
        return 1
    if steam_running():
        print("Steam is running — quit Steam completely first (it overwrites "
              "localconfig.vdf on exit).")
        return 2
    files = find_localconfigs()
    if not files:
        print("no localconfig.vdf found — is Steam installed for this user?")
        return 1
    total = 0
    for lc in files:
        cfg = _load(lc)
        apps = _apps_dict(cfg)
        if not apps:
            continue
        changed = 0
        for _aid, entry in _iter_games(apps):
            cur = _ci_get(entry, "LaunchOptions") or ""
            if token not in cur:
                continue
            # drop the token as a whole word/flag, then collapse extra spaces
            parts = [p for p in cur.split() if p != token]
            new = " ".join(parts)
            for k in list(entry):
                if k.lower() == "launchoptions" and k != "LaunchOptions":
                    del entry[k]
            entry["LaunchOptions"] = new
            changed += 1
        if not changed:
            continue
        if dry:
            print(f"  {lc.parent.parent.name}: would strip token from {changed} game(s)")
        else:
            bak = lc.with_name(lc.name + f".tuxthrottle-bak-{int(time.time())}")
            shutil.copy2(lc, bak)
            _dump(cfg, lc)
            print(f"  {lc.parent.parent.name}: stripped token from {changed} game(s)  "
                  f"(backup {bak.name})")
        total += changed
    verb = "would strip" if dry else "stripped"
    print(f"{verb} {token!r} from {total} game(s)"
          + ("" if dry else " — restart Steam to pick it up"))
    return 0


def main() -> int:
    if os.geteuid() == 0 and not os.environ.get("TUXTHROTTLE_ALLOW_ROOT"):
        sys.exit("run this as your normal user, not root")
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    dry = "--dry-run" in rest
    only_empty = "--only-empty" in rest
    pos = [a for a in rest if not a.startswith("-")]

    if cmd == "list":
        return do_list()
    if cmd == "clear-all":
        return _write_all("", only_empty=False, dry=dry)
    if cmd == "set-all":
        if "--b64" in rest:
            try:
                value = base64.b64decode(pos[0]).decode("utf-8")
            except (IndexError, ValueError) as exc:
                sys.exit(f"bad --b64 argument: {exc}")
        elif pos:
            value = pos[0]
        else:
            sys.exit('set-all needs the launch-options string (or --b64 <blob>)')
        return _write_all(value, only_empty=only_empty, dry=dry)
    if cmd == "remove-token":
        if "--b64" in rest:
            try:
                token = base64.b64decode(pos[0]).decode("utf-8")
            except (IndexError, ValueError) as exc:
                sys.exit(f"bad --b64 argument: {exc}")
        elif pos:
            token = pos[0]
        else:
            sys.exit("remove-token needs the token string (or --b64 <blob>)")
        return _remove_token(token, dry=dry)
    sys.exit(f"unknown command {cmd!r} (list | set-all | clear-all | remove-token)")


if __name__ == "__main__":
    raise SystemExit(main())
