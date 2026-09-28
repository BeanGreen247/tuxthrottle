#!/usr/bin/env python3
"""Mapset checker for mappers: finds the things modders and the ranking criteria catch, before you submit.

Point it at a mapset folder, a .osz, or a single .osu. In osu!lazer, get a folder with the editor's
File > "Edit externally", or an .osz with File > Export.

Usage:
    osu_mapcheck.py PATH [--json] [--no-info]

Checks (✗ problem, ! warning, i info):
  set:        metadata identical in every difficulty, romanised fields ASCII-only, audio format/bitrate,
              background size, video present, preview point, missing files, unused files, custom hitsound
              indexes with no samples
  difficulty: unsnapped objects (heads, slider ends, spinner/hold ends), objects before the first timing
              point, stacked-in-time objects, objects off the playfield, drain time, redundant green lines,
              silent hitsounds, combo colour brightness, difficulty values out of range
Install rosu-pp-py (pip install rosu-pp-py) to also get star ratings for the difficulty spread.
Exit code: 1 if any problem was found, else 0.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import osu_common as c

S = c.Style
PROBLEM, WARNING, INFO = "problem", "warning", "info"
MARK = {PROBLEM: f"{S.RED}✗{S.OFF}", WARNING: f"{S.YEL}!{S.OFF}", INFO: f"{S.DIM}i{S.OFF}"}
SAME_IN_ALL = ("Artist", "ArtistUnicode", "Title", "TitleUnicode", "Creator", "Source", "Tags")
HITSOUND_RE = re.compile(r"^(normal|soft|drum)-(hitnormal|hitwhistle|hitfinish|hitclap|slidertick|sliderslide|"
                         r"sliderwhistle)(\d*)\.(wav|ogg|mp3)$", re.I)
SKIN_ELEMENT_RE = re.compile(r"^(hitcircle|approachcircle|sliderb|sliderfollowcircle|sliderstartcircle|sliderendcircle|"
                             r"reversearrow|followpoint|default-|spinner-|hit0|hit50|hit100|hit300|particle|"
                             r"lighting|cursor|combo-|count|go\.|ready\.|section-|fruit-|taiko|mania|comboburst|"
                             r"sliderscorepoint|play-|pause-|fail-|scorebar-)", re.I)
EDITOR_DIVISORS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 16)
AUDIO_EXT = {".mp3", ".ogg", ".wav"}
IMAGE_EXT = {".png", ".jpg", ".jpeg"}


@dataclass
class Issue:
    level: str
    where: str        # "set" or difficulty name
    message: str
    detail: str = ""


def ts(ms: float) -> str:
    """Editor timestamp (mm:ss:mmm)."""
    ms = max(0, int(round(ms)))
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}:{ms % 1000:03d}"


def times_list(times: list[float], limit: int = 8) -> str:
    shown = ", ".join(ts(t) for t in times[:limit])
    return shown + (f" (+{len(times) - limit} more)" if len(times) > limit else "")


# ---------------------------------------------------------------- per difficulty
def check_difficulty(bm: c.Beatmap) -> list[Issue]:
    name = bm.get("Metadata", "Version", "?")
    out: list[Issue] = []

    def add(level: str, msg: str, detail: str = "") -> None:
        out.append(Issue(level, name, msg, detail))

    for key in ("HPDrainRate", "CircleSize", "OverallDifficulty", "ApproachRate"):
        v = bm.num("Difficulty", key, 5)
        if not 0 <= v <= 10:
            add(PROBLEM, f"{key} is {v:g}, must be 0-10")
    if bm.mode == 3 and not 1 <= bm.num("Difficulty", "CircleSize", 4) <= 18:
        add(PROBLEM, "osu!mania key count (CircleSize) out of range")

    if not bm.objects:
        add(PROBLEM, "no hit objects")
        return out
    reds = bm.uninherited()
    if not reds:
        add(PROBLEM, "no uninherited (red) timing point")
        return out
    if bm.timing and not bm.timing[0].uninherited:
        add(PROBLEM, "a green line comes before the first red line", ts(bm.timing[0].time))
    early = [o.time for o in bm.objects if o.time < reds[0].time - 1]
    if early:
        add(INFO, "objects before the first red line (their timing is extrapolated backwards)", times_list(early))

    # snapping against every divisor the editor offers; 1 ms is normal rounding (times are whole ms),
    # 2 ms or more is what the editor's own verify tab also reports
    heads, ends = [], []
    for o in bm.objects:
        err, _ = c.snap_error(bm, o.time, EDITOR_DIVISORS)
        if abs(err) >= 2:
            heads.append((o.time, err))
        if o.is_slider or o.is_spinner or o.is_hold:
            end = bm.end_time(o)
            err, _ = c.snap_error(bm, end, EDITOR_DIVISORS)
            if abs(err) >= 2.5:
                ends.append((end, err))
    if heads:
        add(PROBLEM, f"{len(heads)} unsnapped object(s)",
            ", ".join(f"{ts(t)} ({e:+.0f} ms)" for t, e in heads[:8]) + (" …" if len(heads) > 8 else ""))
    if ends:
        add(PROBLEM, f"{len(ends)} unsnapped slider/spinner/hold end(s)",
            ", ".join(f"{ts(t)} ({e:+.0f} ms)" for t, e in ends[:8]) + (" …" if len(ends) > 8 else ""))

    # overlapping in time
    if bm.mode == 3:
        keys = max(1, int(bm.num("Difficulty", "CircleSize", 4)))
        busy: dict[int, float] = {}
        overlaps = []
        for o in sorted(bm.objects, key=lambda o: o.time):
            col = min(keys - 1, int(o.x * keys / 512))
            if col in busy and o.time <= busy[col]:
                overlaps.append(o.time)
            busy[col] = bm.end_time(o)
        if overlaps:
            add(PROBLEM, "overlapping notes in the same column", times_list(overlaps))
    else:
        same = [b.time for a, b in zip(bm.objects, bm.objects[1:], strict=False) if a.time == b.time]
        if same:
            add(PROBLEM, "two objects at the same time", times_list(same))

    if bm.mode == 0:
        # the 512x384 playfield sits inside a 640x480 (4:3) screen, so objects may poke out a little
        r = 54.4 - 4.48 * bm.num("Difficulty", "CircleSize", 5)
        off = [o.time for o in bm.objects
               if o.x - r < -64 or o.x + r > 576 or o.y - r < -52 or o.y + r > 428]
        if off:
            add(WARNING, "objects partly off-screen on a 4:3 display", times_list(off))

    drain = bm.drain_time()
    if drain < 30:
        add(WARNING, f"drain time is {drain:.0f} s (ranked maps need at least 30 s)")

    # redundant green lines: nothing changes compared to the effective settings before them
    redundant = []
    prev: c.TimingPoint | None = None
    sv = 1.0
    for t in bm.timing:
        new_sv = 1.0 if t.uninherited else t.sv
        if (prev and not t.uninherited and abs(new_sv - sv) < 1e-9 and t.sample_set == prev.sample_set
                and t.sample_index == prev.sample_index and t.volume == prev.volume and t.effects == prev.effects):
            redundant.append(t.time)
        else:
            prev = t
        sv = new_sv
    if redundant:
        add(INFO, f"{len(redundant)} green line(s) change nothing (osu-tools maptools cleanup removes them)",
            times_list(redundant))

    quiet = [o.time for o in bm.objects if (tp := bm.sample_at(o.time)) and tp.volume < 5
             and (o.hit_sample.split(":") + ["", "", "", ""])[3].strip() in ("", "0")]
    if quiet:
        add(WARNING, "objects with hitsound volume below 5% (practically silent)", times_list(quiet))

    if bm.mode in (0, 2):
        for key, (r, g, b) in bm.colours.items():
            if key.startswith("Combo"):
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if lum < 30:
                    add(INFO, f"{key} ({r},{g},{b}) is very dark and hard to see on dim backgrounds")
                elif lum > 245:
                    add(INFO, f"{key} ({r},{g},{b}) is almost white, hit bursts/kiai flashes wash it out")
    return out


# ---------------------------------------------------------------- whole set
def referenced_files(bm: c.Beatmap) -> set[str]:
    refs = set()
    if bm.get("General", "AudioFilename"):
        refs.add(bm.get("General", "AudioFilename"))
    if bm.background():
        refs.add(bm.background())
    if bm.video():
        refs.add(bm.video()[1])
    for o in bm.objects:
        fname = (o.hit_sample.split(":") + [""] * 5)[4].strip()
        if fname:
            refs.add(fname)
    refs |= storyboard_files(bm.events)
    return refs


def storyboard_files(events: list[str]) -> set[str]:
    refs = set()
    for ev in events:
        parts = [p.strip() for p in ev.split(",")]
        kind = parts[0]
        if kind in ("Sprite", "4") and len(parts) >= 4:
            refs.add(parts[3].strip('"'))
        elif kind in ("Animation", "6") and len(parts) >= 7:
            base = parts[3].strip('"')
            stem, dot, ext = base.rpartition(".")
            try:
                frames = int(parts[6])
            except ValueError:
                frames = 0
            refs.update(f"{stem}{i}.{ext}" for i in range(frames)) if dot else refs.add(base)
        elif kind in ("Sample", "5") and len(parts) >= 4:
            refs.add(parts[3].strip('"'))
    return refs


def used_sample_indexes(bm: c.Beatmap) -> set[int]:
    idx = {t.sample_index for t in bm.timing}
    for o in bm.objects:
        parts = o.hit_sample.split(":")
        if len(parts) > 2 and parts[2].strip().isdigit():
            idx.add(int(parts[2]))
    return {i for i in idx if i > 0}


def probe_audio(path: Path) -> dict:
    out = c.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "stream=codec_name,bit_rate:format=bit_rate,duration", "-of", "json", str(path)])
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format", {})
    rate = stream.get("bit_rate") or fmt.get("bit_rate")
    return {"codec": stream.get("codec_name", "?"), "kbps": int(rate) / 1000 if rate else None,
            "duration": float(fmt.get("duration") or 0)}


def check_set(folder: Path, maps: list[c.Beatmap]) -> list[Issue]:
    out: list[Issue] = []

    def add(level: str, msg: str, detail: str = "") -> None:
        out.append(Issue(level, "set", msg, detail))

    for key in SAME_IN_ALL + ("AudioFilename", "PreviewTime"):
        sec = "General" if key in ("AudioFilename", "PreviewTime") else "Metadata"
        values = {bm.get(sec, key) for bm in maps}
        if len(values) > 1:
            add(PROBLEM, f"{key} differs between difficulties", " | ".join(sorted(repr(v) for v in values)))
    names = [bm.get("Metadata", "Version") for bm in maps]
    for n in {n for n in names if names.count(n) > 1}:
        add(PROBLEM, f"two difficulties are both named {n!r}")
    first = maps[0]
    for key in ("Artist", "Title"):
        v = first.get("Metadata", key)
        if any(ord(ch) > 127 for ch in v):
            add(PROBLEM, f"romanised {key} has non-ASCII characters (put them in {key}Unicode)", v)
    if not first.get("Metadata", "Tags"):
        add(INFO, "no tags: add genre/language/source words so people can find the map")

    files = {p.relative_to(folder).as_posix(): p for p in folder.rglob("*") if p.is_file()}
    lower = {k.lower(): k for k in files}

    def exists(name: str) -> Path | None:
        k = lower.get(name.replace("\\", "/").lower())
        return files[k] if k else None

    # audio
    audio_name = first.get("General", "AudioFilename")
    audio = exists(audio_name) if audio_name else None
    if not audio:
        add(PROBLEM, f"audio file missing: {audio_name or '(none set)'}")
    else:
        info = probe_audio(audio)
        if not info:
            add(INFO, "install ffprobe (ffmpeg) to check the audio bitrate")
        else:
            kbps, codec = info["kbps"], info["codec"]
            if codec not in ("mp3", "vorbis"):
                add(PROBLEM, f"audio is {codec}; ranked maps use MP3 or OGG (Vorbis)")
            elif kbps and codec == "mp3" and kbps > 192.5:
                add(PROBLEM, f"MP3 bitrate {kbps:.0f} kbps, the limit is 192 kbps")
            elif kbps and codec == "vorbis" and kbps > 208.5:
                add(PROBLEM, f"OGG bitrate {kbps:.0f} kbps, the limit is 208 kbps")
            elif kbps and kbps < 127.5:
                add(WARNING, f"audio bitrate {kbps:.0f} kbps is low (128 kbps minimum for ranking)")
            preview = first.num("General", "PreviewTime", -1)
            if info["duration"] and preview > info["duration"] * 1000:
                add(PROBLEM, "preview point is after the end of the audio")
    if first.num("General", "PreviewTime", -1) < 0:
        add(WARNING, "no preview point set (Timing > Set preview point)")

    # background
    bgs = {bm.background() for bm in maps}
    if None in bgs:
        add(WARNING, "a difficulty has no background image")
    for bg in sorted(b for b in bgs if b):
        p = exists(bg)
        if not p:
            add(PROBLEM, f"background missing: {bg}")
            continue
        size = c.image_size(p)
        if size and (size[0] > 2560 or size[1] > 1440):
            add(PROBLEM, f"background {bg} is {size[0]}x{size[1]}, max 2560x1440")
        elif size and (size[0] < 1024 or size[1] < 640):
            add(WARNING, f"background {bg} is only {size[0]}x{size[1]} (looks blurry, 1920x1080 is typical)")
        if p.stat().st_size > 2.5 * 1024 * 1024:
            add(PROBLEM, f"background {bg} is {c.human_size(p.stat().st_size)}, max 2.5 MB")

    # referenced vs present
    refs: set[str] = set()
    for bm in maps:
        refs |= referenced_files(bm)
    for osb in (p for p in files.values() if p.suffix.lower() == ".osb"):
        refs |= storyboard_files(osb.read_text(encoding="utf-8-sig", errors="replace").splitlines())
    missing = sorted(r for r in refs if not exists(r))
    if missing:
        add(PROBLEM, f"{len(missing)} referenced file(s) missing", ", ".join(missing[:10]))

    used_idx: set[int] = set()
    for bm in maps:
        used_idx |= used_sample_indexes(bm)
    present_idx = set()
    ref_lower = {r.replace("\\", "/").lower() for r in refs}
    unused = []
    for rel, p in files.items():
        low = rel.lower()
        if low in ref_lower or p.suffix.lower() in (".osu", ".osb"):
            continue
        m = HITSOUND_RE.match(Path(rel).name)
        if m:
            idx = int(m.group(3) or 1)
            present_idx.add(idx)
            if idx not in used_idx:
                unused.append(rel)
            elif p.stat().st_size == 0:
                continue      # 0-byte samples are the standard way to silence a default sound
            continue
        if SKIN_ELEMENT_RE.match(Path(rel).name):
            add(INFO, f"beatmap skin element: {rel}")
            continue
        unused.append(rel)
    if unused:
        add(WARNING, f"{len(unused)} file(s) not used by any difficulty (they bloat the download)",
            ", ".join(sorted(unused)[:10]))
    no_samples = sorted(i for i in used_idx if i not in present_idx and i > 1)
    if no_samples:
        add(INFO, "custom sample index(es) used without matching files (fall back to skin sounds)",
            ", ".join(str(i) for i in no_samples))
    vid = first.video()
    if vid and not exists(vid[1]):
        add(PROBLEM, f"video missing: {vid[1]}")
    return out


def star_ratings(paths: list[Path]) -> dict[str, float]:
    rosu = c.optional_rosu()
    if not rosu:
        return {}
    out = {}
    for p in paths:
        try:
            out[str(p)] = rosu.Difficulty().calculate(rosu.Beatmap(path=str(p))).stars
        except Exception:   # noqa: BLE001 - a broken diff is reported by the checks themselves
            continue
    return out


def check(path: Path) -> tuple[list[Issue], list[tuple[str, str, float | None]]]:
    """Returns (issues, [(difficulty, mode, stars)])."""
    if path.is_file() and path.suffix.lower() == ".osu":
        folder, osu_files = path.parent, [path]
    else:
        folder = path
        osu_files = sorted(folder.glob("*.osu"))
    if not osu_files:
        return [Issue(PROBLEM, "set", f"no .osu files in {folder}")], []
    maps = [c.parse_osu(p) for p in osu_files]
    issues = check_set(folder, maps) if len(osu_files) > 1 or path.is_dir() else []
    for bm in maps:
        issues += check_difficulty(bm)
    stars = star_ratings(osu_files)
    diffs = [(bm.get("Metadata", "Version", "?"), c.MODE_NAMES.get(bm.mode, "?"), stars.get(str(p)))
             for bm, p in zip(maps, osu_files, strict=True)]
    diffs.sort(key=lambda d: d[2] or 0)
    return issues, diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools mapcheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="mapset folder, .osz or .osu")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-info", action="store_true", help="hide info lines")
    args = ap.parse_args(argv)
    path = Path(args.path).expanduser()
    if not path.exists():
        sys.exit(f"{path} does not exist")
    with tempfile.TemporaryDirectory(prefix="osu-mapcheck-") as tmp:
        if path.suffix.lower() == ".osz":
            with zipfile.ZipFile(path) as z:
                z.extractall(tmp)
            path = Path(tmp)
        issues, diffs = check(path)
    if args.no_info:
        issues = [i for i in issues if i.level != INFO]
    if args.json:
        print(json.dumps({"difficulties": [{"name": n, "mode": m, "stars": s} for n, m, s in diffs],
                          "issues": [asdict(i) for i in issues]}, indent=2))
    else:
        print(f"{S.BOLD}Difficulties{S.OFF}")
        for n, m, s in diffs:
            print(f"  {f'{s:5.2f}★' if s is not None else '     '}  {n}  {S.DIM}({m}){S.OFF}")
        if diffs and diffs[0][2] is None and len(diffs) > 1:
            print(f"  {S.DIM}pip install rosu-pp-py for star ratings{S.OFF}")
        for where in ["set"] + [d[0] for d in diffs]:
            group = [i for i in issues if i.where == where]
            if not group:
                continue
            print(f"\n{S.BOLD}{'Whole set' if where == 'set' else where}{S.OFF}")
            for i in sorted(group, key=lambda i: (PROBLEM, WARNING, INFO).index(i.level)):
                print(f"  {MARK[i.level]} {i.message}")
                if i.detail:
                    print(f"      {S.DIM}{i.detail}{S.OFF}")
        n_prob = sum(i.level == PROBLEM for i in issues)
        n_warn = sum(i.level == WARNING for i in issues)
        print(f"\n{n_prob} problem(s), {n_warn} warning(s)" if issues else "\nno issues found")
    return 1 if any(i.level == PROBLEM for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
