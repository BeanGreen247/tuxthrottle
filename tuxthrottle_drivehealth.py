#!/usr/bin/env python3
"""Drive health - read-only checks plus a few safe maintenance actions.

`report` gathers, per physical drive and per mounted filesystem:
  * SMART (smartctl -j): overall verdict, NVMe critical-warning bits, media
    errors, spare, wear, temperature; SATA reallocated / pending / offline-
    uncorrectable sectors. Unsafe-shutdown and error-log counts are reported
    as notes, not failures.
  * filesystems: how full, whether the kernel remounted one read-only, Btrfs
    per-device error counters and last scrub, ext4 state, and for NTFS whether
    it was dirty when mounted and whether `force` hides that.
  * kernel log of this boot: I/O errors, NVMe timeouts/resets, filesystem
    error lines, grouped and counted.
  * Steam libraries: mounted, writable for the user, free space.

Every finding has a level: ok / note / warn / bad. Nothing here writes to a
disk except the explicit actions:
    scrub <mountpoint>     start a Btrfs scrub (background, online)
    trim                   fstrim every mounted filesystem that supports it
    selftest <device>      start a SMART short self-test
    ntfs-repair <device>   unmount → ntfsfix -d → mount again (clears the
                           dirty flag / fixes basic inconsistencies; a real
                           repair is `chkdsk /f` from Windows)
    fix-all                every safe fix for the whole system in one go
                           (scrub a Btrfs that needs it, repair a dirty NTFS,
                           mend Steam's shadercache link, TRIM) and a list of
                           what only a human can deal with
    check --notify USER    run the report and raise a desktop notification
                           when something is warn/bad (what the cron tweak runs)

Needs root for SMART and the Btrfs/NTFS details - the GUI already is.

Usage:
    tuxthrottle_drivehealth.py report [--json] [--user NAME]
    tuxthrottle_drivehealth.py check --notify NAME
    tuxthrottle_drivehealth.py fix-all [--user NAME]
    tuxthrottle_drivehealth.py scrub <mountpoint> | scrub-all
    tuxthrottle_drivehealth.py trim | selftest <device> | ntfs-repair <device>
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from pathlib import Path

LEVELS = ("ok", "note", "warn", "bad")
REAL_FS = {"btrfs", "ext4", "ext3", "ext2", "xfs", "f2fs", "ntfs", "ntfs3", "fuseblk",
           "exfat", "vfat"}
NTFS = {"ntfs", "ntfs3", "fuseblk"}
# (label, regex) - matched case-insensitively against this boot's kernel log
LOG_PATTERNS = (
    ("I/O errors", r"i/o error|buffer i/o error|blk_update_request|critical medium error"),
    ("NVMe timeouts / resets", r"nvme.*(timeout|reset|abort|controller is down)"),
    ("SATA link errors", r"ata\d+.*(failed command|hard resetting|exception emask|link is slow)"),
    ("Btrfs errors", r"btrfs.*(error|corrupt|csum failed|parent transid)"),
    ("ext4 errors", r"ext4-fs error|ext4-fs warning.*corrupt"),
    ("XFS errors", r"xfs.*(corruption|metadata i/o error|internal error)"),
    ("NTFS errors", r"ntfs3?.*(error|corrupt|dirty|failed)"),
)


def _run(cmd: list, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def _f(level: str, text: str) -> dict:
    return {"level": level, "text": text}


def worst(findings: list) -> str:
    return max((f["level"] for f in findings), key=LEVELS.index, default="ok")


# --------------------------------------------------------------------------- #
#  drives + SMART
# --------------------------------------------------------------------------- #
def drives() -> list[dict]:
    rc, out = _run(["lsblk", "-J", "-b", "-o",
                    "NAME,TYPE,SIZE,MODEL,TRAN,ROTA,RM,FSTYPE,MOUNTPOINT,LABEL"])
    try:
        devs = json.loads(out)["blockdevices"] if rc == 0 else []
    except (ValueError, KeyError):
        devs = []
    res = []
    for d in devs:
        if d.get("type") != "disk" or d["name"].startswith(("zram", "loop", "ram")):
            continue
        parts = [{"dev": f"/dev/{c['name']}", "fstype": c.get("fstype") or "",
                  "mount": c.get("mountpoint") or "", "label": c.get("label") or "",
                  "size": int(c.get("size") or 0)}
                 for c in d.get("children", []) or []]
        res.append({"dev": f"/dev/{d['name']}", "model": (d.get("model") or "").strip(),
                    "size": int(d.get("size") or 0), "tran": d.get("tran") or "",
                    "rotational": bool(d.get("rota")), "removable": bool(d.get("rm")),
                    "parts": parts})
    return res


def smart_findings(data: dict) -> tuple[dict, list]:
    """(summary, findings) from one `smartctl -j -H -A` document."""
    f: list = []
    summ: dict = {}
    status = (data.get("smart_status") or {}).get("passed")
    if status is False:
        f.append(_f("bad", "SMART overall health: FAILED - back up now and replace the drive"))
    elif status is None:
        f.append(_f("note", "the drive did not report a SMART health verdict"))
    temp = (data.get("temperature") or {}).get("current")
    if temp is not None:
        summ["temperature_c"] = temp
    hours = (data.get("power_on_time") or {}).get("hours")
    if hours is not None:
        summ["power_on_hours"] = hours
    nv = data.get("nvme_smart_health_information_log")
    if isinstance(nv, dict):
        summ.update({"wear_percent": nv.get("percentage_used"),
                     "spare_percent": nv.get("available_spare"),
                     "media_errors": nv.get("media_errors"),
                     "unsafe_shutdowns": nv.get("unsafe_shutdowns")})
        cw = nv.get("critical_warning") or 0
        if cw:
            bits = [n for b, n in ((1, "spare below threshold"), (2, "temperature"),
                                   (4, "reliability degraded"), (8, "read-only mode"),
                                   (16, "volatile backup failed")) if cw & b]
            f.append(_f("bad", "NVMe critical warning: " + ", ".join(bits or [hex(cw)])))
        if (nv.get("media_errors") or 0) > 0:
            f.append(_f("bad", f"{nv['media_errors']} media / data-integrity errors - "
                               f"data was read back wrong at least once"))
        spare, thr = nv.get("available_spare"), nv.get("available_spare_threshold") or 10
        if spare is not None:
            if spare <= thr:
                f.append(_f("bad", f"spare blocks at {spare}% (threshold {thr}%)"))
            elif spare < 30:
                f.append(_f("warn", f"spare blocks down to {spare}%"))
        used = nv.get("percentage_used")
        if used is not None:
            if used >= 100:
                f.append(_f("bad", f"rated endurance used up ({used}%)"))
            elif used >= 85:
                f.append(_f("warn", f"{used}% of rated endurance used"))
        if temp is not None and temp >= 75:
            f.append(_f("warn", f"running hot: {temp} °C"))
        if (nv.get("unsafe_shutdowns") or 0) >= 100:
            f.append(_f("note", f"{nv['unsafe_shutdowns']} unsafe shutdowns (power lost "
                                f"without a clean flush) - some drives also count deep "
                                f"power-saving here"))
        if (nv.get("num_err_log_entries") or 0) > 0:
            f.append(_f("note", f"{nv['num_err_log_entries']} entries in the drive's "
                                f"error log (often harmless unsupported-command replies)"))
    table = (data.get("ata_smart_attributes") or {}).get("table") or []
    watch = {5: ("reallocated sectors", "bad"), 187: ("reported uncorrectable errors", "bad"),
             197: ("sectors pending reallocation", "bad"),
             198: ("offline-uncorrectable sectors", "bad"), 199: ("CRC (cable) errors", "warn")}
    for a in table:
        aid, raw = a.get("id"), (a.get("raw") or {}).get("value") or 0
        if aid in watch and raw > 0:
            name, lvl = watch[aid]
            f.append(_f(lvl, f"{raw} {name}"))
            summ[name.replace(" ", "_")] = raw
        if aid in (177, 231, 233) and a.get("value") is not None and a["value"] <= 10:
            f.append(_f("warn", f"SSD wear indicator low ({a.get('name')}: {a['value']})"))
    if not f:
        f.append(_f("ok", "SMART: no problems reported"))
    return summ, f


HISTORY = Path("/var/lib/tuxthrottle/drive-counters.json")


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def track(key: str, unsafe, cycles, path: Path | None = None, now: float | None = None) -> dict:
    """Remember a drive's unsafe-shutdown / power-cycle counters (one sample
    per boot, the latest values win) and say how they moved. The lifetime
    totals can't be reset and say nothing on their own; whether they are
    still climbing does. Returns {} when there is no earlier boot to compare."""
    import time
    path = path or HISTORY
    now = time.time() if now is None else now
    try:
        hist = json.loads(path.read_text())
    except (OSError, ValueError):
        hist = {}
    rows = hist.setdefault(key, [])
    boot = _boot_id()
    if rows and rows[-1].get("boot") == boot:
        rows[-1].update(unsafe=unsafe, cycles=cycles, ts=now)
    else:
        rows.append({"boot": boot, "unsafe": unsafe, "cycles": cycles, "ts": now})
    del rows[:-60]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hist))
    except OSError:
        pass
    if len(rows) < 2:
        return {}
    first = rows[0]
    return {"boots": len(rows) - 1, "unsafe": (unsafe or 0) - (first["unsafe"] or 0),
            "cycles": (cycles or 0) - (first["cycles"] or 0),
            "days": max(0.0, (now - first["ts"]) / 86400)}


def trend_finding(t: dict) -> dict | None:
    if not t:
        return None
    span = f"over {t['boots']} boot(s), {t['days']:.0f} day(s)"
    if t["unsafe"] <= 0:
        return _f("ok", f"unsafe shutdowns have not gone up {span}")
    if t["unsafe"] >= t["boots"]:
        return _f("warn", f"unsafe shutdowns went up by {t['unsafe']} {span} - about one "
                          f"per boot, so the drive is losing power before it is told to "
                          f"shut down (at power-off or in suspend). No data is lost while "
                          f"filesystems are synced first, but it is worth a BIOS / drive "
                          f"firmware update")
    return _f("note", f"unsafe shutdowns went up by {t['unsafe']} {span}")


def smart(dev: str) -> tuple[dict, list]:
    if not shutil.which("smartctl"):
        return {}, [_f("note", "smartctl not installed (package smartmontools)")]
    rc, out = _run(["smartctl", "-j", "-H", "-A", "-i", dev], 40)
    try:
        data = json.loads(out)
    except ValueError:
        return {}, [_f("note", "could not read SMART data (needs root?)")]
    msgs = [m.get("string", "") for m in (data.get("smartctl") or {}).get("messages", [])]
    if not data.get("smart_status") and not data.get("nvme_smart_health_information_log") \
            and not data.get("ata_smart_attributes"):
        return {}, [_f("note", "no SMART data: " + ("; ".join(msgs) or f"exit {rc}"))]
    summ, f = smart_findings(data)
    if os.geteuid() == 0 and summ.get("unsafe_shutdowns") is not None:
        key = data.get("serial_number") or dev
        cycles = (data.get("power_cycle_count")
                  or (data.get("nvme_smart_health_information_log") or {}).get("power_cycles"))
        tf = trend_finding(track(key, summ["unsafe_shutdowns"], cycles))
        if tf:
            f.append(tf)
    return summ, f


# --------------------------------------------------------------------------- #
#  filesystems
# --------------------------------------------------------------------------- #
def mounts() -> list[dict]:
    """Mounted real filesystems, one row per source device (first mount wins)."""
    out, seen = [], set()
    try:
        lines = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return out
    for ln in lines:
        p = ln.split()
        if len(p) < 4 or p[2] not in REAL_FS or not p[0].startswith("/dev/") or p[0] in seen:
            continue
        seen.add(p[0])
        out.append({"dev": p[0], "mount": p[1].replace("\\040", " "), "fstype": p[2],
                    "opts": p[3].split(",")})
    return out


def _fstab_wants_rw(mount: str) -> bool:
    try:
        for ln in Path("/etc/fstab").read_text().splitlines():
            p = ln.split()
            if len(p) >= 4 and not ln.lstrip().startswith("#") and p[1] == mount:
                return "ro" not in p[3].split(",")
    except OSError:
        pass
    return True


def usage_finding(used_pct: float, free_bytes: int) -> dict | None:
    if used_pct >= 97:
        return _f("bad", f"{used_pct:.0f}% full ({free_bytes >> 30} GiB free)")
    if used_pct >= 90:
        return _f("warn", f"{used_pct:.0f}% full ({free_bytes >> 30} GiB free)")
    return None


def btrfs_stats_findings(text: str) -> list:
    """Non-zero counters from `btrfs device stats`."""
    f = []
    for m in re.finditer(r"\[(\S+)\]\.(\w+)\s+(\d+)", text):
        if int(m.group(3)) > 0:
            f.append(_f("bad", f"Btrfs {m.group(2)} = {m.group(3)} on {m.group(1)}"))
    return f


def btrfs_scrub_finding(text: str) -> dict:
    if "no stats available" in text:
        return _f("note", "never scrubbed - a scrub reads every block and verifies its "
                          "checksum (“Start scrub”)")
    m = re.search(r"Error summary:\s*(.+)", text)
    started = re.search(r"Scrub started:\s*(.+)", text)
    status = re.search(r"Status:\s*(\w+)", text)
    when = f" (started {started.group(1).strip()})" if started else ""
    if status and status.group(1) == "running":
        return _f("note", "scrub running" + when)
    if m and "no errors" not in m.group(1):
        return _f("bad", f"last scrub found errors: {m.group(1).strip()}" + when)
    return _f("ok", "last scrub clean" + when)


def ntfs_dirty_flag(dev: str) -> bool | None:
    """Read the NTFS volume's dirty bit straight from $Volume (MFT record 3).
    None = not NTFS / unreadable. Only meaningful for an UNMOUNTED volume -
    ntfs3 sets the bit itself while a volume is mounted read-write."""
    import struct
    try:
        with open(dev, "rb") as fh:
            boot = fh.read(512)
            if boot[3:11] != b"NTFS    ":
                return None
            bps, spc = struct.unpack_from("<HB", boot, 11)
            cluster = bps * (spc if spc < 0x80 else 1 << (256 - spc))
            mft_lcn = struct.unpack_from("<q", boot, 0x30)[0]
            c = struct.unpack_from("<b", boot, 0x40)[0]
            rec_size = c * cluster if c > 0 else 1 << -c
            fh.seek(mft_lcn * cluster + 3 * rec_size)
            rec = bytearray(fh.read(rec_size))
    except (OSError, struct.error):
        return None
    if rec[:4] != b"FILE":
        return None
    # undo the update-sequence fixups (last 2 bytes of every sector)
    uso, usn = struct.unpack_from("<HH", rec, 4)
    for i in range(1, usn):
        end = i * bps
        if end <= len(rec):
            rec[end - 2:end] = rec[uso + 2 * i: uso + 2 * i + 2]
    off = struct.unpack_from("<H", rec, 0x14)[0]
    while off + 16 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, off)
        if atype == 0xFFFFFFFF or alen == 0:
            break
        if atype == 0x70 and rec[off + 8] == 0:              # resident $VOLUME_INFORMATION
            voff = struct.unpack_from("<H", rec, off + 0x14)[0]
            flags = struct.unpack_from("<H", rec, off + voff + 10)[0]
            return bool(flags & 0x0001)
        off += alen
    return None


def filesystems() -> list[dict]:
    rows = []
    boot_log = None
    for m in mounts():
        f: list = []
        try:
            u = shutil.disk_usage(m["mount"])
            pct = u.used * 100 / u.total if u.total else 0
        except OSError:
            u, pct = None, 0
        if u:
            uf = usage_finding(pct, u.free)
            if uf:
                f.append(uf)
        if "ro" in m["opts"] and _fstab_wants_rw(m["mount"]):
            f.append(_f("bad", "mounted READ-ONLY although it should be writable - the "
                               "kernel usually does that after a filesystem error"))
        if m["fstype"] == "btrfs":
            _rc, out = _run(["btrfs", "device", "stats", m["mount"]])
            f += btrfs_stats_findings(out)
            _rc, out = _run(["btrfs", "scrub", "status", m["mount"]])
            f.append(btrfs_scrub_finding(out))
        elif m["fstype"].startswith("ext"):
            _rc, out = _run(["tune2fs", "-l", m["dev"]])
            st = re.search(r"Filesystem state:\s*(.+)", out)
            if st and "clean" not in st.group(1):
                f.append(_f("bad", f"ext filesystem state: {st.group(1).strip()}"))
            if st and "error" in st.group(1):
                f.append(_f("bad", "errors recorded - run fsck from a live system"))
        elif m["fstype"] in NTFS:
            if boot_log is None:
                _rc, boot_log = _run(["journalctl", "-k", "-b", "-o", "cat", "--no-pager"], 30)
            name = m["dev"].split("/")[-1]
            if re.search(rf"ntfs3?.*{re.escape(name)}.*dirty", boot_log, re.I):
                f.append(_f("warn", "was marked dirty when it was mounted (Windows Fast "
                                    "Startup / unclean shutdown) - “Repair NTFS” clears it; "
                                    "a full check is chkdsk /f from Windows"))
            if "force" in m["opts"]:
                f.append(_f("note", "mounted with `force`: it mounts even when dirty, so "
                                    "a dirty volume would not stop it - hard power-offs "
                                    "are the real risk here"))
        if not [x for x in f if x["level"] in ("warn", "bad")]:
            f.insert(0, _f("ok", "no filesystem problems found"))
        rows.append({**m, "used_pct": round(pct, 1), "free": u.free if u else 0,
                     "total": u.total if u else 0, "findings": f})
    return rows


def unmounted_ntfs() -> list[dict]:
    """NTFS partitions that are not mounted, with their real dirty bit."""
    mounted = {m["dev"] for m in mounts()}
    rows = []
    for d in drives():
        for p in d["parts"]:
            if p["fstype"] == "ntfs" and p["dev"] not in mounted:
                dirty = ntfs_dirty_flag(p["dev"])
                f = [_f("warn", "dirty flag set - “Repair NTFS” clears it") if dirty
                     else _f("ok", "clean") if dirty is False
                     else _f("note", "could not read the volume flags")]
                rows.append({"dev": p["dev"], "label": p["label"], "findings": f})
    return rows


# --------------------------------------------------------------------------- #
#  kernel log, Steam libraries
# --------------------------------------------------------------------------- #
def scan_log(text: str) -> list[dict]:
    rows = []
    lines = text.splitlines()
    for label, pat in LOG_PATTERNS:
        rx = re.compile(pat, re.I)
        hits = [ln.strip() for ln in lines if rx.search(ln)]
        if hits:
            rows.append({"label": label, "count": len(hits), "last": hits[-3:],
                         "level": "warn" if label == "NTFS errors" else "bad"})
    return rows


def kernel_log() -> list[dict]:
    _rc, out = _run(["journalctl", "-k", "-b", "-o", "short", "--no-pager"], 30)
    return scan_log(out)


def steam_libraries(user: str) -> list[dict]:
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return []
    rows = []
    for root in (Path(pw.pw_dir) / ".local/share/Steam", Path(pw.pw_dir) / ".steam/steam"):
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            paths = re.findall(r'"path"\s*"([^"]+)"', vdf.read_text(errors="replace"))
        except OSError:
            continue
        for p in paths:
            f = []
            sa = Path(p) / "steamapps"
            if not sa.is_dir():
                f.append(_f("bad", "library folder is missing - drive not mounted?"))
            else:
                st = os.stat(sa)
                if st.st_uid != pw.pw_uid and not (st.st_mode & 0o002):
                    f.append(_f("warn", f"not owned by {user} - Steam may be unable to write"))
                u = shutil.disk_usage(sa)
                if u.free < 10 << 30:
                    f.append(_f("warn", f"only {u.free >> 30} GiB free"))
                sc = sa / "shadercache"
                if sc.is_symlink() and not sc.exists():
                    f.append(_f("bad", "shadercache link is broken - Steam shows “disk "
                                       "write error” (Game Tools → Link Steam cache repairs it)"))
            rows.append({"path": p, "findings": f or [_f("ok", "mounted and writable")]})
        break
    return rows


def report(user: str = "") -> dict:
    drv = drives()
    for d in drv:
        d["smart"], d["findings"] = smart(d["dev"])
    rep = {"drives": drv, "filesystems": filesystems(), "ntfs_unmounted": unmounted_ntfs(),
           "kernel_log": kernel_log(), "steam": steam_libraries(user) if user else []}
    lv = [f["level"] for d in drv for f in d["findings"]]
    lv += [f["level"] for s in ("filesystems", "ntfs_unmounted", "steam")
           for r in rep[s] for f in r["findings"]]
    lv += [k["level"] for k in rep["kernel_log"]]
    rep["bad"], rep["warn"] = lv.count("bad"), lv.count("warn")
    rep["fix_plan"] = fix_plan(rep)
    return rep


def print_report(rep: dict) -> None:
    mark = {"ok": "ok  ", "note": "note", "warn": "WARN", "bad": "BAD "}

    def show(findings):
        for f in findings:
            print(f"      [{mark[f['level']]}] {f['text']}")
    for d in rep["drives"]:
        s = d["smart"]
        extra = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in s.items() if v is not None)
        print(f"  {d['dev']}  {d['model']}  {d['size'] >> 30} GiB  {extra}")
        show(d["findings"])
    for m in rep["filesystems"]:
        print(f"  {m['mount']}  ({m['fstype']} on {m['dev']}, {m['used_pct']}% used)")
        show(m["findings"])
    for n in rep["ntfs_unmounted"]:
        print(f"  {n['dev']}  (NTFS, not mounted)")
        show(n["findings"])
    for k in rep["kernel_log"]:
        print(f"  kernel log: {k['label']} ×{k['count']}")
        for ln in k["last"]:
            print(f"      {ln[:160]}")
    for s in rep["steam"]:
        print(f"  Steam library {s['path']}")
        show(s["findings"])
    print(f"{rep['bad']} problem(s), {rep['warn']} warning(s)")


# --------------------------------------------------------------------------- #
#  actions
# --------------------------------------------------------------------------- #
def scrub(mount: str) -> int:
    if not any(m["mount"] == mount and m["fstype"] == "btrfs" for m in mounts()):
        print(f"{mount} is not a mounted Btrfs filesystem")
        return 1
    rc, out = _run(["btrfs", "scrub", "start", mount], 30)
    print(out.strip() or "scrub started")
    if rc == 0:
        print("it runs in the background (minutes to hours); press “Check drives” "
              "again to see the result")
    return rc


def scrub_all() -> int:
    rc = 0
    for m in mounts():
        if m["fstype"] == "btrfs":
            rc |= scrub(m["mount"])
    return rc


def trim() -> int:
    rc, out = _run(["fstrim", "-av"], 600)
    print(out.strip())
    return rc


def selftest(dev: str) -> int:
    rc, out = _run(["smartctl", "-t", "short", dev], 60)
    print("\n".join(ln for ln in out.splitlines() if ln.strip())[-600:])
    return 0 if rc in (0, 4) else rc


def ntfs_repair(dev: str) -> int:
    part = next((p for d in drives() for p in d["parts"] if p["dev"] == dev), None)
    if part is None or part["fstype"] != "ntfs":
        print(f"{dev} is not an NTFS partition")
        return 1
    if not shutil.which("ntfsfix"):
        print("ntfsfix not installed (package ntfsprogs / ntfs-3g)")
        return 1
    mount = next((m["mount"] for m in mounts() if m["dev"] == dev), "")
    if mount:
        rc, out = _run(["umount", mount], 60)
        if rc != 0:
            print(f"cannot unmount {mount}: {out.strip()}")
            print("something is using it - close Steam, games and file managers "
                  "open on that drive, then try again")
            return 2
        print(f"unmounted {mount}")
    rc, out = _run(["ntfsfix", "-d", dev], 600)
    print(out.strip())
    if mount:
        mrc, mout = _run(["mount", mount], 60)
        print(f"mounted {mount} again" if mrc == 0 else f"remount failed: {mout.strip()}")
        rc = rc or mrc
    return rc


def fix_plan(rep: dict) -> list[dict]:
    """What “Fix issues” would do for this report: only actions that are safe
    to run unattended. Everything else is listed as `manual` with advice."""
    plan = []
    for m in rep["filesystems"]:
        texts = " ".join(f["text"] for f in m["findings"])
        if m.get("fstype") == "btrfs" and ("never scrubbed" in texts or "Btrfs " in texts
                                       or "last scrub found errors" in texts):
            plan.append({"action": "scrub", "arg": m["mount"],
                         "text": f"start a Btrfs scrub of {m['mount']}"})
        if m.get("fstype") in NTFS and "was marked dirty" in texts:
            plan.append({"action": "ntfs-repair", "arg": m["dev"],
                         "text": f"repair NTFS on {m['dev']} ({m['mount']})"})
        if "READ-ONLY" in texts:
            plan.append({"action": "manual", "arg": m["mount"],
                         "text": f"{m['mount']} went read-only: reboot, and if it comes "
                                 f"back run a filesystem check from a live system"})
        for f in m["findings"]:
            if "% full" in f["text"]:
                plan.append({"action": "manual", "arg": m["mount"],
                             "text": f"{m['mount']} is {f['text']}: free up space"})
    for n in rep["ntfs_unmounted"]:
        if any("dirty flag set" in f["text"] for f in n["findings"]):
            plan.append({"action": "ntfs-repair", "arg": n["dev"],
                         "text": f"repair NTFS on {n['dev']}"})
    for s in rep["steam"]:
        if any("shadercache link is broken" in f["text"] for f in s["findings"]):
            plan.append({"action": "heal-shadercache", "arg": "",
                         "text": "repair Steam's broken shadercache link"})
            break
    for d in rep["drives"]:
        for f in d["findings"]:
            if f["level"] == "bad":
                plan.append({"action": "manual", "arg": d["dev"],
                             "text": f"{d['dev']}: {f['text']} - no software fix; back up "
                                     f"and plan a replacement"})
    if any(not d.get("rotational") for d in rep["drives"]):
        plan.append({"action": "trim", "arg": "", "text": "TRIM every SSD filesystem"})
    return plan


def fix_all(user: str = "") -> int:
    """Run every safe automatic fix for the whole system, then say what is
    left for a human."""
    rep = report(user)
    plan = fix_plan(rep)
    rc = 0
    for step in plan:
        if step["action"] == "manual":
            continue
        print(f"== {step['text']}", flush=True)
        if step["action"] == "scrub":
            rc |= scrub(step["arg"])
        elif step["action"] == "ntfs-repair":
            r = ntfs_repair(step["arg"])
            if r == 2:
                print("   skipped (drive is in use)")
            rc |= 0 if r == 2 else r
        elif step["action"] == "trim":
            rc |= trim()
        elif step["action"] == "heal-shadercache" and user:
            script = Path(__file__).resolve().parent / "tuxthrottle_shadercache.py"
            r, out = _run(["su", "-", user, "-c", f"python3 {script} heal"], 60)
            print("   " + out.strip())
            rc |= r
    manual = [s for s in plan if s["action"] == "manual"]
    if manual:
        print("== needs you:")
        for s in manual:
            print(f"   - {s['text']}")
    if not [s for s in plan if s["action"] != "manual"]:
        print("nothing to fix automatically")
    print("lifetime counters (unsafe shutdowns, error-log entries, wear) are history "
          "kept by the drive - they cannot be cleared, only watched")
    return rc


def notify(user: str, rep: dict) -> None:
    """Desktop notification for the user when something needs attention."""
    if not (rep["bad"] or rep["warn"]) or not shutil.which("notify-send"):
        return
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return
    lines = [f["text"] for d in rep["drives"] for f in d["findings"]
             if f["level"] in ("warn", "bad")]
    lines += [f"{m['mount']}: {f['text']}" for m in rep["filesystems"] for f in m["findings"]
              if f["level"] in ("warn", "bad")]
    lines += [f"kernel log: {k['label']} ×{k['count']}" for k in rep["kernel_log"]]
    body = "\n".join(lines[:6]) or "see TuxThrottle → Drives"
    env = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{uid}",
               DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{uid}/bus")
    cmd = ["notify-send", "-a", "TuxThrottle", "-u", "critical" if rep["bad"] else "normal",
           "-i", "drive-harddisk",
           f"Drive health: {rep['bad']} problem(s), {rep['warn']} warning(s)", body]
    if os.geteuid() == 0:
        cmd = ["sudo", "-u", user, "env", f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}",
               f"DBUS_SESSION_BUS_ADDRESS={env['DBUS_SESSION_BUS_ADDRESS']}"] + cmd
    _run(cmd, 20)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--json", action="store_true")
    r.add_argument("--user", default=os.environ.get("SUDO_USER", ""))
    c = sub.add_parser("check")
    c.add_argument("--notify", required=True, metavar="USER")
    s = sub.add_parser("scrub")
    s.add_argument("mount")
    sub.add_parser("scrub-all")
    fa = sub.add_parser("fix-all")
    fa.add_argument("--user", default=os.environ.get("SUDO_USER", ""))
    sub.add_parser("trim")
    t = sub.add_parser("selftest")
    t.add_argument("device")
    n = sub.add_parser("ntfs-repair")
    n.add_argument("device")
    args = ap.parse_args()
    if args.cmd == "report":
        rep = report(args.user)
        if args.json:
            print(json.dumps(rep))
        else:
            print_report(rep)
        return 0
    if args.cmd == "check":
        rep = report(args.notify)
        notify(args.notify, rep)
        return 1 if rep["bad"] else 0
    if args.cmd == "scrub":
        return scrub(args.mount)
    if args.cmd == "fix-all":
        return fix_all(args.user)
    if args.cmd == "scrub-all":
        return scrub_all()
    if args.cmd == "trim":
        return trim()
    if args.cmd == "selftest":
        return selftest(args.device)
    return ntfs_repair(args.device)


if __name__ == "__main__":
    sys.exit(main())
