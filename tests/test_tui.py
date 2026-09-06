"""tuxthrottle_tui.py — the Textual dashboard. Skipped entirely if textual
isn't installed (it's a soft dependency, same as ttkbootstrap for the GUI).

This caught a real bug once already: Static's internal renderable storage
differs between the "textual" package on PyPI and the one Fedora/Nobara
packages, so StatBox/ValueStatic track their own `.value` instead of relying
on Textual internals — these tests assert on `.value` for exactly that
reason, and would have failed loudly if that tracking broke.
"""
import pytest

textual = pytest.importorskip("textual")

import tuxthrottle_tui as tt  # noqa: E402


def test_ctl_returns_false_with_message_when_command_fails(monkeypatch):
    # _ctl()'s last fallback is `[]` (no launcher prefix — runs the bare
    # command, for when the caller is already privileged), so mocking only
    # shutil.which() still lets that branch exec a REAL command. An earlier
    # version of this test did exactly that and flipped Game Mode for real
    # on live hardware. subprocess.run must always be mocked too, never left
    # to fall through to a real call.
    monkeypatch.setattr(tt.shutil, "which", lambda _name: None)

    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "permission denied"

    monkeypatch.setattr(tt.subprocess, "run", lambda *a, **kw: FakeResult())

    ok, msg = tt._ctl("gamemode", "toggle")
    assert ok is False
    assert "permission denied" in msg


def test_value_static_tracks_its_own_text():
    w = tt.ValueStatic("")
    w.update("hello")
    assert w.value == "hello"


def test_statbox_set_value_includes_label_and_value():
    box = tt.StatBox("CPU")
    box.set_value("62 C")
    assert box.value  # never empty once set
    assert "CPU" in box.value
    assert "62 C" in box.value


@pytest.mark.asyncio
async def test_app_builds_and_populates_stat_boxes(monkeypatch):
    # stub out real sensor/privileged calls so this runs identically on any
    # machine (CI included) — the point is exercising the App/widget wiring,
    # not real hardware (that's covered live on g15 separately).
    monkeypatch.setattr(tt.sensors, "read_cpu_power_watts", lambda: 12.3)
    monkeypatch.setattr(tt.sensors, "read_cpu_temp_c", lambda: "60 C")
    monkeypatch.setattr(tt.sensors, "read_cpu_freq_ghz", lambda: "3.0 GHz")
    monkeypatch.setattr(tt.sensors, "read_igpu_clock_temp", lambda: "400 MHz, 50 C")
    monkeypatch.setattr(tt.sensors, "read_dgpu_clock_temp_util", lambda: "n/a")
    monkeypatch.setattr(tt.sensors, "get_game_mode_state", lambda: False)
    monkeypatch.setattr(tt.fixlog, "read_recent", lambda n: [])

    app = tt.TuxThrottleTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        for box in (app.cpu_box, app.igpu_box, app.dgpu_box, app.gamemode_box):
            assert box.value, f"{box.id} never populated"
        assert "60 C" in app.cpu_box.value
        assert "OFF" in app.gamemode_box.value

        calls = []
        monkeypatch.setattr(tt, "_ctl", lambda *a: (calls.append(a) or (True, "")))
        await pilot.click("#btn_fan_0")
        await pilot.pause()
        assert calls and calls[0] == ("set", "fan-boost", "both", "0")
