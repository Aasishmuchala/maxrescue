"""The OLE2 container layer.

The point of interest here is size. `olefile.openstream()` returns a BytesIO
holding the *entire* stream — its own source carries a FIXME saying it should
load sectors on demand instead. MaxRescue exists for files whose `Scene` stream
is gigabytes, so bulk reads follow the FAT sector chain lazily instead.
"""

from __future__ import annotations

import gzip
import io
from array import array

import pytest

from maxrescue.xray.chunks import ChunkReader
from maxrescue.xray.ole import MaxFile, MaxFileError, SectorChainReader
from tests.helpers import container, dll_entry, leaf
from tests.helpers_ole import build_ole, pad_chunks


def _blob(**streams: bytes) -> bytes:
    return build_ole({k.replace("__", "\x05"): pad_chunks(v) for k, v in streams.items()})


def _simple() -> bytes:
    return _blob(
        Scene=leaf(0x0018, b"x" * 100),
        DllDirectory=dll_entry("Editable Poly Object (Autodesk)", "EPoly.dlo"),
        ClassDirectory3=container(0x2040, leaf(0x2060, b"\x00" * 16)),
    )


# --------------------------------------------------------------------------
# opening
# --------------------------------------------------------------------------


def test_opens_a_compound_file_and_lists_its_streams():
    with MaxFile.from_bytes(_simple()) as mf:
        names = {s.name for s in mf.streams}
    assert {"Scene", "DllDirectory", "ClassDirectory3"} <= names


def test_stream_sizes_are_reported():
    with MaxFile.from_bytes(_simple()) as mf:
        assert mf.info("Scene").size == 4096


def test_a_non_ole_file_raises_a_typed_error_naming_the_problem():
    with pytest.raises(MaxFileError, match="not an OLE"):
        MaxFile.from_bytes(b"this is not a compound file, it is a sentence")


def test_an_empty_input_raises_rather_than_returning_an_empty_file():
    with pytest.raises(MaxFileError):
        MaxFile.from_bytes(b"")


def test_has_and_info_for_a_missing_stream():
    with MaxFile.from_bytes(_simple()) as mf:
        assert mf.has("Scene") is True
        assert mf.has("NoSuchStream") is False
        with pytest.raises(MaxFileError, match="no stream"):
            mf.info("NoSuchStream")


def test_stream_lookup_is_case_insensitive_like_the_format():
    with MaxFile.from_bytes(_simple()) as mf:
        assert mf.has("scene") is True
        assert mf.info("SCENE").size == 4096


def test_close_is_idempotent():
    mf = MaxFile.from_bytes(_simple())
    mf.close()
    mf.close()


def test_use_after_close_raises_instead_of_segfaulting_on_a_dead_handle():
    mf = MaxFile.from_bytes(_simple())
    mf.close()
    with pytest.raises(MaxFileError, match="closed"):
        mf.read("Scene")


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def test_read_returns_the_exact_stream_bytes():
    payload = pad_chunks(dll_entry("desc", "x.dlo"))
    with MaxFile.from_bytes(build_ole({"DllDirectory": payload})) as mf:
        assert mf.read("DllDirectory") == payload


def test_reader_yields_parseable_chunks():
    with MaxFile.from_bytes(_simple()) as mf:
        reader = mf.open_reader("Scene")
        idents = [c.ident for c in reader.iter_top_level()]
    assert idents[0] == 0x0018  # our object, then the pad chunk
    assert 0x7FFF in idents


def test_reader_is_a_chunkreader_over_the_declared_size():
    with MaxFile.from_bytes(_simple()) as mf:
        reader = mf.open_reader("Scene")
        assert isinstance(reader, ChunkReader)
        assert reader.size == mf.info("Scene").size


# --------------------------------------------------------------------------
# gzip-framed streams
# --------------------------------------------------------------------------


def test_compressed_stream_is_detected_and_inflated():
    inner = pad_chunks(dll_entry("desc", "packed.dlo"))
    packed = gzip.compress(inner)
    blob = build_ole({"DllDirectory": packed + b"\x00" * max(0, 4096 - len(packed))})
    with MaxFile.from_bytes(blob) as mf:
        assert mf.info("DllDirectory").compressed is True
        assert mf.read("DllDirectory").startswith(inner[:64])


def test_uncompressed_stream_is_not_flagged_compressed():
    with MaxFile.from_bytes(_simple()) as mf:
        assert mf.info("Scene").compressed is False


def test_a_stream_that_only_looks_gzipped_falls_back_to_raw_bytes():
    """Two magic bytes are not proof. A false positive must not lose the stream."""
    fake = b"\x1f\x8b" + b"not actually deflate data" * 200
    with MaxFile.from_bytes(build_ole({"Scene": fake})) as mf:
        assert mf.read("Scene").startswith(b"\x1f\x8b")


def test_reader_over_a_compressed_stream_sees_inflated_content():
    inner = pad_chunks(leaf(0x0018, b"y" * 50))
    packed = gzip.compress(inner)
    blob = build_ole({"Scene": packed + b"\x00" * max(0, 4096 - len(packed))})
    with MaxFile.from_bytes(blob) as mf:
        idents = [c.ident for c in mf.open_reader("Scene").iter_top_level()]
    assert idents[0] == 0x0018


