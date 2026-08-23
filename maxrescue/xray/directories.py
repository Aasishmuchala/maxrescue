"""`DllDirectory` and `ClassDirectory3` — the file's own symbol table.

Together these answer, without loading a single plugin, the questions that
decide whether a scene will even open:

* which plugin DLLs does this file need, and are any of them missing here?
* what class is every object in the `Scene` stream?
* which classes are **scripted** (`DllIndex == -2`) — the doorway MAXScript
  payloads come through?

The load-bearing invariant is positional: a top-level chunk id in the `Scene`
stream *is* an index into `ClassDirectory3`, and a `ClassEntry.dll_index` *is*
an index into `DllDirectory`. So a malformed entry is never dropped — it keeps
its slot and carries `malformed=True`. Dropping one would silently misattribute
every class after it.

Layout (kaetemi part 4/5; ryzomcore `class_directory_3.cpp`, `dll_directory.cpp`):

    DllDirectory      0x21C0  optional 4-byte header (2010+), ignored
                      0x2038  entry container
                        0x2039  UTF-16LE description
                        0x2037  UTF-16LE dll filename

    ClassDirectory3   0x2040  entry container
                        0x2060  int32 DllIndex, uint32 ClassID.A,
                                uint32 ClassID.B, uint32 SuperClassID
                        0x2042  UTF-16LE class display name
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from maxrescue.xray.chunks import ChunkError, iter_chunks

__all__ = [
    "UNKNOWN_DLL",
    "BUILTIN_DLL",
    "SCRIPTED_DLL",
    "ClassCatalog",
    "ClassEntry",
    "DllEntry",
    "parse_class_directory",
    "parse_dll_directory",
]

CHUNK_DLL_ENTRY = 0x2038
CHUNK_DLL_DESCRIPTION = 0x2039
CHUNK_DLL_FILENAME = 0x2037
CHUNK_CLASS_ENTRY = 0x2040
CHUNK_CLASS_HEADER = 0x2060
CHUNK_CLASS_NAME = 0x2042

BUILTIN_DLL = -1
SCRIPTED_DLL = -2
#: Sentinel for an entry whose header could not be decoded. Distinct from
#: builtin/scripted so a damaged file never masquerades as a clean one.
UNKNOWN_DLL = -3

_CLASS_HEADER = struct.Struct("<iIII")

# SuperClassIDs, from the 3ds Max SDK `plugapi.h`.
SUPER_CLASS_NAMES: dict[int, str] = {
    0x00000001: "Node",
    0x00000002: "GenDerivedObject",
    0x00000003: "DerivedObject",
    0x00000004: "WSMDerivedObject",
    0x00000008: "ParamBlock",
    0x00000010: "GeomObject",
    0x00000020: "Camera",
    0x00000030: "Light",
    0x00000040: "Shape",
    0x00000050: "Helper",
    0x00000060: "System",
    0x00000082: "ParamBlock2",
    0x00000100: "RefMaker",
    0x00000200: "RefTarget",
    0x00000810: "Modifier",
    0x00000820: "WSModifier",
    0x00000830: "WSMObject",
    0x00000C00: "Material",
    0x00000C10: "Texmap",
    0x00000C20: "UVGen",
    0x00000C30: "XYZGen",
    0x00000C40: "TexOutput",
    0x00000F00: "Renderer",
    0x00001020: "Utility",
    0x00001080: "TexmapContainer",
    0x000010B0: "Shader",
    0x000010F0: "Layer",
    0x00001160: "CustAttrib",
    0x00009003: "FloatController",
    0x00009008: "Matrix3Controller",
    0x0000900B: "PositionController",
    0x0000900C: "RotationController",
    0x0000900D: "ScaleController",
    0xFFFFFD00: "Scene",
    0xFFFFFF06: "MAXScriptWrapper",
}

# ClassIDs worth naming even when the file's own name string is absent.
# Sources: SDK plugapi.h, Autodesk's 3dsmax-usd repo, and the vendor IDs
# captured in docs/research/max-format.md.
#
# Several low ClassIDs COLLIDE and are only separable by SuperClassID —
# (0x2, 0) is both RootNode and the Standard material; (0x200, 0) is both
# Multi/Sub-Object and Checker. Those live in the super-keyed table and are
# deliberately absent from the ClassID-only one, so a collision can never
# resolve to the wrong name.
KNOWN_CLASS_IDS_BY_SUPER: dict[tuple[int, int, int], str] = {
    (0x00000002, 0x00000000, 0x00000001): "RootNode",
    (0x00000002, 0x00000000, 0x00000C00): "Standard",
    (0x00000200, 0x00000000, 0x00000C00): "Multi/Sub-Object",
    (0x00000200, 0x00000000, 0x00000C10): "Checker",
}

KNOWN_CLASS_IDS: dict[tuple[int, int], str] = {
    (0x00000001, 0x00000000): "Node",
    (0x00002222, 0x00000000): "Scene",
    (0x00000082, 0x00000000): "ParamBlock2",
    (0x00000009, 0x00000000): "TriObject",
    (0xE44F10B3, 0x00000000): "Editable Mesh",
    (0x5D21369A, 0x00000000): "PolyObject",
    (0x1BF8338D, 0x192F6098): "Editable Poly",
    (0x29263A68, 0x405F22F5): "DerivedObject (OSM)",
    (0x4EC13906, 0x5578130E): "DerivedObject (WSM)",
    (0x92AAB38C, 0x00000000): "XRefObject",
    (0x272C0D4B, 0x432A414B): "XRefMaterial",
    (0x00000240, 0x00000000): "Bitmap",
    (0x0D727B3E, 0x491D29A7): "TurboSmooth",
    (0x00000032, 0x00007F9E): "MeshSmooth",
    (0x73CCF34A, 0x9ABC45FC): "OpenSubdiv",
    (0x79AA6E1D, 0x71A075B7): "Edit Poly (modifier)",
    (0x3EF24FE4, 0x5932330A): "ProOptimizer",
    (0x37BF3F2F, 0x7034695C): "VRayMtl",
    (0x6769144B, 0x02C1017D): "VRayBitmap",
    (0x71FA6E51, 0x72057C2F): "VRayNormalMap",
    (0x6066686A, 0x11731B4B): "VRay2SidedMtl",
    (0x3C5575A1, 0x5FD602DF): "VRayLight",
    (0x628140F6, 0x3BDB0E0C): "VRayPlane",
    (0x73BAB286, 0x77F8FD0C): "V-Ray renderer",
    (0x70BE6506, 0x448931DD): "CoronaMtl",
    (0x62A85DCC, 0x523C3604): "Corona renderer",
}


def _text(payload: bytes) -> str:
    """Decode a UTF-16LE name field as tolerantly as possible.

    A corrupt name must never cost us the rest of the entry — the ClassID and
    DllIndex beside it are what the X-ray actually reasons about.
    """
    return payload.decode("utf-16-le", errors="replace").rstrip("\x00")


@dataclass(frozen=True)
class DllEntry:
    """One plugin DLL the file declares. `index` is what `ClassEntry` points at."""

    index: int
    description: str
    filename: str
    malformed: bool = False


@dataclass(frozen=True)
class ClassEntry:
    """One class the file uses. `index` is the `Scene` chunk id that means it."""

    index: int
    dll_index: int
    class_id: tuple[int, int]
    super_class_id: int
    name: str
    malformed: bool = False

    @property
    def is_builtin(self) -> bool:
        return self.dll_index == BUILTIN_DLL

    @property
    def is_scripted(self) -> bool:
        """A MAXScript-defined class. Worth surfacing on its own — this is how
        scripted payloads enter a scene."""
        return self.dll_index == SCRIPTED_DLL

    @property
    def super_class_name(self) -> str:
        return SUPER_CLASS_NAMES.get(
            self.super_class_id, f"<super 0x{self.super_class_id:08X}>"
        )

    @property
    def is_geometry(self) -> bool:
        return self.super_class_id == 0x10

    @property
    def is_material(self) -> bool:
        return self.super_class_id == 0xC00

    @property
    def is_texmap(self) -> bool:
        return self.super_class_id == 0xC10

    @property
    def is_modifier(self) -> bool:
        return self.super_class_id in (0x810, 0x820)

    @property
    def is_node(self) -> bool:
        return self.super_class_id == 0x01


def _entry_children(data: bytes | memoryview, chunk) -> dict[int, bytes]:
    """Map child chunk id → payload for one directory entry.

    Unknown children are kept (harmless) and a malformed interior raises, which
    the callers convert into a flagged entry rather than a lost slot.
    """
    out: dict[int, bytes] = {}
    for child in iter_chunks(data, chunk.payload_start, chunk.end):
        out[child.ident] = child.payload(data)
    return out


def parse_dll_directory(data: bytes | memoryview) -> list[DllEntry]:
    """Parse a `DllDirectory` stream into positionally-indexed entries."""
    entries: list[DllEntry] = []
    for chunk in iter_chunks(data):
        if chunk.ident != CHUNK_DLL_ENTRY:
            continue  # 0x21C0 header, or anything else we do not model
        index = len(entries)
        try:
            children = _entry_children(data, chunk)
        except ChunkError:
            entries.append(DllEntry(index, "", "", malformed=True))
            continue
        filename_raw = children.get(CHUNK_DLL_FILENAME)
        entries.append(
            DllEntry(
                index=index,
                description=_text(children.get(CHUNK_DLL_DESCRIPTION, b"")),
                filename=_text(filename_raw) if filename_raw is not None else "",
                malformed=filename_raw is None,
            )
        )
    return entries


def parse_class_directory(data: bytes | memoryview) -> list[ClassEntry]:
    """Parse a `ClassDirectory3` stream into positionally-indexed entries."""
    entries: list[ClassEntry] = []
    for chunk in iter_chunks(data):
        if chunk.ident != CHUNK_CLASS_ENTRY:
            continue
        index = len(entries)
        try:
            children = _entry_children(data, chunk)
        except ChunkError:
            entries.append(
                ClassEntry(index, UNKNOWN_DLL, (0, 0), 0, "", malformed=True)
            )
            continue

        name = _text(children.get(CHUNK_CLASS_NAME, b""))
        header = children.get(CHUNK_CLASS_HEADER)
        if header is None or len(header) < _CLASS_HEADER.size:
            entries.append(
                ClassEntry(index, UNKNOWN_DLL, (0, 0), 0, name, malformed=True)
            )
            continue

        dll_index, class_a, class_b, super_id = _CLASS_HEADER.unpack_from(header, 0)
        entries.append(
            ClassEntry(
                index=index,
                dll_index=dll_index,
                class_id=(class_a, class_b),
                super_class_id=super_id,
                name=name,
            )
        )
    return entries


@dataclass(frozen=True)
class ClassCatalog:
    """The two directories, joined — the X-ray's lookup table."""

    dlls: list[DllEntry] = field(default_factory=list)
    classes: list[ClassEntry] = field(default_factory=list)

    def by_index(self, index: int) -> ClassEntry | None:
        """The class a `Scene` chunk id refers to, or `None` if out of range.

        Out-of-range is expected, not exceptional: the id comes from a file we
        already suspect of being damaged.
        """
        if 0 <= index < len(self.classes):
            return self.classes[index]
        return None

    def dll_for(self, entry: ClassEntry | None) -> DllEntry | None:
        """The DLL providing `entry`, or `None` for builtin/scripted/unknown."""
        if entry is None or entry.dll_index < 0:
            return None
        if entry.dll_index >= len(self.dlls):
            return None
        return self.dlls[entry.dll_index]

    def referenced_dlls(self) -> list[DllEntry]:
        """DLLs at least one class actually needs.

        A DLL listed but unreferenced costs nothing at load; a referenced one
        that is missing on this machine is why the file will not open.
        """
        used = sorted({c.dll_index for c in self.classes if c.dll_index >= 0})
        return [self.dlls[i] for i in used if i < len(self.dlls)]

    def scripted_classes(self) -> list[ClassEntry]:
        return [c for c in self.classes if c.is_scripted]

    def malformed_entries(self) -> list[ClassEntry]:
        return [c for c in self.classes if c.malformed]

    def describe(self, entry: ClassEntry | None) -> str:
        """Human-readable class name.

        Prefers the file's own name string, falls back to the known-ClassID
        table, then to the raw ID — never to a blank.
        """
        if entry is None:
            return "<unknown class>"
        a, b = entry.class_id
        known = KNOWN_CLASS_IDS_BY_SUPER.get(
            (a, b, entry.super_class_id)
        ) or KNOWN_CLASS_IDS.get(entry.class_id, "")
        base = entry.name or known
        if not base:
            a, b = entry.class_id
            base = f"<class 0x{a:08X}:0x{b:08X}>"
        if entry.is_scripted:
            return f"{base} (scripted)"
        return base
