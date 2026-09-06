"""Structural guard for the modular-refactor: the tab code was split out of
tuxthrottle.py into mixin classes that ToolkitApp multiply-inherits. Two
failure modes this locks down:

  * a method left duplicated on ToolkitApp *and* a mixin (dead, MRO-shadowed
    twin — exactly the Profiles-tab bug found mid-refactor), and
  * a method name colliding across two mixins ToolkitApp inherits.

Pure AST parsing — no ttkbootstrap / Tk import needed, so it runs everywhere.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

MIXIN_MODULES = [
    "tuxthrottle_tab_about",
    "tuxthrottle_tab_category",
    "tuxthrottle_tab_dashboard",
    "tuxthrottle_tab_diagnostics",
    "tuxthrottle_tab_fans",
    "tuxthrottle_tab_games",
    "tuxthrottle_tab_keyboard",
    "tuxthrottle_tab_power_display",
    "tuxthrottle_tab_profiles",
    "tuxthrottle_tab_updates",
    "tuxthrottle_tab_vram",
]


def _class_methods(path, cls_filter=None):
    tree = ast.parse(path.read_text(), filename=str(path))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if cls_filter and node.name != cls_filter:
                continue
            out[node.name] = [
                n.name for n in node.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    return out


def _toolkitapp_methods():
    return _class_methods(ROOT / "tuxthrottle.py", "ToolkitApp")["ToolkitApp"]


def test_no_method_duplicated_between_toolkitapp_and_a_mixin():
    app = set(_toolkitapp_methods())
    for mod in MIXIN_MODULES:
        for cls, methods in _class_methods(ROOT / f"{mod}.py").items():
            overlap = app & set(methods)
            assert not overlap, f"{cls} ({mod}) shadows ToolkitApp method(s): {sorted(overlap)}"


def test_no_method_name_collides_across_mixins():
    seen = {}
    for mod in MIXIN_MODULES:
        for cls, methods in _class_methods(ROOT / f"{mod}.py").items():
            for m in methods:
                if m.startswith("__"):
                    continue
                assert m not in seen, f"{m}: in both {seen[m]} and {cls}"
                seen[m] = cls


def test_toolkitapp_inherits_every_mixin():
    tree = ast.parse((ROOT / "tuxthrottle.py").read_text())
    bases = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolkitApp":
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
    for mod in MIXIN_MODULES:
        cls = next(iter(_class_methods(ROOT / f"{mod}.py")))
        assert cls in bases, f"{cls} not in ToolkitApp bases"


def test_monolith_stayed_shrunk():
    lines = (ROOT / "tuxthrottle.py").read_text().count("\n")
    assert lines < 2200, f"tuxthrottle.py regrew to {lines} lines — extract new tab code into a mixin"
