"""Tier 3: the node graph.

This is the most version-fragile code in the project, so its central contract is
not "parses correctly" but **"degrades instead of failing"**. An unknown chunk
layout must cost one node its name, never the whole X-ray — tier 3 is a nicety
layered on top of tier 2, and it must never become a dependency of it.
"""

from __future__ import annotations

import io
import struct

import pytest

from maxrescue.xray.chunks import ChunkReader
from maxrescue.xray.directories import ClassCatalog, parse_class_directory
from maxrescue.xray.nodes import build_node_graph
from maxrescue.xray.scene_walk import walk_scene
from tests.helpers import (
    class_entry,
    container,
    derived_object,
    leaf,
    node_chunk,
    refs_flat,
    refs_map,
)

NODE, GEOM, MATERIAL, MODIFIER = 0x01, 0x10, 0xC00, 0x810

# class directory indices used by the fixtures below
IDX_NODE = 0
IDX_POLY = 1
IDX_VRAYMTL = 2
IDX_TURBOSMOOTH = 3
IDX_BEND = 4

DERIVED_OSM = 0x2032


def _catalog() -> ClassCatalog:
    return ClassCatalog(
        dlls=[],
        classes=parse_class_directory(
            class_entry(dll_index=-1, class_a=1, class_b=0, super_id=NODE, name="Node")
            + class_entry(dll_index=-1, class_a=0x1BF8338D, class_b=0x192F6098,
                          super_id=GEOM, name="Editable Poly")
            + class_entry(dll_index=-1, class_a=0x37BF3F2F, class_b=0x7034695C,
                          super_id=MATERIAL, name="VRayMtl")
            + class_entry(dll_index=-1, class_a=0x0D727B3E, class_b=0x491D29A7,
                          super_id=MODIFIER, name="TurboSmooth")
            + class_entry(dll_index=-1, class_a=0x10, class_b=0,
                          super_id=MODIFIER, name="Bend")
        ),
    )


def _graph(*objects: bytes, strict: bool = False):
    buf = container(0x2012, b"".join(objects))
    reader = ChunkReader(io.BytesIO(buf), len(buf))
    catalog = _catalog()
    inventory = walk_scene(reader, catalog)
    return build_node_graph(reader, catalog, inventory, strict=strict)


# --------------------------------------------------------------------------
# names and references
# --------------------------------------------------------------------------


def test_node_name_is_read():
    graph = _graph(node_chunk(IDX_NODE, name="Tree_Oak_07"))
    assert [n.name for n in graph.nodes] == ["Tree_Oak_07"]


def test_reference_map_resolves_object_and_material_slots():
    """0x2035 slots: 0 transform, 1 object, 3 material, 6 layer."""
    scene = (
        node_chunk(IDX_NODE, name="Hero", refs=refs_map({1: 1, 3: 2})),
        leaf(IDX_POLY, b"g" * 500),
        leaf(IDX_VRAYMTL, b"m" * 20),
    )
    (node,) = _graph(*scene).nodes
    assert node.object_position == 1
    assert node.object_class == "Editable Poly"
    assert node.material_position == 2
    assert node.material_class == "VRayMtl"


def test_flat_reference_list_resolves_by_position():
    """0x2034 carries the same slots, but positionally rather than keyed."""
    scene = (
        node_chunk(IDX_NODE, name="Hero", refs=refs_flat(-1, 1, -1, 2)),
        leaf(IDX_POLY, b"g" * 500),
        leaf(IDX_VRAYMTL, b"m" * 20),
    )
    (node,) = _graph(*scene).nodes
    assert node.object_position == 1
    assert node.material_position == 2


def test_null_references_are_none_not_zero():
    """-1 means 'no reference'. Reading it as index 0 would attribute every
    unassigned slot to whatever object happens to sit first in the scene."""
    scene = (
        node_chunk(IDX_NODE, name="Empty", refs=refs_flat(-1, -1, -1, -1)),
        leaf(IDX_POLY, b"g" * 10),
    )
    (node,) = _graph(*scene).nodes
    assert node.object_position is None
    assert node.material_position is None


def test_an_out_of_range_reference_degrades_to_unknown():
    scene = (node_chunk(IDX_NODE, name="Broken", refs=refs_map({1: 9999})),)
    (node,) = _graph(*scene).nodes
    assert node.object_position == 9999
    assert "unknown" in node.object_class.lower()


def test_parent_index_is_read():
    """The governor needs this: a parent and its children must merge together
    or the hierarchy breaks."""
    scene = (
        node_chunk(IDX_NODE, name="Child", parent=1, refs=refs_map({1: 2})),
        node_chunk(IDX_NODE, name="Parent"),
        leaf(IDX_POLY, b"g" * 10),
    )
    child = _graph(*scene).nodes[0]
    assert child.parent_position == 1


