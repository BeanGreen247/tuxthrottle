"""tuxthrottle_diag.py — the debug-report / hardware-bundle / onboarding
helpers, pulled out of tuxthrottle.py's monolith in the modular-refactor pass
so the Diagnostics tab mixin can import them without a circular dependency.
Pure read-only logic; every shell command is stubbed here.
"""
import json

import tuxthrottle_diag as td


# --------------------------------------------------------------------------- #
#  collect_debug_report
# --------------------------------------------------------------------------- #
def test_collect_debug_report_assembles_sections(monkeypatch):
    monkeypatch.setattr(td, "run_cmd3", lambda *a, **k: (True, 0, "stub-output"))
    monkeypatch.setattr(td, "toolkit_version", lambda: "99.9.9")
    monkeypatch.setattr(td, "_load_all_items", lambda: [])
    monkeypatch.setattr(td, "ledger_load", lambda: {"x": 1})
    monkeypatch.setattr(td, "format_status_report", lambda items: "STATUS-TABLE")

    rep = td.collect_debug_report()
    assert "TuxThrottle — debug report" in rep
    assert "toolkit 99.9.9" in rep
    assert "STATUS-TABLE" in rep
    assert "APPLY LEDGER" in rep
    assert json.dumps({"x": 1}, indent=2, sort_keys=True) in rep


def test_collect_debug_report_wrap_wraps_in_details(monkeypatch):
    monkeypatch.setattr(td, "run_cmd3", lambda *a, **k: (True, 0, "out"))
    monkeypatch.setattr(td, "toolkit_version", lambda: "1.2.3")
    monkeypatch.setattr(td, "_load_all_items", lambda: [])
    monkeypatch.setattr(td, "ledger_load", lambda: {})
    monkeypatch.setattr(td, "format_status_report", lambda items: "")

    wrapped = td.collect_debug_report(wrap=True)
    assert "<details>" in wrapped and "</details>" in wrapped
    assert "```" in wrapped


def test_wrap_issue_block_is_idempotent_shape():
    body = "line one\nline two"
    out = td.wrap_issue_block(body)
    assert body in out
    assert out.count("```") >= 2
    assert "<summary>" in out


# --------------------------------------------------------------------------- #
#  onboarding helpers
# --------------------------------------------------------------------------- #
def test_model_scaffold_json_is_valid_json():
    obj = json.loads(td._model_scaffold_json())
    assert isinstance(obj, dict)


def test_decode_key_caps_handles_missing_input_devices(monkeypatch):
    monkeypatch.setattr(td, "run_cmd3", lambda *a, **k: (False, 1, ""))
    txt = td._decode_key_caps()
    assert isinstance(txt, str)


def test_github_issue_template_has_paste_marker():
    assert "PASTE THE DEBUG REPORT HERE" in td.GITHUB_ISSUE_TEMPLATE


def test_debug_cmds_are_well_formed():
    for row in td._DEBUG_CMDS:
        assert len(row) == 3
        title, cmd, maxlines = row
        assert isinstance(title, str)
        assert cmd is None or isinstance(cmd, str) or callable(cmd)
        assert maxlines is None or isinstance(maxlines, int)