# --------------------------------------------------------------------------
# the lazy sector-chain reader — why this module exists
# --------------------------------------------------------------------------


def _multi_sector_payload(sectors: int = 40) -> bytes:
    """Distinctive per-sector content so misordered chains are visible."""
    return b"".join(bytes([i % 251]) * 512 for i in range(sectors))


def test_large_stream_uses_the_lazy_reader_not_olefiles_bytesio():
    blob = build_ole({"Scene": _multi_sector_payload()})
    with MaxFile.from_bytes(blob) as mf:
        raw = mf.open_stream("Scene")
        assert isinstance(raw, SectorChainReader)


def test_small_stream_is_delegated_to_olefile():
    """Below the mini-stream cutoff the data lives in the mini-FAT, which the
    lazy reader deliberately does not model."""
    with MaxFile.from_bytes(_simple()) as mf:
        assert mf.info("Scene").size == 4096  # exactly at the cutoff
        # a genuinely small stream can't be produced by our writer, so assert
        # the routing predicate directly
        assert mf._is_mini(10) is True
        assert mf._is_mini(1 << 20) is False


def test_lazy_reader_reproduces_the_stream_byte_for_byte():
    payload = _multi_sector_payload()
    with MaxFile.from_bytes(build_ole({"Scene": payload})) as mf:
        assert mf.read("Scene") == payload


def test_lazy_reader_seeks_and_reads_arbitrary_ranges():
    payload = _multi_sector_payload()
    with MaxFile.from_bytes(build_ole({"Scene": payload})) as mf:
        raw = mf.open_stream("Scene")
        for start, length in [(0, 10), (511, 3), (512, 512), (5000, 1000), (20000, 7)]:
            raw.seek(start)
            assert raw.read(length) == payload[start : start + length], (start, length)


def test_lazy_reader_read_past_the_end_returns_what_exists():
    payload = _multi_sector_payload(10)
    with MaxFile.from_bytes(build_ole({"Scene": payload})) as mf:
        raw = mf.open_stream("Scene")
        raw.seek(len(payload) - 5)
        assert raw.read(500) == payload[-5:]
        assert raw.read(1) == b""


def test_lazy_reader_read_all_with_negative_size():
    payload = _multi_sector_payload(10)
    with MaxFile.from_bytes(build_ole({"Scene": payload})) as mf:
        raw = mf.open_stream("Scene")
        raw.seek(1000)
        assert raw.read() == payload[1000:]


def test_lazy_reader_reports_position_and_seek_modes():
    payload = _multi_sector_payload(10)
    with MaxFile.from_bytes(build_ole({"Scene": payload})) as mf:
        raw = mf.open_stream("Scene")
        raw.seek(100)
        assert raw.tell() == 100
        raw.seek(50, io.SEEK_CUR)
        assert raw.tell() == 150
        raw.seek(-10, io.SEEK_END)
        assert raw.tell() == len(payload) - 10
        assert raw.seekable() and raw.readable()


def test_lazy_reader_never_reads_the_whole_stream_for_a_header_scan():
    """The whole point: walking chunk headers over a big stream must not pull
    the payload bytes through."""
    body = leaf(0x0018, b"A" * 60_000) + leaf(0x0019, b"B" * 60_000)
    with MaxFile.from_bytes(build_ole({"Scene": pad_chunks(body)})) as mf:
        raw = mf.open_stream("Scene")
        before = raw.bytes_read
        idents = [c.ident for c in ChunkReader(raw, mf.info("Scene").size).iter_top_level()]
        assert idents[:2] == [0x0018, 0x0019]
        # three headers, each landing in one 512-byte sector
        assert raw.bytes_read - before < 4096


def test_chain_walker_rejects_a_cycle_inside_the_declared_length():
    """A damaged FAT can point a sector back at one already used. Bounding the
    walk by the declared sector count is not enough — the cycle fits inside it
    and yields duplicate sectors, i.e. plausible garbage."""
    fat = array("I", [1, 0])  # 0 -> 1 -> 0 -> ...
    with pytest.raises(MaxFileError, match="twice|cycl"):
        SectorChainReader(io.BytesIO(b"\x00" * 2048), fat, 0, 4 * 512, 512, 0)


def test_chain_walker_rejects_a_sector_outside_the_fat():
    fat = array("I", [1, 9999])
    with pytest.raises(MaxFileError, match="outside the FAT"):
        SectorChainReader(io.BytesIO(b"\x00" * 2048), fat, 0, 4 * 512, 512, 0)


def test_chain_walker_rejects_a_chain_shorter_than_the_declared_size():
    fat = array("I", [1, 0xFFFFFFFE])  # ends after two sectors
    with pytest.raises(MaxFileError, match="truncated"):
        SectorChainReader(io.BytesIO(b"\x00" * 2048), fat, 0, 4 * 512, 512, 0)


def test_chain_walker_accepts_a_well_formed_chain():
    fat = array("I", [1, 2, 3, 0xFFFFFFFE])
    reader = SectorChainReader(io.BytesIO(bytes(range(256)) * 8), fat, 0, 4 * 512, 512, 0)
    assert reader.read(4) == bytes(range(4))
