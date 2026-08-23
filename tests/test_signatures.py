"""Embedded-script detection.

Two very different claims, deliberately separated:

* **malware** — a named payload family (ALC / CRP / ADSL / PhysXPluginMfx /
  MSCPROP). A hit here is a statement about a specific known thing.
* **suspicious** — a MAXScript command that can touch the filesystem, registry,
  network or shell. Legitimate scripted plugins use these constantly, so a hit
  is a prompt to look, never a verdict.

Conflating them would make the scanner useless: every scene with a real scripted
plugin would read as infected.
"""

from __future__ import annotations

import io

from maxrescue.xray.signatures import (
    Severity,
    scan_bytes,
    scan_stream,
)


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


# --------------------------------------------------------------------------
# finding known payloads
# --------------------------------------------------------------------------


def test_finds_a_known_payload_in_ascii():
    (finding,) = scan_bytes(b"junk CRP_BScript junk", "Scene")
    assert finding.signature == "CRP_BScript"
    assert finding.severity is Severity.MALWARE
    assert finding.stream == "Scene"
    assert finding.encoding == "ascii"


def test_finds_a_known_payload_in_utf16():
    """Max stores scene strings as UTF-16LE, so an ASCII-only scan misses the
    common case entirely."""
    data = b"\x00\x01" + _utf16("AutodeskLicSerStuckCleanBeta") + b"\x02"
    (finding,) = scan_bytes(data, "ScriptedCustAttribDefs")
    assert finding.signature == "AutodeskLicSerStuckCleanBeta"
    assert finding.encoding == "utf-16-le"


def test_matching_is_case_insensitive():
    """Variants rename with different casing; the family is the same."""
    (finding,) = scan_bytes(b"crp_bscript", "Scene")
    assert finding.signature == "CRP_BScript"


def test_offset_is_reported_for_diagnosis():
    data = b"." * 100 + b"ADSL_BScript"
    (finding,) = scan_bytes(data, "Scene")
    assert finding.offset == 100


def test_context_excerpt_is_bounded_and_printable():
    data = b"\x00\xff" * 40 + b"PhysXPluginStl" + b"\x00\xff" * 40
    (finding,) = scan_bytes(data, "Scene")
    assert "PhysXPluginStl" in finding.context
    assert len(finding.context) <= 120


def test_clean_data_yields_nothing():
    assert scan_bytes(b"a perfectly ordinary scene with a Bitmap and a VRayMtl",
                      "Scene") == []


def test_empty_data_yields_nothing():
    assert scan_bytes(b"", "Scene") == []


# --------------------------------------------------------------------------
# unsafe commands are a prompt, not a verdict
# --------------------------------------------------------------------------


def test_unsafe_command_is_suspicious_not_malware():
    (finding,) = scan_bytes(b"local x = DOSCommand cmd", "ScriptedCustAttribDefs")
    assert finding.signature == "DOSCommand"
    assert finding.severity is Severity.SUSPICIOUS


def test_a_scene_using_ordinary_script_commands_is_not_called_infected():
    data = b"fileIn something; deleteFile old; registry.something"
    findings = scan_bytes(data, "Scene")
    assert findings, "these are worth surfacing"
    assert all(f.severity is Severity.SUSPICIOUS for f in findings)
    assert not any(f.severity is Severity.MALWARE for f in findings)


# --------------------------------------------------------------------------
# repetition must not drown the report
# --------------------------------------------------------------------------


def test_repeated_hits_are_capped_but_counted():
    data = b"CRP_BScript " * 500
    findings = scan_bytes(data, "Scene")
    assert len(findings) <= 5
    assert findings[0].occurrences == 500


def test_occurrences_is_one_for_a_single_hit():
    (finding,) = scan_bytes(b"CRP_BScript", "Scene")
    assert finding.occurrences == 1


def test_distinct_signatures_are_reported_separately():
    data = b"CRP_BScript and ADSL_BScript"
    names = {f.signature for f in scan_bytes(data, "Scene")}
    assert names == {"CRP_BScript", "ADSL_BScript"}


