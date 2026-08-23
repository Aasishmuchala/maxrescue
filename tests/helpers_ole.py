"""A minimal OLE2 / CFBF v3 writer, for tests only.

A `.max` file is a Microsoft Compound File, so this lets the X-ray be exercised
end-to-end — through the real `olefile` parser — without a real `.max` on disk.

Deliberately limited: every stream is written into **regular** 512-byte sectors,
never the mini-FAT. Callers must therefore pass streams of at least 4096 bytes
(the mini-stream cutoff), which `build_ole` asserts. Real files do use mini
streams for their small directories; MaxRescue routes those to `olefile`, whose
mini-FAT handling is long-established.

NOT production code. MaxRescue never writes compound files.
"""

from __future__ import annotations

import struct

SECTOR = 512
DIR_ENTRY = 128
MINI_CUTOFF = 4096

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF

STGTY_STREAM = 2
STGTY_ROOT = 5

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _dir_entry(
    name: str,
    entry_type: int,
    start_sector: int,
    size: int,
    left: int = NOSTREAM,
    right: int = NOSTREAM,
    child: int = NOSTREAM,
) -> bytes:
    raw = name.encode("utf-16-le")
    assert len(raw) <= 62, f"directory name too long: {name!r}"
    out = bytearray(DIR_ENTRY)
    out[0 : len(raw)] = raw
    struct.pack_into("<H", out, 64, len(raw) + 2)  # includes the null terminator
    out[66] = entry_type
    out[67] = 1  # colour: black
    struct.pack_into("<III", out, 68, left, right, child)
    # 80..96 CLSID, 96..100 state bits, 100..116 timestamps — all zero
    struct.pack_into("<I", out, 116, start_sector)
    struct.pack_into("<Q", out, 120, size)
    return bytes(out)


def build_ole(streams: dict[str, bytes]) -> bytes:
    """Assemble a valid CFBF v3 container holding `streams`."""
    for name, data in streams.items():
        assert len(data) >= MINI_CUTOFF, (
            f"stream {name!r} is {len(data)} bytes; this writer only emits regular "
            f"sectors, so streams must be >= {MINI_CUTOFF} bytes"
        )

    # --- sector layout: stream data, then directory, then the FAT itself
    placement: dict[str, tuple[int, int]] = {}  # name -> (start sector, n sectors)
    cursor = 0
    for name, data in streams.items():
        count = (len(data) + SECTOR - 1) // SECTOR
        placement[name] = (cursor, count)
        cursor += count

    n_dir_entries = 1 + len(streams)
    n_dir_sectors = (n_dir_entries * DIR_ENTRY + SECTOR - 1) // SECTOR
    dir_start = cursor
    cursor += n_dir_sectors

    entries_per_fat = SECTOR // 4  # 128
    n_fat = 1
    while (cursor + n_fat + entries_per_fat - 1) // entries_per_fat > n_fat:
        n_fat += 1
    fat_start = cursor
    total_sectors = cursor + n_fat

    # --- FAT
    fat = [FREESECT] * (n_fat * entries_per_fat)
    for start, count in placement.values():
        for i in range(count):
            fat[start + i] = start + i + 1
        fat[start + count - 1] = ENDOFCHAIN
    for i in range(n_dir_sectors):
        fat[dir_start + i] = dir_start + i + 1
    fat[dir_start + n_dir_sectors - 1] = ENDOFCHAIN
    for i in range(n_fat):
        fat[fat_start + i] = FATSECT

    # --- directory: root, then a right-leaning chain of streams
    names = list(streams)
    directory = bytearray()
    directory += _dir_entry(
        "Root Entry",
        STGTY_ROOT,
        ENDOFCHAIN,
        0,
        child=1 if names else NOSTREAM,
    )
    for i, name in enumerate(names):
        sid = i + 1
        start, _ = placement[name]
        directory += _dir_entry(
            name,
            STGTY_STREAM,
            start,
            len(streams[name]),
            right=sid + 1 if sid < len(names) else NOSTREAM,
        )
    directory += bytes(DIR_ENTRY) * (n_dir_sectors * 4 - n_dir_entries)

    # --- header
    header = bytearray(SECTOR)
    header[0:8] = SIGNATURE
    struct.pack_into("<HHH", header, 24, 0x003E, 0x0003, 0xFFFE)
    struct.pack_into("<HH", header, 30, 9, 6)  # 512-byte sectors, 64-byte mini
    struct.pack_into("<I", header, 40, 0)  # dir sector count: must be 0 in v3
    struct.pack_into("<I", header, 44, n_fat)
    struct.pack_into("<I", header, 48, dir_start)
    struct.pack_into("<I", header, 56, MINI_CUTOFF)
    struct.pack_into("<I", header, 60, ENDOFCHAIN)  # first mini-FAT sector
    struct.pack_into("<I", header, 64, 0)  # mini-FAT sector count
    struct.pack_into("<I", header, 68, ENDOFCHAIN)  # first DIFAT sector
    struct.pack_into("<I", header, 72, 0)  # DIFAT sector count
    # Unused DIFAT slots must be FREESECT — a reader walks this array and will
    # try to load anything else as a FAT sector.
    difat = [FREESECT] * 109
    for i in range(n_fat):
        difat[i] = fat_start + i
    struct.pack_into("<109I", header, 76, *difat)

    # --- emit
    body = bytearray(SECTOR * total_sectors)
    for name, data in streams.items():
        start, _ = placement[name]
        body[start * SECTOR : start * SECTOR + len(data)] = data
    body[dir_start * SECTOR : dir_start * SECTOR + len(directory)] = directory
    packed_fat = struct.pack(f"<{len(fat)}I", *fat)
    body[fat_start * SECTOR : fat_start * SECTOR + len(packed_fat)] = packed_fat

    return bytes(header) + bytes(body)


def pad_chunks(payload: bytes, minimum: int = MINI_CUTOFF) -> bytes:
    """Pad a chunk stream to `minimum` bytes with an ignorable filler chunk.

    Uses a real chunk header so the padding parses as a chunk the X-ray simply
    does not model, exactly like the unknown chunks in a real file.
    """
    if len(payload) >= minimum:
        return payload
    needed = minimum - len(payload)
    if needed < 6:
        needed = 6
    return payload + struct.pack("<HI", 0x7FFF, needed) + b"\x00" * (needed - 6)
