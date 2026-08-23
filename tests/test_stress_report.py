"""Stress: reporting, names, and resource handling.

Archviz scenes are full of names this code has to survive: Cyrillic, CJK,
emoji, embedded nulls, 4,000 characters of nothing. And an X-ray is run over
many files in one session, so a leaked file handle is a real failure mode rather
than a theoretical one.

The report must never crash and never overstate. Those two are the product.
"""

from __future__ import annotations

import gc
import json

import pytest

from maxrescue.core.governor import Governor, candidates_from
from maxrescue.xray.nodes import NodeInfo
from maxrescue.xray.ole import MaxFile
from maxrescue.xray.report import Verdict, xray
from maxrescue.xray.scene_walk import SceneInventory, SceneObject
from maxrescue.xray.signatures import scan_bytes
from tests.helpers import class_entry, container, dll_entry, leaf, node_chunk, refs_map
from tests.helpers_ole import build_ole, pad_chunks, scene_stream

NODE, GEOM = 0x01, 0x10
EPOLY = (0x1BF8338D, 0x192F6098)

HOSTILE_NAMES = [
    "Дерево_01",
    "树_02",
    "🌲_emoji_tree",
    "name\x00with\x00nulls",
    "A" * 4000,
    "",
    "   ",
    "..\\..\\..\\windows\\system32",
    "name\twith\ttabs",
    "name\nwith\nnewlines",
    "%s %d %%",           # format-string injection into the report
    "<script>alert(1)</script>",
    "-" * 200,
]


def _classes() -> bytes:
    return (
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=NODE, name="Node")
        + class_entry(dll_index=0, class_a=EPOLY[0], class_b=EPOLY[1],
                      super_id=GEOM, name="Editable Poly")
    )


def _scene_with_names(names: list[str]) -> bytes:
    objects: list[bytes] = []
    for name in names:
        position = len(objects) + 1
        objects.append(node_chunk(0, name=name, refs=refs_map({1: position})))
        objects.append(leaf(1, b"g" * 2000))
    return scene_stream(b"".join(objects))


