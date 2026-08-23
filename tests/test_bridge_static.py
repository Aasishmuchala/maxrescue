"""The bridge honours the ports — checked without importing it.

`maxbridge/` cannot be imported off a machine with 3ds Max, so none of it is
covered by the runtime suite. That makes a name mismatch — `set_scatter_display`
in the core, `setScatterDisplay` in the bridge — a bug that survives every test
and then surfaces forty minutes into a session on the box, after a 45-second Max
launch and a batch merge.

So the bridge is parsed and compared against the Protocols it claims to
implement. This is the test that makes staging unrunnable code defensible.

The fakes are checked the same way, in the opposite direction: a fake that has
drifted from the port is a test suite quietly proving the wrong thing.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from maxrescue.core import interfaces
from tests import fakes

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "maxrescue" / "maxbridge"

#: port protocol -> (module file, implementing class)
CONTRACTS = {
    "SceneQuery": ("scene_query.py", "MaxSceneQuery"),
    "FixServices": ("fix_services.py", "MaxFixServices"),
    "MergeServices": ("merge.py", "MaxMergeServices"),
    "GuardServices": ("guard_bridge.py", "MaxGuardServices"),
    "UndoService": ("undo.py", "MaxUndoService"),
    "BackupService": ("backup.py", "MaxBackupService"),
    "MemoryProbe": ("memory.py", "MaxMemoryProbe"),
    "RenderServices": ("render_services.py", "MaxRenderServices"),
}

#: port protocol -> fake class
FAKE_CONTRACTS = {
    "SceneQuery": fakes.FakeSceneQuery,
    "FixServices": fakes.FakeFixServices,
    "MergeServices": fakes.FakeMergeServices,
    "GuardServices": fakes.FakeGuardServices,
    "UndoService": fakes.FakeUndoService,
    "BackupService": fakes.FakeBackupService,
    "MemoryProbe": fakes.FakeMemoryProbe,
}


def _protocol_methods(name: str) -> set[str]:
    protocol = getattr(interfaces, name)
    return {
        attr
        for attr, value in vars(protocol).items()
        if not attr.startswith("_") and callable(value)
    }


def _class_methods(path: pathlib.Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not item.name.startswith("_")
            }
    raise AssertionError(f"class {class_name} not found in {path.name}")


def _bridge_modules() -> list[pathlib.Path]:
    return sorted(p for p in BRIDGE.glob("*.py") if p.name != "__init__.py")


# ---------------------------------------------------------------------------
# it at least parses
# ---------------------------------------------------------------------------


def test_the_bridge_exists():
    assert _bridge_modules(), "no bridge modules found — has the layout moved?"


@pytest.mark.parametrize("path", _bridge_modules(), ids=lambda p: p.name)
def test_bridge_module_compiles(path: pathlib.Path):
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


@pytest.mark.parametrize("path", _bridge_modules(), ids=lambda p: p.name)
def test_bridge_module_first_party_imports_resolve(path: pathlib.Path):
    """`from maxrescue.core.types import X` must name a real X."""
    import importlib

    missing = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not node.module.startswith("maxrescue"):
            continue
        if "maxbridge" in node.module:
            continue  # cannot import a sibling that needs pymxs
        module = importlib.import_module(node.module)
        for alias in node.names:
            if alias.name != "*" and not hasattr(module, alias.name):
                missing.append(f"{node.module}.{alias.name}")
    assert not missing, f"{path.name} imports names that do not exist: {missing}"


# ---------------------------------------------------------------------------
# it honours the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", sorted(CONTRACTS), ids=str)
def test_bridge_class_implements_every_port_method(protocol: str):
    filename, class_name = CONTRACTS[protocol]
    required = _protocol_methods(protocol)
    implemented = _class_methods(BRIDGE / filename, class_name)
    missing = sorted(required - implemented)
    assert not missing, (
        f"{class_name} is missing {missing} required by {protocol}. "
        "This would surface only after a Max launch and a batch merge."
    )


@pytest.mark.parametrize("protocol", sorted(FAKE_CONTRACTS), ids=str)
def test_fake_implements_every_port_method(protocol: str):
    """A fake that has drifted from the port makes the whole suite prove the
    wrong thing."""
    required = _protocol_methods(protocol)
    fake = FAKE_CONTRACTS[protocol]
    missing = sorted(name for name in required if not hasattr(fake, name))
    assert not missing, f"{fake.__name__} is missing {missing} required by {protocol}"


@pytest.mark.parametrize("protocol", sorted(FAKE_CONTRACTS), ids=str)
def test_fake_and_bridge_agree_on_argument_names(protocol: str):
    """Keyword arguments must match too — the core calls some of these by name."""
    if protocol not in CONTRACTS:
        return
    filename, class_name = CONTRACTS[protocol]
    tree = ast.parse((BRIDGE / filename).read_text(encoding="utf-8"))
    bridge_args: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and not item.name.startswith("_"):
                    bridge_args[item.name] = [
                        a.arg for a in item.args.args if a.arg != "self"
                    ]

    fake = FAKE_CONTRACTS[protocol]
    for name, args in bridge_args.items():
        member = getattr(fake, name, None)
        if member is None or not callable(member):
            continue
        try:
            fake_args = [
                p for p in inspect.signature(member).parameters if p != "self"
            ]
        except (TypeError, ValueError):
            continue
        # Positional names must line up for the shared prefix; extra optional
        # arguments on either side are fine.
        shared = min(len(args), len(fake_args))
        assert args[:shared] == fake_args[:shared], (
            f"{class_name}.{name}{tuple(args)} does not line up with "
            f"{fake.__name__}.{name}{tuple(fake_args)}"
        )


# ---------------------------------------------------------------------------
# specific traps the bridge must not fall into
# ---------------------------------------------------------------------------


def test_the_safe_bitmap_mode_is_the_default_and_the_unsafe_one_raises():
    """Asserted structurally, not by grepping for the word: the guard and the
    docstring both legitimately mention `UseProxies`, and a substring check
    cannot tell an assignment from a warning about one."""
    tree = ast.parse((BRIDGE / "fix_services.py").read_text(encoding="utf-8"))

    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert constants.get("BITMAP_MODE_SAFE") == "renderMode_UseFullRes_FlushFromMemory"

    fn = next(
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "set_bitmap_proxy_mode"
    )
    default = fn.args.defaults[-1]
    assert isinstance(default, ast.Name) and default.id == "BITMAP_MODE_SAFE", (
        "the default mode must be the render-identical one"
    )
    assert any(isinstance(n, ast.Raise) for n in ast.walk(fn)), (
        "passing the render-changing mode must raise, not proceed"
    )


def test_material_deletion_uses_deleteitem_not_free():
    """`rt.free` does NOT delete a material. Using it looks like it worked."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    assert "deleteItem" in source
    assert "rt.free(" not in source


