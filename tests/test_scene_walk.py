"""The shallow `Scene` walk (tier 2).

Reads every top-level object's *header* only. A chunk's id is its index into
`ClassDirectory3`, and its size is its on-disk weight — so class and weight come
free, with no content decoding and no version-fragile parsing.

This is what sizes the merge batches later, and what finds a single monstrous
object instantly. It must survive a damaged file: a rescue tool that raises on
the scenes it exists for is worthless.
"""

from __future__ import annotations

import io
import struct

from maxrescue.xray.chunks import ChunkReader
from maxrescue.xray.directories import ClassCatalog, parse_class_directory
from maxrescue.xray.scene_walk import (
    DERIVED_OBJECT_OSM,
    DERIVED_OBJECT_WSM,
    walk_scene,
)
from tests.helpers import class_entry, container, leaf

GEOMOBJECT = 0x10
MATERIAL = 0xC00


def _catalog() -> ClassCatalog:
    return ClassCatalog(
        dlls=[],
        classes=parse_class_directory(
            class_entry(dll_index=-1, class_a=1, class_b=0, super_id=0x01, name="Node")
            + class_entry(dll_index=0, class_a=0x1BF8338D, class_b=0x192F6098,
                          super_id=GEOMOBJECT, name="Editable Poly")
            + class_entry(dll_index=1, class_a=0x37BF3F2F, class_b=0x7034695C,
                          super_id=MATERIAL, name="VRayMtl")
        ),
    )


def _reader(buf: bytes) -> ChunkReader:
    return ChunkReader(io.BytesIO(buf), len(buf))


def _scene(*objects: bytes, wrapped: bool = True) -> bytes:
    body = b"".join(objects)
    return container(0x2012, body) if wrapped else body


# --------------------------------------------------------------------------
# finding the objects
# --------------------------------------------------------------------------


def test_objects_are_found_inside_the_outer_version_container():
    buf = _scene(leaf(1, b"x" * 100), leaf(2, b"y" * 50))
    inv = walk_scene(_reader(buf), _catalog())
    assert [o.ident for o in inv.objects] == [1, 2]
    assert inv.version_ident == 0x2012


def test_objects_are_found_when_the_stream_has_no_outer_container():
    """Not every file wraps its scene; the walk must not depend on it."""
    buf = _scene(leaf(1, b"x" * 100), leaf(2, b"y" * 50), wrapped=False)
    inv = walk_scene(_reader(buf), _catalog())
    assert [o.ident for o in inv.objects] == [1, 2]
    assert inv.version_ident is None


def test_positions_are_the_reference_indices():
    """Objects reference each other by 0-based position in this container, so
    the numbering is load-bearing, not cosmetic."""
    buf = _scene(*[leaf(1, b"x" * 10) for _ in range(5)])
    inv = walk_scene(_reader(buf), _catalog())
    assert [o.position for o in inv.objects] == [0, 1, 2, 3, 4]


def test_an_empty_scene_is_empty_not_an_error():
    inv = walk_scene(_reader(b""), _catalog())
    assert inv.objects == ()
    assert inv.total_bytes == 0


# --------------------------------------------------------------------------
# class resolution
# --------------------------------------------------------------------------


def test_chunk_id_resolves_to_a_class_through_the_catalog():
    buf = _scene(leaf(1, b"x" * 100))
    (obj,) = walk_scene(_reader(buf), _catalog()).objects
    assert obj.class_name == "Editable Poly"
    assert obj.super_class_name == "GeomObject"
    assert obj.is_geometry is True


def test_derived_object_ids_are_special_cased_not_looked_up():
    """0x2032 / 0x2033 are hard-coded in the format and absent from the class
    directory — looking them up would name them after whatever class happens to
    sit at index 8242."""
    buf = _scene(leaf(DERIVED_OBJECT_OSM, b"x" * 10), leaf(DERIVED_OBJECT_WSM, b"y" * 10))
    inv = walk_scene(_reader(buf), _catalog())
    assert [o.class_name for o in inv.objects] == [
        "DerivedObject (OSM)",
        "DerivedObject (WSM)",
    ]
    assert all(o.is_derived_object for o in inv.objects)