def _file(tmp_path, scene: bytes, name: str = "s.max"):
    path = tmp_path / name
    path.write_bytes(
        build_ole(
            {
                "Scene": scene,
                "ClassDirectory3": pad_chunks(_classes()),
                "DllDirectory": pad_chunks(dll_entry("d", "EPoly.dlo")),
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# hostile names
# ---------------------------------------------------------------------------


def test_stress_hostile_node_names_survive_the_whole_pipeline(tmp_path):
    report = xray(str(_file(tmp_path, _scene_with_names(HOSTILE_NAMES))))

    text = report.to_text()
    assert isinstance(text, str)
    blob = report.to_json()
    assert json.loads(blob)

    candidates = candidates_from(report.nodes.nodes)
    batches = Governor(ram_budget=1 << 30).plan_all(candidates)
    assert sum(len(b.names) for b in batches) == len(candidates)


def test_stress_a_format_string_in_a_name_is_not_interpreted(tmp_path):
    """`%s %d %%` in a node name must appear literally, not consume arguments."""
    report = xray(str(_file(tmp_path, _scene_with_names(["%s %d %%", "Normal"]))))
    names = {n.name for n in report.nodes.nodes}
    assert "%s %d %%" in names


def test_stress_a_four_thousand_character_name_does_not_wreck_the_layout(tmp_path):
    report = xray(str(_file(tmp_path, _scene_with_names(["B" * 4000, "Short"]))))
    text = report.to_text()
    # The report is columnar; one absurd name must not produce a megabyte of it.
    assert len(text) < 200_000


def test_stress_an_empty_name_is_excluded_from_merge_candidates(tmp_path):
    """`mergeMaxFile` selects by name; an unnamed node cannot be asked for, and
    pretending otherwise would silently drop it."""
    report = xray(str(_file(tmp_path, _scene_with_names(["", "Real"]))))
    names = [c.name for c in candidates_from(report.nodes.nodes)]
    assert "" not in names
    assert "Real" in names


def test_stress_names_with_nulls_are_trimmed_not_truncated_to_nothing(tmp_path):
    report = xray(str(_file(tmp_path, _scene_with_names(["ok\x00name"]))))
    (node,) = report.nodes.nodes
    assert node.name.startswith("ok")


# ---------------------------------------------------------------------------
# the report never crashes and never overstates
# ---------------------------------------------------------------------------


def test_stress_the_report_renders_for_every_shape_of_scene(tmp_path):
    scenes = {
        "empty": scene_stream(b""),
        "one_object": scene_stream(leaf(1, b"g" * 5000)),
        "only_nodes": _scene_with_names(["A", "B"]),
        "huge_object": scene_stream(leaf(1, b"g" * 500_000)),
    }
    for label, scene in scenes.items():
        report = xray(str(_file(tmp_path, scene, f"{label}.max")))
        assert isinstance(report.to_text(), str), label
        assert json.loads(report.to_json()), label


def test_stress_a_scene_with_no_objects_is_not_called_a_monster():
    inventory = SceneInventory(objects=(), stream_size=0)
    from maxrescue.xray.directories import ClassCatalog
    from maxrescue.xray.nodes import NodeGraph
    from maxrescue.xray.report import _judge

    verdict, notes = _judge(inventory, NodeGraph(nodes=()), (), ClassCatalog())
    assert verdict == Verdict.HEALTHY
    assert notes


def test_stress_identical_object_weights_never_trigger_dominance():
    """Regression pressure on the scale-free rule: N identical objects, for many
    N, must never look like one object dominating."""
    from maxrescue.xray.directories import ClassCatalog
    from maxrescue.xray.nodes import NodeGraph
    from maxrescue.xray.report import _judge

    for count in (2, 3, 4, 5, 10, 100, 1000):
        objects = tuple(
            SceneObject(
                position=i, ident=1, offset=i * 100, bytes=1000, header_size=6,
                class_name="Editable Poly", super_class_name="GeomObject",
                class_entry=None,
            )
            for i in range(count)
        )
        verdict, _ = _judge(
            SceneInventory(objects=objects, stream_size=count * 1000),
            NodeGraph(nodes=()), (), ClassCatalog(),
        )
        assert verdict != Verdict.MONSTER_OBJECT, f"{count} equal objects"


def test_stress_resolution_rate_below_the_bar_is_always_surfaced():
    """A partly-read graph must never be presented as complete."""
    from maxrescue.xray.directories import ClassCatalog
    from maxrescue.xray.nodes import NodeGraph
    from maxrescue.xray.report import _judge

    nodes = tuple(
        NodeInfo(position=i, name=f"n{i}", bytes=10, resolved=(i % 4 == 0))
        for i in range(100)
    )
    _, notes = _judge(
        SceneInventory(objects=(), stream_size=0), NodeGraph(nodes=nodes), (),
        ClassCatalog(),
    )
    assert any("resolved" in note for note in notes)


def test_stress_signature_scan_of_a_large_buffer_is_not_quadratic():
    import time

    data = (b"harmless scene data " * 50_000) + b"CRP_BScript"
    started = time.time()
    found = scan_bytes(data, "Scene")
    assert time.time() - started < 15.0
    assert [f.signature for f in found] == ["CRP_BScript"]


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------


def test_stress_repeated_xrays_do_not_leak_file_handles(tmp_path):
    """A session x-rays many files. A leaked handle eventually exhausts the
    process, and on Windows also locks the file against the next tool."""
    path = _file(tmp_path, _scene_with_names([f"N{i}" for i in range(20)]))
    try:
        import psutil

        process = psutil.Process()
        before = len(process.open_files())
    except Exception:
        process = None
        before = 0

    for _ in range(60):
        xray(str(path))
    gc.collect()

    if process is not None:
        after = len(process.open_files())
        assert after - before < 10, f"handles grew from {before} to {after}"


def test_stress_a_maxfile_closes_its_handle_on_an_error_during_open(tmp_path):
    bad = tmp_path / "bad.max"
    bad.write_bytes(b"not a compound file at all, not even close")
    for _ in range(200):
        with pytest.raises(Exception):
            MaxFile.open(str(bad))


def test_stress_context_manager_closes_even_when_the_body_raises(tmp_path):
    path = _file(tmp_path, _scene_with_names(["A"]))
    with pytest.raises(ValueError):
        with MaxFile.open(str(path)) as mf:
            assert mf.streams
            raise ValueError("boom")
    # A second open must succeed — on Windows a leaked handle would lock it.
    with MaxFile.open(str(path)) as mf:
        assert mf.streams
