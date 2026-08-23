"""DllDirectory + ClassDirectory3 — the backbone of the X-ray.

A top-level chunk id in the `Scene` stream *is* an index into ClassDirectory3,
so positional alignment of these entries is load-bearing. Every tolerance
decision here is made to preserve that alignment: a malformed entry is still an
entry, flagged, never silently dropped.
"""

from __future__ import annotations

import struct

from maxrescue.xray.directories import (
    UNKNOWN_DLL,
    ClassCatalog,
    parse_class_directory,
    parse_dll_directory,
)
from tests.helpers import class_entry, container, dll_entry, leaf, utf16

EPOLY = (0x1BF8338D, 0x192F6098)
GEOMOBJECT = 0x10
MATERIAL = 0xC00


def _catalog() -> ClassCatalog:
    dlls = parse_dll_directory(
        dll_entry("Editable Poly Object (Autodesk)", "EPoly.dlo")
        + dll_entry("V-Ray renderer", "vrender2026.dlr")
        + dll_entry("Forest Pack Pro", "ForestPackPro.dlo")
    )
    classes = parse_class_directory(
        class_entry(dll_index=0, class_a=EPOLY[0], class_b=EPOLY[1],
                    super_id=GEOMOBJECT, name="Editable Poly")
        + class_entry(dll_index=1, class_a=0x37BF3F2F, class_b=0x7034695C,
                      super_id=MATERIAL, name="VRayMtl")
        + class_entry(dll_index=-1, class_a=0x0002, class_b=0x0000,
                      super_id=MATERIAL, name="Standard")
        + class_entry(dll_index=-2, class_a=0x11111111, class_b=0x22222222,
                      super_id=GEOMOBJECT, name="MyScriptedThing")
    )
    return ClassCatalog(dlls=dlls, classes=classes)


# --------------------------------------------------------------------------
# DllDirectory
# --------------------------------------------------------------------------


def test_dll_entries_keep_file_order_as_their_index():
    dlls = parse_dll_directory(
        dll_entry("first", "a.dlo") + dll_entry("second", "b.dlo")
    )
    assert [(d.index, d.filename) for d in dlls] == [(0, "a.dlo"), (1, "b.dlo")]
    assert dlls[0].description == "first"


def test_dll_directory_tolerates_the_optional_header_chunk():
    buf = leaf(0x21C0, struct.pack("<i", 1)) + dll_entry("desc", "x.dlo")
    dlls = parse_dll_directory(buf)
    assert [d.filename for d in dlls] == ["x.dlo"]


def test_dll_directory_ignores_unknown_sibling_chunks():
    buf = dll_entry("a", "a.dlo") + leaf(0x9999, b"junk") + dll_entry("b", "b.dlo")
    assert [d.index for d in parse_dll_directory(buf)] == [0, 1]


def test_empty_dll_directory_is_empty_not_an_error():
    assert parse_dll_directory(b"") == []


def test_dll_entry_missing_its_filename_is_flagged_but_keeps_its_slot():
    buf = dll_entry("ok", "a.dlo") + container(0x2038, leaf(0x2039, utf16("no file")))
    dlls = parse_dll_directory(buf)
    assert len(dlls) == 2
    assert dlls[1].index == 1
    assert dlls[1].filename == ""
    assert dlls[1].malformed is True


def test_dll_names_survive_a_corrupt_utf16_payload():
    buf = container(0x2038, leaf(0x2039, b"\xff"), leaf(0x2037, utf16("x.dlo")))
    dlls = parse_dll_directory(buf)
    assert dlls[0].filename == "x.dlo"  # the good field still parses


def test_trailing_nulls_are_stripped_from_names():
    buf = container(
        0x2038, leaf(0x2039, utf16("desc\x00")), leaf(0x2037, utf16("x.dlo\x00"))
    )
    assert parse_dll_directory(buf)[0].filename == "x.dlo"


# --------------------------------------------------------------------------
# ClassDirectory3
# --------------------------------------------------------------------------


def test_class_entry_unpacks_dll_index_class_id_and_superclass():
    classes = parse_class_directory(
        class_entry(dll_index=7, class_a=EPOLY[0], class_b=EPOLY[1],
                    super_id=GEOMOBJECT, name="Editable Poly")
    )
    entry = classes[0]
    assert entry.index == 0
    assert entry.dll_index == 7
    assert entry.class_id == EPOLY
    assert entry.super_class_id == GEOMOBJECT
    assert entry.name == "Editable Poly"
    assert entry.malformed is False


def test_class_index_is_the_position_in_the_directory():
    buf = b"".join(
        class_entry(dll_index=-1, class_a=i, class_b=0, super_id=GEOMOBJECT,
                    name=f"C{i}")
        for i in range(5)
    )
    classes = parse_class_directory(buf)
    assert [c.index for c in classes] == [0, 1, 2, 3, 4]
    assert classes[3].name == "C3"


def test_dll_index_minus_one_is_builtin():
    (entry,) = parse_class_directory(
        class_entry(dll_index=-1, class_a=2, class_b=0, super_id=MATERIAL,
                    name="Standard")
    )
    assert entry.is_builtin is True
    assert entry.is_scripted is False


def test_dll_index_minus_two_is_scripted():
    """A scripted class is a first-class signal — it is how MAXScript payloads
    enter a scene."""
    (entry,) = parse_class_directory(
        class_entry(dll_index=-2, class_a=1, class_b=2, super_id=GEOMOBJECT,
                    name="Suspicious")
    )
    assert entry.is_scripted is True
    assert entry.is_builtin is False


