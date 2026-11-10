"""Shared helpers for osu-lazer-tools: data folder lookup and parsers for .osu beatmaps and .osr replays.

Standard library only. Formats follow the osu! wiki "File formats" pages (osu (file format), osr (file format)).
"""
from __future__ import annotations

import contextlib
import hashlib
import lzma
import os
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

HOME_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
TOOLS_DATA = HOME_DATA / "osu-lazer-tools"


# ---------------------------------------------------------------- osu! data folder
def data_dir(dev: bool = False) -> Path:
    """osu!lazer's data folder. Honours a custom location from Settings > Maintenance (storage.ini FullPath).

    dev=True gives the folder debug builds from source use (osu-development).
    """
    base = HOME_DATA / ("osu-development" if dev else "osu")
    storage = base / "storage.ini"
    if storage.is_file():
        for line in storage.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "FullPath" and value.strip():
                return Path(value.strip())
    return base


def osu_running() -> bool:
    """True if an osu!lazer process is running (its main thread is named "osu!")."""
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                if (d / "comm").read_text().strip() == "osu!":
                    return True
            except OSError:
                continue
    return False


def run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def md5_file(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def iter_store(root: Path | None = None):
    """Yield (path, first 32 bytes) for every file in lazer's hashed file store (data/files/a/ab/abc...)."""
    root = root or data_dir() / "files"
    for dirpath, _, names in os.walk(root):
        for name in names:
            p = Path(dirpath, name)
            try:
                with p.open("rb") as fh:
                    yield p, fh.read(32)
            except OSError:
                continue


def is_osu_file(head: bytes) -> bool:
    return head.lstrip(b"\xef\xbb\xbf").startswith(b"osu file format")


def is_osr_file(head: bytes) -> bool:
    # mode byte (0-3), int32 version, then the beatmap MD5 as a 32-char osu! string (0x0b, length 32)
    return len(head) > 7 and head[0] <= 3 and head[5] == 0x0B and head[6] == 32


def find_beatmap_by_md5(md5: str, root: Path | None = None) -> Path | None:
    """Find the .osu file with this MD5 in lazer's file store (the store names files by SHA-256, not MD5)."""
    for p, head in iter_store(root):
        if is_osu_file(head) and md5_file(p) == md5:
            return p
    return None


# ---------------------------------------------------------------- mods
MOD_BITS = [
    ("NF", 1), ("EZ", 2), ("TD", 4), ("HD", 8), ("HR", 16), ("SD", 32), ("DT", 64), ("RX", 128), ("HT", 256),
    ("NC", 512), ("FL", 1024), ("AT", 2048), ("SO", 4096), ("AP", 8192), ("PF", 16384), ("4K", 32768),
    ("5K", 65536), ("6K", 131072), ("7K", 262144), ("8K", 524288), ("FI", 1048576), ("RD", 2097152),
    ("CN", 4194304), ("TP", 8388608), ("9K", 16777216), ("CO", 33554432), ("1K", 67108864), ("3K", 134217728),
    ("2K", 268435456), ("V2", 536870912), ("MR", 1073741824),
]


def mods_to_str(mods: int) -> str:
    names = [n for n, bit in MOD_BITS if mods & bit]
    if "NC" in names:
        names.remove("DT")
    if "PF" in names:
        names.remove("SD")
    return "".join(names) or "NM"


def str_to_mods(text: str) -> int:
    text = text.upper().replace("+", "").replace(",", "")
    lookup = dict(MOD_BITS)
    mods = 0
    for i in range(0, len(text), 2):
        name = text[i:i + 2]
        if name == "NM":
            continue
        if name not in lookup:
            raise ValueError(f"unknown mod {name!r}")
        mods |= lookup[name]
        if name == "NC":
            mods |= lookup["DT"]
        if name == "PF":
            mods |= lookup["SD"]
    return mods


def clock_rate(mods: int) -> float:
    if mods & 64:      # DT / NC
        return 1.5
    if mods & 256:     # HT
        return 0.75
    return 1.0


# ---------------------------------------------------------------- .osu beatmaps
@dataclass
class TimingPoint:
    time: float
    beat_length: float
    meter: int = 4
    sample_set: int = 0
    sample_index: int = 0
    volume: int = 100
    uninherited: bool = True
    effects: int = 0

    @property
    def sv(self) -> float:
        """Slider velocity multiplier of an inherited point (beat_length is -100/sv)."""
        return 1.0 if self.uninherited or self.beat_length >= 0 else -100.0 / self.beat_length

    def to_line(self) -> str:
        t = int(self.time) if float(self.time).is_integer() else self.time
        bl = repr(self.beat_length) if not float(self.beat_length).is_integer() else str(int(self.beat_length))
        return (f"{t},{bl},{self.meter},{self.sample_set},{self.sample_index},{self.volume},"
                f"{1 if self.uninherited else 0},{self.effects}")


@dataclass
class HitObject:
    x: float
    y: float
    time: int
    type: int
    hitsound: int
    params: list[str]          # everything after the hitsound field, unparsed
    line_no: int = -1          # index into Beatmap.lines

    @property
    def is_circle(self) -> bool:
        return bool(self.type & 1)

    @property
    def is_slider(self) -> bool:
        return bool(self.type & 2)

    @property
    def is_spinner(self) -> bool:
        return bool(self.type & 8)

    @property
    def is_hold(self) -> bool:
        return bool(self.type & 128)

    @property
    def new_combo(self) -> bool:
        return bool(self.type & 4)

    @property
    def slides(self) -> int:
        return int(self.params[1]) if self.is_slider and len(self.params) > 1 else 0

    @property
    def pixel_length(self) -> float:
        return float(self.params[2]) if self.is_slider and len(self.params) > 2 else 0.0

    @property
    def end_time_field(self) -> int | None:
        """End time stored in the line itself (spinners and mania holds)."""
        if self.is_spinner and self.params:
            return int(float(self.params[0]))
        if self.is_hold and self.params:
            return int(float(self.params[0].split(":", 1)[0]))
        return None

    @property
    def hit_sample(self) -> str:
        """The trailing normalSet:additionSet:index:volume:filename field ("" if absent)."""
        if self.is_slider:
            return self.params[5] if len(self.params) > 5 else ""
        if self.is_spinner:
            return self.params[1] if len(self.params) > 1 else ""
        if self.is_hold:
            return self.params[0].split(":", 1)[1] if self.params and ":" in self.params[0] else ""
        return self.params[0] if self.params else ""

    def to_line(self) -> str:
        x = int(self.x) if float(self.x).is_integer() else self.x
        y = int(self.y) if float(self.y).is_integer() else self.y
        return ",".join([str(x), str(y), str(self.time), str(self.type), str(self.hitsound), *self.params])


@dataclass
class Beatmap:
    path: Path | None
    lines: list[str]
    version: int = 14
    sections: dict[str, dict[str, str]] = field(default_factory=dict)   # key: value sections
    events: list[str] = field(default_factory=list)
    timing: list[TimingPoint] = field(default_factory=list)
    objects: list[HitObject] = field(default_factory=list)
    colours: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    section_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)   # name -> (first, end) line idx

    def get(self, section: str, key: str, default: str = "") -> str:
        return self.sections.get(section, {}).get(key, default)

    def num(self, section: str, key: str, default: float) -> float:
        try:
            return float(self.get(section, key, str(default)))
        except ValueError:
            return default

    @property
    def mode(self) -> int:
        return int(self.num("General", "Mode", 0))

    @property
    def title(self) -> str:
        m = self.sections.get("Metadata", {})
        return f"{m.get('Artist', '?')} - {m.get('Title', '?')} [{m.get('Version', '?')}]"

    # ---- timing
    def uninherited(self) -> list[TimingPoint]:
        return [t for t in self.timing if t.uninherited]

    def red_at(self, time: float) -> TimingPoint | None:
        reds = self.uninherited()
        cur = reds[0] if reds else None
        for t in reds:
            if t.time <= time:
                cur = t
            else:
                break
        return cur

    def sv_at(self, time: float) -> float:
        sv = 1.0
        for t in self.timing:
            if t.time > time:
                break
            sv = 1.0 if t.uninherited else t.sv
        return sv

    def sample_at(self, time: float) -> TimingPoint | None:
        """Timing point whose sample settings apply at `time` (the last one at or before it)."""
        cur = self.timing[0] if self.timing else None
        for t in self.timing:
            if t.time <= time + 5:     # osu! uses a small lookahead for hitsound sampling
                cur = t
            else:
                break
        return cur

    def slider_duration(self, obj: HitObject) -> float:
        red = self.red_at(obj.time)
        if not red:
            return 0.0
        mult = self.num("Difficulty", "SliderMultiplier", 1.4)
        return obj.pixel_length / (mult * 100 * self.sv_at(obj.time)) * red.beat_length * obj.slides

    def end_time(self, obj: HitObject) -> float:
        if obj.is_slider:
            return obj.time + self.slider_duration(obj)
        end = obj.end_time_field
        return float(end) if end is not None else float(obj.time)

    def breaks(self) -> list[tuple[int, int]]:
        out = []
        for ev in self.events:
            parts = ev.split(",")
            if parts[0].strip() in ("2", "Break") and len(parts) >= 3:
                with contextlib.suppress(ValueError):
                    out.append((int(float(parts[1])), int(float(parts[2]))))
        return out

    def background(self) -> str | None:
        for ev in self.events:
            parts = [p.strip() for p in ev.split(",")]
            if parts[0] == "0" and len(parts) >= 3:
                return parts[2].strip('"')
        return None

    def video(self) -> tuple[int, str] | None:
        for ev in self.events:
            parts = [p.strip() for p in ev.split(",")]
            if parts[0] in ("1", "Video") and len(parts) >= 3:
                return int(float(parts[1])), parts[2].strip('"')
        return None

    def drain_time(self) -> float:
        """Seconds between the first and last object, minus breaks (the value ranking rules use)."""
        if not self.objects:
            return 0.0
        start = self.objects[0].time
        end = max(self.end_time(o) for o in self.objects)
        brk = sum(max(0, min(e, end) - max(s, start)) for s, e in self.breaks())
        return max(0.0, (end - start - brk) / 1000)

    # ---- writing
    def rewrite_section(self, name: str, new_lines: list[str]) -> None:
        """Replace the body of [name] in self.lines (keeps the header and trailing blank line)."""
        first, end = self.section_ranges[name]
        # keep blank lines at the end of the section so the file layout stays the same
        tail = end
        while tail > first and not self.lines[tail - 1].strip():
            tail -= 1
        self.lines[first:tail] = new_lines
        delta = len(new_lines) - (tail - first)
        for k, (a, b) in list(self.section_ranges.items()):
            if a > first:
                self.section_ranges[k] = (a + delta, b + delta)
        self.section_ranges[name] = (first, end + delta)

    def text(self) -> str:
        return "\r\n".join(self.lines) + "\r\n"


