"""tuxthrottle_lutris_env: merge/unmerge rules and a full apply/remove round
trip on a fake Lutris install under tmp_path."""
import sqlite3

import pytest

import tuxthrottle_lutris_env as le

yaml = pytest.importorskip("yaml")


def test_merge_keeps_existing_unless_overwrite():
    env = {"DXVK_HUD": "fps"}
    want = le.wanted_env("/p")
    changed = le.merge(env, want, overwrite=False)
    assert env["DXVK_HUD"] == "fps"
    assert "DXVK_HUD" not in changed and len(changed) == len(want) - 1
    assert le.merge(env, want, overwrite=True) == ["DXVK_HUD"]
    assert env["DXVK_HUD"] == "compiler"


def test_unmerge_only_drops_our_values():
    env = {**le.wanted_env("/p"), "__GL_SHADER_DISK_CACHE_PATH": "/mine", "OTHER": "x"}
    le.unmerge(env, le.wanted_env("/p"))
    assert env == {"__GL_SHADER_DISK_CACHE_PATH": "/mine", "OTHER": "x"}


def test_cache_dir_prefers_prefix():
    assert le.cache_dir_for({"game": {"prefix": "/pfx"}}, "/dir") == "/pfx"
    assert le.cache_dir_for({"game": {}}, "/dir") == "/dir"
    assert le.cache_dir_for({}, "") == ""


def test_apply_then_remove_round_trip(tmp_path, monkeypatch):
    games = tmp_path / "games"
    games.mkdir()
    db = tmp_path / "pga.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE games (name, runner, directory, configpath, installed)")
    con.execute("INSERT INTO games VALUES ('WoW', 'wine', '/d', 'wow-1', 1)")
    con.execute("INSERT INTO games VALUES ('Native', 'linux', '/n', 'nat-1', 1)")
    con.commit()
    con.close()
    yml = games / "wow-1.yml"
    yml.write_text(yaml.safe_dump({"game": {"prefix": "/mnt/G/wow"},
                                   "system": {"env": {"KEEP": "1"}}}))
    (games / "nat-1.yml").write_text("game: {}\n")
    monkeypatch.setattr(le, "lutris_dirs", lambda: [(db, [games])])

    assert le._process("apply", overwrite=False, dry=False) == 0
    env = yaml.safe_load(yml.read_text())["system"]["env"]
    assert env["KEEP"] == "1"
    assert env["DXVK_STATE_CACHE_PATH"] == "/mnt/G/wow"
    assert env["__GL_SHADER_DISK_CACHE_SKIP_CLEANUP"] == "1"
    assert yaml.safe_load((games / "nat-1.yml").read_text()) == {"game": {}}
    assert list(games.glob("wow-1.yml.tuxthrottle-bak-*"))

    assert le._process("remove", overwrite=False, dry=False) == 0
    assert yaml.safe_load(yml.read_text())["system"]["env"] == {"KEEP": "1"}
