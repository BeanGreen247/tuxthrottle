"""Tests for the player/mapper/developer tools (synthetic beatmaps, replays and skins; no osu! install needed)."""

import struct
import zlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import osu_backup
import osu_common as c
import osu_input
import osu_mapcheck
import osu_maptools
import osu_replay
import osu_report
import osu_skincheck
import osu_tap

BEAT = 500.0     # 120 BPM


def osu_text(objects: list[str], timing: list[str] | None = None, version: str = "Normal", title: str = "Song",
             extra_general: str = "", events: str = '0,0,"bg.png",0,0') -> str:
    timing = timing or [f"1000,{BEAT},4,2,0,60,1,0"]
    return "\n".join([
        "osu file format v14", "", "[General]", "AudioFilename: audio.mp3", "PreviewTime: 2000", "Mode: 0",
        extra_general, "", "[Metadata]", f"Title:{title}", f"TitleUnicode:{title}", "Artist:Artist",
        "ArtistUnicode:Artist", "Creator:me", f"Version:{version}", "Source:", "Tags:test", "",
        "[Difficulty]", "HPDrainRate:5", "CircleSize:4", "OverallDifficulty:8", "ApproachRate:9",
        "SliderMultiplier:1", "SliderTickRate:1", "", "[Events]", events, "", "[TimingPoints]", *timing, "",
        "[HitObjects]", *objects, ""])


def circles(times: list[int], x: int = 256, y: int = 192) -> list[str]:
    return [f"{x},{y},{t},1,0,0:0:0:0:" for t in times]


# ---------------------------------------------------------------- common: .osu / .osr
def test_parse_osu_and_slider_end():
    bm = c.parse_osu("x.osu", osu_text(["100,100,1000,2,0,L|200:100,1,100,0|0,0:0|0:0,0:0:0:0:"],
                                       timing=[f"1000,{BEAT},4,2,0,60,1,0", "1000,-50,4,2,0,60,0,0"]))
    assert bm.mode == 0 and bm.get("Metadata", "Version") == "Normal"
    assert bm.sv_at(1000) == 2.0
    # 100 px at SliderMultiplier 1 x SV 2 = 200 px/beat -> half a beat
    assert bm.end_time(bm.objects[0]) == pytest.approx(1000 + BEAT / 2)


def test_snap_error():
    bm = c.parse_osu("x.osu", osu_text(circles([1000])))
    assert c.snap_error(bm, 1125)[0] == pytest.approx(0)          # 1/4
    assert c.snap_error(bm, 1000 + BEAT / 3)[1] == 3               # 1/3
    err, _ = c.snap_error(bm, 1130, (1, 2, 4))
    assert err == pytest.approx(5)


def test_drain_time_excludes_breaks():
    bm = c.parse_osu("x.osu", osu_text(circles([1000, 61000]), events="2,11000,41000"))
    assert bm.drain_time() == pytest.approx(30)


def test_mods_roundtrip():
    assert c.mods_to_str(c.str_to_mods("HDDT")) == "HDDT"
    assert c.mods_to_str(c.str_to_mods("NC")) == "NC"
    assert c.clock_rate(c.str_to_mods("HT")) == 0.75


def make_replay(frames: list[c.Frame], mods: int = 0, **kw) -> c.Replay:
    base = {"mode": 0, "version": 30000019, "beatmap_md5": "a" * 32, "player": "tester", "replay_md5": "b" * 32, "n300": 10,
                "n100": 2, "n50": 0, "geki": 0, "katu": 0, "miss": 1, "score": 12345, "max_combo": 9, "perfect": False, "mods": mods,
                "life_bar": "", "timestamp": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "frames": frames, "score_id": 0,
                "seed": 0}
    base.update(kw)
    return c.Replay(**base)