def test_malformed_class_header_keeps_its_slot_and_is_flagged():
    good = class_entry(dll_index=0, class_a=1, class_b=2, super_id=GEOMOBJECT,
                       name="Good")
    bad = container(0x2040, leaf(0x2060, b"\x01\x02"), leaf(0x2042, utf16("Bad")))
    classes = parse_class_directory(good + bad + good)
    assert [c.index for c in classes] == [0, 1, 2]
    assert classes[1].malformed is True
    assert classes[1].dll_index == UNKNOWN_DLL
    assert classes[2].name == "Good"  # alignment survived


def test_class_entry_with_no_name_is_still_an_entry():
    buf = container(0x2040, leaf(0x2060, struct.pack("<iIII", -1, 9, 0, GEOMOBJECT)))
    (entry,) = parse_class_directory(buf)
    assert entry.name == ""
    assert entry.class_id == (9, 0)


# --------------------------------------------------------------------------
# catalog: joining the two directories
# --------------------------------------------------------------------------


def test_catalog_joins_a_class_to_its_dll():
    cat = _catalog()
    entry = cat.by_index(0)
    assert entry is not None
    dll = cat.dll_for(entry)
    assert dll is not None and dll.filename == "EPoly.dlo"


def test_catalog_returns_none_for_builtin_and_scripted_classes():
    cat = _catalog()
    assert cat.dll_for(cat.by_index(2)) is None  # builtin
    assert cat.dll_for(cat.by_index(3)) is None  # scripted


def test_catalog_by_index_out_of_range_is_none_not_an_exception():
    """Scene chunk ids come from a possibly-damaged file; an out-of-range id
    must degrade to 'unknown class', never crash the X-ray."""
    assert _catalog().by_index(9999) is None
    assert _catalog().by_index(-1) is None


def test_catalog_lists_only_dlls_actually_referenced_by_a_class():
    cat = _catalog()
    used = {d.filename for d in cat.referenced_dlls()}
    assert used == {"EPoly.dlo", "vrender2026.dlr"}
    assert "ForestPackPro.dlo" in {d.filename for d in cat.dlls}


def test_catalog_reports_scripted_classes():
    assert [c.name for c in _catalog().scripted_classes()] == ["MyScriptedThing"]


def test_catalog_resolves_known_class_ids_to_friendly_names():
    cat = _catalog()
    assert cat.describe(cat.by_index(0)).startswith("Editable Poly")
    # even when the file's own name string is missing, the ClassID is known
    (entry,) = parse_class_directory(
        container(0x2040, leaf(0x2060, struct.pack("<iIII", -1, EPOLY[0], EPOLY[1],
                                                   GEOMOBJECT)))
    )
    assert "Editable Poly" in ClassCatalog(dlls=[], classes=[entry]).describe(entry)


def test_catalog_describes_an_unknown_class_by_its_raw_id():
    (entry,) = parse_class_directory(
        class_entry(dll_index=-1, class_a=0xDEADBEEF, class_b=0xFEEDFACE,
                    super_id=GEOMOBJECT, name="")
    )
    described = ClassCatalog(dlls=[], classes=[entry]).describe(entry)
    assert "DEADBEEF" in described.upper()


def test_superclass_names_are_resolved():
    cat = _catalog()
    assert cat.by_index(0).super_class_name == "GeomObject"
    assert cat.by_index(1).super_class_name == "Material"


def test_unknown_superclass_is_reported_as_hex_not_dropped():
    (entry,) = parse_class_directory(
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=0xABCD, name="X")
    )
    assert "ABCD" in entry.super_class_name.upper()


def test_geometry_and_modifier_predicates():
    cat = _catalog()
    assert cat.by_index(0).is_geometry is True
    assert cat.by_index(1).is_geometry is False
    assert cat.by_index(1).is_material is True
    (modifier,) = parse_class_directory(
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=0x810, name="Bend")
    )
    assert modifier.is_modifier is True


# --------------------------------------------------------------------------
# ClassID collisions — only SuperClassID separates these
# --------------------------------------------------------------------------


def _unnamed(class_a: int, class_b: int, super_id: int) -> ClassCatalog:
    """An entry with no name string, so `describe` must fall back to the tables."""
    (entry,) = parse_class_directory(
        container(
            0x2040, leaf(0x2060, struct.pack("<iIII", -1, class_a, class_b, super_id))
        )
    )
    return ClassCatalog(dlls=[], classes=[entry])


def test_class_id_2_resolves_by_superclass_not_first_match():
    """(0x2, 0) is BOTH RootNode and the Standard material. A ClassID-only
    lookup silently mislabels one of them."""
    assert _unnamed(2, 0, 0x001).describe(_unnamed(2, 0, 0x001).by_index(0)) == "RootNode"
    assert _unnamed(2, 0, 0xC00).describe(_unnamed(2, 0, 0xC00).by_index(0)) == "Standard"


def test_class_id_200_resolves_by_superclass():
    """(0x200, 0) is both Multi/Sub-Object (Material) and Checker (Texmap)."""
    cat_m = _unnamed(0x200, 0, 0xC00)
    cat_t = _unnamed(0x200, 0, 0xC10)
    assert cat_m.describe(cat_m.by_index(0)) == "Multi/Sub-Object"
    assert cat_t.describe(cat_t.by_index(0)) == "Checker"


def test_colliding_ids_are_absent_from_the_superclass_free_table():
    """Belt and braces: if one ever leaks back into the ClassID-only table the
    collision returns silently."""
    from maxrescue.xray.directories import KNOWN_CLASS_IDS

    assert (0x2, 0) not in KNOWN_CLASS_IDS
    assert (0x200, 0) not in KNOWN_CLASS_IDS
