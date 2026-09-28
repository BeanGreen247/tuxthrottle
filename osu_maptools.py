#!/usr/bin/env python3
"""Editing helpers for mappers, for the chores the osu! editor has no button for. Works on a mapset folder:
in osu!lazer use the editor's File > "Edit externally", run the command, then "Finish editing" to import.

Usage:
    osu_maptools.py sync-metadata FOLDER --from DIFF.osu
        copy Artist/Title (+Unicode), Creator, Source, Tags and the preview point to every other difficulty
    osu_maptools.py copy-hitsounds FOLDER --from DIFF.osu [--to DIFF.osu ...] [--leniency 5]
        copy hitsound additions, sample sets and volume/sample changes from one difficulty to the others;
        objects are matched by time (within --leniency ms). The targets keep their own slider velocities.
    osu_maptools.py resnap FOLDER [--max 10]
        move unsnapped objects (off by 1 < x <= --max ms) onto the nearest tick of the editor's divisors
    osu_maptools.py cleanup FOLDER
        delete green lines that change nothing
    osu_maptools.py offset FOLDER MS
        shift everything (timing, objects, breaks, preview, bookmarks, video) by MS; for re-cut audio

Every command shows what it will change and asks first (--yes skips that, --dry-run only shows). Originals
are backed up to ~/.local/share/osu-lazer-tools/map-backups/ before writing.
"""
from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
import time
from pathlib import Path

import osu_common as c

S = c.Style
BACKUP_ROOT = c.TOOLS_DATA / "map-backups"
EDITOR_DIVISORS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 16)


def load(folder: Path) -> list[c.Beatmap]:
    maps = [c.parse_osu(p) for p in sorted(folder.glob("*.osu"))]
    if not maps:
        sys.exit(f"no .osu files in {folder}")
    return maps


def pick(folder: Path, name: str) -> Path:
    p = Path(name).expanduser()
    if p.is_file():
        return p
    p = folder / name
    if p.is_file():
        return p
    # allow just the difficulty name, e.g. --from Insane
    for f in folder.glob("*.osu"):
        if c.parse_osu(f).get("Metadata", "Version").lower() == name.lower():
            return f
    sys.exit(f"difficulty {name!r} not found in {folder}")


def write_all(changes: list[tuple[c.Beatmap, list[str]]], args) -> int:
    """changes: (modified beatmap, human-readable change lines). Backs up, confirms, writes."""
    changes = [(bm, lines) for bm, lines in changes if lines]
    if not changes:
        print("nothing to change")
        return 0
    for bm, lines in changes:
        print(f"{S.BOLD}{bm.path.name}{S.OFF}")
        for ln in lines[:15]:
            print(f"  {ln}")
        if len(lines) > 15:
            print(f"  … {len(lines) - 15} more")
    if args.dry_run:
        return 0
    if not args.yes:
        try:
            if input(f"\nwrite {len(changes)} file(s)? [y/N] ").strip().lower() not in ("y", "yes"):
                print("nothing written")
                return 1
        except EOFError:
            return 1
    backup = BACKUP_ROOT / f"{changes[0][0].path.parent.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    for bm, _ in changes:
        shutil.copy2(bm.path, backup / bm.path.name)
        bm.path.write_text(bm.text(), encoding="utf-8")
    print(f"written; originals in {backup}")
    return 0


def set_kv(bm: c.Beatmap, section: str, key: str, value: str) -> bool:
    """Set Key: value inside a key/value section. True if the file changed."""
    first, end = bm.section_ranges.get(section, (None, None))
    if first is None:
        return False
    sep = ":" if section in ("Metadata", "Difficulty") else ": "
    for i in range(first, end):
        k, s, v = bm.lines[i].partition(":")
        if s and k.strip() == key:
            if v.strip() == value:
                return False
            bm.lines[i] = f"{key}{sep}{value}"
            bm.sections.setdefault(section, {})[key] = value
            return True
    insert = end
    while insert > first and not bm.lines[insert - 1].strip():
        insert -= 1
    bm.lines.insert(insert, f"{key}{sep}{value}")
    for name, (a, b) in list(bm.section_ranges.items()):
        if a > first:
            bm.section_ranges[name] = (a + 1, b + 1)
    bm.section_ranges[section] = (first, end + 1)
    bm.sections.setdefault(section, {})[key] = value
    return True


