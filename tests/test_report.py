"""The X-ray report and CLI, end to end through a synthetic `.max`.

The verdict logic is the interesting part: it has to name the *one* thing most
likely to explain a file that will not open, without overclaiming. Everything it
says must be traceable to something measured.
"""

from __future__ import annotations

import json

import pytest

from maxrescue.app.cli import EXIT_MALWARE, EXIT_OK, main
from maxrescue.xray.report import Verdict, xray
from tests.helpers import class_entry, container, dll_entry, leaf
from tests.helpers_ole import build_ole, pad_chunks

GEOMOBJECT = 0x10
IDX_POLY_FOR_PLAN = 1
EPOLY = (0x1BF8338D, 0x192F6098)


def _classes(*extra: bytes) -> bytes:
    return (
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=0x01, name="Node")
        + class_entry(dll_index=0, class_a=EPOLY[0], class_b=EPOLY[1],
                      super_id=GEOMOBJECT, name="Editable Poly")
        + b"".join(extra)
    )


def _write(tmp_path, *, scene: bytes, classes: bytes | None = None,
           dlls: bytes | None = None, extra: dict[str, bytes] | None = None):
    streams = {
        "Scene": pad_chunks(scene),
        "ClassDirectory3": pad_chunks(classes if classes is not None else _classes()),
        "DllDirectory": pad_chunks(
            dlls if dlls is not None else dll_entry("Editable Poly", "EPoly.dlo")
        ),
    }
    for name, payload in (extra or {}).items():
        streams[name] = pad_chunks(payload)
    path = tmp_path / "scene.max"
    path.write_bytes(build_ole(streams))
    return path


def _objects(*sizes: int) -> bytes:
    """A wrapped scene whose objects are all Editable Poly (class index 1).

    A real Scene stream has exactly ONE top-level chunk — the version container
    — with every object inside it. Sizes here are chosen so the stream clears
    the compound-file writer's 4096-byte floor without needing filler, since a
    filler chunk would otherwise be counted as an enormous object.
    """
    body = b"".join(leaf(1, b"x" * n) for n in sizes)
    assert len(body) + 6 >= 4096, "pick sizes that clear the 4096-byte floor"
    return container(0x2012, body)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_reports_streams_classes_and_objects(tmp_path):
    report = xray(str(_write(tmp_path, scene=_objects(1000, 2000, 3000))))
    assert {s.name for s in report.streams} >= {"Scene", "ClassDirectory3", "DllDirectory"}
    assert len(report.inventory.objects) == 3
    assert report.required_dlls == ["EPoly.dlo"]


def test_a_plain_scene_is_reported_healthy(tmp_path):
    report = xray(str(_write(tmp_path, scene=_objects(1400, 1400, 1400))))
    assert report.verdict == Verdict.HEALTHY
    assert report.observations


def test_file_bytes_is_the_real_file_size(tmp_path):
    path = _write(tmp_path, scene=_objects(4200))
    assert xray(str(path)).file_bytes == path.stat().st_size


# --------------------------------------------------------------------------
# verdicts — each must be traceable to something measured
# --------------------------------------------------------------------------


def test_one_dominant_object_is_called_out_as_the_likely_cause(tmp_path):
    report = xray(str(_write(tmp_path, scene=_objects(500, 500, 50_000))))
    assert report.verdict == Verdict.MONSTER_OBJECT
    assert any("of the scene" in o for o in report.observations)


def test_a_known_payload_outranks_every_other_verdict(tmp_path):
    """Malware must win the headline even when the file also looks bloated."""
    path = _write(
        tmp_path,
        scene=_objects(500, 500, 50_000),
        extra={"ScriptedCustAttribDefs": b"junk CRP_BScript junk"},
    )
    report = xray(str(path))
    assert report.verdict == Verdict.MALWARE
    assert report.malware
    assert "CRP_BScript" in report.observations[0]


def test_a_damaged_scene_is_reported_damaged_not_healthy(tmp_path):
    import struct

    # one readable object, then a chunk whose size is smaller than its header
    broken = container(0x2012, leaf(1, b"x" * 4200) + struct.pack("<HI", 7, 2))
    report = xray(str(_write(tmp_path, scene=broken)))
    assert report.verdict == Verdict.DAMAGED
    assert report.inventory.truncated is True
    assert any("floor" in o for o in report.observations)


def test_scripted_classes_are_surfaced_even_with_no_signature_hit(tmp_path):
    classes = _classes(
        class_entry(dll_index=-2, class_a=0xAA, class_b=0xBB,
                    super_id=GEOMOBJECT, name="SomeScriptedThing")
    )
    report = xray(str(_write(tmp_path, scene=_objects(4200), classes=classes)))
    assert any("scripted" in o.lower() for o in report.observations)


def test_unsafe_tokens_alone_do_not_make_a_file_malware(tmp_path):
    path = _write(
        tmp_path,
        scene=_objects(4200),
        extra={"ScriptedCustAttribDefs": b"local f = fileIn something"},
    )
    report = xray(str(path))
    assert report.verdict != Verdict.MALWARE
    assert report.suspicious


# --------------------------------------------------------------------------
# honesty about what is measured
# --------------------------------------------------------------------------


def test_the_report_never_claims_an_in_memory_figure(tmp_path):
    """The disk-to-RAM multiplier is uncalibrated; inventing one would produce
    the most quotable and least true number on the page."""
    report = xray(str(_write(tmp_path, scene=_objects(4200))))
    blob = json.dumps(report.to_dict()).lower()
    assert "on-disk" in blob
    for forbidden in ("estimated_ram", "ram_bytes", "memory_estimate"):
        assert forbidden not in blob


