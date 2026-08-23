"""The hexagon wall, enforced.

`core/` and `xray/` must stay importable on a machine with no 3ds Max — that is
what lets the planner, the governor and the whole X-ray be tested on macOS in
0.04 s instead of behind a 45-second Max launch on Windows.

Adapted from MaxSlim's `tests/test_no_max_imports.py`, which has held this line
across two products.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "maxrescue"

FORBIDDEN = re.compile(r"\b(pymxs|qtmax|PySide2|PySide6|maxrescue\.maxbridge)\b")

PURE_DIRS = ("core", "xray")
PURE_APP_MODULES = ("settings.py", "controller.py")


def _pure_modules() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for sub in PURE_DIRS:
        found.extend(sorted((PKG / sub).rglob("*.py")))
    for name in PURE_APP_MODULES:
        path = PKG / "app" / name
        if path.exists():
            found.append(path)
    return found


def test_there_are_pure_modules_to_check():
    # A wall that guards nothing passes silently forever.
    assert _pure_modules(), "no pure modules found — is the package laid out right?"


@pytest.mark.parametrize("path", _pure_modules(), ids=lambda p: p.name)
def test_pure_module_has_no_max_dependency(path: pathlib.Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if FORBIDDEN.search(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if FORBIDDEN.search(node.module):
                offenders.append(node.module)

    rel = path.relative_to(ROOT)
    assert not offenders, (
        f"{rel} imports {offenders} — pure modules must run without 3ds Max. "
        "Put the Max-facing part behind a port in core/interfaces.py."
    )


@pytest.mark.parametrize("path", _pure_modules(), ids=lambda p: p.name)
def test_pure_module_imports_cleanly_without_max(path: pathlib.Path):
    """Importing must not fail on a machine with no Max — this catches a
    forbidden import hidden inside a function body, which the AST scan above
    deliberately does not look for."""
    rel = path.relative_to(ROOT).with_suffix("")
    module = ".".join(rel.parts)
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    __import__(module)


def test_bridge_modules_are_the_only_pymxs_importers():
    """The converse: if nothing imports pymxs, the wall is guarding an empty
    room and the port abstraction has quietly become decoration."""
    bridge = PKG / "maxbridge"
    modules = [p for p in bridge.rglob("*.py") if p.name != "__init__.py"]
    if not modules:
        pytest.skip("no bridge modules yet")
    sources = [p.read_text(encoding="utf-8") for p in modules]
    assert any("pymxs" in s for s in sources), (
        "maxbridge/ contains no pymxs importer — either the bridge is unbuilt "
        "or Max access has leaked somewhere it should not be."
    )
