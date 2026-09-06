"""Rolling session-history jsonl (Phase 3b) — daemon side."""
import json

import tuxthrottle_powerd as pd


def test_append_and_cap(tmp_path):
    hp = tmp_path / "sessions.jsonl"
    for i in range(60):
        pd._append_session_history(hp, {"game": f"g{i}", "ended": i}, keep=50)
    lines = hp.read_text().splitlines()
    assert len(lines) == 50
    assert json.loads(lines[0])["game"] == "g10"    # oldest kept
    assert json.loads(lines[-1])["game"] == "g59"   # newest


def test_survives_corrupt_existing(tmp_path):
    hp = tmp_path / "sessions.jsonl"
    hp.write_text("not json\n{partial\n")
    pd._append_session_history(hp, {"game": "real", "ended": 1})
    lines = hp.read_text().splitlines()
    assert json.loads(lines[-1])["game"] == "real"
