"""The OLE2 / Compound File layer of a `.max` scene.

`olefile` parses the container — header, FAT, directory — and it does that well.
What it does not do is read large streams lazily: `openstream()` returns an
`io.BytesIO` holding the entire stream, and olefile's own source says so::

    # FIXME: should store the list of sects obtained by following the fat
    # chain, and load new sectors on demand instead of loading it all in one go.

MaxRescue exists precisely for files whose `Scene` stream is gigabytes, so bulk
reads go through :class:`SectorChainReader`, which follows the FAT chain and
pulls only the sectors actually touched. olefile still does the parsing; we only
bypass its bulk read path.

Streams below the mini-stream cutoff (4096 bytes by default) live inside the
root storage's mini-stream rather than in regular sectors. Those are tiny by
definition, so they are delegated to olefile, whose mini-FAT handling is
long-established.
"""

from __future__ import annotations

import io
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path

import olefile

from maxrescue.xray.chunks import ChunkReader

__all__ = ["MaxFile", "MaxFileError", "SectorChainReader", "StreamInfo"]

_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_GZIP_MAGIC = b"\x1f\x8b"


class MaxFileError(Exception):
    """The file is not a readable `.max` container."""


@dataclass(frozen=True)
class StreamInfo:
    name: str
    size: int
    compressed: bool


