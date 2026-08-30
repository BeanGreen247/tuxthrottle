#!/usr/bin/env python3
"""Give every fixed internal data partition a permanent, stable mount at
/mnt/<label> via /etc/fstab (with `nofail`), so a second games/data NVMe is
always in the same place — unlike /run/media/<user>/<uuid>, which is meant
for transient removable media and moves things around.

The OS disks (anything at /, /home, /boot*, swap, or already in fstab),
LUKS containers, and removable media are left alone. NTFS is mounted via the
in-kernel `ntfs3` driver with your uid/gid so games and files are writable.

Reversible: `disable-all` removes exactly the fstab block it added (between
marker comments), unmounts, and cleans up the /mnt dirs. It also tears down
any older /run/media-style registration a previous version of this tool set
up through nobara-automount.

    sudo tuxthrottle-automount --user <name> list
    sudo tuxthrottle-automount --user <name> enable-all
    sudo tuxthrottle-automount --user <name> disable-all
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import sys

FSTAB = "/etc/fstab"
MNT = "/mnt"
BEGIN = "# >>> tuxthrottle AutoMountDrives (managed) — edit above/below, not inside"
END = "# <<< tuxthrottle AutoMountDrives"
SUPPORTED = {"ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs", "ntfs", "exfat", "vfat"}
COMMON = "rw,noatime,nofail,x-systemd.device-timeout=10,x-gvfs-show"


def _run(*cmd):
    return subprocess.run(cmd, text=True, capture_output=True)


def _lsblk():
    out = _run("lsblk", "-J", "-o",
               "NAME,PATH,FSTYPE,UUID,LABEL,SIZE,MOUNTPOINT,TYPE,PKNAME,RM,HOTPLUG").stdout
    flat = []
    def walk(ns):
        for n in ns:
            flat.append(n)
            walk(n.get("children", []))
    walk(json.loads(out or "{}").get("blockdevices", []))
    return flat


def _fstab_uuids() -> set[str]:
    uu = set()
    try:
        text = open(FSTAB).read()
    except OSError:
        return uu
    # ignore our own managed block when checking "already in fstab"
    text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), "", text, flags=re.S)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()[0]
        if tok.upper().startswith("UUID="):
            uu.add(tok.split("=", 1)[1])
    return uu


def _os_disks(rows) -> set[str]:
    disks = set()
    for r in rows:
        mp = r.get("mountpoint") or ""
        if mp in ("/", "/home", "/var") or mp.startswith("/boot") or r.get("fstype") == "swap":
            disks.add(r.get("pkname") or r.get("name"))
    return disks


def _sanitize(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_.")
    return s or ""


def candidates(rows):
    fstab = _fstab_uuids()
    osd = _os_disks(rows)
    used_names: set[str] = set()
    out = []
    for r in rows:
        if r.get("type") != "part":
            continue
        fs = (r.get("fstype") or "").lower()
        uuid = r.get("uuid") or ""
        if fs not in SUPPORTED or not uuid:
            continue
        if r.get("mountpoint"):
            continue
        if uuid in fstab:
            continue
        if (r.get("pkname") or "") in osd:
            continue
        if r.get("rm") in (True, 1, "1") or r.get("hotplug") in (True, 1, "1"):
            continue
        name = _sanitize(r.get("label") or "") or f"{fs}-{uuid[:8]}"
        if name in used_names or os.path.ismount(f"{MNT}/{name}"):
            name = f"{name}-{uuid[:8]}"
        used_names.add(name)
        out.append({"path": r["path"], "uuid": uuid, "fstype": fs,
                    "label": r.get("label") or "", "size": r.get("size") or "",
                    "target": f"{MNT}/{name}"})
    return out


def fstab_line(c, uid: int, gid: int) -> str:
    fs = c["fstype"]
    if fs == "ntfs":
        typ, opts = "ntfs3", f"uid={uid},gid={gid},umask=022,windows_names,{COMMON}"
    elif fs == "exfat":
        typ, opts = "exfat", f"uid={uid},gid={gid},umask=022,{COMMON}"
    elif fs == "vfat":
        typ, opts = "vfat", f"uid={uid},gid={gid},umask=022,utf8,{COMMON}"
    else:
        typ, opts = fs, f"{COMMON}"
    return f"UUID={c['uuid']}  {c['target']}  {typ}  {opts}  0 0"


def _read_fstab() -> tuple[str, str]:
    """Return (fstab text with our block stripped, the old managed block)."""
    try:
        text = open(FSTAB).read()
    except OSError:
        return "", ""
    m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", text, flags=re.S)
    block = m.group(0) if m else ""
    return (text[:m.start()] + text[m.end():]) if m else text, block


def _cleanup_legacy(user: str):
    """Undo a previous nobara-automount / run-media registration by this tool."""
    marker = "/etc/nobara/automount/.tuxthrottle-managed"
    try:
        uuids = [l.strip() for l in open(marker) if l.strip()]
    except OSError:
        return
    for u in uuids:
        _run("systemctl", "disable", "--now", f"nobara-automount@{u}.service")
        _run("systemd-umount", f"/run/media/{user}/{u}")
        try:
            os.rmdir(f"/run/media/{user}/{u}")
        except OSError:
            pass
        for p in (f"/etc/udev/rules.d/99-tuxthrottle-automount-{u}.rules",
                  f"/etc/nobara/automount/{u}.env"):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            conf = "/etc/nobara/automount/enabled.conf"
            lines = [l for l in open(conf) if u not in l]
            open(conf, "w").writelines(lines)
        except OSError:
            pass
    try:
        os.remove(marker)
    except OSError:
        pass
    _run("udevadm", "control", "--reload")
    print(f"  (migrated {len(uuids)} drive(s) off the old /run/media setup)")


def enable_all(user: str) -> int:
    pw = pwd.getpwnam(user)
    _cleanup_legacy(user)
    rows = _lsblk()
    cands = candidates(rows)
    if not cands:
        print("No unmounted internal data partitions found — nothing to do.")
        return 0
    body, _old = _read_fstab()
    body = body.rstrip("\n") + "\n"
    lines = [BEGIN]
    for c in cands:
        os.makedirs(c["target"], exist_ok=True)
        lines.append(fstab_line(c, pw.pw_uid, pw.pw_gid))
        print(f"  + {c['path']}  {c['fstype']}  -> {c['target']}  ({c['size']})")
    lines.append(END)
    open(FSTAB, "w").write(body + "\n".join(lines) + "\n")
    _run("systemctl", "daemon-reload")
    for c in cands:
        r = _run("mount", c["target"])
        if r.returncode != 0:
            print(f"    ! mount {c['target']} failed: {r.stderr.strip()}", file=sys.stderr)
    print(f"\nDone. {len(cands)} drive(s) mounted at /mnt and set to mount on every boot.")
    return 0


def disable_all(user: str) -> int:
    _cleanup_legacy(user)
    body, block = _read_fstab()
    if not block:
        print("No managed fstab block — nothing to remove.")
        open(FSTAB, "w").write(body)
        return 0
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("UUID="):
            target = line.split()[1]
            _run("umount", target)
            try:
                os.rmdir(target)
            except OSError:
                pass
            print(f"  - {target} unmounted, fstab entry removed")
    open(FSTAB, "w").write(body)
    _run("systemctl", "daemon-reload")
    return 0


def list_drives(_user: str) -> int:
    cands = candidates(_lsblk())
    if not cands:
        print("No unmounted internal data partitions.")
        return 0
    print("Would mount permanently:")
    for c in cands:
        print(f"  {c['path']:16} {c['fstype']:7} {c['size']:>8}  -> {c['target']}   ({c['uuid']})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True, help="desktop user the drives are owned by")
    p.add_argument("action", choices=("list", "enable-all", "disable-all"))
    a = p.parse_args()
    if a.action != "list" and os.geteuid() != 0:
        print("enable-all / disable-all need root.", file=sys.stderr)
        return 1
    return {"list": list_drives, "enable-all": enable_all,
            "disable-all": disable_all}[a.action](a.user)


if __name__ == "__main__":
    raise SystemExit(main())
