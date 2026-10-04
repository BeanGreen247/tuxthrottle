"""tuxthrottle_drivehealth.py - finding logic on canned data. No root, no disks."""
import struct

import tuxthrottle_drivehealth as dh


def _levels(findings):
    return [f["level"] for f in findings]


def test_healthy_nvme_is_ok_with_notes_only():
    data = {"smart_status": {"passed": True}, "temperature": {"current": 41},
            "nvme_smart_health_information_log": {
                "critical_warning": 0, "available_spare": 100, "available_spare_threshold": 10,
                "percentage_used": 8, "media_errors": 0, "unsafe_shutdowns": 1129,
                "num_err_log_entries": 0}}
    summ, f = dh.smart_findings(data)
    assert summ["wear_percent"] == 8 and summ["temperature_c"] == 41
    assert set(_levels(f)) == {"note"} and dh.worst(f) == "note"


def test_failing_nvme_is_bad():
    data = {"smart_status": {"passed": False},
            "nvme_smart_health_information_log": {
                "critical_warning": 5, "available_spare": 4, "available_spare_threshold": 10,
                "percentage_used": 101, "media_errors": 3}}
    _s, f = dh.smart_findings(data)
    text = " | ".join(x["text"] for x in f)
    assert dh.worst(f) == "bad"
    for needle in ("FAILED", "spare below threshold", "reliability degraded",
                   "3 media", "spare blocks at 4%", "endurance used up"):
        assert needle in text


