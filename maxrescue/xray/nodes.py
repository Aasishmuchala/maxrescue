"""Tier 3: the node graph — names, hierarchy, materials, modifier stacks.

Tier 2 can tell you object #30 weighs 878 MB. This tells you it is called
`Tree_Oak_07`, carries a TurboSmooth over an Editable Poly, and hangs off
`Vegetation_Group`.

Layout (ryzomcore `node_impl.cpp`, kaetemi part 5, confirmed against a real
chunk dump):

    0x0960  parent   : uint32 parent index, uint32 flags
    0x0962  name     : UTF-16LE
    0x2034  refs     : flat int32 array, position == slot, -1 == null
    0x2035  refs     : [flags, key0, idx0, key1, idx1, ...] as uint32
      slot 0 transform controller · 1 object · 3 material · 6 layer

A node's object slot often points at a **DerivedObject** (`0x2032`/`0x2033`)
rather than the mesh. Its reference list *is* the modifier stack: the refs whose
class has a modifier superclass, in order, and the remaining one is the base
object.

**This is the most version-fragile code in the project, and it is deliberately
the least load-bearing.** Everything that matters operationally — weights,
batching, plugin lists — comes from tier 2. So an unknown layout here costs one
node its name and nothing else: parsing runs per node inside its own guard, and
a failure records `resolved=False` with a reason. `strict=True` turns that into
a raise, for development only.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from maxrescue.xray.chunks import ChunkError, ChunkReader
from maxrescue.xray.directories import ClassCatalog
from maxrescue.xray.scene_walk import (
    DERIVED_OBJECT_OSM,
    DERIVED_OBJECT_WSM,
    SceneInventory,
    SceneObject,
)

__all__ = ["NodeGraph", "NodeInfo", "build_node_graph"]

CHUNK_PARENT = 0x0960
CHUNK_NAME = 0x0962
CHUNK_REFS_FLAT = 0x2034
CHUNK_REFS_MAP = 0x2035

SLOT_TRANSFORM = 0
SLOT_OBJECT = 1
SLOT_MATERIAL = 3
SLOT_LAYER = 6

_NULL_SIGNED = -1
_NULL_UNSIGNED = 0xFFFFFFFF
_UNKNOWN = "<unknown>"


@dataclass(frozen=True)
class NodeInfo:
    position: int
    name: str
    bytes: int

    parent_position: int | None = None
    layer_position: int | None = None

    object_position: int | None = None
    object_class: str = _UNKNOWN
    base_object_position: int | None = None
    base_object_class: str = _UNKNOWN
    material_position: int | None = None
    material_class: str = _UNKNOWN

    modifiers: tuple[str, ...] = ()
    object_bytes: int = 0

    resolved: bool = False
    degradation: str | None = None

    @property
    def modifier_depth(self) -> int:
        return len(self.modifiers)


@dataclass(frozen=True)
class NodeGraph:
    nodes: tuple[NodeInfo, ...]

    @property
    def total_nodes(self) -> int:
        return len(self.nodes)

    @property
    def resolved_count(self) -> int:
        return sum(1 for n in self.nodes if n.resolved)

    @property
    def resolution_rate(self) -> float:
        """Share of nodes fully resolved. Reported so a degraded run is visible
        as a degraded run, rather than reading as a complete one."""
        if not self.nodes:
            return 1.0
        return self.resolved_count / len(self.nodes)

    def heaviest(self, count: int = 20) -> list[NodeInfo]:
        return sorted(self.nodes, key=lambda n: -n.object_bytes)[:count]

    def owner_of(self, position: int) -> NodeInfo | None:
        """The node an object belongs to — what turns "object #30" into a name.

        Checks the object slot, the base object behind a modifier stack, and the
        node chunk itself, since the heaviest thing found by tier 2 may be any
        of the three.
        """
        for node in self.nodes:
            if position in (
                node.object_position,
                node.base_object_position,
                node.position,
            ):
                return node
        return None


def _text(payload: bytes) -> str:
    return payload.decode("utf-16-le", errors="replace").rstrip("\x00")


def _ref_list(payload: bytes, ident: int) -> list[int | None]:
    """Reference indices in slot order, with nulls preserved as `None`.

    Reading a null as index 0 would attribute every unassigned slot in the scene
    to whichever object happens to sit first — a very plausible-looking lie.
    """
    if ident == CHUNK_REFS_FLAT:
        count = len(payload) // 4
        values = struct.unpack_from(f"<{count}i", payload, 0)
        return [None if v == _NULL_SIGNED or v < 0 else v for v in values]

    count = len(payload) // 4
    raw = struct.unpack_from(f"<{count}I", payload, 0)
    pairs = raw[1:]  # element 0 is a flags word, not a slot
    slots: dict[int, int | None] = {}
    for i in range(0, len(pairs) - 1, 2):
        key, value = pairs[i], pairs[i + 1]
        slots[key] = None if value == _NULL_UNSIGNED else value
    if not slots:
        return []
    return [slots.get(k) for k in sorted(slots)]


def _ref_slots(payload: bytes, ident: int) -> dict[int, int | None]:
    """Reference indices keyed by slot number."""
    if ident == CHUNK_REFS_FLAT:
        return dict(enumerate(_ref_list(payload, ident)))
    count = len(payload) // 4
    raw = struct.unpack_from(f"<{count}I", payload, 0)
    pairs = raw[1:]
    out: dict[int, int | None] = {}
    for i in range(0, len(pairs) - 1, 2):
        key, value = pairs[i], pairs[i + 1]
        out[key] = None if value == _NULL_UNSIGNED else value
    return out


def _interior(reader: ChunkReader, obj: SceneObject) -> dict[int, bytes]:
    """Child chunk id → payload for one object. Small by construction."""
    out: dict[int, bytes] = {}
    for child in reader.iter_range(obj.payload_start, obj.end):
        out[child.ident] = reader.read_payload(child)
    return out


def _class_of(index: int | None, by_position: dict[int, SceneObject]) -> str:
    if index is None:
        return _UNKNOWN
    target = by_position.get(index)
    return target.class_name if target else f"<unknown object {index}>"


def build_node_graph(
    reader: ChunkReader,
    catalog: ClassCatalog | None,
    inventory: SceneInventory,
    *,
    strict: bool = False,
) -> NodeGraph:
    """Resolve names, hierarchy and modifier stacks for every INode."""
    by_position = {o.position: o for o in inventory.objects}
    nodes: list[NodeInfo] = []

    for obj in inventory.objects:
        entry = obj.class_entry
        if entry is None or not entry.is_node:
            continue
        nodes.append(_read_node(reader, obj, by_position, strict=strict))

    return NodeGraph(nodes=tuple(nodes))


def _read_node(
    reader: ChunkReader,
    obj: SceneObject,
    by_position: dict[int, SceneObject],
    *,
    strict: bool,
) -> NodeInfo:
    name = ""
    try:
        interior = _interior(reader, obj)
    except (ChunkError, struct.error, ValueError) as exc:
        if strict:
            raise
        return NodeInfo(
            position=obj.position,
            name=name,
            bytes=obj.bytes,
            resolved=False,
            degradation=f"interior unreadable: {exc}",
        )

    if CHUNK_NAME in interior:
        name = _text(interior[CHUNK_NAME])

    parent: int | None = None
    if CHUNK_PARENT in interior and len(interior[CHUNK_PARENT]) >= 4:
        (raw_parent,) = struct.unpack_from("<I", interior[CHUNK_PARENT], 0)
        parent = None if raw_parent == _NULL_UNSIGNED else raw_parent

    ref_ident = next(
        (i for i in (CHUNK_REFS_MAP, CHUNK_REFS_FLAT) if i in interior), None
    )
    if ref_ident is None:
        if strict:
            raise ChunkError("node has no reference list", obj.offset)
        return NodeInfo(
            position=obj.position,
            name=name,
            bytes=obj.bytes,
            parent_position=parent,
            resolved=False,
            degradation="no reference list (0x2034/0x2035) in this node",
        )

    try:
        slots = _ref_slots(interior[ref_ident], ref_ident)
    except (struct.error, ValueError) as exc:
        if strict:
            raise
        return NodeInfo(
            position=obj.position,
            name=name,
            bytes=obj.bytes,
            parent_position=parent,
            resolved=False,
            degradation=f"reference list unreadable: {exc}",
        )

    object_position = slots.get(SLOT_OBJECT)
    material_position = slots.get(SLOT_MATERIAL)
    layer_position = slots.get(SLOT_LAYER)

    modifiers, base_position, object_bytes = _resolve_object_graph(
        reader, object_position, by_position, strict=strict
    )

    return NodeInfo(
        position=obj.position,
        name=name,
        bytes=obj.bytes,
        parent_position=parent,
        layer_position=layer_position,
        object_position=object_position,
        object_class=_class_of(object_position, by_position),
        base_object_position=base_position,
        base_object_class=_class_of(base_position, by_position),
        material_position=material_position,
        material_class=_class_of(material_position, by_position),
        modifiers=modifiers,
        object_bytes=object_bytes,
        resolved=True,
    )


def _resolve_object_graph(
    reader: ChunkReader,
    object_position: int | None,
    by_position: dict[int, SceneObject],
    *,
    strict: bool,
) -> tuple[tuple[str, ...], int | None, int]:
    """Walk a node's object slot into (modifier classes, base object, weight).

    Weight covers the whole object graph, not just the base mesh: an
    uncollapsed stack caches a mesh per modifier, so counting only the base
    understates what the node actually costs.
    """
    if object_position is None:
        return (), None, 0

    target = by_position.get(object_position)
    if target is None:
        return (), None, 0

    if target.ident not in (DERIVED_OBJECT_OSM, DERIVED_OBJECT_WSM):
        # No stack — the object slot points straight at the base object.
        return (), object_position, target.bytes

    try:
        interior = _interior(reader, target)
        ref_ident = next(
            (i for i in (CHUNK_REFS_MAP, CHUNK_REFS_FLAT) if i in interior), None
        )
        if ref_ident is None:
            return (), object_position, target.bytes
        references = _ref_list(interior[ref_ident], ref_ident)
    except (ChunkError, struct.error, ValueError):
        if strict:
            raise
        return (), object_position, target.bytes

    modifiers: list[str] = []
    base_position: int | None = None
    total = target.bytes

    for index in references:
        if index is None:
            continue
        referenced = by_position.get(index)
        if referenced is None:
            continue
        total += referenced.bytes
        if referenced.class_entry is not None and referenced.class_entry.is_modifier:
            modifiers.append(referenced.class_name)
        else:
            # The non-modifier reference is the base object; the last one wins.
            base_position = index

    return tuple(modifiers), base_position, total
