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
