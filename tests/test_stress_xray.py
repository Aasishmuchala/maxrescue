"""Stress: the X-ray against hostile and damaged input.

This tool's entire input population is files something is already wrong with.
So the bar is not "parses valid files" — it is:

* **never hang.** Every loop must be bounded by something the file cannot lie
  about.
* **never raise an unhandled exception type.** Callers can only reasonably
  catch `ChunkError` / `MaxFileError`; anything else escapes as a crash.
* **never silently misreport.** A partial read must be labelled partial.

Run just these with: pytest -k stress
"""

from __future__ import annotations

import io
import os
import random
import struct
import time

import pytest

from maxrescue.xray.chunks import (
    ChunkError,
    ChunkReader,
    iter_chunks,
    walk_chunks,
)
from maxrescue.xray.directories import parse_class_directory, parse_dll_directory
from maxrescue.xray.nodes import build_node_graph
from maxrescue.xray.ole import MaxFile, MaxFileError
from maxrescue.xray.scene_walk import walk_scene
from maxrescue.xray.signatures import scan_bytes, scan_stream
from tests.helpers import class_entry, container, dll_entry, leaf
from tests.helpers_ole import build_ole, pad_chunks

#: Anything else escaping to a caller is a crash, not a diagnosis.
ALLOWED = (ChunkError, MaxFileError)

SEED = 20260823


def _valid_scene() -> bytes:
    body = b"".join(leaf(1, b"x" * 400) for _ in range(20))
    return container(0x2012, body)


def _valid_file() -> bytes:
    return build_ole(
        {
            "Scene": _valid_scene(),
            "ClassDirectory3": pad_chunks(
                class_entry(dll_index=0, class_a=1, class_b=0, super_id=0x10, name="EP")
            ),
            "DllDirectory": pad_chunks(dll_entry("d", "EPoly.dlo")),
        }
    )


# ---------------------------------------------------------------------------
# fuzzing the chunk reader
# ---------------------------------------------------------------------------


def test_stress_random_bytes_never_crash_unexpectedly():
    rng = random.Random(SEED)
    for _ in range(400):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200)))
        try:
            list(iter_chunks(blob))
        except ALLOWED:
            pass
        except Exception as exc:  # pragma: no cover - this failing IS the finding
            pytest.fail(f"unexpected {type(exc).__name__}: {exc} on {blob!r}")


def test_stress_truncation_at_every_offset():
    """A file cut short at any byte must degrade, not explode."""
    scene = _valid_scene()
    for cut in range(len(scene)):
        try:
            list(iter_chunks(scene[:cut]))
        except ALLOWED:
            pass
        except Exception as exc:
            pytest.fail(f"unexpected {type(exc).__name__} truncating at {cut}: {exc}")


def test_stress_single_bit_flips():
    rng = random.Random(SEED + 1)
    scene = bytearray(_valid_scene())
    for _ in range(500):
        blob = bytearray(scene)
        index = rng.randrange(len(blob))
        blob[index] ^= 1 << rng.randrange(8)
        try:
            list(iter_chunks(bytes(blob)))
        except ALLOWED:
            pass
        except Exception as exc:
            pytest.fail(f"unexpected {type(exc).__name__} after flipping byte {index}: {exc}")


def test_stress_a_chunk_claiming_an_enormous_size_fails_fast():
    """A 6-byte header can claim 4 GB. Reading it must not try to allocate it."""
    blob = struct.pack("<HI", 1, 0xFFFFFFF) + b"tiny"
    started = time.time()
    with pytest.raises(ChunkError, match="overruns"):
        list(iter_chunks(blob))
    assert time.time() - started < 1.0


def test_stress_a_wide_chunk_claiming_a_petabyte_fails_fast():
    blob = struct.pack("<HIQ", 1, 0, 1 << 50) + b"tiny"
    with pytest.raises(ChunkError, match="overruns"):
        list(iter_chunks(blob))


def test_stress_every_two_byte_size_value_terminates():
    """Exhaustive over the small size space — no value may loop forever."""
    for size in range(0, 300):
        blob = struct.pack("<HI", 1, size) + b"\x00" * 64
        try:
            list(iter_chunks(blob))
        except ALLOWED:
            pass
        except Exception as exc:
            pytest.fail(f"size={size} raised {type(exc).__name__}: {exc}")