def test_only_nodes_are_reported_not_every_object():
    scene = (
        node_chunk(IDX_NODE, name="A"),
        leaf(IDX_POLY, b"g" * 10),
        leaf(IDX_VRAYMTL, b"m" * 10),
    )
    assert [n.name for n in _graph(*scene).nodes] == ["A"]


# --------------------------------------------------------------------------
# modifier stacks via DerivedObject
# --------------------------------------------------------------------------


def test_modifier_stack_is_read_in_order_and_the_base_object_found():
    """A DerivedObject's reference list IS the stack: the modifier-superclass
    refs in order, and the remaining one is the base object."""
    scene = (
        node_chunk(IDX_NODE, name="Hero", refs=refs_map({1: 1})),
        derived_object(DERIVED_OSM, refs_flat(3, 4, 2)),  # TurboSmooth, Bend, base
        leaf(IDX_POLY, b"g" * 500),
        leaf(IDX_TURBOSMOOTH, b"t" * 30),
        leaf(IDX_BEND, b"b" * 30),
    )
    (node,) = _graph(*scene).nodes
    assert node.modifiers == ("TurboSmooth", "Bend")
    assert node.base_object_position == 2
    assert node.base_object_class == "Editable Poly"
    assert node.modifier_depth == 2


def test_a_node_with_no_derived_object_has_an_empty_stack():
    scene = (
        node_chunk(IDX_NODE, name="Plain", refs=refs_map({1: 1})),
        leaf(IDX_POLY, b"g" * 500),
    )
    (node,) = _graph(*scene).nodes
    assert node.modifiers == ()
    assert node.modifier_depth == 0
    assert node.base_object_position == 1  # the object IS the base object


def test_object_weight_covers_the_whole_stack_not_just_the_base():
    """What a node costs is its object graph — an uncollapsed stack caches a
    mesh per modifier, so counting only the base understates it."""
    scene = (
        node_chunk(IDX_NODE, name="Hero", refs=refs_map({1: 1})),
        derived_object(DERIVED_OSM, refs_flat(3, 2)),
        leaf(IDX_POLY, b"g" * 1000),
        leaf(IDX_TURBOSMOOTH, b"t" * 500),
    )
    (node,) = _graph(*scene).nodes
    assert node.object_bytes > 1000 + 500


# --------------------------------------------------------------------------
# degradation — the contract
# --------------------------------------------------------------------------


def test_a_node_with_no_reference_chunk_degrades_but_keeps_its_name():
    (node,) = _graph(node_chunk(IDX_NODE, name="Nameless refs")).nodes
    assert node.name == "Nameless refs"
    assert node.resolved is False
    assert node.degradation is not None


def test_a_node_with_an_unreadable_interior_does_not_raise():
    """The whole point of tier 3: unknown layouts cost one node, not the run."""
    broken = container(IDX_NODE, struct.pack("<HI", 0x0962, 2))  # impossible size
    graph = _graph(broken, node_chunk(IDX_NODE, name="Fine", refs=refs_map({1: 0})))
    assert len(graph.nodes) == 2
    assert graph.nodes[0].resolved is False
    assert graph.nodes[1].name == "Fine"


def test_strict_mode_raises_so_development_notices():
    broken = container(IDX_NODE, struct.pack("<HI", 0x0962, 2))
    with pytest.raises(Exception):
        _graph(broken, strict=True)


def test_resolution_rate_is_reported():
    graph = _graph(
        node_chunk(IDX_NODE, name="Good", refs=refs_map({1: 2})),
        node_chunk(IDX_NODE, name="Bare"),
        leaf(IDX_POLY, b"g" * 10),
    )
    assert graph.total_nodes == 2
    assert graph.resolved_count == 1
    assert graph.resolution_rate == pytest.approx(0.5)


def test_resolution_rate_of_an_empty_scene_is_not_a_division_by_zero():
    graph = _graph(leaf(IDX_POLY, b"g" * 10))
    assert graph.total_nodes == 0
    assert graph.resolution_rate == 1.0


def test_a_node_with_a_corrupt_name_still_resolves_its_references():
    scene = (
        container(
            IDX_NODE,
            leaf(0x0962, b"\xff") + refs_map({1: 1}),
        ),
        leaf(IDX_POLY, b"g" * 500),
    )
    (node,) = _graph(*scene).nodes
    assert node.object_position == 1
    assert node.resolved is True


# --------------------------------------------------------------------------
# joining back to the weight inventory
# --------------------------------------------------------------------------


def test_heaviest_nodes_names_the_object_the_tier2_walk_only_numbered():
    scene = (
        node_chunk(IDX_NODE, name="Small", refs=refs_map({1: 2})),
        node_chunk(IDX_NODE, name="Tree_Oak_07", refs=refs_map({1: 3})),
        leaf(IDX_POLY, b"g" * 100),
        leaf(IDX_POLY, b"G" * 90_000),
    )
    top = _graph(*scene).heaviest(1)
    assert top[0].name == "Tree_Oak_07"