class SectorChainReader(io.RawIOBase):
    """Seekable, lazy reader over a CFBF FAT sector chain.

    Memory is one 4-byte entry per sector of *this stream* (a 4 GB stream at
    512-byte sectors costs ~32 MB), versus the full stream under olefile.
    """

    def __init__(
        self,
        fp,
        fat,
        start_sector: int,
        size: int,
        sector_size: int,
        data_offset: int,
    ):
        self._fp = fp
        self._size = size
        self._sector_size = sector_size
        self._data_offset = data_offset
        self._pos = 0
        #: Bytes actually pulled from the container — the laziness is testable.
        self.bytes_read = 0
        self._sectors = self._follow(fat, start_sector, size, sector_size)

    @staticmethod
    def _follow(fat, start: int, size: int, sector_size: int) -> array:
        """Collect the sector chain, refusing to trust a damaged FAT.

        Bounding the walk by the declared sector count is not enough on its own:
        a cycle *inside* that range would quietly yield the same sector twice and
        produce plausible-looking garbage. A visited bitmap catches that exactly,
        and costs one bit per sector in the file — about 1 MB for a 4 GB stream,
        against several hundred for a set of ints.
        """
        needed = (size + sector_size - 1) // sector_size
        sectors = array("I")
        seen = bytearray((len(fat) + 7) // 8)
        sect = start

        while sect not in (_ENDOFCHAIN, _FREESECT) and len(sectors) < needed:
            if sect < 0 or sect >= len(fat):
                raise MaxFileError(
                    f"sector {sect} is outside the FAT ({len(fat)} entries) — "
                    "the allocation table is damaged"
                )
            byte, bit = divmod(sect, 8)
            if seen[byte] & (1 << bit):
                raise MaxFileError(
                    f"sector {sect} appears twice in one chain — cyclic FAT, "
                    "the file is damaged"
                )
            seen[byte] |= 1 << bit
            sectors.append(sect)
            sect = fat[sect]

        if len(sectors) < needed:
            raise MaxFileError(
                f"sector chain ended after {len(sectors)} sectors but the stream "
                f"declares {needed} — the file is truncated"
            )
        return sectors

    # -- io plumbing -------------------------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        self._pos = max(0, target)
        return self._pos

    def readinto(self, buffer) -> int:  # type: ignore[override]
        want = len(buffer)
        remaining = min(want, max(0, self._size - self._pos))
        written = 0
        while written < remaining:
            index, offset = divmod(self._pos, self._sector_size)
            take = min(self._sector_size - offset, remaining - written)
            physical = self._data_offset + self._sectors[index] * self._sector_size
            self._fp.seek(physical + offset)
            piece = self._fp.read(take)
            if not piece:
                break
            buffer[written : written + len(piece)] = piece
            written += len(piece)
            self._pos += len(piece)
            self.bytes_read += len(piece)
        return written

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            size = max(0, self._size - self._pos)
        buf = bytearray(size)
        got = self.readinto(buf)
        return bytes(buf[:got])


class MaxFile:
    """A `.max` file's streams, opened read-only."""

    def __init__(self, ole: olefile.OleFileIO, fp, source: str | None = None):
        self._ole = ole
        self._fp = fp
        self.source = source
        self._closed = False
        self._index: dict[str, StreamInfo] = {}
        for path in ole.listdir(streams=True, storages=False):
            name = "/".join(path)
            size = ole.get_size(path)
            self._index[name.lower()] = StreamInfo(
                name=name, size=size, compressed=self._peek_gzip(path, size)
            )

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> MaxFile:
        location = str(path)
        try:
            fp = open(location, "rb")
        except OSError as exc:
            raise MaxFileError(f"cannot open {location}: {exc}") from exc
        try:
            return cls._wrap(fp, location)
        except Exception:
            fp.close()
            raise

    @classmethod
    def from_bytes(cls, data: bytes) -> MaxFile:
        return cls._wrap(io.BytesIO(data), "<bytes>")

    @classmethod
    def _wrap(cls, fp, source: str) -> MaxFile:
        try:
            fp.seek(0)
            if not olefile.isOleFile(fp):
                raise MaxFileError(
                    f"{source} is not an OLE compound file — a .max always is, "
                    "so this is either a different format or badly damaged"
                )
            fp.seek(0)
            ole = olefile.OleFileIO(fp)
        except MaxFileError:
            raise
        except Exception as exc:
            raise MaxFileError(f"{source} is not a readable .max: {exc}") from exc
        return cls(ole, fp, source)

    # -- inventory ---------------------------------------------------------

    @property
    def streams(self) -> tuple[StreamInfo, ...]:
        return tuple(self._index.values())

    def has(self, name: str) -> bool:
        return name.lower() in self._index

    def info(self, name: str) -> StreamInfo:
        try:
            return self._index[name.lower()]
        except KeyError:
            raise MaxFileError(f"no stream named {name!r} in {self.source}") from None

    # -- reading -----------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise MaxFileError("this MaxFile is closed")

    def _is_mini(self, size: int) -> bool:
        """Mini streams live in the root mini-stream, not in regular sectors."""
        return size < self._ole.minisectorcutoff

    def _peek_gzip(self, path, size: int) -> bool:
        """Read the first two bytes — and only the first two bytes.

        `olefile.openstream()` would load the WHOLE stream to answer this. On a
        6 GB `Scene` stream that allocates ~13 GB before a single chunk is
        parsed, which would kill the X-ray on exactly the files it exists for —
        and this runs in `__init__`, before any lazy reader is reachable.
        """
        if size < 2:
            return False
        try:
            if self._is_mini(size):
                # Under the cutoff the whole stream is a few KB; olefile's
                # mini-FAT handling is the right tool.
                with self._ole.openstream(path) as stream:
                    return stream.read(2) == _GZIP_MAGIC
            entry = self._ole.direntries[self._ole._find(path)]
            reader = SectorChainReader(
                fp=self._fp,
                fat=self._ole.fat,
                start_sector=entry.isectStart,
                size=size,
                sector_size=self._ole.sectorsize,
                data_offset=self._ole.sectorsize,
            )
            return reader.read(2) == _GZIP_MAGIC
        except Exception:
            return False

    def open_stream(self, name: str):
        """A seekable binary reader over the raw (still compressed) stream."""
        self._check_open()
        info = self.info(name)
        path = info.name.split("/")
        if self._is_mini(info.size):
            return self._ole.openstream(path)
        entry = self._ole.direntries[self._ole._find(path)]
        return SectorChainReader(
            fp=self._fp,
            fat=self._ole.fat,
            start_sector=entry.isectStart,
            size=info.size,
            sector_size=self._ole.sectorsize,
            data_offset=self._ole.sectorsize,
        )

    def read(self, name: str) -> bytes:
        """The whole stream, inflated if it is gzip-framed.

        Only for streams small enough to hold — the directories, not `Scene`.
        """
        self._check_open()
        info = self.info(name)
        raw = self.open_stream(info.name).read()
        return self._maybe_inflate(raw) if info.compressed else raw

    @staticmethod
    def _maybe_inflate(raw: bytes) -> bytes:
        """Inflate a gzip-framed stream, falling back to the raw bytes.

        Two magic bytes are a hint, not proof; a false positive must not cost
        us the stream.
        """
        try:
            return zlib.decompress(raw, zlib.MAX_WBITS | 32)
        except zlib.error:
            try:
                return zlib.decompressobj(zlib.MAX_WBITS | 32).decompress(raw)
            except zlib.error:
                return raw

    def open_reader(self, name: str) -> ChunkReader:
        """A :class:`ChunkReader` over the stream's chunk tree.

        Uncompressed streams are read lazily. A compressed stream has to be
        inflated whole — there is no seeking inside a deflate stream.
        """
        self._check_open()
        info = self.info(name)
        if info.compressed:
            data = self.read(info.name)
            return ChunkReader(io.BytesIO(data), len(data))
        return ChunkReader(self.open_stream(info.name), info.size)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ole.close()
        finally:
            try:
                self._fp.close()
            except Exception:
                pass

    def __enter__(self) -> MaxFile:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