# --------------------------------------------------------------------------
# streaming — the Scene stream does not fit in memory
# --------------------------------------------------------------------------


def test_streaming_scan_finds_the_same_things_as_a_buffer_scan():
    data = b"." * 5000 + b"CRP_BScript" + b"." * 5000
    streamed = scan_stream(io.BytesIO(data), "Scene", len(data), window=1024)
    assert [f.signature for f in streamed] == ["CRP_BScript"]
    assert streamed[0].offset == 5000


def test_a_match_straddling_a_window_boundary_is_still_found():
    """The bug this test exists for: naive windowing silently loses any match
    that spans two windows, which is exactly where a scanner is trusted most."""
    window = 1024
    prefix = b"." * (window - 5)
    data = prefix + b"CRP_BScript" + b"." * 100
    found = scan_stream(io.BytesIO(data), "Scene", len(data), window=window)
    assert [f.signature for f in found] == ["CRP_BScript"]
    assert found[0].offset == len(prefix)


def test_utf16_match_straddling_a_boundary_is_found():
    window = 1024
    prefix = b"." * (window - 8)
    data = prefix + _utf16("PhysXPluginStl") + b"." * 100
    found = scan_stream(io.BytesIO(data), "Scene", len(data), window=window)
    assert [f.signature for f in found] == ["PhysXPluginStl"]


def test_streaming_does_not_report_the_same_hit_twice_from_the_overlap():
    window = 1024
    data = b"." * (window - 3) + b"CRP_BScript" + b"." * 2000
    found = scan_stream(io.BytesIO(data), "Scene", len(data), window=window)
    assert len(found) == 1
    assert found[0].occurrences == 1


def test_streaming_respects_the_declared_size():
    data = b"." * 100 + b"CRP_BScript"
    found = scan_stream(io.BytesIO(data), "Scene", 100, window=64)
    assert found == []


# --------------------------------------------------------------------------
# which patterns are looked for where
# --------------------------------------------------------------------------


def test_the_scene_stream_is_scanned_for_named_payloads_only(tmp_path):
    """`Scene` can be gigabytes. An unsafe-command token there would be noise
    even if it were free — a four-letter sequence somewhere in six billion bytes
    of geometry says nothing. Named payload families still get found."""
    from maxrescue.xray.ole import MaxFile
    from maxrescue.xray.signatures import scan_max_file
    from tests.helpers_ole import build_ole, pad_chunks

    payload = b"harmless " + b"fopen deleteFile registry.stuff " * 40 + b"CRP_BScript"
    blob = build_ole({"Scene": pad_chunks(payload)})
    with MaxFile.from_bytes(blob) as mf:
        findings = scan_max_file(mf, ["Scene"])

    names = {f.signature for f in findings}
    assert "CRP_BScript" in names, "a named payload must still be found"
    assert "fopen" not in names
    assert "registry." not in names


def test_script_definition_streams_are_scanned_for_everything(tmp_path):
    """These are small, and a token like `fopen` is only interpretable inside a
    script body — which is exactly what they hold."""
    from maxrescue.xray.ole import MaxFile
    from maxrescue.xray.signatures import scan_max_file
    from tests.helpers_ole import build_ole, pad_chunks

    blob = build_ole(
        {"ScriptedCustAttribDefs": pad_chunks(b"local f = fopen something")}
    )
    with MaxFile.from_bytes(blob) as mf:
        findings = scan_max_file(mf, ["ScriptedCustAttribDefs"])
    assert "fopen" in {f.signature for f in findings}


def test_narrowing_the_pattern_set_is_measurably_cheaper():
    """The reason the split is worth having at all."""
    import time

    data = b"ordinary scene data with names and paths " * 60_000

    started = time.time()
    scan_bytes(data, "Scene")
    everything = time.time() - started

    started = time.time()
    scan_bytes(data, "Scene", (Severity.MALWARE,))
    malware_only = time.time() - started

    assert malware_only < everything
