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


def test_scene_totals_are_read_in_one_crossing_not_per_node():
    """`stats()` runs twice per session and the session runs per batch. Counting
    polygons with a per-node pymxs call there costs millions of crossings on a
    large scene — the same O(n) trap that made collapse verification quadratic."""
    tree = ast.parse((BRIDGE / "scene_query.py").read_text(encoding="utf-8"))
    fn = next(
        item
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, ast.FunctionDef) and item.name == "stats"
    )
    loops_over_scene = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.Attribute)
        and n.iter.attr in ("geometry", "objects")
    ]
    assert not loops_over_scene, (
        "stats() iterates the scene from Python — use the bulk MAXScript helper"
    )
    assert "mrSceneTotals" in ast.dump(fn)


def test_proxy_export_uses_the_per_object_mode():
    """`exportMultiple=False` is the single-file mode: it bakes world transforms
    into the mesh and requires the proxy to sit at the origin. Combined with
    autoCreateProxies applying the original transform too, the object ends up
    displaced by its own position — a building 12 km from where it belongs, with
    the run reporting success."""
    tree = ast.parse((BRIDGE / "fix_services.py").read_text(encoding="utf-8"))
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "vrayMeshExport"
    )
    kwargs = {k.arg: k.value for k in call.keywords}
    assert isinstance(kwargs["exportMultiple"], ast.Constant)
    assert kwargs["exportMultiple"].value is True
    assert kwargs["exportPointClouds"].value is False, "point clouds change the render"


def test_proxy_conversion_verifies_the_object_did_not_move():
    """A handle check cannot notice a transform applied twice. Only the bounding
    box can."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    assert "_bounds_close" in source
    index = source.index("def convert_to_proxy")
    body = source[index : source.index("def set_proxy_display")]
    assert "_bounds(node)" in body and "_bounds_close" in body


def test_proxy_resolution_returns_a_node_not_a_base_object():
    """getClassInstances yields base objects. A base object fails isValidNode,
    so using one as a node handle makes the bounding-box display silently fail —
    and the entire memory saving depends on that display mode."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    assert "dependentNodes" in source, "base objects must be walked back to nodes"
    assert "isValidNode" in source


def test_messages_after_the_export_never_claim_the_original_survived():
    """autoCreateProxies deletes the original. Saying otherwise, at the moment
    an operator most needs the truth, sends them to an undo that headless Max
    does not honour."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    body = source[
        source.index("proxy = self._resolve_created_node") :
        source.index("def set_proxy_display")
    ]
    assert "left untouched" not in body
    assert "recover from the backup" in body


# ---------------------------------------------------------------------------
# proxy quality — "nothing should look any different"
# ---------------------------------------------------------------------------


def test_export_never_condenses_material_ids():
    """condenseMultiMtl renumbers material IDs. A Multi/Sub whose IDs have moved
    renders the wrong material on every face."""
    tree = ast.parse((BRIDGE / "fix_services.py").read_text(encoding="utf-8"))
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "vrayMeshExport"
    )
    kwargs = {k.arg: k.value for k in call.keywords}
    assert kwargs["condenseMultiMtl"].value is False
    assert kwargs["createMultiMtl"].value is True, "sub-materials must follow faces"
    assert kwargs["animation"].value is False


def test_export_keeps_chunked_voxelisation():
    """oneVoxelPerMesh collapses the mesh to a single voxel, so V-Ray loads the
    whole thing at render time instead of streaming the chunks a bucket needs —
    the opposite of what a memory tool wants."""
    tree = ast.parse((BRIDGE / "fix_services.py").read_text(encoding="utf-8"))
    call = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "vrayMeshExport"
    )
    kwargs = {k.arg: k.value for k in call.keywords}
    assert kwargs["oneVoxelPerMesh"].value is False


def test_every_render_affecting_proxy_property_is_asserted_neutral():
    """Display mode is viewport-only, but point clouds, LOD scale, scale, axis
    flip and map-channel remapping all reach the renderer."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    for prop in (
        "point_cloud",
        "lod_scale",
        "scale",
        "flip_axis",
        "map_channel",
    ):
        assert prop in source, f"{prop} is render-affecting and is not checked"
    assert "_render_affecting_drift" in source

    body = source[source.index("def convert_to_proxy") : source.index("def set_proxy_display")]
    assert "_render_affecting_drift" in body, (
        "the check must run during conversion, not merely exist"
    )


def test_a_build_exposing_no_proxy_properties_says_so():
    """Silence would mean 'confirmed neutral' when nothing was confirmed."""
    source = (BRIDGE / "fix_services.py").read_text(encoding="utf-8")
    # Phrases checked separately: the message is split across string literals,
    # so a contiguous match would fail on formatting alone.
    assert "no render-affecting proxy properties were exposed" in source
    assert "confirmed neutral" in source


# ---------------------------------------------------------------------------
# the boundary-crossing rule, enforced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _bridge_modules(), ids=lambda p: p.name)
def test_no_bridge_module_iterates_the_scene_from_python(path: pathlib.Path):
    """Per-node pymxs access is ~10x a single bulk crossing. The guard walks the
    scene twice per session and a session runs per batch, so on a 50k-object
    scene across 50 batches that is five million crossings — the exact shape
    that stalled a sibling project for nine minutes."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        node.iter.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Attribute)
        and isinstance(node.iter.value, ast.Name)
        and node.iter.value.id == "rt"
        and node.iter.attr in ("objects", "geometry", "shapes", "lights", "cameras")
    ]
    assert not offenders, (
        f"{path.name} iterates rt.{offenders} from Python — use a bulk "
        "MAXScript helper in maxscript.py instead"
    )


def test_undo_records_when_it_cannot_actually_roll_back():
    """A hold already open means no rollback. Doing that silently leaves a
    failed operation half-applied while the caller believes it was undone."""
    source = (BRIDGE / "undo.py").read_text(encoding="utf-8")
    assert "notes" in source
    assert "would not be rolled back" in source


def test_vram_records_whether_it_measured_the_process_or_the_whole_card():
    """A Vantage session on the same GPU would otherwise be attributed to Max."""
    source = (BRIDGE / "memory.py").read_text(encoding="utf-8")
    assert "whole-gpu" in source
    assert "nvidia-smi/process" in source