KV_SECTIONS = {"General", "Editor", "Metadata", "Difficulty"}


def parse_osu(source: str | Path, text: str | None = None) -> Beatmap:
    path = Path(source) if text is None or isinstance(source, Path) else None
    if text is None:
        text = Path(source).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    bm = Beatmap(path=path, lines=lines)
    if lines and lines[0].lower().startswith("osu file format v"):
        with contextlib.suppress(ValueError):
            bm.version = int(lines[0].strip().rsplit("v", 1)[1])
    section = ""
    start = 0
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            if section:
                bm.section_ranges[section] = (start, i)
            section = line[1:-1]
            start = i + 1
            continue
        if not line or line.startswith("//"):
            continue
        if section in KV_SECTIONS:
            key, sep, value = line.partition(":")
            if sep:
                bm.sections.setdefault(section, {})[key.strip()] = value.strip()
        elif section == "Events":
            bm.events.append(line)
        elif section == "TimingPoints":
            p = line.split(",")
            with contextlib.suppress(ValueError, IndexError):
                bm.timing.append(TimingPoint(
                    time=float(p[0]), beat_length=float(p[1]),
                    meter=int(p[2]) if len(p) > 2 else 4,
                    sample_set=int(p[3]) if len(p) > 3 else 0,
                    sample_index=int(p[4]) if len(p) > 4 else 0,
                    volume=int(p[5]) if len(p) > 5 else 100,
                    uninherited=(p[6].strip() != "0") if len(p) > 6 else True,
                    effects=int(p[7]) if len(p) > 7 else 0))
        elif section == "Colours":
            key, sep, value = line.partition(":")
            try:
                r, g, b = (int(v) for v in value.split(",")[:3])
                bm.colours[key.strip()] = (r, g, b)
            except ValueError:
                pass
        elif section == "HitObjects":
            p = line.split(",")
            with contextlib.suppress(ValueError, IndexError):
                bm.objects.append(HitObject(float(p[0]), float(p[1]), int(float(p[2])), int(p[3]), int(p[4]),
                                            p[5:], line_no=i))
    if section:
        bm.section_ranges[section] = (start, len(lines))
    bm.timing.sort(key=lambda t: (t.time, not t.uninherited))
    return bm


