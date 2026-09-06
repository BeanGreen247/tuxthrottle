"""tuxthrottle_items.py — the Item/tweaks-engine data layer, extracted from
tuxthrottle.py's monolith in the modular-refactor pass. This was previously
untestable in isolation (buried inside an 8000-line Tkinter file); now it's
pure logic with tmp_path isolation for the ledger.
"""
import tuxthrottle_items as ti


# --------------------------------------------------------------------------- #
#  Item construction
# --------------------------------------------------------------------------- #
def test_item_tweak_substitutes_user_and_toolkit_dir():
    data = {
        "Content": "Test tweak",
        "check": "test -f /home/{USER}/marker",
        "apply": ["touch {TOOLKIT_DIR}/x", "echo {USER}"],
        "undo": ["rm {TOOLKIT_DIR}/x"],
    }
    it = ti.Item("MyTweak", data, "tweak", "alice")
    assert it.check_cmd == "test -f /home/alice/marker"
    assert it.apply_cmds[0] == f"touch {ti.BASE_DIR}/x"
    assert it.apply_cmds[1] == "echo alice"
    assert it.undo_cmds[0] == f"rm {ti.BASE_DIR}/x"
    assert it.kind == "tweak"
    assert it.state == "unknown"
    assert it.hw_supported is True
    assert it.hidden is False


def test_item_app_dnf_default_apply_cmd():
    it = ti.Item("Firefox", {"Content": "Firefox"}, "app", "bob")
    assert it.apply_cmds == ["dnf install -y Firefox"]
    assert it.undo_cmds == []


def test_item_app_flatpak_apply_cmds():
    data = {"Content": "X", "manager": "flatpak", "package": "org.example.X"}
    it = ti.Item("X", data, "app", "bob")
    assert any("flathub org.example.X" in c for c in it.apply_cmds)


def test_item_app_check_ors_in_provides_and_binary():
    data = {"check": "false", "provides": ["true"], "binary": "ls"}
    it = ti.Item("Y", data, "app", "bob")
    assert "false" in it.check_cmd
    assert "true" in it.check_cmd
    assert "command -v ls" in it.check_cmd


def test_item_requires_models_parsed_as_list_of_str():
    it = ti.Item("Z", {"models": ["g15-5515", 42]}, "tweak", "bob")
    assert it.requires_models == ["g15-5515", "42"]


def test_item_requires_models_none_when_absent():
    it = ti.Item("Z", {}, "tweak", "bob")
    assert it.requires_models is None


def test_item_state_properties():
    it = ti.Item("Z", {}, "tweak", "bob")
    it.state = "applied"
    assert it.applied is True
    assert it.done is True
    it.state = "pending"
    assert it.pending is True
    assert it.done is True
    it.state = "not_applied"
    assert it.applied is False
    assert it.done is False


# --------------------------------------------------------------------------- #
#  run_cmd / run_cmd3
# --------------------------------------------------------------------------- #
def test_run_cmd3_success():
    ok, rc, out = ti.run_cmd3("echo hello")
    assert ok is True
    assert rc == 0
    assert "hello" in out


def test_run_cmd3_failure_rc():
    ok, rc, out = ti.run_cmd3("exit 3")
    assert ok is False
    assert rc == 3


def test_run_cmd_wraps_run_cmd3():
    ok, out = ti.run_cmd("echo x")
    assert ok is True
    assert "x" in out


# --------------------------------------------------------------------------- #
#  evaluate_item
# --------------------------------------------------------------------------- #
def test_evaluate_item_unsupported():
    it = ti.Item("A", {"check": "true"}, "tweak", "bob")
    it.hw_supported = False
    ti.evaluate_item(it, {})
    assert it.state == "unsupported"


def test_evaluate_item_no_check_cmd_is_not_applied():
    it = ti.Item("A", {}, "tweak", "bob")
    ti.evaluate_item(it, {})
    assert it.state == "not_applied"


def test_evaluate_item_applied_when_check_passes():
    it = ti.Item("A", {"check": "true"}, "tweak", "bob")
    ti.evaluate_item(it, {})
    assert it.state == "applied"


def test_evaluate_item_pending_when_check_pending_passes():
    it = ti.Item("A", {"check": "false", "check_pending": "true"}, "tweak", "bob")
    ti.evaluate_item(it, {})
    assert it.state == "pending"


def test_evaluate_item_drifted_when_ledger_says_we_applied_it():
    it = ti.Item("A", {"check": "false"}, "tweak", "bob")
    ledger = {"A": {"action": "apply", "ok": True, "ts": "x", "note": ""}}
    ti.evaluate_item(it, ledger)
    assert it.state == "drifted"


def test_evaluate_item_failed_when_ledger_says_last_attempt_failed():
    it = ti.Item("A", {"check": "false"}, "tweak", "bob")
    ledger = {"A": {"action": "apply", "ok": False, "ts": "x", "note": "boom"}}
    ti.evaluate_item(it, ledger)
    assert it.state == "failed"


def test_evaluate_item_error_on_command_not_found():
    it = ti.Item("A", {"check": "this-command-does-not-exist-xyz"}, "tweak", "bob")
    ti.evaluate_item(it, {})
    assert it.state == "error"


# --------------------------------------------------------------------------- #
#  ledger (tmp_path isolated)
# --------------------------------------------------------------------------- #
def test_ledger_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(ti, "_ledger_path", lambda: tmp_path / "state.json")
    ti.ledger_record("MyItem", "apply", True, "worked fine")
    data = ti.ledger_load()
    assert data["MyItem"]["action"] == "apply"
    assert data["MyItem"]["ok"] is True
    assert data["MyItem"]["note"] == "worked fine"


def test_ledger_load_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(ti, "_ledger_path", lambda: tmp_path / "nope.json")
    assert ti.ledger_load() == {}


def test_ledger_load_corrupt_json_returns_empty(monkeypatch, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(ti, "_ledger_path", lambda: p)
    assert ti.ledger_load() == {}


# --------------------------------------------------------------------------- #
#  format_status_report / load_json
# --------------------------------------------------------------------------- #
def test_format_status_report_groups_by_category_and_counts():
    a = ti.Item("A", {"Content": "Alpha", "category": "GPU", "check": "true"}, "tweak", "bob")
    b = ti.Item("B", {"Content": "Beta", "category": "Power", "check": "false"}, "tweak", "bob")
    ti.evaluate_item(a, {})
    ti.evaluate_item(b, {})
    report = ti.format_status_report([a, b])
    assert "[GPU]" in report
    assert "[Power]" in report
    assert "APPLIED" in report
    assert "NOT_APPLIED" in report
    assert "applied=1" in report
    assert "not_applied=1" in report


def test_load_json_reads_real_config():
    tweaks = ti.load_json("tweaks.json")
    assert isinstance(tweaks, dict)
    assert len(tweaks) > 0