def test_osr_roundtrip():
    rep = make_replay([c.Frame(0, 10, 20, 0), c.Frame(16, 12, 22, 5), c.Frame(40, 12, 22, 0)], mods=8)
    back = c.parse_osr(c.build_osr(rep))
    assert back.player == "tester" and back.mods == 8 and back.timestamp == rep.timestamp
    assert [(f.time, f.keys) for f in back.frames] == [(0, 0), (16, 5), (40, 0)]
    assert back.accuracy == pytest.approx((300 * 10 + 100 * 2) / (300 * 13))
    assert c.is_osr_file(c.build_osr(rep)[:32])


# ---------------------------------------------------------------- replay analysis
def test_judge_std_hit_errors_and_misses():
    bm = c.parse_osu("x.osu", osu_text(circles([1000, 1500, 2000, 2500])))
    frames = []
    for t, dt in ((1000, -10), (1500, 20), (2500, 0)):       # 2000 is never clicked
        frames += [c.Frame(t + dt, 256, 192, 5), c.Frame(t + dt + 30, 256, 192, 0)]
    js = osu_replay.judge_std(bm, make_replay(frames))
    errors = [j.error for j in js]
    assert errors.count(None) == 1
    assert sorted(e for e in errors if e is not None) == [-10, 0, 20]


def test_judge_std_ignores_clicks_outside_circle_and_hr_flips():
    bm = c.parse_osu("x.osu", osu_text(circles([1000], y=100)))
    outside = osu_replay.judge_std(bm, make_replay([c.Frame(1000, 256, 300, 1), c.Frame(1020, 256, 300, 0)]))
    assert outside[0].error is None
    flipped = osu_replay.judge_std(bm, make_replay([c.Frame(1000, 256, 284, 1), c.Frame(1020, 256, 284, 0)],
                                                   mods=16))
    assert flipped[0].error == 0


def test_error_stats_scale_ur_by_rate():
    js = [osu_replay.Judgement(0, e) for e in (-10, 10, -10, 10)]
    assert osu_replay.error_stats(js, 1.0)["ur"] == pytest.approx(100)
    assert osu_replay.error_stats(js, 1.5)["ur"] == pytest.approx(100 / 1.5)


def test_key_stats():
    frames = [c.Frame(0, 0, 0, 5), c.Frame(50, 0, 0, 0), c.Frame(100, 0, 0, 10), c.Frame(180, 0, 0, 0)]
    ks = osu_replay.key_stats(make_replay(frames))
    assert ks["K1/M1"] == {"presses": 1, "avg_hold_ms": 50}
    assert ks["K2/M2"]["avg_hold_ms"] == 80


# ---------------------------------------------------------------- mapcheck
def write_set(tmp_path, diffs: dict[str, str], files=("audio.mp3", "bg.png")):
    for name, text in diffs.items():
        (tmp_path / name).write_text(text)
    for f in files:
        (tmp_path / f).write_bytes(b"")
    return tmp_path


def test_mapcheck_flags_unsnapped_and_metadata(tmp_path):
    good = osu_text(circles([1000 + i * 250 for i in range(200)]))
    bad = osu_text(circles([1000, 1255]), version="Hard", title="Other")
    write_set(tmp_path, {"a.osu": good, "b.osu": bad}, files=("audio.mp3", "bg.png", "unused.txt"))
    issues, _ = osu_mapcheck.check(tmp_path)
    msgs = [(i.level, i.where, i.message) for i in issues]
    assert any(lvl == "problem" and "Title differs" in m for lvl, _, m in msgs)
    assert any(w == "Hard" and "unsnapped object" in m for _, w, m in msgs)
    assert not any(w == "Normal" and "unsnapped" in m for _, w, m in msgs)
    assert any("not used" in m and lvl == "warning" for lvl, _, m in msgs)
    assert any(w == "Hard" and "drain time" in m for _, w, m in msgs)