def snap_error(bm: Beatmap, time: float, divisors: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 16)) -> tuple[float, int]:
    """(ms off the nearest tick, divisor of that tick) using the uninherited point in effect at `time`."""
    red = bm.red_at(time)
    if not red or red.beat_length <= 0:
        return 0.0, 1
    best = (float("inf"), 1)
    for div in divisors:
        step = red.beat_length / div
        n = round((time - red.time) / step)
        err = time - (red.time + n * step)
        if abs(err) < abs(best[0]) - 1e-9:
            best = (err, div)
    return best


# ---------------------------------------------------------------- .osr replays
@dataclass
class Frame:
    time: int          # absolute ms (beatmap time)
    x: float
    y: float
    keys: int


@dataclass
class Replay:
    mode: int
    version: int
    beatmap_md5: str
    player: str
    replay_md5: str
    n300: int
    n100: int
    n50: int
    geki: int
    katu: int
    miss: int
    score: int
    max_combo: int
    perfect: bool
    mods: int
    life_bar: str
    timestamp: datetime
    frames: list[Frame]
    score_id: int
    seed: int | None = None

    @property
    def accuracy(self) -> float:
        n300, n100, n50, geki, katu, miss = self.n300, self.n100, self.n50, self.geki, self.katu, self.miss
        if self.mode == 0:
            total = n300 + n100 + n50 + miss
            return (300 * n300 + 100 * n100 + 50 * n50) / (300 * total) if total else 0.0
        if self.mode == 1:
            total = n300 + n100 + miss
            return (n300 + 0.5 * n100) / total if total else 0.0
        if self.mode == 2:
            total = n300 + n100 + n50 + katu + miss
            return (n300 + n100 + n50) / total if total else 0.0
        total = geki + n300 + katu + n100 + n50 + miss
        return (300 * (geki + n300) + 200 * katu + 100 * n100 + 50 * n50) / (300 * total) if total else 0.0


