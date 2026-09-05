"""tuxthrottle_gui_widgets.py — the pure color-math / formatting helpers from
the second module-extraction slice. Skipped if ttkbootstrap isn't importable
(same soft-dependency pattern as test_tui.py/textual) — this sandbox's own
ttkbootstrap install is broken (PIL/ImageTk), and CI's main "checks" job
doesn't install it either (only the separate gui-smoke job does), so this
suite only actually runs where ttkbootstrap really works (e.g. on g15).

The widget *classes* (RingGauge, HistoryChart, SidebarNav, _Tooltip) need a
real Tk root and are already exercised end-to-end by the gui-smoke CI job
and this session's live g15 verification — not duplicated here.
"""
import pytest

pytest.importorskip("ttkbootstrap", exc_type=ImportError)

import tuxthrottle_gui_widgets as gw  # noqa: E402


def test_human_bytes_formatting():
    assert gw._human_bytes(0) == "0 B"
    assert gw._human_bytes(512) == "512 B"
    assert gw._human_bytes(2048) == "2.0 KiB"
    assert gw._human_bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert gw._human_bytes(3 * 1024 ** 3) == "3.0 GiB"


def test_rgb_str_to_hex_parses_qt_style_rgb_string():
    assert gw._rgb_str_to_hex("61,174,233") == "#3daee9"
    assert gw._rgb_str_to_hex("255,255,255") == "#ffffff"


def test_rgb_str_to_hex_none_on_bad_input():
    assert gw._rgb_str_to_hex("not a color") is None
    assert gw._rgb_str_to_hex("1,2") is None


def test_mix_endpoints_and_midpoint():
    assert gw._mix("#000000", "#ffffff", 0.0) == "#000000"
    assert gw._mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert gw._mix("#000000", "#ffffff", 0.5) == "#808080"


def test_relative_luminance_black_and_white():
    assert gw._rel_luminance("#000000") == pytest.approx(0.0, abs=1e-9)
    assert gw._rel_luminance("#ffffff") == pytest.approx(1.0, abs=1e-9)


def test_contrast_ratio_black_on_white_is_max():
    assert gw._contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_symmetric():
    a = gw._contrast_ratio("#3daee9", "#141a21")
    b = gw._contrast_ratio("#141a21", "#3daee9")
    assert a == pytest.approx(b)


def test_readable_on_returns_input_when_already_high_contrast():
    # black text on white background already clears any reasonable target
    assert gw.readable_on("#000000", "#ffffff", target=4.5) == "#000000"


def test_readable_on_lightens_a_low_contrast_color():
    # a dark accent on the dark BIOS panel needs lightening to stay legible
    fg, bg = "#202020", "#141a21"
    assert gw._contrast_ratio(fg, bg) < 4.5
    result = gw.readable_on(fg, bg, target=4.5)
    assert gw._contrast_ratio(result, bg) >= 4.5


def test_theme_constants_are_valid_hex_colors():
    import re
    hexre = re.compile(r"^#[0-9a-fA-F]{6}$")
    for name in ("ACCENT_FALLBACK", "BIOS_PANEL", "BIOS_SUNKEN", "BIOS_BG",
                "BIOS_PANEL_HI", "BIOS_FG", "BIOS_MUTED", "BIOS_BORDER",
                "BIOS_BORDER_HI", "BIOS_CARD", "CHART_AXIS", "SEM_SUCCESS",
                "SEM_DANGER", "SEM_WARNING", "SEM_INFO", "SEM_SECONDARY",
                "HELP_AMBER", "HELP_BANNER_BG"):
        value = getattr(gw, name)
        assert hexre.match(value), f"{name} = {value!r} is not a valid #rrggbb color"
