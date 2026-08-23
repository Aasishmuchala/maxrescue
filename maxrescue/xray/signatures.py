"""Detection of embedded script payloads in a `.max` file.

Scene malware in 3ds Max does not exploit the format — it rides ordinary scene
constructs that Max executes on load: persistent globals, persistent callbacks
(`callbacks.addScript … persistent:true`), script controllers, and scripted
custom-attribute definitions in the `ScriptedCustAttribDefs` stream. So the
X-ray looks for the *names* those families are known to use, plus the MAXScript
commands that can reach outside the scene.

Two severities, deliberately far apart:

* :attr:`Severity.MALWARE` — a named payload family. A specific claim.
* :attr:`Severity.SUSPICIOUS` — a command that can touch shell, filesystem,
  registry or network. Legitimate scripted plugins use these constantly, so
  this is a prompt to look, never a verdict.

This finds **known** things. A clean report means "no known signature", never
"this file is safe" — and the report says so. Autodesk's own Scene Security
Tools carry the same limitation.

Sources: Autodesk security advisories, the 3dground ALC/CRP write-ups, and the
Safe Scene Script Execution block list. See docs/research/max-format.md §10.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import BinaryIO, Iterable

__all__ = [
    "Finding",
    "Severity",
    "KNOWN_PAYLOADS",
    "UNSAFE_COMMANDS",
    "scan_bytes",
    "scan_stream",
    "scan_max_file",
]

#: How many distinct hits of one signature to report before summarising.
MAX_HITS_PER_SIGNATURE = 5
_CONTEXT = 40


class Severity(Enum):
    MALWARE = "malware"
    SUSPICIOUS = "suspicious"


#: Named payload families. A hit is a specific claim about a specific thing.
KNOWN_PAYLOADS: tuple[str, ...] = (
    "AutodeskLicSerStuckCleanBeta",
    "AutodeskLicSerStuckCleanAlpha",
    "AutodeskLicSerStuckAlpha",
    "ADSL_BScript",
    "CRP_BScript",
    "CRP_AScript",
    "physXCrtRbkInfoCleanBeta",
    "PhysXPluginStl",
    "PhysXPluginMfx",
    "Sound_GrayDeskUnderFourOldDriverGoPlane",
    "vrdematcleanbeta",
    "vrdematcleanalpha",
    "vrdematpropalpha",
    "vrdestermatconvert",
    "vrayimportinfo.mse",
    "mscprop.dll",
    "PropertyParametersLocal.mse",
    "Local_temp.ms",
    "upscript.mse",
    "3dsmj.com",
    "znzmo.com",
)

#: MAXScript that can reach outside the scene. Present in plenty of honest
#: plugins — surfaced, not accused.
UNSAFE_COMMANDS: tuple[str, ...] = (
    "HiddenDOSCommand",
    "DOSCommand",
    "ShellLaunch",
    "createOLEObject",
    "downloadPackage",
    "DownloadUrlToDisk",
    "LoadDllsFromDir",
    "encryptScript",
    "openEncryptedFile",
    "executeScriptFile",
    "setFileAttribute",
    "systemTools.setEnvVariable",
    "windows.postMessage",
    "windows.sendMessage",
    "macros.load",
    "registry.",
    "deleteFile",
    "removeDir",
    "renameFile",
    "copyFile",
    "createFile",
    "openFile",
    "fileIn",
    "fopen",
)

_SIGNATURES: tuple[tuple[str, Severity], ...] = tuple(
    [(s, Severity.MALWARE) for s in KNOWN_PAYLOADS]
    + [(s, Severity.SUSPICIOUS) for s in UNSAFE_COMMANDS]
)

#: Longest pattern in either encoding, used to size the streaming overlap.
_MAX_PATTERN_BYTES = max(len(s) for s, _ in _SIGNATURES) * 2


@dataclass(frozen=True)
class Finding:
    signature: str
    severity: Severity
    stream: str
    offset: int
    encoding: str
    context: str
    occurrences: int = 1


def _printable(raw: bytes) -> str:
    """A short, safe excerpt. Report text must never be a shell of raw bytes."""
    text = raw.decode("ascii", errors="replace")
    return re.sub(r"[^\x20-\x7e]+", ".", text)


def _hits(haystack: bytes, needle: bytes, limit: int | None = None) -> list[int]:
    out: list[int] = []
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found < 0:
            return out
        out.append(found)
        start = found + 1
        if limit is not None and len(out) >= limit:
            return out


#: A single located match, before capping and counting.
_Hit = tuple[str, Severity, str, int, str]  # signature, severity, encoding, offset, context


def _raw_hits(data: bytes, base: int) -> list[_Hit]:
    """Every match in one buffer, at absolute offsets.

    Case-insensitivity works in both encodings because `bytes.lower()` lowers
    ASCII bytes and leaves the interleaved NULs of UTF-16LE untouched.
    """
    if not data:
        return []

    lowered = data.lower()
    hits: list[_Hit] = []

    for signature, severity in _SIGNATURES:
        for encoding, pattern in (
            ("ascii", signature.encode("ascii", errors="ignore")),
            ("utf-16-le", signature.encode("utf-16-le")),
        ):
            if not pattern:
                continue
            for position in _hits(lowered, pattern.lower()):
                start = max(0, position - _CONTEXT)
                context = _printable(data[start : position + len(pattern) + _CONTEXT])
                hits.append(
                    (signature, severity, encoding, base + position, context)
                )
    return hits


def _summarise(hits: Iterable[_Hit], stream: str) -> list[Finding]:
    """Cap repeated hits of one signature while keeping the true count.

    A payload written 500 times is one finding with a count, not 500 findings —
    but the count is what tells you it was written 500 times.
    """
    grouped: dict[tuple[str, str], list[_Hit]] = {}
    for hit in hits:
        grouped.setdefault((hit[0], hit[2]), []).append(hit)

    findings: list[Finding] = []
    for group in grouped.values():
        group.sort(key=lambda h: h[3])
        total = len(group)
        for signature, severity, encoding, offset, context in group[
            :MAX_HITS_PER_SIGNATURE
        ]:
            findings.append(
                Finding(
                    signature=signature,
                    severity=severity,
                    stream=stream,
                    offset=offset,
                    encoding=encoding,
                    context=context,
                    occurrences=total,
                )
            )

    findings.sort(key=lambda f: (f.severity is not Severity.MALWARE, f.offset))
    return findings


def scan_bytes(data: bytes, stream: str) -> list[Finding]:
    """Scan a whole buffer. For the small streams, read in full."""
    return _summarise(_raw_hits(data, 0), stream)


def scan_stream(
    fp: BinaryIO,
    stream: str,
    size: int,
    window: int = 8 << 20,
) -> list[Finding]:
    """Scan a large stream in overlapping windows.

    Consecutive windows overlap by at least the longest pattern, so every match
    lands wholly inside at least one window — a signature straddling a boundary
    is the failure a scanner is least forgiven for.

    Deduplication is by **absolute offset**, not by window position. Filtering
    out hits that merely *start* inside the overlap would discard exactly the
    straddling match the overlap exists to catch, since that match was never
    fully visible in the earlier window.
    """
    window = max(window, _MAX_PATTERN_BYTES * 4)
    overlap = _MAX_PATTERN_BYTES

    seen: dict[tuple[str, str, int], _Hit] = {}
    position = 0

    while position < size:
        fp.seek(position)
        chunk = fp.read(min(window, size - position))
        if not chunk:
            break
        for hit in _raw_hits(chunk, position):
            seen.setdefault((hit[0], hit[2], hit[3]), hit)
        if position + len(chunk) >= size:
            break
        position += len(chunk) - overlap

    return _summarise(seen.values(), stream)


def scan_max_file(max_file, streams: Iterable[str] | None = None) -> list[Finding]:
    """Scan every stream of an open :class:`~maxrescue.xray.ole.MaxFile`."""
    names = (
        list(streams)
        if streams is not None
        else [info.name for info in max_file.streams]
    )
    findings: list[Finding] = []
    for name in names:
        info = max_file.info(name)
        if info.compressed or info.size <= (8 << 20):
            findings.extend(scan_bytes(max_file.read(name), name))
        else:
            findings.extend(
                scan_stream(max_file.open_stream(name), name, info.size)
            )
    return findings
