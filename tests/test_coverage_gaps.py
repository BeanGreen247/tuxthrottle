"""Coverage for modules that had zero unit tests: automount, savevault,
prefix_relocate, modelgen, kde_panel. Pure-logic / real-tmp_path-filesystem
functions only — nothing that needs root, real hardware, or a live Steam
install.
"""
import os

import tuxthrottle_automount as am
import tuxthrottle_kde_panel as kp
import tuxthrottle_modelgen as mg
import tuxthrottle_prefix_relocate as pr
import tuxthrottle_savevault as sv


# --------------------------------------------------------------------------- #
#  tuxthrottle_automount
# --------------------------------------------------------------------------- #
def test_sanitize_strips_unsafe_chars():
    assert am._sanitize("My Game Drive!") == "My_Game_Drive"
    # str.strip() removes ALL leading/trailing chars in the given set, not a
    # fixed prefix — "../../etc" -> "..\_.._etc" pre-strip, then every
    # leading '.'/'_' run is stripped down to "etc"
    assert am._sanitize("../../etc") == "etc"
    assert am._sanitize("") == ""
    assert am._sanitize("___") == ""


def test_fstab_line_ntfs():
    c = {"fstype": "ntfs", "uuid": "ABCD-1234", "target": "/mnt/Games"}
    line = am.fstab_line(c, uid=1000, gid=1000)
    assert line.startswith("UUID=ABCD-1234  /mnt/Games  ntfs3  ")
    assert "windows_names" in line
    assert "uid=1000,gid=1000" in line


def test_fstab_line_ext4_uses_common_opts_only():
    c = {"fstype": "ext4", "uuid": "X", "target": "/mnt/Data"}
    line = am.fstab_line(c, uid=1000, gid=1000)
    assert "ext4" in line
    assert "uid=" not in line  # ext4 doesn't take uid/gid mount opts


def test_candidates_filters_os_disks_and_mounted_and_removable(monkeypatch):
    monkeypatch.setattr(am, "_fstab_uuids", lambda: set())
    rows = [
        {"type": "part", "fstype": "ext4", "uuid": "root-uuid", "mountpoint": "/",
         "pkname": "sda", "name": "sda1", "path": "/dev/sda1"},
        {"type": "part", "fstype": "ntfs", "uuid": "data-uuid", "mountpoint": None,
         "pkname": "sdb", "name": "sdb1", "path": "/dev/sdb1", "label": "Games",
         "size": "1T"},
        {"type": "part", "fstype": "vfat", "uuid": "usb-uuid", "mountpoint": None,
         "pkname": "sdc", "name": "sdc1", "path": "/dev/sdc1", "rm": True},
        {"type": "part", "fstype": "swap", "uuid": "swap-uuid", "mountpoint": None,
         "pkname": "sda", "name": "sda2", "path": "/dev/sda2"},
        {"type": "disk", "fstype": "", "uuid": "", "mountpoint": None,
         "pkname": "", "name": "sdb", "path": "/dev/sdb"},
    ]
    out = am.candidates(rows)
    assert len(out) == 1
    assert out[0]["uuid"] == "data-uuid"
    assert out[0]["target"] == "/mnt/Games"


# --------------------------------------------------------------------------- #
#  tuxthrottle_savevault
# --------------------------------------------------------------------------- #
def test_human_byte_formatting():
    assert sv._human(0) == "0B"
    assert sv._human(512) == "512B"
    assert sv._human(2048) == "2.0K"
    assert sv._human(5 * 1024 * 1024) == "5.0M"


def test_stats_counts_files_and_bytes(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"1234")
    (tmp_path / "b.txt").write_bytes(b"12345678")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"12")
    n, size = sv._stats(tmp_path)
    assert n == 3
    assert size == 4 + 8 + 2


def test_check_vault_rejects_same_filesystem_as_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sv, "home_dev", lambda: os.stat(str(tmp_path)).st_dev)
    import pytest
    with pytest.raises(SystemExit, match="SEPARATE drive"):
        sv.check_vault(str(tmp_path), create=False)


def test_check_vault_accepts_a_different_device(monkeypatch, tmp_path):
    monkeypatch.setattr(sv, "home_dev", lambda: -12345)  # a device id tmp_path can't have
    vault = tmp_path / "vault"
    result = sv.check_vault(str(vault), create=True)
    assert result == vault.resolve()
    assert vault.is_dir()


# --------------------------------------------------------------------------- #
#  tuxthrottle_prefix_relocate
# --------------------------------------------------------------------------- #
def test_colon_ok_on_a_real_posix_tmpdir(tmp_path):
    # tmp_path is a real filesystem (whatever backs pytest's tmp dir, normally
    # ext4/btrfs/tmpfs) — all of those accept ':' in a filename.
    assert pr.colon_ok(tmp_path) is True


def test_classify_absent_when_no_compatdata(tmp_path):
    # classify() falls back to probing the *parent* compatdata dir when the
    # specific prefix is missing — "absent" only happens when that parent
    # doesn't exist either (steamapps/ itself, with no compatdata at all).
    lib = tmp_path / "SteamLibrary"
    (lib / "steamapps").mkdir(parents=True)
    status, path = pr.classify(lib, "12345")
    assert status == "absent"
    assert path == lib / "steamapps" / "compatdata" / "12345"


def test_classify_ok_when_only_parent_compatdata_exists(tmp_path):
    lib = tmp_path / "SteamLibrary"
    (lib / "steamapps" / "compatdata").mkdir(parents=True)
    status, _path = pr.classify(lib, "12345")
    assert status == "ok"


def test_classify_ok_when_prefix_dir_exists_and_colon_ok(tmp_path):
    lib = tmp_path / "SteamLibrary"
    prefix = lib / "steamapps" / "compatdata" / "999"
    prefix.mkdir(parents=True)
    status, path = pr.classify(lib, "999")
    assert status == "ok"
    assert path == prefix


def test_classify_symlink(tmp_path):
    lib = tmp_path / "SteamLibrary"
    (lib / "steamapps" / "compatdata").mkdir(parents=True)
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = lib / "steamapps" / "compatdata" / "42"
    link.symlink_to(real)
    status, path = pr.classify(lib, "42")
    assert status == "symlink"
    assert path == link


# --------------------------------------------------------------------------- #
#  tuxthrottle_modelgen
# --------------------------------------------------------------------------- #
def test_slugify_basic():
    assert mg._slugify("Dell G15 5515") == "dell-g15-5515"
    assert mg._slugify("") == "unknown-model"
    assert mg._slugify("   ") == "unknown-model"
    assert mg._slugify("ALL-CAPS_Weird!!Chars") == "all-caps-weird-chars"


def test_pci_id_reads_vendor_and_device(tmp_path):
    d = tmp_path / "0000:01:00.0"
    d.mkdir()
    (d / "vendor").write_text("0x10de\n")
    (d / "device").write_text("0x25a0\n")
    assert mg._pci_id(str(d)) == "10de:25a0"


def test_pci_id_none_when_missing():
    assert mg._pci_id(None) is None
    assert mg._pci_id("/no/such/dir") is None


# --------------------------------------------------------------------------- #
#  tuxthrottle_kde_panel
# --------------------------------------------------------------------------- #
def test_csv_parses_and_strips_and_drops_empties():
    assert kp._csv("a, b ,, c") == ["a", "b", "c"]
    assert kp._csv("") == []
    assert kp._csv("solo") == ["solo"]
