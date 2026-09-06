#!/usr/bin/env python3
"""ProtonDB compatibility-tier lookup for a Steam AppID.

Uses ProtonDB's public (unofficial, widely relied-on by community Linux
gaming tools) summary endpoint:

    https://www.protondb.com/api/v1/reports/summaries/<appid>.json
    -> {"tier": "gold", "trendingTier": "platinum",
        "confidence": "strong", "score": 0.71, "total": 2034}

Stdlib only (urllib) — no extra dependency for a feature that's cosmetic
(a badge next to a game's name), and results are cached to disk so the
Setup Games tab doesn't hit the network on every open/redraw. Offline or
rate-limited: returns None, callers just don't show a badge.

    tuxthrottle_protondb.py <appid> [--json] [--no-cache]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE_TTL_S = 7 * 24 * 3600     # tiers move slowly; a week-old badge is fine
REQUEST_TIMEOUT_S = 5

TIER_LABELS = {
    "platinum": "Platinum", "gold": "Gold", "silver": "Silver",
    "bronze": "Bronze", "borked": "Borked", "pending": "Pending",
}


def _cache_dir() -> Path:
    return Path.home() / ".cache" / "tuxthrottle" / "protondb"


def _cache_path(appid: str) -> Path:
    return _cache_dir() / f"{appid}.json"


def _read_cache(appid: str, max_age: float) -> dict | None:
    p = _cache_path(appid)
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text())
        if time.time() - data.get("_fetched", 0) > max_age:
            return None
        return data
    except (OSError, ValueError):
        return None


def _write_cache(appid: str, data: dict) -> None:
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        data = {**data, "_fetched": time.time()}
        _cache_path(appid).write_text(json.dumps(data))
    except OSError:
        pass


def lookup(appid: str, use_cache: bool = True) -> dict | None:
    """Return {'tier', 'trendingTier', 'confidence', 'score', 'total'} or
    None (no data / unreachable / not a Steam game with reports)."""
    if not str(appid).isdigit():
        return None
    if use_cache:
        cached = _read_cache(appid, CACHE_TTL_S)
        if cached is not None:
            return cached
    url = f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TuxThrottle"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        # offline / rate-limited / no report for this appid — negative-cache
        # briefly so a bad connection doesn't retry every single redraw
        _write_cache(appid, {"tier": None})
        return None
    _write_cache(appid, data)
    return data


def label(data: dict | None) -> str:
    """Short display string for a GUI badge, e.g. 'ProtonDB: Gold'."""
    if not data or not data.get("tier"):
        return ""
    tier = TIER_LABELS.get(data["tier"], data["tier"].title())
    return f"ProtonDB: {tier}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("appid")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()
    data = lookup(a.appid, use_cache=not a.no_cache)
    if a.json:
        print(json.dumps(data))
    else:
        print(label(data) or "no ProtonDB data")
    return 0 if data else 1


if __name__ == "__main__":
    raise SystemExit(main())
