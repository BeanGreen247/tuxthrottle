import pytest

import tuxthrottle_watchdog as wd


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_arm_rejects_nonpositive_timeout():
    with pytest.raises(ValueError):
        wd.arm(0, user="bean")


def test_arm_invokes_systemd_run_with_rollback_last(monkeypatch):
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return FakeCompleted(0)
    monkeypatch.setattr(wd.subprocess, "run", fake_run)

    unit = wd.arm(20, user="bean", toolkit_dir="/opt/tuxthrottle")

    assert unit.startswith(wd.UNIT_PREFIX)
    cmd = captured["cmd"]
    assert cmd[0] == "systemd-run"
    assert f"--unit={unit}" in cmd
    assert "--on-active=20" in cmd
    assert "/opt/tuxthrottle/tuxthrottle_profiles.py" in cmd
    assert cmd[-4:] == ["rollback", "last", "--user", "bean"]


def test_arm_raises_when_systemd_run_fails(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run",
                         lambda *a, **k: FakeCompleted(1, "", "no systemd"))
    with pytest.raises(RuntimeError, match="no systemd"):
        wd.arm(10, user="bean")


def test_arm_raises_when_systemd_run_missing(monkeypatch):
    def raiser(*a, **k):
        raise FileNotFoundError("systemd-run not found")
    monkeypatch.setattr(wd.subprocess, "run", raiser)
    with pytest.raises(RuntimeError, match="systemd-run unavailable"):
        wd.arm(10, user="bean")


def test_disarm_rejects_foreign_unit_names():
    assert wd.disarm("some-other-service") is False


def test_disarm_stops_timer_and_service(monkeypatch):
    stopped = []

    def fake_run(cmd, **k):
        if cmd[0] == "systemctl" and cmd[1] == "stop":
            stopped.append(cmd[2])
            return FakeCompleted(0)
        return FakeCompleted(0)
    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    unit = wd.UNIT_PREFIX + "abc123"
    assert wd.disarm(unit) is True
    assert stopped == [f"{unit}.timer", f"{unit}.service"]


def test_disarm_treats_already_gone_unit_as_ok(monkeypatch):
    def fake_run(cmd, **k):
        if cmd[0] == "systemctl" and cmd[1] == "stop":
            return FakeCompleted(5, "", "Unit not loaded.")
        return FakeCompleted(0)
    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    assert wd.disarm(wd.UNIT_PREFIX + "gone") is True


def test_is_armed_true_when_timer_active(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run",
                         lambda *a, **k: FakeCompleted(0, "active\n"))
    assert wd.is_armed(wd.UNIT_PREFIX + "x") is True


def test_is_armed_false_when_not_active(monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run",
                         lambda *a, **k: FakeCompleted(3, "inactive\n"))
    assert wd.is_armed(wd.UNIT_PREFIX + "x") is False


def test_is_armed_false_on_error(monkeypatch):
    def raiser(*a, **k):
        raise OSError("no systemctl")
    monkeypatch.setattr(wd.subprocess, "run", raiser)
    assert wd.is_armed(wd.UNIT_PREFIX + "x") is False


def test_cli_arm_prints_unit(monkeypatch, capsys):
    monkeypatch.setattr(wd, "arm", lambda seconds, user, toolkit_dir: "tuxthrottle-watchdog-abc")
    assert wd.main(["arm", "20", "--user", "bean"]) == 0
    assert capsys.readouterr().out.strip() == "tuxthrottle-watchdog-abc"


def test_cli_arm_failure_exit_code(monkeypatch, capsys):
    def raiser(seconds, user, toolkit_dir):
        raise RuntimeError("no systemd")
    monkeypatch.setattr(wd, "arm", raiser)
    assert wd.main(["arm", "20", "--user", "bean"]) == 1
    assert "no systemd" in capsys.readouterr().err


def test_cli_disarm(monkeypatch):
    monkeypatch.setattr(wd, "disarm", lambda unit: True)
    assert wd.main(["disarm", "tuxthrottle-watchdog-x"]) == 0


def test_cli_status(monkeypatch, capsys):
    monkeypatch.setattr(wd, "is_armed", lambda unit: True)
    assert wd.main(["status", "tuxthrottle-watchdog-x"]) == 0
    assert capsys.readouterr().out.strip() == "armed"
