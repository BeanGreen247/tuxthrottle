#!/usr/bin/env python3
"""Replay analyzer for osu!lazer (.osr): score info, unstable rate, hit-error histogram, key and cursor stats.

Reads replays exported from osu! (.osr) and the ones lazer keeps in its own file store, so nothing has to be
exported by hand. The beatmap is found automatically in lazer's store by its MD5.

Usage:
    osu_replay.py list [-n 20]                 newest replays in your osu! data, numbered
    osu_replay.py show [N | FILE.osr] [--map FILE.osu] [--json]
                                               analyse replay N from "list" (default 1 = newest) or a file
    osu_replay.py export [N] [-o DIR]          copy replay N out of the store with a readable file name

Hit errors are re-judged from the replay's inputs (osu! and osu!mania). They match the game's own numbers
closely but not exactly: stacking offsets and slider/hold-note tails are not simulated.
Install rosu-pp-py (pip install rosu-pp-py) to also get star rating and pp.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import osu_common as c

S = c.Style
INDEX_FILE = c.TOOLS_DATA / "cache" / "beatmap-index.json"


# ---------------------------------------------------------------- beatmap index (md5 -> store file)
def beatmap_index(refresh: bool = False) -> dict[str, dict]:
    """md5 -> {path, title, mode} for every .osu in lazer's store. Store files are content-addressed
    (named by SHA-256), so a cached entry never goes stale; only new files are hashed."""
    cache: dict[str, dict] = {}
    if INDEX_FILE.is_file() and not refresh:
        try:
            cache = json.loads(INDEX_FILE.read_text())
        except (OSError, ValueError):
            cache = {}
    by_name = {v["path"].rsplit("/", 1)[-1]: k for k, v in cache.items()}
    changed = False
    for p, head in c.iter_store():
        if p.name in by_name or not c.is_osu_file(head):
            continue
        try:
            bm = c.parse_osu(p)
        except OSError:
            continue
        cache[c.md5_file(p)] = {"path": str(p), "title": bm.title, "mode": bm.mode}
        changed = True
    live = {k: v for k, v in cache.items() if Path(v["path"]).exists()}
    if changed or len(live) != len(cache):
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(live))
    return live


def store_replays() -> list[tuple[Path, c.Replay]]:
    out = []
    for p, head in c.iter_store():
        if c.is_osr_file(head):
            try:
                out.append((p, c.parse_osr(p.read_bytes())))
            except Exception:   # noqa: BLE001 - one truncated or odd file must not stop the listing
                continue
    out.sort(key=lambda pr: pr[1].timestamp, reverse=True)
    return out


# ---------------------------------------------------------------- hit-error analysis
@dataclass
class Judgement:
    time: float
    error: float | None      # ms, negative = early; None = miss
    column: int = 0


def od_cs(bm: c.Beatmap, mods: int) -> tuple[float, float]:
    od = bm.num("Difficulty", "OverallDifficulty", 5)
    cs = bm.num("Difficulty", "CircleSize", 5)
    if mods & 16:     # HR
        od, cs = min(10.0, od * 1.4), min(10.0, cs * 1.3)
    if mods & 2:      # EZ
        od, cs = od / 2, cs / 2
    return od, cs


def presses(frames: list[c.Frame], bits: tuple[int, ...]) -> list[tuple[c.Frame, int]]:
    """(frame, bit) for every rising edge of the given key bits."""
    out = []
    prev = 0
    for f in frames:
        for b in bits:
            if f.keys & b and not prev & b:
                out.append((f, b))
        prev = f.keys
    return out


def judge_std(bm: c.Beatmap, rep: c.Replay) -> list[Judgement]:
    od, cs = od_cs(bm, rep.mods)
    w50 = 200 - 10 * od
    miss_window = 400
    radius = (54.4 - 4.48 * cs) * 1.05       # small allowance for stack offsets, which aren't simulated
    flip = bool(rep.mods & 16)
    targets = [o for o in bm.objects if o.is_circle or o.is_slider]
    out: list[Judgement] = []
    i = 0
    for f, _ in presses(rep.frames, (1, 2)):       # M1/K1 set bit 1, M2/K2 set bit 2
        while i < len(targets) and f.time > targets[i].time + w50:
            out.append(Judgement(targets[i].time, None))
            i += 1
        if i >= len(targets):
            break
        o = targets[i]
        oy = 384 - o.y if flip else o.y
        if (f.x - o.x) ** 2 + (f.y - oy) ** 2 > radius ** 2:
            continue                              # clicked outside the object (or on a later one: notelock)
        dt = f.time - o.time
        if dt < -miss_window:
            continue
        out.append(Judgement(o.time, dt if dt >= -w50 else None))
        i += 1
    out.extend(Judgement(o.time, None) for o in targets[i:])
    return out


def judge_mania(bm: c.Beatmap, rep: c.Replay) -> list[Judgement]:
    od, _ = od_cs(bm, rep.mods & ~16)            # mania HR changes OD differently; close enough for UR
    keys = max(1, int(bm.num("Difficulty", "CircleSize", 4)))
    meh, miss = 151 - 3 * od, 188 - 3 * od
    columns: dict[int, list[c.HitObject]] = {}
    for o in bm.objects:
        columns.setdefault(min(keys - 1, int(o.x * keys / 512)), []).append(o)
    out: list[Judgement] = []
    pos = dict.fromkeys(columns, 0)
    prev = 0
    for f in rep.frames:
        mask = int(f.x)
        for col in range(keys):
            bit = 1 << col
            if not (mask & bit and not prev & bit) or col not in columns:
                continue
            notes = columns[col]
            while pos[col] < len(notes) and f.time > notes[pos[col]].time + meh:
                out.append(Judgement(notes[pos[col]].time, None, col))
                pos[col] += 1
            if pos[col] >= len(notes):
                continue
            dt = f.time - notes[pos[col]].time
            if dt < -miss:
                continue
            out.append(Judgement(notes[pos[col]].time, dt if abs(dt) <= meh else None, col))
            pos[col] += 1
        prev = mask
    for col, notes in columns.items():
        out.extend(Judgement(o.time, None, col) for o in notes[pos[col]:])
    return out


def error_stats(judgements: list[Judgement], rate: float) -> dict:
    errs = [j.error for j in judgements if j.error is not None]
    if len(errs) < 2:
        return {}
    mean = statistics.fmean(errs)
    # osu! shows UR in real time, so rate mods divide it (DT makes the same timing look 1.5x tighter)
    return {"hits": len(errs), "misses": sum(j.error is None for j in judgements), "mean": mean,
            "ur": statistics.pstdev(errs) * 10 / rate,
            "early": statistics.fmean([e for e in errs if e < 0] or [0]),
            "late": statistics.fmean([e for e in errs if e >= 0] or [0])}


def histogram(errors: list[float], limit: float, width: int = 40, rows: int = 8) -> list[str]:
    bins = 21
    counts = [0] * bins
    for e in errors:
        k = int((max(-limit, min(limit, e)) + limit) / (2 * limit) * (bins - 1) + 0.5)
        counts[k] += 1
    top = max(counts) or 1
    lines = []
    for r in range(rows, 0, -1):
        lines.append("  " + "".join(("█" if cnt / top * rows >= r else ("▄" if cnt / top * rows >= r - 0.5 else " ")) * 2
                                    for cnt in counts))
    lines.append(f"  {-limit:<.0f} ms (early)" + " " * (bins * 2 - 30) + f"(late) +{limit:.0f} ms")
    return lines


# ---------------------------------------------------------------- input stats
def key_stats(rep: c.Replay) -> dict:
    if rep.mode == 3:
        return {}
    names = {1: "K1/M1", 2: "K2/M2"}
    stats = {}
    for bit, name in names.items():
        downs, holds = 0, []
        start = None
        for f in rep.frames:
            if f.keys & bit and start is None:
                start = f.time
                downs += 1
            elif not f.keys & bit and start is not None:
                holds.append(f.time - start)
                start = None
        if downs:
            stats[name] = {"presses": downs, "avg_hold_ms": statistics.fmean(holds) if holds else 0.0}
    return stats


def cursor_stats(rep: c.Replay) -> dict:
    if rep.mode != 0 or len(rep.frames) < 2:
        return {}
    dist = sum(((b.x - a.x) ** 2 + (b.y - a.y) ** 2) ** 0.5 for a, b in zip(rep.frames, rep.frames[1:], strict=False))
    deltas = [b.time - a.time for a, b in zip(rep.frames, rep.frames[1:], strict=False) if b.time > a.time]
    return {"distance_px": dist, "frame_ms": statistics.median(deltas) if deltas else 0}


def pp_info(map_path: Path, rep: c.Replay) -> dict:
    rosu = c.optional_rosu()
    if not rosu:
        return {}
    try:
        bm = rosu.Beatmap(path=str(map_path))
        if rep.mode:
            bm.convert(rosu.GameMode(rep.mode), rep.mods)
        diff = rosu.Difficulty(mods=rep.mods, lazer=True).calculate(bm)
        perf = rosu.Performance(mods=rep.mods, lazer=True, combo=rep.max_combo, n300=rep.n300, n100=rep.n100,
                                n50=rep.n50, misses=rep.miss, n_geki=rep.geki, n_katu=rep.katu).calculate(bm)
        fc = rosu.Performance(mods=rep.mods, lazer=True, accuracy=rep.accuracy * 100).calculate(bm)
        return {"stars": diff.stars, "pp": perf.pp, "pp_if_fc": fc.pp}
    except Exception as e:   # noqa: BLE001 - rosu raises its own error types; pp is a nice-to-have
        return {"error": str(e)}


# ---------------------------------------------------------------- commands
def analyse(rep: c.Replay, map_path: Path | None) -> dict:
    info: dict = {
        "player": rep.player, "mode": c.MODE_NAMES.get(rep.mode, rep.mode), "mods": c.mods_to_str(rep.mods),
        "score": rep.score, "accuracy": rep.accuracy * 100, "max_combo": rep.max_combo, "perfect": rep.perfect,
        "counts": {"300": rep.n300, "100": rep.n100, "50": rep.n50, "geki": rep.geki, "katu": rep.katu,
                   "miss": rep.miss},
        "date": rep.timestamp.isoformat(timespec="seconds"), "beatmap_md5": rep.beatmap_md5,
        "keys": key_stats(rep), "cursor": cursor_stats(rep), "online_score_id": rep.score_id,
    }
    if map_path:
        bm = c.parse_osu(map_path)
        info["beatmap"] = bm.title
        judge = judge_std if rep.mode == 0 and bm.mode == 0 else judge_mania if rep.mode == 3 == bm.mode else None
        if judge:
            js = judge(bm, rep)
            info["timing"] = error_stats(js, c.clock_rate(rep.mods))
            info["_errors"] = [j.error for j in js if j.error is not None]
            od, _ = od_cs(bm, rep.mods)
            info["_limit"] = (200 - 10 * od) if rep.mode == 0 else (151 - 3 * od)
        info["difficulty"] = pp_info(map_path, rep)
    return info


def print_report(info: dict) -> None:
    b = S.BOLD
    print(f"{b}{info.get('beatmap', 'beatmap ' + info['beatmap_md5'][:12] + ' (not in your osu! data)')}{S.OFF}")
    print(f"  {info['player']}  ·  {info['mode']}  ·  +{info['mods']}  ·  {info['date'][:16].replace('T', ' ')}")
    cnt = info["counts"]
    print(f"  score {info['score']:,}   acc {info['accuracy']:.2f}%   combo {info['max_combo']}x"
          f"{' (FC)' if info['perfect'] else ''}")
    print(f"  300 {cnt['300']}  100 {cnt['100']}  50 {cnt['50']}  miss {S.RED if cnt['miss'] else ''}{cnt['miss']}{S.OFF}"
          + (f"  geki {cnt['geki']}  katu {cnt['katu']}" if info["mode"] in ("osu!mania",) else ""))
    d = info.get("difficulty") or {}
    if "stars" in d:
        print(f"  {d['stars']:.2f}★   {d['pp']:.0f}pp   ({d['pp_if_fc']:.0f}pp if FC)")
    t = info.get("timing")
    if t:
        print(f"\n{b}Timing{S.OFF} (re-judged from the inputs)")
        lean = "early" if t["mean"] < 0 else "late"
        print(f"  unstable rate {t['ur']:.1f}   mean {t['mean']:+.1f} ms ({lean})   "
              f"early avg {t['early']:.1f} ms / late avg +{t['late']:.1f} ms")
        if abs(t["mean"]) >= 5:
            print(f"  {S.YEL}you hit {abs(t['mean']):.0f} ms {lean} on average: set this beatmap's offset "
                  f"{'+' if t['mean'] < 0 else '-'}{abs(t['mean']):.0f} ms (results screen: \"Calibrate using last "
                  f"play\"), or redo the global offset if every map feels {lean}{S.OFF}")
        for line in histogram(info["_errors"], info["_limit"]):
            print(line)
    elif "beatmap" in info and info["mode"] in ("osu!taiko", "osu!catch"):
        print(f"\n  {S.DIM}hit-error analysis is available for osu! and osu!mania replays{S.OFF}")
    if info["keys"]:
        print(f"\n{b}Keys{S.OFF}")
        for name, k in info["keys"].items():
            print(f"  {name}: {k['presses']} presses, average hold {k['avg_hold_ms']:.0f} ms")
        ps = [k["presses"] for k in info["keys"].values()]
        if len(ps) == 2 and min(ps) and max(ps) / min(ps) > 1.6:
            print(f"  {S.DIM}uneven key usage: alternating more evenly helps on streams{S.OFF}")
    cur = info["cursor"]
    if cur:
        print(f"\n{b}Cursor{S.OFF}\n  travelled {cur['distance_px'] / 1000:.1f}k osu!px, "
              f"input sampled every {cur['frame_ms']:.0f} ms")
    if "beatmap" not in info:
        print(f"\n  {S.DIM}beatmap not found in your osu! data; pass --map FILE.osu for timing analysis{S.OFF}")
    elif not d:
        print(f"\n  {S.DIM}pip install rosu-pp-py for star rating and pp{S.OFF}")


def resolve(arg: str | None) -> tuple[c.Replay, Path | None]:
    if arg and not arg.isdigit():
        p = Path(arg).expanduser()
        return c.parse_osr(p.read_bytes()), p
    reps = store_replays()
    n = int(arg or 1)
    if not reps:
        sys.exit("no replays in your osu! data yet (play something, or pass a .osr file)")
    if not 1 <= n <= len(reps):
        sys.exit(f"replay {n} doesn't exist; there are {len(reps)} (see: list)")
    return reps[n - 1][1], reps[n - 1][0]


def cmd_list(args) -> int:
    reps = store_replays()[: args.n]
    idx = beatmap_index() if reps else {}
    for i, (_, r) in enumerate(reps, 1):
        title = idx.get(r.beatmap_md5, {}).get("title", r.beatmap_md5[:12] + "…")
        print(f"{i:>3}  {r.timestamp:%Y-%m-%d %H:%M}  {r.accuracy * 100:6.2f}%  {r.max_combo:>5}x  "
              f"+{c.mods_to_str(r.mods):<6} {r.player:<14} {title}")
    if not reps:
        print("no replays found in", c.data_dir() / "files")
    return 0


def cmd_show(args) -> int:
    rep, _ = resolve(args.replay)
    map_path = Path(args.map).expanduser() if args.map else None
    if not map_path:
        hit = beatmap_index().get(rep.beatmap_md5)
        map_path = Path(hit["path"]) if hit else None
    info = analyse(rep, map_path)
    if args.json:
        print(json.dumps({k: v for k, v in info.items() if not k.startswith("_")}, indent=2))
    else:
        print_report(info)
    return 0


def safe_name(s: str) -> str:
    return "".join(ch for ch in s if ch not in '/\\:*?"<>|').strip()


def cmd_export(args) -> int:
    rep, src = resolve(args.replay)
    title = beatmap_index().get(rep.beatmap_md5, {}).get("title", rep.beatmap_md5[:12])
    out_dir = Path(args.output).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / safe_name(f"{rep.player} - {title} ({rep.timestamp:%Y-%m-%d_%H-%M}).osr")
    shutil.copyfile(src, dest)
    print(dest)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools replay", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("list", help="newest replays in your osu! data")
    p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("show", help="analyse a replay")
    p.add_argument("replay", nargs="?", help="number from 'list' (default 1) or a .osr file")
    p.add_argument("--map", help=".osu file (found automatically for replays of beatmaps you have)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("export", help="copy a replay out of the store with a readable name")
    p.add_argument("replay", nargs="?")
    p.add_argument("-o", "--output", default=str(Path.home() / "osu-replays"))
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] not in ("list", "show", "export", "-h", "--help"):
        argv = ["show", *argv]          # "osu-tools replay file.osr" works too
    args = ap.parse_args(argv or ["show"])
    return {"list": cmd_list, "show": cmd_show, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