def test_mapcheck_missing_files_and_hitsound_index(tmp_path):
    timing = [f"1000,{BEAT},4,2,3,60,1,0"]
    text = osu_text(circles([1000 + i * 500 for i in range(80)]), timing=timing,
                    events='0,0,"missing.png",0,0\nSprite,Foreground,Centre,"sb/star.png",320,240')
    write_set(tmp_path, {"a.osu": text}, files=("audio.mp3", "soft-hitclap3.wav", "normal-hitclap9.wav"))
    issues, _ = osu_mapcheck.check(tmp_path)
    missing = next(i for i in issues if "referenced file" in i.message)
    assert "missing.png" in missing.detail and "sb/star.png" in missing.detail
    unused = next(i for i in issues if "not used" in i.message)
    assert "normal-hitclap9.wav" in unused.detail and "soft-hitclap3.wav" not in unused.detail


def test_mapcheck_concurrent_objects():
    bm = c.parse_osu("x.osu", osu_text(circles([1000, 1000, 1500])))
    assert any("same time" in i.message for i in osu_mapcheck.check_difficulty(bm))


# ---------------------------------------------------------------- maptools
def args(**kw):
    return SimpleNamespace(dry_run=False, yes=True, **kw)


def test_cleanup_removes_redundant_green_lines(tmp_path):
    timing = [f"1000,{BEAT},4,2,0,60,1,0", "2000,-100,4,2,0,60,0,0", "3000,-50,4,2,0,60,0,0",
              "4000,-50,4,2,0,60,0,0"]
    (tmp_path / "a.osu").write_text(osu_text(circles([1000, 5000]), timing=timing))
    osu_maptools.BACKUP_ROOT = tmp_path / "bk"
    osu_maptools.cmd_cleanup(args(folder=str(tmp_path)))
    bm = c.parse_osu(tmp_path / "a.osu")
    assert [t.time for t in bm.timing] == [1000, 3000]
    assert len(bm.objects) == 2 and bm.objects[1].time == 5000
    assert any((tmp_path / "bk").rglob("a.osu"))


def test_offset_shifts_everything(tmp_path):
    text = osu_text(["256,192,1000,1,0,0:0:0:0:", "256,192,2000,12,0,3000,0:0:0:0:"], events="2,1500,1800")
    (tmp_path / "a.osu").write_text(text)
    osu_maptools.BACKUP_ROOT = tmp_path / "bk"
    osu_maptools.cmd_offset(args(folder=str(tmp_path), ms=25))
    bm = c.parse_osu(tmp_path / "a.osu")
    assert bm.timing[0].time == 1025
    assert [o.time for o in bm.objects] == [1025, 2025] and bm.objects[1].end_time_field == 3025
    assert bm.breaks() == [(1525, 1825)] and bm.get("General", "PreviewTime") == "2025"


def test_resnap(tmp_path):
    (tmp_path / "a.osu").write_text(osu_text(circles([1000, 1504, 1750])))
    osu_maptools.BACKUP_ROOT = tmp_path / "bk"
    osu_maptools.cmd_resnap(args(folder=str(tmp_path), max=10))
    assert [o.time for o in c.parse_osu(tmp_path / "a.osu").objects] == [1000, 1500, 1750]


def test_copy_hitsounds_keeps_target_sv(tmp_path):
    src_timing = [f"1000,{BEAT},4,2,0,60,1,0", "2000,-100,4,1,2,90,0,0"]
    dst_timing = [f"1000,{BEAT},4,2,0,60,1,0", "1500,-50,4,2,0,60,0,0"]
    src = osu_text(["256,192,1000,1,2,1:2:0:0:", "256,192,2500,1,8,0:0:0:0:"], timing=src_timing, version="HS")
    dst = osu_text(circles([1000, 2500]), timing=dst_timing, version="Hard")
    (tmp_path / "src.osu").write_text(src)
    (tmp_path / "dst.osu").write_text(dst)
    osu_maptools.BACKUP_ROOT = tmp_path / "bk"
    osu_maptools.cmd_copy_hitsounds(args(folder=str(tmp_path), source="HS", to=[], leniency=5))
    bm = c.parse_osu(tmp_path / "dst.osu")
    assert [o.hitsound for o in bm.objects] == [2, 8]
    assert bm.objects[0].hit_sample.startswith("1:2:0")
    assert bm.sv_at(2600) == 2.0                                  # target's own SV survives
    tp = bm.sample_at(2500)
    assert (tp.sample_set, tp.sample_index, tp.volume) == (1, 2, 90)   # source's sample change arrived


