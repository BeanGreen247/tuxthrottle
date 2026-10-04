"""tuxthrottle_mangohud_launchers.py - anti-cheat scan, Heroic + Lutris."""
import json
import sqlite3

import tuxthrottle_mangohud_launchers as ml


def test_scan_anticheat_finds_known_markers(tmp_path):
    (tmp_path / "eac" / "EasyAntiCheat").mkdir(parents=True)
    (tmp_path / "be").mkdir()
    (tmp_path / "be" / "Game_BE.exe").write_text("")
    (tmp_path / "clean" / "bin").mkdir(parents=True)
    (tmp_path / "clean" / "bin" / "pb").mkdir()          # nested "pb" ≠ PunkBuster
    (tmp_path / "clean" / "game.exe").write_text("")
    assert ml.scan_anticheat(tmp_path / "eac") == "EasyAntiCheat"
    assert ml.scan_anticheat(tmp_path / "be") == "BattlEye"
    assert ml.scan_anticheat(tmp_path / "clean") == ""
    assert ml.scan_anticheat(tmp_path / "missing") == ""


def test_anticheat_cache_reuses_until_mtime_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(ml, "_cache_file", lambda: tmp_path / "ac.json")
    game = tmp_path / "g"
    game.mkdir()
    calls = []
    real = ml.scan_anticheat
    monkeypatch.setattr(ml, "scan_anticheat", lambda p: calls.append(p) or real(p))
    assert ml.anticheat_for({"a": str(game)}) == {"a": ""}
    assert ml.anticheat_for({"a": str(game)}) == {"a": ""}
    assert len(calls) == 1


def test_heroic_lists_and_toggles(monkeypatch, tmp_path):
    base = tmp_path / "heroic"
    (base / "legendaryConfig" / "legendary").mkdir(parents=True)
    (base / "legendaryConfig" / "legendary" / "installed.json").write_text(json.dumps(
        {"Fortnite": {"title": "Fortnite", "install_path": str(tmp_path / "fn")}}))
    (base / "gog_store").mkdir()
    (base / "gog_store" / "installed.json").write_text(json.dumps(
        {"installed": [{"appName": "123", "install_path": "/games/Witcher"}]}))
    monkeypatch.setattr(ml, "heroic_dirs", lambda: [base])
    rows = {r["id"]: r for r in ml.heroic_games()}
    assert rows["heroic:Fortnite"]["source"] == "Heroic · Epic"
    assert rows["heroic:123"]["name"] == "Witcher" and not rows["heroic:123"]["wanted"]
    assert ml.set_enabled("heroic:Fortnite", True)
    cfg = json.loads((base / "GamesConfig" / "Fortnite.json").read_text())
    assert cfg["Fortnite"]["showMangohud"] is True and cfg["version"] == "v0"
    assert {r["id"]: r["wanted"] for r in ml.heroic_games()}["heroic:Fortnite"]
    assert not ml.set_enabled("heroic:nope", True)


def test_lutris_lists_and_toggles(monkeypatch, tmp_path):
    db = tmp_path / "pga.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE games (id INTEGER, name TEXT, runner TEXT, directory TEXT, "
                "configpath TEXT, installed INTEGER)")
    con.execute("INSERT INTO games VALUES (7, 'Old Game', 'wine', '/g/old', 'old-game-1', 1)")
    con.execute("INSERT INTO games VALUES (8, 'Steam One', 'steam', '', 'steam-1', 1)")
    con.commit()
    con.close()
    games = tmp_path / "games"
    games.mkdir()
    (games / "old-game-1.yml").write_text("game:\n  exe: /g/old/a.exe\nsystem: {}\n")
    monkeypatch.setattr(ml, "lutris_dirs", lambda: [(db, [games])])
    rows = ml.lutris_games()
    assert [r["id"] for r in rows] == ["lutris:7"] and not rows[0]["wanted"]
    assert ml.set_enabled("lutris:7", True)
    assert ml.lutris_games()[0]["wanted"]
    assert "exe: /g/old/a.exe" in (games / "old-game-1.yml").read_text()
