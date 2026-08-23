"""Reader for the chunk tree inside a `.max` file's streams.

Read-only. MaxRescue never writes `.max` data — no independent writer for the
format exists, and inventing one would risk the very files it is meant to save.

Layout, cross-verified against three independent parsers (kaetemi's
`pipeline_max`, Blender's `io_scene_max`, NeKidaem's `max_dump`):

    offset 0: uint16 id
    offset 2: uint32 size    -- INCLUDES the 6-byte header; bit 31 set = container
    if that uint32 is 0:
        offset 6: uint64 size -- INCLUDES the 14-byte header; bit 63 set = container

A chunk id is only meaningful relative to its parent, except at the top level of
the `Scene` stream, where the id *is* the object's index into `ClassDirectory3`.

Two access paths:

* :func:`iter_chunks` / :func:`walk_chunks` over an in-memory buffer, for the
  small streams (`DllDirectory`, `ClassDirectory3`) that are read whole.
* :class:`ChunkReader` over a seekable stream, which reads **headers only** and
  seeks past payloads. The `Scene` stream of a rescue-grade file is multiple
  gigabytes; it is never materialised.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

__all__ = ["Chunk", "ChunkError", "ChunkReader", "iter_chunks", "walk_chunks"]

_NARROW_HEADER = 6
_WIDE_HEADER = 14
_CONTAINER_32 = 0x80000000
_CONTAINER_64 = 0x8000000000000000
_SIZE_MASK_32 = 0x7FFFFFFF
_SIZE_MASK_64 = 0x7FFFFFFFFFFFFFFF

_HEAD_32 = struct.Struct("<HI")
_WIDE_LEN = struct.Struct("<Q")


class ChunkError(Exception):
    """Malformed chunk data.

    Always carries the byte `offset` it was detected at — a rescue tool's most
    common input is a file something is already wrong with, so "where" matters
    as much as "what".
    """

    def __init__(self, message: str, offset: int):
        super().__init__(f"{message} (at offset {offset})")
        self.offset = offset


@dataclass(frozen=True)
class Chunk:
    """One chunk header. Payload bytes are fetched separately, never held here."""

    ident: int
    is_container: bool
    start: int
    header_size: int
    total_size: int

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def payload_size(self) -> int:
        return self.total_size - self.header_size

    @property
    def end(self) -> int:
        return self.start + self.total_size

    def payload(self, data: bytes | memoryview) -> bytes:
        """The payload, from the buffer this chunk was parsed out of.

        Only valid for chunks produced by :func:`iter_chunks` / :func:`walk_chunks`,
        whose offsets are relative to that buffer. For chunks from
        :class:`ChunkReader`, use :meth:`ChunkReader.read_payload`.
        """
        return bytes(data[self.payload_start : self.end])


def _decode_header(head: bytes | memoryview, offset: int) -> Chunk:
    """Decode a header from `head`, which must start at absolute `offset`.

    `head` needs 6 bytes, or 14 when the narrow size field is the wide escape.
    """
    if len(head) < _NARROW_HEADER:
        raise ChunkError("truncated header", offset)

    ident, raw = _HEAD_32.unpack_from(head, 0)

    if raw != 0:
        container = bool(raw & _CONTAINER_32)
        total = raw & _SIZE_MASK_32
        header = _NARROW_HEADER
    else:
        if len(head) < _WIDE_HEADER:
            raise ChunkError("truncated header", offset)
        (raw64,) = _WIDE_LEN.unpack_from(head, _NARROW_HEADER)
        container = bool(raw64 & _CONTAINER_64)
        total = raw64 & _SIZE_MASK_64
        header = _WIDE_HEADER

    # A size that cannot even cover its own header would advance the cursor by
    # zero or backwards — the classic infinite loop in naive readers.
    if total < header:
        raise ChunkError(f"impossible size {total}", offset)

    return Chunk(
        ident=ident,
        is_container=container,
        start=offset,
        header_size=header,
        total_size=total,
    )


def iter_chunks(
    data: bytes | memoryview,
    start: int = 0,
    end: int | None = None,
) -> Iterator[Chunk]:
    """Yield the chunks laid out consecutively in `data[start:end]`.

    Offsets on the yielded chunks are relative to `data`.
    """
    limit = len(data) if end is None else end
    pos = start
    while pos < limit:
        chunk = _decode_header(data[pos : pos + _WIDE_HEADER], pos)
        if chunk.end > limit:
            raise ChunkError(
                f"chunk 0x{chunk.ident:04X} overruns its container "
                f"({chunk.end} > {limit})",
                pos,
            )
        yield chunk
        pos = chunk.end


def walk_chunks(
    data: bytes | memoryview,
    start: int = 0,
    end: int | None = None,
    max_depth: int | None = None,
    _depth: int = 0,
) -> Iterator[tuple[Chunk, int]]:
    """Depth-first walk yielding `(chunk, depth)`, descending into containers."""
    for chunk in iter_chunks(data, start, end):
        yield chunk, _depth
        if chunk.is_container and (max_depth is None or _depth < max_depth):
            yield from walk_chunks(
                data, chunk.payload_start, chunk.end, max_depth, _depth + 1
            )


class ChunkReader:
    """Lazy chunk access over a seekable binary stream.

    Reads headers only; payloads are fetched on demand. This is what keeps the
    shallow `Scene` walk O(1) in memory over a multi-gigabyte stream.
    """

    def __init__(self, stream: BinaryIO, size: int):
        self._stream = stream
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    def iter_range(self, start: int, end: int) -> Iterator[Chunk]:
        """Yield the chunks between two absolute stream offsets, headers only.

        Never reads a payload. Everything else here is built on this, which is
        what keeps a walk over a multi-gigabyte stream cheap.
        """
        pos = start
        while pos < end:
            self._stream.seek(pos)
            chunk = _decode_header(self._stream.read(_WIDE_HEADER), pos)
            if chunk.end > end:
                raise ChunkError(
                    f"chunk 0x{chunk.ident:04X} overruns its container "
                    f"({chunk.end} > {end})",
                    pos,
                )
            yield chunk
            pos = chunk.end

    def iter_top_level(self) -> Iterator[Chunk]:
        """Yield every top-level chunk, seeking past each payload."""
        yield from self.iter_range(0, self._size)

    def read_payload(self, chunk: Chunk) -> bytes:
        """Fetch one chunk's payload. The only place bytes are materialised."""
        self._stream.seek(chunk.payload_start)
        return self._stream.read(chunk.payload_size)

    def iter_children(self, chunk: Chunk) -> Iterator[Chunk]:
        """Yield the children of one container, in absolute stream coordinates.

        Headers only — the `Scene` stream wraps every object in a single outer
        container, so materialising a container's payload here would mean
        loading the entire scene. Use :meth:`read_payload` on the results rather
        than ``Chunk.payload``, since their offsets are stream-relative.
        """
        if not chunk.is_container:
            return
        yield from self.iter_range(chunk.payload_start, chunk.end)
