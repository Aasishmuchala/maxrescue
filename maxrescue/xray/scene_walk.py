"""Tier 2: the shallow walk of the `Scene` stream.

Two facts about the format make this cheap and robust:

* a top-level object chunk's **id is its index into `ClassDirectory3`**, so the
  class is known without decoding anything;
* the chunk's **size is its on-disk weight**, so the weight is known without
  reading a byte of payload.

Together those give a per-object `(class, bytes)` inventory from headers alone —
no content decoding, nothing version-fragile. That is what sizes merge batches,
and what finds a single monstrous object immediately.

Two ids are hard-coded in the format rather than living in the class directory:
`0x2032` (OSM DerivedObject) and `0x2033` (WSM DerivedObject). Looking those up
by index would name them after whichever class happens to sit at index 8242.

The walk degrades rather than raising. A tool for damaged files that throws on
damage is worthless, so a malformed chunk truncates the inventory, sets
`truncated`, and records where it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxrescue.xray.chunks import ChunkError, ChunkReader
from maxrescue.xray.directories import ClassCatalog, ClassEntry

__all__ = [
    "DERIVED_OBJECT_OSM",
    "DERIVED_OBJECT_WSM",
    "ClassWeight",
    "SceneInventory",
    "SceneObject",
    "walk_scene",
]

DERIVED_OBJECT_OSM = 0x2032
DERIVED_OBJECT_WSM = 0x2033

_DERIVED = {
    DERIVED_OBJECT_OSM: "DerivedObject (OSM)",
    DERIVED_OBJECT_WSM: "DerivedObject (WSM)",
}


@dataclass(frozen=True)
class SceneObject:
    """One top-level object, described from its header alone."""

    position: int
    """0-based position in the scene container — how objects reference each
    other, so this is an identifier, not a display detail."""

    ident: int
    offset: int
    bytes: int
    header_size: int
    """6 or 14 — needed to find this object's interior when tier 3 descends."""

    class_name: str
    super_class_name: str
    class_entry: ClassEntry | None

    @property
    def payload_start(self) -> int:
        return self.offset + self.header_size

    @property
    def end(self) -> int:
        return self.offset + self.bytes

    @property
    def is_derived_object(self) -> bool:
        return self.ident in _DERIVED

    @property
    def is_geometry(self) -> bool:
        return self.class_entry is not None and self.class_entry.is_geometry

    @property
    def is_material(self) -> bool:
        return self.class_entry is not None and self.class_entry.is_material


@dataclass(frozen=True)
class ClassWeight:
    class_name: str
    count: int
    bytes: int


@dataclass(frozen=True)
class SceneInventory:
    objects: tuple[SceneObject, ...]
    stream_size: int
    version_ident: int | None = None
    truncated: bool = False
    error: str | None = None

    @property
    def total_bytes(self) -> int:
        return sum(o.bytes for o in self.objects)

    @property
    def geometry_bytes(self) -> int:
        return sum(o.bytes for o in self.objects if o.is_geometry)

    @property
    def unaccounted_bytes(self) -> int:
        """Stream bytes not covered by any object span.

        Container headers account for a few; anything larger means the walk
        missed something, and the report should say so rather than implying
        full coverage.
        """
        return max(0, self.stream_size - self.total_bytes)

    def histogram(self) -> list[ClassWeight]:
        """Per-class totals, heaviest first."""
        totals: dict[str, list[int]] = {}
        for obj in self.objects:
            row = totals.setdefault(obj.class_name, [0, 0])
            row[0] += 1
            row[1] += obj.bytes
        rows = [ClassWeight(name, c, b) for name, (c, b) in totals.items()]
        rows.sort(key=lambda r: (-r.bytes, r.class_name))
        return rows

    def heaviest(self, count: int = 20) -> list[SceneObject]:
        return sorted(self.objects, key=lambda o: -o.bytes)[:count]


def _describe(ident: int, catalog: ClassCatalog | None) -> tuple[str, str, ClassEntry | None]:
    if ident in _DERIVED:
        return _DERIVED[ident], "DerivedObject", None
    if catalog is None:
        return f"<unknown class {ident}>", "<unknown>", None
    entry = catalog.by_index(ident)
    if entry is None:
        return f"<unknown class {ident}>", "<unknown>", None
    return catalog.describe(entry), entry.super_class_name, entry


def walk_scene(
    reader: ChunkReader,
    catalog: ClassCatalog | None,
) -> SceneInventory:
    """Inventory the top-level objects of a `Scene` stream, headers only."""
    version_ident: int | None = None
    start, end = 0, reader.size

    # Most files wrap the whole scene in one version container; some do not.
    try:
        top = list(reader.iter_top_level())
    except ChunkError as exc:
        return SceneInventory(
            objects=(), stream_size=reader.size, truncated=True, error=str(exc)
        )

    if len(top) == 1 and top[0].is_container:
        version_ident = top[0].ident
        start, end = top[0].payload_start, top[0].end

    objects: list[SceneObject] = []
    truncated = False
    error: str | None = None

    try:
        for position, chunk in enumerate(reader.iter_range(start, end)):
            class_name, super_name, entry = _describe(chunk.ident, catalog)
            objects.append(
                SceneObject(
                    position=position,
                    ident=chunk.ident,
                    offset=chunk.start,
                    bytes=chunk.total_size,
                    header_size=chunk.header_size,
                    class_name=class_name,
                    super_class_name=super_name,
                    class_entry=entry,
                )
            )
    except ChunkError as exc:
        # Keep what was read. A partial inventory of a damaged scene is the
        # whole point of this tool.
        truncated = True
        error = str(exc)

    return SceneInventory(
        objects=tuple(objects),
        stream_size=reader.size,
        version_ident=version_ident,
        truncated=truncated,
        error=error,
    )