def rewrite_objects(bm: c.Beatmap) -> None:
    for o in bm.objects:
        bm.lines[o.line_no] = o.to_line()


def rewrite_timing(bm: c.Beatmap) -> None:
    bm.rewrite_section("TimingPoints", [t.to_line() for t in bm.timing])
    # object line numbers move with the section; re-parse to keep them right
    fresh = c.parse_osu(bm.path, bm.text())
    fresh.path = bm.path
    bm.__dict__.update(fresh.__dict__)


# ---------------------------------------------------------------- sync-metadata
META_KEYS = ("Artist", "ArtistUnicode", "Title", "TitleUnicode", "Creator", "Source", "Tags")


def cmd_sync_metadata(args) -> int:
    folder = Path(args.folder).expanduser()
    src = c.parse_osu(pick(folder, args.source))
    out = []
    for bm in load(folder):
        if bm.path.resolve() == src.path.resolve():
            continue
        lines = []
        for key in META_KEYS:
            if key in src.sections.get("Metadata", {}) and set_kv(bm, "Metadata", key, src.get("Metadata", key)):
                lines.append(f"{key} = {src.get('Metadata', key)!r}")
        pv = src.get("General", "PreviewTime")
        if pv and set_kv(bm, "General", "PreviewTime", pv):
            lines.append(f"PreviewTime = {pv}")
        out.append((bm, lines))
    return write_all(out, args)


# ---------------------------------------------------------------- copy-hitsounds
def split_sample(s: str) -> list[str]:
    parts = s.split(":") if s else []
    return (parts + ["0", "0", "0", "0", ""])[:5]


def copy_object_sounds(src: c.HitObject, dst: c.HitObject) -> bool:
    """Copy hitsound bits and the sample-set part of hitSample (keeps dst's custom filename/volume)."""
    before = dst.to_line()
    dst.hitsound = src.hitsound
    s_sample, d_sample = split_sample(src.hit_sample), split_sample(dst.hit_sample)
    d_sample[:3] = s_sample[:3]
    new = ":".join(d_sample)
    if dst.is_slider:
        while len(dst.params) < 6:
            dst.params.append({3: "|".join(["0"] * (dst.slides + 1)),
                               4: "|".join(["0:0"] * (dst.slides + 1))}.get(len(dst.params), "0:0:0:0:"))
        # per-node edge sounds only line up when both sliders have the same number of repeats
        if src.is_slider and src.slides == dst.slides and len(src.params) > 4:
            dst.params[3], dst.params[4] = src.params[3], src.params[4]
        dst.params[5] = new
    elif dst.is_spinner:
        if len(dst.params) < 2:
            dst.params.append(new)
        else:
            dst.params[1] = new
    elif dst.is_hold:
        end = dst.params[0].split(":", 1)[0]
        dst.params[0] = f"{end}:{new}"
    elif dst.params:
        dst.params[0] = new
    else:
        dst.params.append(new)
    return dst.to_line() != before


def effective(points: list[c.TimingPoint], t: float) -> c.TimingPoint | None:
    cur = None
    for p in points:
        if p.time <= t:
            cur = p
        else:
            break
    return cur


