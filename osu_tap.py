#!/usr/bin/env python3
"""Tapping speed test in the terminal: stream BPM, tap consistency (unstable rate) and key balance.

Usage:
    osu_tap.py [--taps 100 | --seconds 10] [--keys zx]

Tap your two keys (default z and x) as fast and evenly as you can; the clock starts on the first tap.
BPM is for 1/4 streams (four taps per beat), the usual way osu! players quote tapping speed.
Esc or Ctrl+C stops early. Holding a key down is ignored (key repeat isn't a tap).
"""
from __future__ import annotations

import argparse
import os
import select
import statistics
import sys
import termios
import time
import tty

S_BOLD = "\033[1m" if sys.stdout.isatty() else ""
S_OFF = "\033[0m" if sys.stdout.isatty() else ""
REPEAT_GUARD = 0.035    # same key again within 35 ms of a held key = terminal auto-repeat, not a tap


def bpm(times: list[float]) -> float:
    """1/4-stream BPM from tap timestamps (seconds)."""
    if len(times) < 2:
        return 0.0
    return (len(times) - 1) / (times[-1] - times[0]) * 60 / 4


def unstable_rate(times: list[float]) -> float:
    """osu!-style UR of the gaps between taps: 10 x their standard deviation in ms."""
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:], strict=False)]
    return statistics.pstdev(gaps) * 10 if len(gaps) > 1 else 0.0


def summary(taps: list[tuple[float, str]], keys: str) -> list[str]:
    times = [t for t, _ in taps]
    counts = {k: sum(1 for _, key in taps if key == k) for k in keys}
    gaps = [(b - a) * 1000 for a, b in zip(times, times[1:], strict=False)]
    lines = [f"{S_BOLD}{bpm(times):.0f} BPM{S_OFF} (1/4 streams)   {len(taps)} taps in {times[-1] - times[0]:.2f} s",
             f"unstable rate {unstable_rate(times):.1f}   average gap {statistics.fmean(gaps):.1f} ms"
             if gaps else "",
             "keys: " + "  ".join(f"{k}={n}" for k, n in counts.items())]
    if len(times) >= 20:
        half = len(times) // 2
        a, b = bpm(times[:half]), bpm(times[half:])
        if a and b < a * 0.93:
            lines.append(f"you slowed down {100 - b / a * 100:.0f}% in the second half ({a:.0f} -> {b:.0f} BPM): "
                         f"stamina, not speed, is the limit here")
    if len(set(counts.values())) > 1 and min(counts.values()) and max(counts.values()) / min(counts.values()) > 1.1:
        lines.append("uneven alternation: a single-tapped section or a double press")
    return [ln for ln in lines if ln]


def run(max_taps: int | None, seconds: float | None, keys: str) -> list[tuple[float, str]]:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    taps: list[tuple[float, str]] = []
    last_key, last_time = "", 0.0
    try:
        tty.setcbreak(fd)
        goal = f"{max_taps} taps" if max_taps else f"{seconds:g} seconds"
        print(f"Tap {' and '.join(keys)} ({goal}, starts on the first tap, Esc stops)")
        while True:
            timeout = 0.1
            ready, _, _ = select.select([fd], [], [], timeout)
            now = time.perf_counter()
            if ready:
                ch = os.read(fd, 1).decode(errors="ignore").lower()
                if ch in ("\x1b", "\x03", "q"):
                    break
                if ch in keys:
                    if ch == last_key and now - last_time < REPEAT_GUARD:
                        last_time = now
                        continue
                    last_key, last_time = ch, now
                    taps.append((now, ch))
            if taps:
                elapsed = now - taps[0][0]
                shown = bpm([t for t, _ in taps])
                sys.stdout.write(f"\r  {len(taps):4d} taps  {elapsed:5.1f} s  {shown:5.0f} BPM   ")
                sys.stdout.flush()
                if (max_taps and len(taps) >= max_taps) or (seconds and elapsed >= seconds):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()
    return taps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osu-tools tap", description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--taps", type=int)
    g.add_argument("--seconds", type=float)
    ap.add_argument("--keys", default="zx", help="two (or more) keys to tap, default zx")
    args = ap.parse_args(argv)
    if not sys.stdin.isatty():
        sys.exit("run this in a terminal")
    keys = args.keys.lower()
    if "q" in keys:
        sys.exit("q is the quit key; pick other keys")
    taps = run(args.taps if args.taps or args.seconds else 100, args.seconds, keys)
    if len(taps) < 3:
        print("not enough taps")
        return 1
    print("\n".join(summary(taps, keys)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
