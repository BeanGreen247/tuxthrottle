#!/usr/bin/env python3
"""Bug-report helper for osu!lazer on Linux: read the game's logs, and pack everything a GitHub issue needs.

Usage:
    osu_report.py logs [--type runtime] [--errors] [--follow] [--session N] [--dev]
        show a log from the newest session (types: runtime, input, performance, database, network)
    osu_report.py sessions [--dev]
        list the logged sessions
    osu_report.py bundle [-o DIR] [--with-network] [--dev]
        make osu-report-DATE.zip (system info, settings, newest session's logs, diagnostics) plus a
        summary.md to paste into the issue form

The bundle leaves out auth logs, and the network log unless --with-network is given. Your user name and
home folder are replaced with <user> / ~ in everything it contains. Look through the zip before you post it.
--dev reads the data folder that debug builds from source use (osu-development).
"""
from __future__ import annotations

import argparse
import getpass
import os
import platform
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import osu_common as c

S = c.Style
TOOLS_DIR = Path(__file__).resolve().parent
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(\w+)\]:")
SAFE_TYPES = ("runtime", "input", "performance", "database")


def sessions(logs: Path) -> list[str]:
    """Session ids (the unix-time prefix of the log files), newest first."""
    ids = {p.name.split(".", 1)[0] for p in logs.glob("*.log") if p.name.split(".", 1)[0].isdigit()}
    return sorted(ids, key=int, reverse=True)


def redact(text: str) -> str:
    home = str(Path.home())
    user = getpass.getuser()
    text = text.replace(home, "~")
    if len(user) > 2:
        text = re.sub(rf"\b{re.escape(user)}\b", "<user>", text)
    # bearer/access tokens and anything that looks like one
    text = re.sub(r"(?i)(authorization|access_token|refresh_token|token|password)([\"'=:\s]+)[^\s\"',&]+",
                  r"\1\2<redacted>", text)
    text = re.sub(r"(?m)^(Username\s*=\s*).+$", r"\1<redacted>", text)     # game.ini: osu! account name
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", text)


def errors_only(lines: list[str]) -> list[str]:
    """[error] lines plus the stack trace lines that follow them."""
    out, keep = [], False
    for ln in lines:
        m = LOG_LINE.match(ln)
        if m:
            level = m.group(1).lower()
            keep = level == "error" or " exception" in ln.lower()
        if keep:
            out.append(ln)
    return out


def system_info() -> dict[str, str]:
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            k, _, v = line.partition("=")
            os_release[k] = v.strip('"')
    except OSError:
        pass
    cpu = next((ln.split(":", 1)[1].strip() for ln in c.run(["cat", "/proc/cpuinfo"]).splitlines()
                if ln.startswith("model name")), platform.processor())
    gpus = [ln.split(": ", 1)[-1] for ln in c.run(["lspci"]).splitlines()
            if re.search(r"VGA|3D controller|Display controller", ln)]
    nvidia = c.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"]).strip()
    mesa = re.search(r"Mesa [\d.]+\S*", c.run(["glxinfo", "-B"]))
    audio = re.search(r"Server Name: (.+)", c.run(["pactl", "info"]))
    return {
        "OS": os_release.get("PRETTY_NAME", platform.system()),
        "Kernel": platform.release(),
        "Desktop": f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')} ({os.environ.get('XDG_SESSION_TYPE', '?')})",
        "CPU": cpu,
        "GPU": "; ".join(gpus) or "?",
        "GPU driver": f"NVIDIA {nvidia}" if nvidia else (mesa.group(0) if mesa else "?"),
        "Audio": audio.group(1) if audio else "?",
        "RAM": f"{int(Path('/proc/meminfo').read_text().split()[1]) / 1024 / 1024:.1f} GB",
    }


def osu_version(logs: Path, session: str | None) -> str:
    if not session:
        return "?"
    try:
        head = (logs / f"{session}.runtime.log").read_text(errors="replace")[:2000]
    except OSError:
        return "?"
    m = re.search(r"Running osu[^\n]*", head)
    return m.group(0).replace("Running ", "") if m else "?"


def cmd_sessions(args) -> int:
    logs = c.data_dir(args.dev) / "logs"
    for i, sid in enumerate(sessions(logs), 1):
        errs = len([ln for ln in errors_only(read_lines(logs / f"{sid}.runtime.log")) if LOG_LINE.match(ln)])
        print(f"{i:>3}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(sid)))}  "
              f"{osu_version(logs, sid):<35} {S.RED if errs else ''}{errs} error(s){S.OFF}")
    return 0