def merge_sample_timing(src: c.Beatmap, dst: c.Beatmap) -> list[c.TimingPoint]:
    """dst's timing (red lines, SV, kiai untouched) with src's sample set / index / volume at every point."""
    times = sorted({p.time for p in src.timing} | {p.time for p in dst.timing})
    out: list[c.TimingPoint] = []
    for t in times:
        here = [c.TimingPoint(**p.__dict__) for p in dst.timing if p.time == t]
        if not here:
            base = effective(dst.timing, t)
            if base is None:
                continue                         # before the target's first timing point
            here = [c.TimingPoint(t, -100.0 / dst.sv_at(t), base.meter, 0, 0, 100, False, base.effects)]
        s = effective(src.timing, t)
        if s:
            for p in here:
                p.sample_set, p.sample_index, p.volume = s.sample_set, s.sample_index, s.volume
        out += here
    return drop_redundant(out)


def drop_redundant(points: list[c.TimingPoint]) -> list[c.TimingPoint]:
    out: list[c.TimingPoint] = []
    prev: c.TimingPoint | None = None
    sv = 1.0
    for p in points:
        new_sv = 1.0 if p.uninherited else p.sv
        if (prev and not p.uninherited and abs(new_sv - sv) < 1e-9 and p.sample_set == prev.sample_set
                and p.sample_index == prev.sample_index and p.volume == prev.volume and p.effects == prev.effects):
            continue
        out.append(p)
        prev = p
        sv = new_sv
    return out


def cmd_copy_hitsounds(args) -> int:
    folder = Path(args.folder).expanduser()
    src = c.parse_osu(pick(folder, args.source))
    targets = [c.parse_osu(pick(folder, t)) for t in args.to] if args.to else \
        [bm for bm in load(folder) if bm.path.resolve() != src.path.resolve()]
    out = []
    for dst in targets:
        lines = []
        by_time = sorted(src.objects, key=lambda o: o.time)
        j = 0
        copied = 0
        for o in dst.objects:
            while j + 1 < len(by_time) and abs(by_time[j + 1].time - o.time) <= abs(by_time[j].time - o.time):
                j += 1
            if by_time and abs(by_time[j].time - o.time) <= args.leniency and copy_object_sounds(by_time[j], o):
                copied += 1
        if copied:
            rewrite_objects(dst)
            lines.append(f"hitsounds copied onto {copied} object(s)")
        old = [t.to_line() for t in dst.timing]
        dst.timing = merge_sample_timing(src, dst)
        if [t.to_line() for t in dst.timing] != old:
            rewrite_timing(dst)
            lines.append(f"timing points: {len(old)} -> {len(dst.timing)} (sample set / volume from {src.path.name})")
        out.append((dst, lines))
    return write_all(out, args)


# ---------------------------------------------------------------- resnap / cleanup / offset
def cmd_resnap(args) -> int:
    folder = Path(args.folder).expanduser()
    out = []
    for bm in load(folder):
        lines = []
        for o in bm.objects:
            err, div = c.snap_error(bm, o.time, EDITOR_DIVISORS)
            if 1 < abs(err) <= args.max:
                new = int(round(o.time - err))
                lines.append(f"{c_ts(o.time)} -> {c_ts(new)}  (1/{div})")
                shift = new - o.time
                o.time = new
                if o.is_spinner or o.is_hold:        # keep the length, then snap the end too
                    end = o.end_time_field + shift
                    e_err, _ = c.snap_error(bm, end, EDITOR_DIVISORS)
                    end = int(round(end - e_err)) if abs(e_err) <= args.max else end
                    if o.is_spinner:
                        o.params[0] = str(end)
                    else:
                        o.params[0] = f"{end}:{o.params[0].split(':', 1)[1]}" if ":" in o.params[0] else str(end)
        if lines:
            rewrite_objects(bm)
        out.append((bm, lines))
    return write_all(out, args)


def cmd_cleanup(args) -> int:
    folder = Path(args.folder).expanduser()
    out = []
    for bm in load(folder):
        before = len(bm.timing)
        bm.timing = drop_redundant(bm.timing)
        lines = [f"removed {before - len(bm.timing)} green line(s) that changed nothing"] if len(bm.timing) < before \
            else []
        if lines:
            rewrite_timing(bm)
        out.append((bm, lines))
    return write_all(out, args)