def test_unaccounted_bytes_are_reported_rather_than_hidden(tmp_path):
    report = xray(str(_write(tmp_path, scene=_objects(4200))))
    assert "unaccounted_bytes" in report.to_dict()["scene"]


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------


def test_json_round_trips(tmp_path):
    report = xray(str(_write(tmp_path, scene=_objects(2000, 2500))))
    parsed = json.loads(report.to_json())
    assert parsed["scene"]["objects"] == 2
    assert parsed["verdict"] == report.verdict


def test_text_report_mentions_the_verdict_and_the_heaviest_class(tmp_path):
    text = xray(str(_write(tmp_path, scene=_objects(1000, 5000)))).to_text()
    assert "MaxRescue X-ray" in text
    assert "Editable Poly" in text
    assert "EPoly.dlo" in text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_prints_a_report_and_exits_zero(tmp_path, capsys):
    path = _write(tmp_path, scene=_objects(2000, 2500))
    assert main(["xray", str(path)]) == EXIT_OK
    assert "MaxRescue X-ray" in capsys.readouterr().out


def test_cli_writes_json_to_a_file(tmp_path):
    path = _write(tmp_path, scene=_objects(4200))
    out = tmp_path / "report.json"
    assert main(["xray", str(path), "--json", str(out)]) == EXIT_OK
    assert json.loads(out.read_text())["scene"]["objects"] == 1


def test_cli_exits_nonzero_on_a_known_payload(tmp_path, capsys):
    path = _write(
        tmp_path,
        scene=_objects(4200),
        extra={"ScriptedCustAttribDefs": b"CRP_BScript"},
    )
    assert main(["xray", str(path)]) == EXIT_MALWARE


def test_cli_reports_a_non_max_file_without_a_traceback(tmp_path, capsys):
    bad = tmp_path / "notamax.max"
    bad.write_bytes(b"definitely not a compound file")
    assert main(["xray", str(bad)]) == 2
    assert "not an OLE" in capsys.readouterr().err


def test_cli_requires_a_subcommand(capsys):
    with pytest.raises(SystemExit):
        main([])


def test_evenly_sized_objects_are_never_called_a_monster(tmp_path):
    """In a four-object scene every object is 25% of it and nothing is wrong.
    Dominance has to be measured against the typical object, not the total."""
    for sizes in [(1400, 1400, 1400), (1100, 1100, 1100, 1100), (2100, 2100)]:
        report = xray(str(_write(tmp_path, scene=_objects(*sizes))))
        assert report.verdict != Verdict.MONSTER_OBJECT, sizes


def test_a_single_object_scene_is_not_a_monster_finding(tmp_path):
    """One object is 100% of the scene by definition — that is arithmetic, not
    a diagnosis."""
    report = xray(str(_write(tmp_path, scene=_objects(4200))))
    assert report.verdict != Verdict.MONSTER_OBJECT


def test_the_monster_note_quantifies_dominance_against_the_median(tmp_path):
    report = xray(str(_write(tmp_path, scene=_objects(500, 500, 50_000))))
    assert report.verdict == Verdict.MONSTER_OBJECT
    assert any("median object" in o for o in report.observations)


# --------------------------------------------------------------------------
# plan subcommand
# --------------------------------------------------------------------------


def test_cli_plan_prints_batches_and_announces_the_unmeasured_prior(tmp_path, capsys):
    from tests.helpers import node_chunk, refs_map

    scene = container(
        0x2012,
        b"".join(
            node_chunk(0, name=f"Obj_{i}", refs=refs_map({1: 40 + i}))
            for i in range(40)
        )
        + b"".join(leaf(IDX_POLY_FOR_PLAN, b"g" * 5000) for _ in range(40)),
    )
    classes = (
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=0x01, name="Node")
        + class_entry(dll_index=0, class_a=EPOLY[0], class_b=EPOLY[1],
                      super_id=GEOMOBJECT, name="Editable Poly")
    )
    path = _write(tmp_path, scene=scene, classes=classes)
    assert main(["plan", str(path), "--ceiling-gb", "0.001"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "batch" in out.lower()
    assert "not measured" in out.lower()


# --------------------------------------------------------------------------
# rescue / verify subcommands
# --------------------------------------------------------------------------


def test_cli_rescue_off_windows_explains_instead_of_pretending(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("os.name", "posix")
    path = _write(tmp_path, scene=_objects(4200))
    assert main(["rescue", str(path)]) == 2
    err = capsys.readouterr().err
    assert "only runs on Windows" in err
    assert "run_rescue.ps1" in err


def test_cli_verify_reports_a_difference_with_a_nonzero_exit(capsys, monkeypatch):
    from maxrescue.core.verify import VerifyResult

    monkeypatch.setattr(
        "maxrescue.app.cli.compare",
        lambda *a, **k: VerifyResult(
            identical=False, passed=False, exit_code=2, summary="differences"
        ),
    )
    assert main(["verify", "a.exr", "b.exr"]) == 4
    assert "do not ship" in capsys.readouterr().out


def test_cli_verify_passes_identical_renders(capsys, monkeypatch):
    from maxrescue.core.verify import VerifyResult

    monkeypatch.setattr(
        "maxrescue.app.cli.compare",
        lambda *a, **k: VerifyResult(
            identical=True, passed=True, exit_code=0, summary="identical"
        ),
    )
    assert main(["verify", "a.exr", "b.exr"]) == 0
    assert "bit-identical" in capsys.readouterr().out