def test_merge_passes_explicit_reparent_and_material_policies():
    """The defaults are `#promptMtlDups` and `#promptReparent`, which open a
    dialog. In batch there is nobody to click it and the run hangs."""
    tree = ast.parse((BRIDGE / "merge.py").read_text(encoding="utf-8"))
    # Only the symbols actually handed to rt.name() count; the docstring names
    # the prompting defaults precisely in order to warn about them.
    passed = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "name"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    for flag in ("mergeDups", "useSceneMtlDups", "neverReparent", "noRedraw"):
        assert flag in passed, f"merge does not pass #{flag} explicitly"
    assert "promptMtlDups" not in passed
    assert "promptReparent" not in passed


def test_backup_does_not_make_the_backup_the_open_document():
    source = (BRIDGE / "backup.py").read_text(encoding="utf-8")
    assert "useNewFile=False" in source, (
        "without useNewFile:false the backup becomes the open document and the "
        "next save writes over the wrong file"
    )


def test_collapse_preserves_instancing():
    """`CollapseNodeTo(node, count, True)` makes every instance unique and
    memory goes UP — the exact opposite of the point."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    assert "CollapseNodeTo" in source
    assert "CollapseNodeTo(node, count, False)" in source


def test_scatter_properties_are_discovered_not_assumed():
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    assert "getPropNames" in source, (
        "Forest/RailClone/tyFlow property names are undocumented and version "
        "dependent; they must be discovered at runtime"
    )


def test_renderer_matching_never_uses_exact_equality():
    """V-Ray's class name changes between hotfixes."""
    source = (BRIDGE / "scene_query.py").read_text(encoding="utf-8")
    assert "matchPattern" in source


def test_render_hidden_defaults_to_the_conservative_answer():
    """If it cannot be read, assume hidden objects DO render, so nothing is
    deleted on a guess."""
    source = (BRIDGE / "scene_query.py").read_text(encoding="utf-8")
    index = source.index("def render_hidden")
    body = source[index : index + 400]
    assert "return True" in body