def test_sync_metadata(tmp_path):
    (tmp_path / "a.osu").write_text(osu_text(circles([1000]), title="Right"))
    (tmp_path / "b.osu").write_text(osu_text(circles([1000]), title="Wrong", version="Hard"))
    osu_maptools.BACKUP_ROOT = tmp_path / "bk"
    osu_maptools.cmd_sync_metadata(args(folder=str(tmp_path), source="a.osu"))
    bm = c.parse_osu(tmp_path / "b.osu")
    assert bm.get("Metadata", "Title") == "Right" and bm.get("Metadata", "Version") == "Hard"


# ---------------------------------------------------------------- skincheck
def png(path, w, h):
    raw = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    path.write_bytes(raw + struct.pack(">I", zlib.crc32(raw[12:])) + b"\0" * 16)


def test_image_size_png(tmp_path):
    png(tmp_path / "a.png", 64, 32)
    assert c.image_size(tmp_path / "a.png") == (64, 32)


def test_skincheck(tmp_path):
    (tmp_path / "skin.ini").write_text("[General]\nName: Test\nAuthor: me\nVersion: latest\n[Colours]\nCombo1: 300,0,0\n")
    png(tmp_path / "hitcircle.png", 64, 64)
    png(tmp_path / "hitcircle@2x.png", 100, 100)                 # not 2x
    for i in (0, 1, 3):
        png(tmp_path / f"followpoint-{i}.png", 8, 8)             # frame 2 missing
    png(tmp_path / "cursor2x.png", 32, 32)
    res = osu_skincheck.check(tmp_path)
    text = " ".join(res["problems"] + res["warnings"])
    assert "hitcircle" in text and "followpoint" in text and "Combo1" in text and "cursor2x" in text
    assert res["name"] == "Test"


# ---------------------------------------------------------------- input / tap / report / backup
def test_cm_per_screen():
    assert osu_input.cm_per_screen(800, 1.0, 1920) == pytest.approx(1920 / 800 * 2.54)


def test_tap_bpm_and_ur():
    times = [i * 0.1 for i in range(11)]           # 10 taps/s = 150 BPM streams
    assert osu_tap.bpm(times) == pytest.approx(150)
    assert osu_tap.unstable_rate(times) == pytest.approx(0, abs=1e-6)


def test_redact(monkeypatch):
    monkeypatch.setattr(osu_report.getpass, "getuser", lambda: "alice")
    out = osu_report.redact("path /home/alice/x user alice token=abc123 Username = alice\nmail a@b.com")
    assert "alice" not in out and "abc123" not in out and "a@b.com" not in out


def test_backup_settings_roundtrip(tmp_path, monkeypatch):
    data = tmp_path / "osu"
    data.mkdir()
    (data / "game.ini").write_text("A = 1\n")
    monkeypatch.setattr(c, "data_dir", lambda dev=False: data)
    monkeypatch.setattr(c, "osu_running", lambda: False)
    osu_backup.cmd_backup(SimpleNamespace(dest=str(tmp_path / "bk"), settings_only=True, keep=0, force=False))
    archive = next((tmp_path / "bk").glob("*-settings.tar.gz"))
    (data / "game.ini").write_text("A = 2\n")
    osu_backup.cmd_restore(SimpleNamespace(file=str(archive), yes=True, force=False))
    assert (data / "game.ini").read_text() == "A = 1\n"
