"""Chunk reader — the foundation of the whole X-ray.

Every other xray module trusts these invariants, so they are tested hard:
sizes reconcile exactly, malformed input raises rather than looping, and the
streaming path never materialises a payload it was not asked for.
"""

from __future__ import annotations

import io
import struct

import pytest

from maxrescue.xray.chunks import (
    Chunk,
    ChunkError,
    ChunkReader,
    iter_chunks,
    walk_chunks,
)
from tests.helpers import container, leaf, utf16

# --------------------------------------------------------------------------
# header decoding
# --------------------------------------------------------------------------


def test_leaf_reports_id_payload_and_span():
    buf = leaf(0x2042, b"abcd")
    (chunk,) = list(iter_chunks(buf))
    assert chunk.ident == 0x2042
    assert chunk.is_container is False
    assert chunk.header_size == 6
    assert chunk.total_size == 10
    assert chunk.payload_start == 6
    assert chunk.payload_size == 4
    assert chunk.end == 10
    assert chunk.payload(buf) == b"abcd"


def test_container_flag_is_bit_31_and_not_part_of_the_size():
    buf = container(0x2040, leaf(0x2060, b"xyz"))
    (chunk,) = list(iter_chunks(buf))
    assert chunk.is_container is True
    assert chunk.total_size == len(buf)  # flag stripped, not counted


def test_empty_payload_leaf_is_legal():
    buf = leaf(0x204B, b"")
    (chunk,) = list(iter_chunks(buf))
    assert chunk.payload_size == 0
    assert chunk.total_size == 6


def test_wide_header_escape_reads_uint64_length():
    buf = leaf(0x0100, b"z" * 32, wide=True)
    (chunk,) = list(iter_chunks(buf))
    assert chunk.header_size == 14
    assert chunk.payload_size == 32
    assert chunk.total_size == 46
    assert chunk.payload(buf) == b"z" * 32


def test_wide_container_flag_is_bit_63():
    buf = container(0x2012, leaf(0x0001, b"ab"), wide=True)
    (chunk,) = list(iter_chunks(buf))
    assert chunk.is_container is True
    assert chunk.header_size == 14
    assert chunk.total_size == len(buf)


# --------------------------------------------------------------------------
# sibling / nesting traversal
# --------------------------------------------------------------------------


def test_siblings_are_yielded_in_file_order():
    buf = leaf(1, b"a") + leaf(2, b"bb") + leaf(3, b"ccc")
    assert [c.ident for c in iter_chunks(buf)] == [1, 2, 3]


def test_spans_reconcile_exactly_to_the_buffer_length():
    buf = leaf(1, b"a") + container(2, leaf(3, b"bb")) + leaf(4, b"c" * 9)
    chunks = list(iter_chunks(buf))
    assert sum(c.total_size for c in chunks) == len(buf)
    assert chunks[-1].end == len(buf)


def test_children_of_a_container_are_read_from_its_payload():
    inner = [leaf(0x2060, b"1234"), leaf(0x2042, utf16("Editable Poly"))]
    buf = container(0x2040, *inner)
    (entry,) = list(iter_chunks(buf))
    kids = list(iter_chunks(buf, entry.payload_start, entry.end))
    assert [k.ident for k in kids] == [0x2060, 0x2042]
    assert kids[1].payload(buf).decode("utf-16-le") == "Editable Poly"


def test_walk_yields_depth_and_never_descends_into_leaves():
    buf = container(0xA, container(0xB, leaf(0xC, b"x")), leaf(0xD, b"y"))
    seen = [(c.ident, depth) for c, depth in walk_chunks(buf)]
    assert seen == [(0xA, 0), (0xB, 1), (0xC, 2), (0xD, 1)]


def test_walk_respects_max_depth():
    buf = container(0xA, container(0xB, leaf(0xC, b"x")))
    seen = [c.ident for c, _ in walk_chunks(buf, max_depth=1)]
    assert seen == [0xA, 0xB]


# --------------------------------------------------------------------------
# malformed input — must raise, must not hang
# --------------------------------------------------------------------------


def test_truncated_header_raises():
    with pytest.raises(ChunkError, match="truncated header"):
        list(iter_chunks(leaf(1, b"abc")[:4]))


def test_truncated_wide_header_raises():
    with pytest.raises(ChunkError, match="truncated header"):
        list(iter_chunks(leaf(1, b"abc", wide=True)[:9]))


def test_payload_running_past_the_buffer_raises():
    buf = bytearray(leaf(1, b"abcd"))
    struct.pack_into("<I", buf, 2, 999)  # claims far more than exists
    with pytest.raises(ChunkError, match="overruns"):
        list(iter_chunks(bytes(buf)))


def test_size_smaller_than_its_own_header_raises_instead_of_looping():
    # size=2 is less than the 6-byte header; a naive reader advances by 2 or 0
    # and spins forever.
    buf = struct.pack("<HI", 1, 2) + b"padding"
    with pytest.raises(ChunkError, match="impossible size"):
        list(iter_chunks(buf))