def test_stress_deeply_nested_containers_do_not_blow_the_stack():
    """`walk_chunks` recurses. A file can nest containers thousands deep, which
    is a perfectly ordinary way to turn a parser into a RecursionError."""
    blob = leaf(0xFF, b"x")
    for _ in range(2000):
        blob = container(0xA, blob)
    try:
        list(walk_chunks(blob, max_depth=None))
    except ALLOWED:
        pass
    except RecursionError:
        pytest.fail(
            "deep nesting raises RecursionError — a hostile file can crash the "
            "X-ray with ~1000 nested containers"
        )


def test_stress_walk_with_max_depth_is_bounded_on_deep_nesting():
    blob = leaf(0xFF, b"x")
    for _ in range(5000):
        blob = container(0xA, blob)
    seen = list(walk_chunks(blob, max_depth=10))
    assert len(seen) <= 11


# ---------------------------------------------------------------------------
# fuzzing the directories
# ---------------------------------------------------------------------------


def test_stress_directories_survive_random_input():
    rng = random.Random(SEED + 2)
    for _ in range(300):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 120)))
        for parser in (parse_dll_directory, parse_class_directory):
            try:
                parser(blob)
            except ALLOWED:
                pass
            except Exception as exc:
                pytest.fail(f"{parser.__name__} raised {type(exc).__name__}: {exc}")


def test_stress_a_class_header_of_every_truncated_length():
    """The 16-byte header may arrive at any length. None may raise struct.error."""
    for length in range(0, 20):
        blob = container(0x2040, leaf(0x2060, b"\x01" * length))
        entries = parse_class_directory(blob)
        assert len(entries) == 1
        assert entries[0].malformed == (length < 16)


def test_stress_directory_alignment_survives_a_run_of_malformed_entries():
    """Positional alignment is load-bearing; damage must not shift it."""
    good = class_entry(dll_index=0, class_a=7, class_b=0, super_id=0x10, name="Good")
    bad = container(0x2040, leaf(0x2060, b"\x00"))
    entries = parse_class_directory(bad * 5 + good)
    assert len(entries) == 6
    assert entries[5].class_id == (7, 0), "the good entry landed at the wrong index"


# ---------------------------------------------------------------------------
# fuzzing the container
# ---------------------------------------------------------------------------


def test_stress_random_bytes_are_rejected_as_not_a_max_file():
    rng = random.Random(SEED + 3)
    for _ in range(50):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 4096)))
        with pytest.raises(MaxFileError):
            MaxFile.from_bytes(blob)


def test_stress_a_valid_header_with_a_shredded_body():
    """The OLE magic is only eight bytes; anything can follow it."""
    rng = random.Random(SEED + 4)
    good = bytearray(_valid_file())
    for _ in range(60):
        blob = bytearray(good)
        for _ in range(40):
            blob[rng.randrange(512, len(blob))] = rng.getrandbits(8)
        try:
            with MaxFile.from_bytes(bytes(blob)) as mf:
                for info in mf.streams:
                    try:
                        mf.read(info.name)
                    except ALLOWED:
                        pass
        except ALLOWED:
            pass
        except Exception as exc:
            pytest.fail(f"unexpected {type(exc).__name__}: {exc}")


def test_stress_truncated_container_files():
    good = _valid_file()
    for fraction in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        blob = good[: int(len(good) * fraction)]
        try:
            with MaxFile.from_bytes(blob) as mf:
                list(mf.streams)
        except ALLOWED:
            pass
        except Exception as exc:
            pytest.fail(f"unexpected {type(exc).__name__} at {fraction:.0%}: {exc}")


# ---------------------------------------------------------------------------
# the walk and the node graph
# ---------------------------------------------------------------------------


def test_stress_scene_walk_never_raises_whatever_the_stream():
    rng = random.Random(SEED + 5)
    for _ in range(200):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 300)))
        inventory = walk_scene(ChunkReader(io.BytesIO(blob), len(blob)), None)
        assert isinstance(inventory.objects, tuple)
        if inventory.truncated:
            assert inventory.error