def test_an_id_outside_the_class_directory_degrades_to_unknown():
    buf = _scene(leaf(9999, b"x" * 10))
    (obj,) = walk_scene(_reader(buf), _catalog()).objects
    assert "unknown" in obj.class_name.lower()
    assert "9999" in obj.class_name
    assert obj.class_entry is None
    assert obj.is_geometry is False


def test_walk_without_a_catalog_still_reports_weights():
    """Weight is readable even when the class directory is unreadable."""
    buf = _scene(leaf(1, b"x" * 100))
    (obj,) = walk_scene(_reader(buf), None).objects
    assert obj.bytes == 106
    assert "unknown" in obj.class_name.lower()


# --------------------------------------------------------------------------
# weights
# --------------------------------------------------------------------------


def test_object_weight_is_its_whole_chunk_span():
    buf = _scene(leaf(1, b"x" * 100))
    (obj,) = walk_scene(_reader(buf), _catalog()).objects
    assert obj.bytes == 106  # 6-byte header + payload


def test_total_bytes_sums_the_objects():
    buf = _scene(leaf(1, b"x" * 100), leaf(2, b"y" * 200))
    inv = walk_scene(_reader(buf), _catalog())
    assert inv.total_bytes == 106 + 206


def test_histogram_aggregates_by_class_and_sorts_by_weight():
    buf = _scene(
        leaf(2, b"m" * 10),          # VRayMtl, small
        leaf(1, b"x" * 1000),        # Editable Poly, big
        leaf(1, b"x" * 1000),
        leaf(2, b"m" * 10),
    )
    rows = walk_scene(_reader(buf), _catalog()).histogram()
    assert [r.class_name for r in rows] == ["Editable Poly", "VRayMtl"]
    assert rows[0].count == 2
    assert rows[0].bytes == 2 * 1006
    assert rows[1].count == 2


def test_heaviest_returns_the_biggest_objects_first():
    buf = _scene(
        leaf(1, b"a" * 10),
        leaf(1, b"b" * 5000),
        leaf(1, b"c" * 100),
    )
    top = walk_scene(_reader(buf), _catalog()).heaviest(2)
    assert [o.bytes for o in top] == [5006, 106]


def test_heaviest_handles_asking_for_more_than_exist():
    buf = _scene(leaf(1, b"a" * 10))
    assert len(walk_scene(_reader(buf), _catalog()).heaviest(50)) == 1


def test_geometry_bytes_are_separable_from_everything_else():
    """The geometry/other split is the first thing the batch planner wants."""
    buf = _scene(leaf(1, b"x" * 1000), leaf(2, b"m" * 100))
    inv = walk_scene(_reader(buf), _catalog())
    assert inv.geometry_bytes == 1006
    assert inv.total_bytes - inv.geometry_bytes == 106


# --------------------------------------------------------------------------
# damaged files — degrade, never raise
# --------------------------------------------------------------------------


def test_a_malformed_chunk_truncates_the_walk_instead_of_raising():
    good = leaf(1, b"x" * 100)
    bad = struct.pack("<HI", 7, 2)  # size smaller than its own header
    inv = walk_scene(_reader(_scene(good + bad + good)), _catalog())
    assert len(inv.objects) == 1        # what could be read is kept
    assert inv.truncated is True
    assert inv.error is not None
    assert "offset" in inv.error


def test_a_clean_walk_is_not_marked_truncated():
    inv = walk_scene(_reader(_scene(leaf(1, b"x" * 10))), _catalog())
    assert inv.truncated is False
    assert inv.error is None


def test_reconciliation_reports_unaccounted_bytes():
    """If object spans do not add up to the stream, something was missed and the
    report should say so rather than implying full coverage."""
    buf = _scene(leaf(1, b"x" * 100))
    inv = walk_scene(_reader(buf), _catalog())
    # container header (6) is the only overhead
    assert inv.unaccounted_bytes == 6


# --------------------------------------------------------------------------
# laziness
# --------------------------------------------------------------------------


class _Counting(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, n=-1):  # type: ignore[override]
        out = super().read(n)
        self.bytes_read += len(out)
        return out


def test_the_walk_never_reads_object_payloads():
    buf = _scene(*[leaf(1, b"P" * 100_000) for _ in range(10)])
    stream = _Counting(buf)
    inv = walk_scene(ChunkReader(stream, len(buf)), _catalog())
    assert len(inv.objects) == 10
    assert inv.total_bytes > 1_000_000
    assert stream.bytes_read < 4096  # eleven headers
