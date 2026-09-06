"""Lazy tab construction + per-tab live-poll gating (the Phase-1 speed work).

Needs a real Tk root, so it's skipped where ttkbootstrap isn't importable
(this sandbox — PIL/ImageTk); runs on g15 and in the gui-smoke CI job.
"""
import pytest

pytest.importorskip("ttkbootstrap", exc_type=ImportError)

import tkinter as tk  # noqa: E402

import ttkbootstrap as tb  # noqa: E402

import tuxthrottle as tt  # noqa: E402


@pytest.fixture
def app():
    tt.self_elevate = lambda: None
    try:
        root = tb.Window(themename="darkly")
    except tk.TclError as exc:  # no display (plain `pytest` without Xvfb)
        pytest.skip(f"no X/Wayland display for a Tk root: {exc}")
    a = tt.ToolkitApp(root)
    for _ in range(8):
        root.update()
    yield a
    root.destroy()


def _frame_for(app, label):
    return next(f for t, f, b in app.notebook._pages if t == label)


def test_only_dashboard_built_at_startup(app):
    # Dashboard is eager; every other page frame has no children until selected.
    for text, frame, _btn in app.notebook._pages:
        n = len(frame.winfo_children())
        if text == "Dashboard":
            assert n > 0, "Dashboard should be built eagerly"
        else:
            assert n == 0, f"{text!r} was built at startup (should be lazy)"


def test_selecting_a_tab_builds_it_once(app):
    fans = _frame_for(app, "Fans")
    assert len(fans.winfo_children()) == 0
    app.notebook.select(fans)
    app.root.update()
    assert len(fans.winfo_children()) > 0
    n = len(fans.winfo_children())
    app.notebook.select(_frame_for(app, "Dashboard"))
    app.notebook.select(fans)
    app.root.update()
    assert len(fans.winfo_children()) == n, "re-selecting rebuilt the tab"


def test_live_poll_flag_follows_visible_tab(app):
    app.notebook.select(_frame_for(app, "Fans"))
    app.root.update()
    assert getattr(app, "_fan_live", False) is True
    assert getattr(app, "_power_live", False) is False

    app.notebook.select(_frame_for(app, "Power & Limits"))
    app.root.update()
    assert getattr(app, "_fan_live", False) is False
    assert getattr(app, "_power_live", False) is True


def test_poll_token_prevents_stacked_loops(app):
    fans = _frame_for(app, "Fans")
    dash = _frame_for(app, "Dashboard")
    for _ in range(5):
        app.notebook.select(dash)
        app.notebook.select(fans)
        app.root.update()
    # one active generation, whatever the churn
    assert app._fan_live is True
    tok = app._fan_tok
    app.root.update()
    assert app._fan_tok == tok, "a stale loop bumped the token"
