"""games.json sanity: every step is well-formed and every bundled helper it calls exists."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAMES = json.loads((ROOT / "config" / "games.json").read_text())


def test_steps_well_formed():
    for gid, game in GAMES.items():
        ids = [s["id"] for s in game["steps"]]
        assert len(ids) == len(set(ids)), gid
        for s in game["steps"]:
            assert s.get("title"), (gid, s["id"])
            assert s.get("run") or s.get("manual"), (gid, s["id"])


def test_bundled_helpers_exist():
    for game in GAMES.values():
        for s in game["steps"]:
            for key in ("run", "check", "show_if", "copy"):
                for name in re.findall(r"\{TOOLKIT_DIR\}/(\S+?\.(?:py|sh))", s.get(key, "")):
                    assert (ROOT / name).is_file(), name


def test_osu_card_present():
    osu = GAMES["OsuLazer"]
    assert {s["id"] for s in osu["steps"]} >= {"install", "g15conf", "settings"}


OSU_SHIPPED = {"osu-lazer-launcher.sh", "osu_lazer_diag.py", "osu_lazer_settings.py", "LICENSE"}


def test_osu_card_only_uses_shipped_files():
    """install.sh / the spec prune osu_lazer_tools down to the performance setup; the card must not need more."""
    used = set(re.findall(r"osu_lazer_tools/([\w.\-]+)", json.dumps(GAMES["OsuLazer"])))
    assert used <= OSU_SHIPPED
    for f in (ROOT / "install.sh", ROOT / "packaging" / "tuxthrottle.spec"):
        text = f.read_text()
        assert all(f"! -name {name}" in text for name in OSU_SHIPPED), f.name