MODE_NAMES = {0: "osu!", 1: "osu!taiko", 2: "osu!catch", 3: "osu!mania"}


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        vals = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return vals[0] if len(vals) == 1 else vals

    def string(self) -> str:
        flag = self.unpack("B")
        if flag == 0:
            return ""
        if flag != 0x0B:
            raise ValueError(f"bad string marker {flag:#x} at {self.pos - 1}")
        length = shift = 0
        while True:
            b = self.unpack("B")
            length |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        s = self.data[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return s


def _ticks_to_datetime(ticks: int) -> datetime:
    try:
        return datetime(1, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
    except OverflowError:
        return datetime(1970, 1, 1, tzinfo=UTC)


def parse_frames(text: str) -> tuple[list[Frame], int | None]:
    frames: list[Frame] = []
    seed = None
    t = 0
    for chunk in text.split(","):
        parts = chunk.split("|")
        if len(parts) < 4:
            continue
        try:
            w, x, y, z = int(parts[0]), float(parts[1]), float(parts[2]), int(float(parts[3]))
        except ValueError:
            continue
        if w == -12345:
            seed = z
            continue
        t += w
        frames.append(Frame(t, x, y, z))
    return frames, seed


def parse_osr(data: bytes) -> Replay:
    r = _Reader(data)
    mode = r.unpack("B")
    version = r.unpack("i")
    beatmap_md5 = r.string()
    player = r.string()
    replay_md5 = r.string()
    n300, n100, n50, geki, katu, miss = r.unpack("6h")
    score = r.unpack("i")
    max_combo = r.unpack("h")
    perfect = bool(r.unpack("B"))
    mods = r.unpack("i")
    life_bar = r.string()
    ticks = r.unpack("q")
    length = r.unpack("i")
    blob = data[r.pos:r.pos + length]
    r.pos += length
    frames: list[Frame] = []
    seed = None
    if length > 0:
        frames, seed = parse_frames(lzma.decompress(blob, format=lzma.FORMAT_ALONE).decode("ascii", errors="replace"))
    score_id = r.unpack("q") if r.pos + 8 <= len(data) else 0
    return Replay(mode, version, beatmap_md5, player, replay_md5, n300, n100, n50, geki, katu, miss,
                  score, max_combo, perfect, mods, life_bar, _ticks_to_datetime(ticks), frames, score_id, seed)


def _osr_string(s: str) -> bytes:
    if not s:
        return b"\x00"
    raw = s.encode()
    n = len(raw)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return b"\x0b" + bytes(out) + raw


def build_osr(rep: Replay) -> bytes:
    """Encode a Replay (used by the tests; also handy for writing trimmed replays)."""
    body = []
    prev = 0
    for f in rep.frames:
        body.append(f"{f.time - prev}|{f.x:g}|{f.y:g}|{f.keys}")
        prev = f.time
    if rep.seed is not None:
        body.append(f"-12345|0|0|{rep.seed}")
    blob = lzma.compress(",".join(body).encode(), format=lzma.FORMAT_ALONE)
    ticks = int((rep.timestamp - datetime(1, 1, 1, tzinfo=UTC)).total_seconds() * 10_000_000)
    return b"".join([
        struct.pack("<Bi", rep.mode, rep.version), _osr_string(rep.beatmap_md5), _osr_string(rep.player),
        _osr_string(rep.replay_md5),
        struct.pack("<6hihBi", rep.n300, rep.n100, rep.n50, rep.geki, rep.katu, rep.miss, rep.score,
                    rep.max_combo, rep.perfect, rep.mods),
        _osr_string(rep.life_bar), struct.pack("<qi", ticks, len(blob)), blob, struct.pack("<q", rep.score_id),
    ])


# ---------------------------------------------------------------- output
class Style:
    """ANSI colours, off when stdout isn't a terminal or NO_COLOR is set."""
    on = os.isatty(1) and "NO_COLOR" not in os.environ
    RED = "\033[31m" if on else ""
    YEL = "\033[33m" if on else ""
    GRN = "\033[32m" if on else ""
    DIM = "\033[2m" if on else ""
    BOLD = "\033[1m" if on else ""
    OFF = "\033[0m" if on else ""


def optional_rosu():
    """rosu-pp-py (pip install rosu-pp-py) gives lazer-accurate star rating and pp. None if not installed."""
    try:
        import rosu_pp_py  # type: ignore[import-not-found]
        return rosu_pp_py
    except ImportError:
        return None


# ---------------------------------------------------------------- images
def image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a PNG or JPEG from its header, None if unreadable."""
    try:
        with path.open("rb") as fh:
            head = fh.read(26)
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                return struct.unpack(">II", head[16:24])
            if not head.startswith(b"\xff\xd8"):
                return None
            fh.seek(2)
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                if marker[1] in (0xD8, 0x01) or 0xD0 <= marker[1] <= 0xD7:
                    continue
                size = struct.unpack(">H", fh.read(2))[0]
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">xHH", fh.read(5))
                    return w, h
                fh.seek(size - 2, 1)
    except (OSError, struct.error):
        return None
