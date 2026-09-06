import tuxthrottle_btrfs as tb


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_not_available_when_not_btrfs(monkeypatch):
    monkeypatch.setattr(tb.subprocess, "run",
                         lambda *a, **k: FakeCompleted(0, "ext4\n"))
    assert tb.is_btrfs_root() is False
    assert tb.method() is None
    assert tb.available() is False


def test_not_available_when_btrfs_but_no_snapper(monkeypatch):
    def fake_run(cmd, **k):
        if cmd[0] == "findmnt":
            return FakeCompleted(0, "btrfs\n")
        raise AssertionError("snapper should not be probed if `which` fails first")
    monkeypatch.setattr(tb.subprocess, "run", fake_run)
    monkeypatch.setattr(tb.shutil, "which", lambda name: None)
    assert tb.is_btrfs_root() is True
    assert tb.method() is None


def test_available_via_snapper(monkeypatch):
    def fake_run(cmd, **k):
        if cmd[0] == "findmnt":
            return FakeCompleted(0, "btrfs\n")
        if cmd[:2] == ["snapper", "-c"]:
            return FakeCompleted(0, "")
        raise AssertionError(cmd)
    monkeypatch.setattr(tb.subprocess, "run", fake_run)
    monkeypatch.setattr(tb.shutil, "which", lambda name: "/usr/bin/snapper")
    assert tb.method() == "snapper"
    assert tb.available() is True


def test_create_snapshot_reports_unavailable_gracefully(monkeypatch):
    monkeypatch.setattr(tb, "method", lambda: None)
    res = tb.create_snapshot("pre-apply-selected")
    assert res["ok"] is False
    assert res["method"] is None
    assert "skipped" in res["msg"]


def test_create_snapshot_success(monkeypatch):
    monkeypatch.setattr(tb, "method", lambda: "snapper")

    def fake_run(cmd, **k):
        assert cmd[0] == "snapper"
        assert "pre-apply-selected" in cmd[-1]
        return FakeCompleted(0, "42\n")
    monkeypatch.setattr(tb.subprocess, "run", fake_run)
    res = tb.create_snapshot("pre-apply-selected")
    assert res == {"ok": True, "method": "snapper", "id": "42",
                    "msg": "snapper snapshot #42 created"}


def test_create_snapshot_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(tb, "method", lambda: "snapper")
    monkeypatch.setattr(tb.subprocess, "run",
                         lambda *a, **k: FakeCompleted(1, "", "boom"))
    res = tb.create_snapshot("x")
    assert res["ok"] is False
    assert "boom" in res["msg"]


def test_create_snapshot_never_raises_on_missing_binary(monkeypatch):
    monkeypatch.setattr(tb, "method", lambda: "snapper")

    def raiser(*a, **k):
        raise FileNotFoundError("no snapper")
    monkeypatch.setattr(tb.subprocess, "run", raiser)
    res = tb.create_snapshot("x")
    assert res["ok"] is False
    assert "snapper failed" in res["msg"]


def test_list_snapshots_empty_without_snapper(monkeypatch):
    monkeypatch.setattr(tb, "snapper_available", lambda: False)
    assert tb.list_snapshots() == []


def test_list_snapshots_filters_and_sorts(monkeypatch):
    monkeypatch.setattr(tb, "snapper_available", lambda: True)
    listing = (
        "# | Date | Description\n"
        "1 | 2026-09-01 | some other tool\n"
        "3 | 2026-09-03 | tuxthrottle: pre-apply-selected\n"
        "2 | 2026-09-02 | tuxthrottle: pre-rollback\n"
    )
    monkeypatch.setattr(tb.subprocess, "run",
                         lambda *a, **k: FakeCompleted(0, listing))
    rows = tb.list_snapshots(limit=10)
    assert [r["number"] for r in rows] == ["3", "2"]
    assert rows[0]["description"] == "pre-apply-selected"


def test_rollback_hint_names_the_real_command():
    hint = tb.rollback_hint("42")
    assert "snapper -c root rollback 42" in hint


def test_cli_available(monkeypatch, capsys):
    monkeypatch.setattr(tb, "method", lambda: None)
    assert tb.main(["available"]) == 0
    out = capsys.readouterr().out
    assert '"available": false' in out


def test_cli_create_success(monkeypatch, capsys):
    monkeypatch.setattr(tb, "create_snapshot",
                         lambda desc: {"ok": True, "method": "snapper", "id": "9",
                                       "msg": "snapper snapshot #9 created"})
    assert tb.main(["create", "my-label"]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_cli_create_failure_exit_code(monkeypatch):
    monkeypatch.setattr(tb, "create_snapshot",
                         lambda desc: {"ok": False, "method": None, "id": None,
                                       "msg": "skipped"})
    assert tb.main(["create"]) == 1