def test_stress_node_graph_never_raises_on_damaged_nodes():
    """Tier 3 must degrade per node. One bad node may not cost the whole graph."""
    rng = random.Random(SEED + 6)
    catalog_bytes = class_entry(
        dll_index=-1, class_a=1, class_b=0, super_id=0x01, name="Node"
    )
    catalog = parse_class_directory(catalog_bytes)
    from maxrescue.xray.directories import ClassCatalog

    cat = ClassCatalog(dlls=[], classes=catalog)

    for _ in range(200):
        payload = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 60)))
        blob = container(0x2012, container(0, payload))
        reader = ChunkReader(io.BytesIO(blob), len(blob))
        inventory = walk_scene(reader, cat)
        graph = build_node_graph(reader, cat, inventory)
        for node in graph.nodes:
            if not node.resolved:
                assert node.degradation, "an unresolved node must say why"


def test_stress_a_node_referencing_itself_does_not_loop():
    from maxrescue.xray.directories import ClassCatalog
    from tests.helpers import node_chunk, refs_map

    classes = parse_class_directory(
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=0x01, name="Node")
    )
    cat = ClassCatalog(dlls=[], classes=classes)
    blob = container(0x2012, node_chunk(0, name="Ouroboros", refs=refs_map({1: 0})))
    reader = ChunkReader(io.BytesIO(blob), len(blob))
    inventory = walk_scene(reader, cat)

    started = time.time()
    graph = build_node_graph(reader, cat, inventory)
    assert time.time() - started < 2.0
    assert graph.nodes


# ---------------------------------------------------------------------------
# the signature scanner
# ---------------------------------------------------------------------------


def test_stress_scanner_survives_random_input():
    rng = random.Random(SEED + 7)
    for _ in range(200):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 5000)))
        assert isinstance(scan_bytes(blob, "Scene"), list)


def test_stress_streaming_scan_terminates_for_every_window_size():
    """A window smaller than the longest pattern must not deadlock or loop."""
    data = b"." * 5000 + b"CRP_BScript" + b"." * 5000
    for window in (1, 2, 7, 64, 100, 512, 4096, 100_000):
        started = time.time()
        found = scan_stream(io.BytesIO(data), "Scene", len(data), window=window)
        assert time.time() - started < 5.0, f"window={window} took too long"
        assert [f.signature for f in found] == ["CRP_BScript"], f"window={window}"


def test_stress_a_payload_at_every_offset_is_found():
    """Windowing must not have a blind spot at any position."""
    payload = b"CRP_BScript"
    for offset in range(0, 300, 7):
        data = b"." * offset + payload + b"." * 200
        found = scan_stream(io.BytesIO(data), "Scene", len(data), window=128)
        assert [f.signature for f in found] == ["CRP_BScript"], f"missed at {offset}"


# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------


def test_stress_a_hundred_thousand_objects_walks_in_reasonable_time():
    """The real scene has tens of thousands of objects. An O(n^2) walk here
    would look like a hang on the box, exactly as it did in a sibling project."""
    body = b"".join(leaf(1, b"") for _ in range(100_000))
    blob = container(0x2012, body)
    reader = ChunkReader(io.BytesIO(blob), len(blob))

    started = time.time()
    inventory = walk_scene(reader, None)
    elapsed = time.time() - started

    assert len(inventory.objects) == 100_000
    assert elapsed < 20.0, f"walking 100k objects took {elapsed:.1f}s"


def test_stress_histogram_and_heaviest_scale():
    body = b"".join(leaf(1, b"x" * (i % 97)) for i in range(50_000))
    blob = container(0x2012, body)
    inventory = walk_scene(ChunkReader(io.BytesIO(blob), len(blob)), None)

    started = time.time()
    inventory.histogram()
    inventory.heaviest(20)
    assert time.time() - started < 5.0


def test_stress_a_large_stream_is_not_materialised_by_the_walk():
    payload = b"P" * 2_000_000
    body = b"".join(leaf(1, payload) for _ in range(20))
    blob = container(0x2012, body)

    class Counting(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.bytes_read = 0

        def read(self, n=-1):
            out = super().read(n)
            self.bytes_read += len(out)
            return out

    stream = Counting(blob)
    inventory = walk_scene(ChunkReader(stream, len(blob)), None)
    assert len(inventory.objects) == 20
    assert stream.bytes_read < 4096, (
        f"the walk pulled {stream.bytes_read:,} bytes through for 21 headers"
    )