def read_lines(p: Path) -> list[str]:
    try:
        return p.read_text(errors="replace").splitlines()
    except OSError:
        return []


def cmd_logs(args) -> int:
    logs = c.data_dir(args.dev) / "logs"
    ids = sessions(logs)
    if not ids:
        sys.exit(f"no logs in {logs}")
    sid = ids[min(args.session, len(ids)) - 1]
    path = logs / f"{sid}.{args.type}.log"
    if not path.exists():
        sys.exit(f"{path.name} doesn't exist (types in this session: "
                 f"{', '.join(sorted(p.name.split('.')[1] for p in logs.glob(f'{sid}.*.log')))})")
    if args.follow:
        cmd = ["tail", "-n", "40", "-F", str(path)]
        if args.errors:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
            try:
                for ln in p.stdout:
                    if "[error]" in ln or "exception" in ln.lower():
                        print(ln, end="")
            except KeyboardInterrupt:
                p.terminate()
            return 0
        try:
            return subprocess.call(cmd)
        except KeyboardInterrupt:
            return 0
    lines = read_lines(path)
    if args.errors:
        lines = errors_only(lines)
        if not lines:
            print(f"no errors in {path.name}")
    for ln in lines:
        m = LOG_LINE.match(ln)
        colour = S.RED if m and m.group(1) == "error" else (S.DIM if m and m.group(1) == "debug" else "")
        print(f"{colour}{ln}{S.OFF}" if colour else ln)
    return 0


def cmd_bundle(args) -> int:
    data = c.data_dir(args.dev)
    logs = data / "logs"
    ids = sessions(logs)
    sid = ids[0] if ids else None
    out_dir = Path(args.output).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zpath = out_dir / f"osu-report-{stamp}.zip"
    info = system_info()
    info["osu!"] = osu_version(logs, sid)
    launcher_cfg = Path.home() / ".config" / "osu-lazer-launcher" / "config"
    info["Launched with"] = "osu-lazer-launcher" if launcher_cfg.exists() else "?"

    errors = errors_only(read_lines(logs / f"{sid}.runtime.log")) if sid else []
    summary = ["### Environment", "", *[f"- **{k}:** {v}" for k, v in info.items()], ""]
    if errors:
        summary += ["### Errors from the latest session's runtime.log", "", "```",
                    *errors[:60], *(["... (see runtime.log in the zip)"] if len(errors) > 60 else []), "```", ""]
    summary_text = redact("\n".join(summary))

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.md", summary_text)
        types = SAFE_TYPES + (("network",) if args.with_network else ())
        if sid:
            for t in types:
                p = logs / f"{sid}.{t}.log"
                if p.exists():
                    z.writestr(f"logs/{p.name}", redact(p.read_text(errors="replace")))
        for name in ("framework.ini", "game.ini", "input.json"):
            p = data / name
            if p.exists():
                z.writestr(f"config/{name}", redact(p.read_text(errors="replace")))
        if launcher_cfg.exists():
            z.writestr("config/osu-lazer-launcher.conf", redact(launcher_cfg.read_text(errors="replace")))
        diag = TOOLS_DIR / "osu_lazer_diag.py"
        if diag.exists():
            res = subprocess.run([sys.executable, str(diag), "--once"], capture_output=True, text=True,
                                 timeout=60, check=False, env={**os.environ, "NO_COLOR": "1"})
            z.writestr("diagnostics.txt", redact(res.stdout + res.stderr))
    md = out_dir / f"osu-report-{stamp}.md"
    md.write_text(summary_text + "\n")
    print(f"{S.BOLD}{zpath}{S.OFF}\n{md}\n")
    print(summary_text)
    print(f"\n{S.DIM}Paste {md.name} into the issue (github.com/ppy/osu/issues) and attach the zip. "
          f"Check both for anything private first.{S.OFF}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools report", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("logs", help="show a log from a session")
    p.add_argument("--type", default="runtime")
    p.add_argument("--errors", action="store_true", help="only errors and their stack traces")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--session", type=int, default=1, help="1 = newest (see: sessions)")
    p.add_argument("--dev", action="store_true")
    p = sub.add_parser("sessions", help="list sessions")
    p.add_argument("--dev", action="store_true")
    p = sub.add_parser("bundle", help="make a bug-report zip")
    p.add_argument("-o", "--output", default=str(Path.home()))
    p.add_argument("--with-network", action="store_true")
    p.add_argument("--dev", action="store_true")
    argv = sys.argv[1:] if argv is None else argv
    args = ap.parse_args(argv or ["sessions"])
    return {"logs": cmd_logs, "sessions": cmd_sessions, "bundle": cmd_bundle}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
