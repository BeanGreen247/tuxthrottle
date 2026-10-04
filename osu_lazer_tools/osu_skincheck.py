#!/usr/bin/env python3
"""Skin checker for players and skinners: finds broken HD (@2x) pairs, missing animation frames, oversized
images, skin.ini mistakes and duplicate files, and shows which gameplay elements fall back to the default skin.

Usage:
    osu_skincheck.py SKIN.osk | SKIN_FOLDER [--json]

In osu!lazer, export a skin with Settings > Skin > Export, or edit it with "Edit externally" (skin editor).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import osu_common as c

S = c.Style
IMAGE_EXT = (".png", ".jpg", ".jpeg")
SOUND_EXT = (".wav", ".ogg", ".mp3")
# the osu! (standard) elements a player sees every map; missing ones come from the default skin
CORE = ["hitcircle", "hitcircleoverlay", "approachcircle", "cursor", "sliderb0", "sliderfollowcircle",
        "reversearrow", "hit0", "hit50", "hit100", "hit300", *[f"default-{i}" for i in range(10)],
        "spinner-circle", "spinner-approachcircle", "scorebar-bg", "scorebar-colour"]
CORE_SOUNDS = ["normal-hitnormal", "normal-hitclap", "normal-hitwhistle", "normal-hitfinish", "soft-hitnormal",
               "drum-hitnormal", "combobreak"]
# how big an element can reasonably be at @2x before it's a mistake (typical HD skins are well under these)
MAX_2X = {"cursor": 512, "cursortrail": 512, "hitcircle": 512, "hitcircleoverlay": 512, "approachcircle": 512,
          "default-": 256, "hit0": 512, "hit50": 512, "hit100": 512, "hit300": 512, "sliderb": 512}
KNOWN_INI_KEYS = {
    "General": {"Name", "Author", "Version", "AnimationFramerate", "AllowSliderBallTint", "ComboBurstRandom",
                "CursorCentre", "CursorExpand", "CursorRotate", "CursorTrailRotate", "CustomComboBurstSounds",
                "HitCircleOverlayAboveNumber", "HitCircleOverlayAboveNumer", "LayeredHitSounds", "SliderStyle",
                "SliderBallFrames", "UseColorsFromSkin", "SliderTrackOverride",
                "SliderBallFlip", "SpinnerFadePlayfield", "SpinnerFrequencyModulate", "SpinnerNoBlink"},
    "Colours": None, "Fonts": None, "CatchTheBeat": None, "Mania": None,
}
FRAME_RE = re.compile(r"^(.*?)-?(\d+)$")


def stem_of(name: str) -> tuple[str, bool]:
    """('hitcircle', True) for 'hitcircle@2x.png'."""
    base = name.rsplit(".", 1)[0]
    hd = base.endswith("@2x")
    return (base[:-3] if hd else base).lower(), hd


def parse_skin_ini(text: str) -> tuple[dict[str, dict[str, str]], list[str]]:
    sections: dict[str, dict[str, str]] = {}
    problems = []
    cur = ""
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("//", 1)[0].strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            sections.setdefault(cur, {})
            continue
        key, sep, value = line.partition(":")
        if not sep:
            problems.append(f"line {n}: {raw.strip()!r} is not 'Key: value'")
            continue
        sections.setdefault(cur, {})[key.strip()] = value.strip()
    return sections, problems


def check(folder: Path) -> dict:
    files = [p for p in folder.rglob("*") if p.is_file()]
    rel = {p.relative_to(folder).as_posix(): p for p in files}
    out: dict = {"problems": [], "warnings": [], "info": [], "name": folder.name}

    # duplicates that only differ by case: which one osu! picks is not defined
    lower: dict[str, list[str]] = {}
    for r in rel:
        lower.setdefault(r.lower(), []).append(r)
    for group in lower.values():
        if len(group) > 1:
            out["problems"].append(f"files differ only by upper/lower case: {', '.join(group)}")

    ini_path = next((p for r, p in rel.items() if r.lower() == "skin.ini"), None)
    ini: dict[str, dict[str, str]] = {}
    if not ini_path:
        out["warnings"].append("no skin.ini: name/author are unknown and old (version 1.0) behaviour is used")
    else:
        ini, bad = parse_skin_ini(ini_path.read_text(encoding="utf-8-sig", errors="replace"))
        out["problems"] += [f"skin.ini {b}" for b in bad]
        gen = ini.get("General", {})
        out["name"] = gen.get("Name") or out["name"]
        out["author"] = gen.get("Author", "")
        version = gen.get("Version", "")
        if not version:
            out["warnings"].append("skin.ini has no Version: osu! treats it as 1.0 (old slider/spinner/combo "
                                   "number behaviour). Use 'Version: latest' or e.g. 2.7")
        for key in gen:
            if key not in KNOWN_INI_KEYS["General"]:
                out["info"].append(f"skin.ini [General] {key} is not a known setting (typo?)")
        for key, value in ini.get("Colours", {}).items():
            parts = [p.strip() for p in value.split(",")]
            if not 3 <= len(parts) <= 4 or not all(p.isdigit() and int(p) <= 255 for p in parts):
                out["problems"].append(f"skin.ini colour {key}: {value!r} is not R,G,B (0-255)")

    images: dict[str, dict[bool, Path]] = {}
    sounds: set[str] = set()
    for r, p in rel.items():
        name = Path(r).name
        if name.lower().endswith(IMAGE_EXT):
            stem, hd = stem_of(name)
            images.setdefault(stem, {})[hd] = p
        elif name.lower().endswith(SOUND_EXT):
            sounds.add(name.rsplit(".", 1)[0].lower())
            if p.stat().st_size == 0:
                out["info"].append(f"{r} is empty (silences that sound on purpose)")

    # HD pairs
    mismatched, sd_only, huge, unreadable = [], [], [], []
    for stem, variants in sorted(images.items()):
        sd, hd = variants.get(False), variants.get(True)
        s_sd = c.image_size(sd) if sd else None
        s_hd = c.image_size(hd) if hd else None
        for p, sz in ((sd, s_sd), (hd, s_hd)):
            if p and sz is None and p.stat().st_size > 0:
                unreadable.append(p.name)
        if "background" in stem:          # backgrounds are scaled to the screen, SD/HD sizes don't matter
            continue
        if s_sd and s_hd and (abs(s_hd[0] - 2 * s_sd[0]) > 2 or abs(s_hd[1] - 2 * s_sd[1]) > 2):
            mismatched.append(f"{stem}: {s_sd[0]}x{s_sd[1]} vs @2x {s_hd[0]}x{s_hd[1]}")
        if sd and not hd and s_sd and max(s_sd) > 1:
            sd_only.append(stem)
        size_2x = s_hd or (tuple(v * 2 for v in s_sd) if s_sd else None)
        for prefix, limit in MAX_2X.items():
            if stem.startswith(prefix) and size_2x and max(size_2x) > limit * 2:
                huge.append(f"{stem} ({size_2x[0]}x{size_2x[1]} at @2x)")
    if mismatched:
        out["problems"].append("@2x image isn't exactly twice the normal one (looks different in HD vs SD): "
                               + "; ".join(mismatched[:8]) + (" …" if len(mismatched) > 8 else ""))
    if unreadable:
        out["problems"].append("not valid PNG/JPEG (wrong extension or corrupt): " + ", ".join(unreadable[:8]))
    if huge:
        out["warnings"].append("very large elements (check they aren't accidentally oversized): "
                               + ", ".join(huge[:8]))
    if sd_only:
        out["info"].append(f"{len(sd_only)} element(s) without an @2x version (upscaled, blurry on 1080p+): "
                           + ", ".join(sd_only[:10]) + (" …" if len(sd_only) > 10 else ""))

    # animation frame gaps (name-0, name-1, ...), e.g. hit100-0..n, followpoint-0..n
    frames: dict[str, set[int]] = {}
    for stem in images:
        m = FRAME_RE.match(stem)
        if m and "-" in stem and not stem.startswith(("default-", "score-", "combo-")):
            frames.setdefault(m.group(1), set()).add(int(m.group(2)))
    for base, nums in sorted(frames.items()):
        if len(nums) > 1 and 0 in nums:
            gaps = sorted(set(range(max(nums) + 1)) - nums)
            if gaps:
                out["problems"].append(f"animation {base}-N skips frame(s) {gaps[:6]}: it stops at frame {gaps[0] - 1}")

    def present(e: str) -> bool:        # animated versions (name-0 / name0) count too
        return any(k in images for k in (e, f"{e}-0", f"{e}0", e.rstrip("0")))

    missing = [e for e in CORE if not present(e)]
    misnamed = sorted(k for k in images if k.endswith("2x") and not k.endswith("@2x"))
    if misnamed:
        out["warnings"].append("looks like a misnamed HD file (needs '@2x'): "
                               + ", ".join(f"{k}.png" for k in misnamed[:8]))
    if "default-0" not in images and any(k.startswith("default-") for k in images):
        out["problems"].append("some default-N number images exist but not all ten: numbers mix skins")
    elif missing:
        out["info"].append("uses the default skin for: " + ", ".join(missing))
    missing_snd = [s for s in CORE_SOUNDS if s not in sounds]
    if missing_snd and sounds:
        out["info"].append("default sounds used for: " + ", ".join(missing_snd))
    total = sum(p.stat().st_size for p in files)
    out["size"] = total
    if total > 50 * 1024 * 1024:
        out["warnings"].append(f"skin is {c.human_size(total)}: large skins load slowly; look for huge "
                               f"backgrounds or uncompressed WAV")
    out["counts"] = {"images": len(images), "sounds": len(sounds), "files": len(files)}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools skincheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("skin", help=".osk file or skin folder")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.skin).expanduser()
    if not path.exists():
        sys.exit(f"{path} does not exist")
    with tempfile.TemporaryDirectory(prefix="osu-skincheck-") as tmp:
        if path.is_file():
            try:
                with zipfile.ZipFile(path) as z:
                    z.extractall(tmp)
            except zipfile.BadZipFile:
                sys.exit(f"{path} is not a valid .osk (zip) file")
            root = Path(tmp)
            inner = list(root.iterdir())
            if len(inner) == 1 and inner[0].is_dir():      # skins zipped with a top-level folder
                root = inner[0]
            res = check(root)
            res["name"] = res["name"] if res.get("author") is not None else path.stem
        else:
            res = check(path)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{S.BOLD}{res['name']}{S.OFF}" + (f" by {res['author']}" if res.get("author") else "")
              + f"  {S.DIM}({res['counts']['images']} images, {res['counts']['sounds']} sounds, "
                f"{c.human_size(res['size'])}){S.OFF}")
        for key, mark in (("problems", f"{S.RED}✗{S.OFF}"), ("warnings", f"{S.YEL}!{S.OFF}"),
                          ("info", f"{S.DIM}i{S.OFF}")):
            for msg in res[key]:
                print(f"  {mark} {msg}")
        if not (res["problems"] or res["warnings"]):
            print(f"  {S.GRN}no problems found{S.OFF}")
    return 1 if res["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