def test_zero_wide_size_raises_instead_of_looping():
    buf = struct.pack("<HIQ", 1, 0, 0) + b"padding"
    with pytest.raises(ChunkError, match="impossible size"):
        list(iter_chunks(buf))


def test_trailing_garbage_shorter_than_a_header_raises():
    buf = leaf(1, b"ab") + b"\x00\x00"
    with pytest.raises(ChunkError, match="truncated header"):
        list(iter_chunks(buf))


def test_error_carries_the_offset_for_diagnosis():
    buf = leaf(1, b"ab") + struct.pack("<HI", 9, 2) + b"pad"
    with pytest.raises(ChunkError) as exc:
        list(iter_chunks(buf))
    assert exc.value.offset == 8


# --------------------------------------------------------------------------
# streaming reader — O(1) memory over a multi-GB Scene stream
# --------------------------------------------------------------------------


class CountingStream(io.BytesIO):
    """Records how many bytes were actually read, to prove laziness."""

    def __init__(self, data: bytes):
        super().__init__(data)
        self.bytes_read = 0

    def read(self, n=-1):  # type: ignore[override]
        out = super().read(n)
        self.bytes_read += len(out)
        return out


def test_streaming_top_level_reads_headers_only():
    payload = b"P" * 200_000
    buf = leaf(1, payload) + leaf(2, payload) + leaf(3, payload)
    stream = CountingStream(buf)

    chunks = list(ChunkReader(stream, len(buf)).iter_top_level())

    assert [c.ident for c in chunks] == [1, 2, 3]
    assert [c.payload_size for c in chunks] == [200_000] * 3
    # three 6-byte headers, not 600 KB of payload
    assert stream.bytes_read < 1024


def test_streaming_offsets_are_absolute():
    buf = leaf(1, b"a" * 10) + leaf(2, b"b" * 10)
    chunks = list(ChunkReader(io.BytesIO(buf), len(buf)).iter_top_level())
    assert chunks[0].start == 0
    assert chunks[1].start == 16
    assert chunks[1].payload_start == 22


def test_streaming_can_fetch_one_payload_on_demand():
    buf = leaf(1, b"a" * 10) + leaf(2, b"target")
    reader = ChunkReader(io.BytesIO(buf), len(buf))
    chunks = list(reader.iter_top_level())
    assert reader.read_payload(chunks[1]) == b"target"


def test_streaming_children_loads_only_that_container():
    big = leaf(0xFF, b"X" * 100_000)
    entry = container(0x2040, leaf(0x2060, b"1234"), leaf(0x2042, utf16("VRayMtl")))
    stream = CountingStream(big + entry)
    reader = ChunkReader(stream, len(big) + len(entry))

    top = list(reader.iter_top_level())
    kids = list(reader.iter_children(top[1]))

    assert [k.ident for k in kids] == [0x2060, 0x2042]
    # read the small entry's payload, never the 100 KB sibling
    assert stream.bytes_read < 10_000


def test_streaming_raises_on_a_chunk_that_overruns_the_stream():
    buf = bytearray(leaf(1, b"abcd"))
    struct.pack_into("<I", buf, 2, 999)
    reader = ChunkReader(io.BytesIO(bytes(buf)), len(buf))
    with pytest.raises(ChunkError, match="overruns"):
        list(reader.iter_top_level())


def test_streaming_stops_cleanly_at_the_declared_end():
    buf = leaf(1, b"ab") + leaf(2, b"cd")
    # declare only the first chunk's worth of stream
    reader = ChunkReader(io.BytesIO(buf), 8)
    assert [c.ident for c in reader.iter_top_level()] == [1]


def test_chunk_is_immutable():
    (chunk,) = list(iter_chunks(leaf(1, b"a")))
    assert isinstance(chunk, Chunk)
    with pytest.raises(Exception):
        chunk.ident = 2  # type: ignore[misc]


def test_streaming_children_reads_headers_not_the_container_payload():
    """The Scene stream wraps every object in one outer container, so loading a
    container's payload to enumerate its children would mean loading the whole
    scene."""
    kids = b"".join(leaf(i, b"P" * 50_000) for i in range(1, 6))
    buf = container(0x2012, kids)
    stream = CountingStream(buf)
    reader = ChunkReader(stream, len(buf))

    (outer,) = list(reader.iter_top_level())
    idents = [c.ident for c in reader.iter_children(outer)]

    assert idents == [1, 2, 3, 4, 5]
    assert stream.bytes_read < 1024  # six headers, not 250 KB


def test_iter_range_is_bounded_by_its_end():
    buf = leaf(1, b"a" * 10) + leaf(2, b"b" * 10)
    reader = ChunkReader(io.BytesIO(buf), len(buf))
    assert [c.ident for c in reader.iter_range(0, 16)] == [1]
    assert [c.ident for c in reader.iter_range(16, 32)] == [2]