def test_sata_reallocated_sectors_are_bad_and_clean_disk_is_ok():
    bad = {"smart_status": {"passed": True}, "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 12}},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "raw": {"value": 2}}]}}
    _s, f = dh.smart_findings(bad)
    assert "bad" in _levels(f) and "warn" in _levels(f)
    _s, f = dh.smart_findings({"smart_status": {"passed": True},
                               "ata_smart_attributes": {"table": []}})
    assert _levels(f) == ["ok"]


def test_usage_btrfs_and_scrub_findings():
    assert dh.usage_finding(50, 1 << 40) is None
    assert dh.usage_finding(92, 5 << 30)["level"] == "warn"
    assert dh.usage_finding(98, 1 << 30)["level"] == "bad"
    stats = "[/dev/a].write_io_errs    0\n[/dev/a].corruption_errs  7\n"
    f = dh.btrfs_stats_findings(stats)
    assert len(f) == 1 and "corruption_errs = 7" in f[0]["text"]
    assert dh.btrfs_scrub_finding("\tno stats available\n")["level"] == "note"
    assert dh.btrfs_scrub_finding("Status: finished\nError summary:    no errors found")["level"] == "ok"
    assert dh.btrfs_scrub_finding("Status: finished\nError summary:    csum=3")["level"] == "bad"


def test_scan_log_groups_and_counts():
    log = ("kernel: nvme nvme0: I/O 12 QID 3 timeout, aborting\n"
           "kernel: blk_update_request: I/O error, dev sda, sector 5\n"
           "kernel: BTRFS error (device nvme0n1p3): bdev errs: corrupt 1\n"
           "kernel: usb 1-1: new device\n")
    rows = {r["label"]: r for r in dh.scan_log(log)}
    assert rows["I/O errors"]["count"] == 1 and rows["NVMe timeouts / resets"]["count"] == 1
    assert rows["Btrfs errors"]["level"] == "bad" and "SATA link errors" not in rows
    assert dh.scan_log("kernel: all quiet\n") == []


def _ntfs_image(tmp_path, dirty):
    """A tiny fake NTFS: boot sector + MFT record 3 ($Volume) with a resident
    $VOLUME_INFORMATION attribute."""
    bps, spc, rec = 512, 1, 1024
    boot = bytearray(512)
    boot[3:11] = b"NTFS    "
    struct.pack_into("<HB", boot, 11, bps, spc)
    struct.pack_into("<q", boot, 0x30, 4)              # MFT at cluster 4
    struct.pack_into("<b", boot, 0x40, -10)            # record size 2^10
    r = bytearray(rec)
    r[:4] = b"FILE"
    struct.pack_into("<HH", r, 4, 0x30, 3)             # update sequence: offset, count
    struct.pack_into("<H", r, 0x14, 0x38)              # first attribute
    off = 0x38
    struct.pack_into("<II", r, off, 0x70, 0x28)        # $VOLUME_INFORMATION, length
    r[off + 8] = 0                                     # resident
    struct.pack_into("<H", r, off + 0x14, 0x18)        # value offset
    struct.pack_into("<H", r, off + 0x18 + 10, 0x0001 if dirty else 0)
    struct.pack_into("<I", r, off + 0x28, 0xFFFFFFFF)
    # apply fixups the way NTFS stores them
    usn = b"\x01\x00"
    r[0x30:0x32] = usn
    for i in (1, 2):
        r[0x30 + 2 * i:0x32 + 2 * i] = r[i * bps - 2:i * bps]
        r[i * bps - 2:i * bps] = usn
    img = tmp_path / f"ntfs-{dirty}.img"
    img.write_bytes(bytes(boot) + bytes(4 * 512 - 512) + bytes(3 * rec) + bytes(r))
    return img


def test_ntfs_dirty_flag_is_read_from_the_volume(tmp_path):
    assert dh.ntfs_dirty_flag(str(_ntfs_image(tmp_path, True))) is True
    assert dh.ntfs_dirty_flag(str(_ntfs_image(tmp_path, False))) is False
    other = tmp_path / "ext.img"
    other.write_bytes(bytes(4096))
    assert dh.ntfs_dirty_flag(str(other)) is None
    assert dh.ntfs_dirty_flag(str(tmp_path / "missing")) is None


def test_report_counts_levels(monkeypatch):
    monkeypatch.setattr(dh, "drives", lambda: [{"dev": "/dev/x", "model": "m", "size": 1,
                                                "tran": "nvme", "rotational": False,
                                                "removable": False, "parts": []}])
    monkeypatch.setattr(dh, "smart", lambda d: ({}, [dh._f("bad", "x"), dh._f("note", "y")]))
    monkeypatch.setattr(dh, "filesystems", lambda: [{"findings": [dh._f("warn", "full")]}])
    monkeypatch.setattr(dh, "unmounted_ntfs", lambda: [])
    monkeypatch.setattr(dh, "kernel_log", lambda: [{"label": "I/O errors", "count": 2,
                                                    "last": [], "level": "bad"}])
    rep = dh.report()
    assert rep["bad"] == 2 and rep["warn"] == 1


def test_track_reports_movement_per_boot(monkeypatch, tmp_path):
    hist = tmp_path / "c.json"
    boots = iter(["a", "b", "b", "c"])
    monkeypatch.setattr(dh, "_boot_id", lambda: next(boots))
    assert dh.track("S1", 100, 500, hist, now=0) == {}
    assert dh.track("S1", 101, 501, hist, now=86400) == {"boots": 1, "unsafe": 1, "cycles": 1,
                                                         "days": 1.0}
    assert dh.track("S1", 101, 501, hist, now=90000)["boots"] == 1      # same boot: no new row
    t = dh.track("S1", 102, 503, hist, now=2 * 86400)
    assert t["boots"] == 2 and t["unsafe"] == 2
    assert dh.trend_finding(t)["level"] == "warn"
    assert dh.trend_finding({"boots": 5, "unsafe": 0, "cycles": 9, "days": 3})["level"] == "ok"
    assert dh.trend_finding({"boots": 5, "unsafe": 1, "cycles": 9, "days": 3})["level"] == "note"
    assert dh.trend_finding({}) is None


def _rep(**kw):
    base = {"drives": [{"dev": "/dev/x", "rotational": False, "findings": []}],
            "filesystems": [], "ntfs_unmounted": [], "steam": [], "kernel_log": []}
    base.update(kw)
    return base


def test_fix_plan_only_offers_safe_automatic_fixes():
    rep = _rep(
        filesystems=[
            {"mount": "/", "dev": "/dev/a", "fstype": "btrfs",
             "findings": [dh._f("note", "never scrubbed - ...")]},
            {"mount": "/mnt/w", "dev": "/dev/b", "fstype": "ntfs3",
             "findings": [dh._f("warn", "was marked dirty when it was mounted"),
                          dh._f("bad", "98% full (1 GiB free)")]},
            {"mount": "/data", "dev": "/dev/c", "fstype": "ext4",
             "findings": [dh._f("bad", "mounted READ-ONLY although ...")]}],
        ntfs_unmounted=[{"dev": "/dev/d", "findings": [dh._f("warn", "dirty flag set - ...")]}],
        steam=[{"path": "/s", "findings": [dh._f("bad", "shadercache link is broken")]}])
    rep["drives"][0]["findings"] = [dh._f("bad", "3 media / data-integrity errors")]
    plan = dh.fix_plan(rep)
    auto = [(p["action"], p["arg"]) for p in plan if p["action"] != "manual"]
    assert auto == [("scrub", "/"), ("ntfs-repair", "/dev/b"), ("ntfs-repair", "/dev/d"),
                    ("heal-shadercache", ""), ("trim", "")]
    manual = " | ".join(p["text"] for p in plan if p["action"] == "manual")
    assert "free up space" in manual and "read-only" in manual and "no software fix" in manual


def test_fix_plan_on_a_healthy_system_is_just_trim():
    rep = _rep(filesystems=[{"mount": "/", "dev": "/dev/a", "fstype": "btrfs",
                             "findings": [dh._f("ok", "last scrub clean")]}])
    assert [p["action"] for p in dh.fix_plan(rep)] == ["trim"]
