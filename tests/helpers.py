"""Synthetic chunk-tree builders for tests.

These encode the 3ds Max chunk format so the reader can be exercised without a
real `.max` file. NOT production code — MaxRescue never writes `.max` data.

Format (verified against kaetemi's format series, Blender's io_scene_max, and
ryzomcore's pipeline_max):

    offset 0: uint16 id
    offset 2: uint32 size   -- INCLUDES the 6-byte header; bit 31 = container
    if that uint32 == 0:
        offset 6: uint64 size  -- INCLUDES the 14-byte header; bit 63 = container
"""

from __future__ import annotations

import struct

CONTAINER_32 = 0x80000000
CONTAINER_64 = 0x8000000000000000


def leaf(ident: int, payload: bytes, *, wide: bool = False) -> bytes:
    """A leaf chunk carrying `payload`."""
    if wide:
        return struct.pack("<HIQ", ident, 0, 14 + len(payload)) + payload
    return struct.pack("<HI", ident, 6 + len(payload)) + payload


def container(ident: int, *children: bytes, wide: bool = False) -> bytes:
    """A container chunk holding already-encoded `children`."""
    body = b"".join(children)
    if wide:
        return struct.pack("<HIQ", ident, 0, (14 + len(body)) | CONTAINER_64) + body
    return struct.pack("<HI", ident, (6 + len(body)) | CONTAINER_32) + body


def utf16(text: str) -> bytes:
    """UTF-16LE with no terminator — how Max stores names."""
    return text.encode("utf-16-le")


def class_entry(
    *,
    dll_index: int,
    class_a: int,
    class_b: int,
    super_id: int,
    name: str,
) -> bytes:
    """One ClassDirectory3 entry (0x2040) with its 0x2060 header and 0x2042 name."""
    header = struct.pack("<iIII", dll_index, class_a, class_b, super_id)
    return container(0x2040, leaf(0x2060, header), leaf(0x2042, utf16(name)))


def dll_entry(description: str, filename: str) -> bytes:
    """One DllDirectory entry (0x2038)."""
    return container(
        0x2038, leaf(0x2039, utf16(description)), leaf(0x2037, utf16(filename))
    )


def refs_flat(*indices: int) -> bytes:
    """A 0x2034 reference list: a flat int32 array where position == slot."""
    return leaf(0x2034, struct.pack(f"<{len(indices)}i", *indices))


def refs_map(pairs: dict[int, int], flags: int = 0x10) -> bytes:
    """A 0x2035 reference map: [flags, key0, idx0, key1, idx1, ...]."""
    values = [flags]
    for key in sorted(pairs):
        values += [key, pairs[key]]
    return leaf(0x2035, struct.pack(f"<{len(values)}I", *values))


def node_chunk(
    ident: int,
    *,
    name: str | None = None,
    refs: bytes | None = None,
    parent: int | None = None,
    extra: bytes = b"",
) -> bytes:
    """An INode object chunk: name (0x0962), parent (0x0960), references."""
    body = b""
    if parent is not None:
        body += leaf(0x0960, struct.pack("<II", parent, 0))
    if name is not None:
        body += leaf(0x0962, utf16(name))
    if refs is not None:
        body += refs
    body += extra
    return container(ident, body)


def derived_object(ident: int, refs: bytes) -> bytes:
    """A DerivedObject (0x2032/0x2033): its reference list IS the modifier stack."""
    return container(ident, refs)