def c_ts(ms: float) -> str:
    ms = int(round(ms))
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    return f"{sign}{ms // 60000:02d}:{ms // 1000 % 60:02d}:{ms % 1000:03d}"


def shift_line_fields(line: str, idx: list[int], ms: int) -> str:
    parts = line.split(",")
    for i in idx:
        if i < len(parts):
            with contextlib.suppress(ValueError):
                parts[i] = str(int(float(parts[i])) + ms)
    return ",".join(parts)


def cmd_offset(args) -> int:
    folder = Path(args.folder).expanduser()
    ms = args.ms
    if any(folder.glob("*.osb")):
        print(f"{S.YEL}note: the .osb storyboard is not shifted; move it in the storyboard editor{S.OFF}")
    out = []
    for bm in load(folder):
        for t in bm.timing:
            t.time += ms
        for o in bm.objects:
            o.time += ms
            if o.is_spinner:
                o.params[0] = str(int(float(o.params[0])) + ms)
            elif o.is_hold:
                end, _, rest = o.params[0].partition(":")
                o.params[0] = f"{int(float(end)) + ms}:{rest}" if rest else str(int(float(end)) + ms)
        rewrite_objects(bm)
        a, b = bm.section_ranges["Events"]
        sb = False
        for i in range(a, b):
            line = bm.lines[i]
            kind = line.split(",", 1)[0].strip()
            if kind in ("2", "Break"):
                bm.lines[i] = shift_line_fields(line, [1, 2], ms)
            elif kind in ("1", "Video"):
                bm.lines[i] = shift_line_fields(line, [1], ms)
            elif line.startswith((" ", "_")) or kind in ("Sprite", "Animation", "Sample", "4", "5", "6"):
                sb = True
        if sb:
            print(f"{S.YEL}note: {bm.path.name} has storyboard commands in [Events]; they are not shifted{S.OFF}")
        pv = bm.num("General", "PreviewTime", -1)
        if pv >= 0:
            set_kv(bm, "General", "PreviewTime", str(int(pv) + ms))
        marks = bm.get("Editor", "Bookmarks")
        if marks:
            set_kv(bm, "Editor", "Bookmarks", ",".join(str(int(x) + ms) for x in marks.split(",") if x.strip()))
        rewrite_timing(bm)
        out.append((bm, [f"everything moved by {ms:+d} ms ({len(bm.objects)} objects, {len(bm.timing)} timing points)"]))
    return write_all(out, args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools maptools", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("folder", help="mapset folder (editor: File > Edit externally)")
    common.add_argument("--dry-run", action="store_true", help="show the changes, write nothing")
    common.add_argument("-y", "--yes", action="store_true", help="don't ask before writing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync-metadata", parents=[common], help="copy metadata from one difficulty to the rest")
    p.add_argument("--from", dest="source", required=True, help=".osu file or difficulty name")
    p = sub.add_parser("copy-hitsounds", parents=[common], help="copy hitsounds from one difficulty to others")
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--to", nargs="*", default=[], help="targets (default: every other difficulty)")
    p.add_argument("--leniency", type=int, default=5, help="ms two objects may differ and still match")
    p = sub.add_parser("resnap", parents=[common], help="snap slightly unsnapped objects")
    p.add_argument("--max", type=float, default=10, help="only move objects off by at most this many ms")
    sub.add_parser("cleanup", parents=[common], help="remove green lines that change nothing")
    p = sub.add_parser("offset", parents=[common], help="shift the whole map in time")
    p.add_argument("ms", type=int)
    args = ap.parse_args(argv)
    return {"sync-metadata": cmd_sync_metadata, "copy-hitsounds": cmd_copy_hitsounds, "resnap": cmd_resnap,
            "cleanup": cmd_cleanup, "offset": cmd_offset}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
