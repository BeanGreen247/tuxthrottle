"""Theme palette system (set_palette / PALETTES). Needs ttkbootstrap importable
(the module pulls it in), so it runs on g15 / gui-smoke, skips in the sandbox.
"""
import pytest

pytest.importorskip("ttkbootstrap", exc_type=ImportError)

import tuxthrottle_gui_widgets as gw  # noqa: E402


def test_every_palette_applies_and_rebinds_globals():
    for name in gw.PALETTES:
        applied = gw.set_palette(name)
        assert applied == name
        assert gw.BIOS_PANEL == gw._P["panel"]
        assert gw.BIOS_SUNKEN == gw._P["sunken"]
        assert gw.SEM_DANGER == gw._P["danger"]
    gw.set_palette("BIOS Dark")


def test_unknown_palette_falls_back():
    assert gw.set_palette("does-not-exist") == "BIOS Dark"
    assert gw.BIOS_PANEL == gw._BASE_PALETTE["panel"]


def test_palette_omitted_key_uses_base():
    # "Carbon" doesn't override 'success' → must equal the base value
    gw.set_palette("Carbon")
    assert gw.SEM_SUCCESS == gw._BASE_PALETTE["success"]
    gw.set_palette("BIOS Dark")


def test_bios_dark_is_the_base():
    gw.set_palette("Nord")
    gw.set_palette("BIOS Dark")
    for k, v in gw._BASE_PALETTE.items():
        assert gw._P[k] == v
