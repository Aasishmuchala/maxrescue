"""End to end: a real file on disk, through every layer.

The unit tests each prove one component. This proves they fit together — the
X-ray's node graph feeds the governor's candidates, whose batches feed the
merge loop, whose measurements feed the calibration. A seam that has drifted
shows up here and nowhere else.

The only fake is 3ds Max itself.
"""

from __future__ import annotations

from maxrescue.core.batch import BatchRunner
from maxrescue.core.governor import Governor, candidates_from
from maxrescue.core.plan import PlanConfig
from maxrescue.core.types import NodeFacts
from maxrescue.xray.report import Verdict, xray
from tests.fakes import FakeScene, make_context
from tests.helpers import class_entry, container, dll_entry, leaf, node_chunk, refs_map
from tests.helpers_ole import build_ole, pad_chunks

GB = 1 << 30
NODE, GEOM, MATERIAL = 0x01, 0x10, 0xC00
EPOLY = (0x1BF8338D, 0x192F6098)


def _villa(tmp_path, *, props: int = 30, malware: bool = False):
    """A scene shaped like a real one: a hero mesh, scatter, and many props."""
    classes = (
        class_entry(dll_index=-1, class_a=1, class_b=0, super_id=NODE, name="Node")
        + class_entry(dll_index=0, class_a=EPOLY[0], class_b=EPOLY[1],
                      super_id=GEOM, name="Editable Poly")
        + class_entry(dll_index=1, class_a=0x37BF3F2F, class_b=0x7034695C,
                      super_id=MATERIAL, name="VRayMtl")
        + class_entry(dll_index=2, class_a=0xAAAA, class_b=0xBBBB,
                      super_id=GEOM, name="Forest_Pro")
    )
    dlls = (
        dll_entry("Editable Poly Object (Autodesk)", "EPoly.dlo")
        + dll_entry("V-Ray renderer", "vrender2026.dlr")
        + dll_entry("Forest Pack Pro", "ForestPackPro.dlo")
    )

    objects: list[bytes] = []
    objects.append(node_chunk(0, name="Villa_Hero", refs=refs_map({1: 1, 3: 2})))
    objects.append(leaf(1, b"g" * 400_000))          # the monster
    objects.append(leaf(2, b"m" * 400))
    objects.append(node_chunk(0, name="Trees_Forest", refs=refs_map({1: 4, 3: 2})))
    objects.append(leaf(3, b"f" * 40_000))
    for i in range(props):
        position = len(objects) + 1
        objects.append(
            node_chunk(0, name=f"Prop_{i:02d}", refs=refs_map({1: position, 3: 2}))
        )
        objects.append(leaf(1, b"p" * 3000))

    streams = {
        "Scene": container(0x2012, b"".join(objects)),
        "ClassDirectory3": pad_chunks(classes),
        "DllDirectory": pad_chunks(dlls),
    }
    if malware:
        streams["ScriptedCustAttribDefs"] = pad_chunks(b"junk CRP_BScript junk")

    path = tmp_path / "villa.max"
    path.write_bytes(build_ole(streams))
    return path


# ---------------------------------------------------------------------------
# X-ray -> governor
# ---------------------------------------------------------------------------


def test_the_xray_feeds_the_governor_without_an_adapter_in_between(tmp_path):
    report = xray(str(_villa(tmp_path)))
    candidates = candidates_from(report.nodes.nodes)

    assert candidates, "the node graph produced nothing the governor can use"
    assert all(c.name for c in candidates)
    assert all(c.weight > 0 for c in candidates)

    governor = Governor(ram_budget=8 * GB)
    batches = governor.plan_all(candidates)
    planned = {name for b in batches for name in b.names}
    assert planned == {c.name for c in candidates}, "every candidate must be planned"


def test_the_heaviest_node_is_named_and_reaches_its_own_batch(tmp_path):
    report = xray(str(_villa(tmp_path)))
    heaviest = report.nodes.heaviest(1)[0]
    assert heaviest.name == "Villa_Hero"

    governor = Governor(ram_budget=1 * GB)
    batches = governor.plan_all(candidates_from(report.nodes.nodes))
    home = [b for b in batches if "Villa_Hero" in b.names]
    assert len(home) == 1


def test_the_plugin_list_survives_all_the_way_to_the_report(tmp_path):
    report = xray(str(_villa(tmp_path)))
    assert "ForestPackPro.dlo" in report.required_dlls


# ---------------------------------------------------------------------------
# the whole pipeline
# ---------------------------------------------------------------------------


def test_a_full_rescue_merges_everything_and_reduces_it(tmp_path):
    source = _villa(tmp_path)
    report = xray(str(source))
    candidates = candidates_from(report.nodes.nodes)

    # A stand-in Max: every named node resolves to a heavy mesh.
    library = {
        c.name: NodeFacts(
            handle=5000 + i,
            name=c.name,
            class_name="Editable_Poly",
            super_class="GeomObject",
            faces=250_000,
        )
        for i, c in enumerate(candidates)
    }
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)

    runner = BatchRunner(
        context=ctx,
        governor=Governor(ram_budget=1 * GB),
        source_path=str(source),
        output_path=str(tmp_path / "villa_rescued.max"),
        config=PlanConfig(),
    )
    list(runner.run(candidates))
    outcome = runner.outcome

    assert outcome.merged == len(candidates)
    assert outcome.complete
    assert not outcome.halted
    assert ctx.merge.saved_to == str(tmp_path / "villa_rescued.max")
    # Reduction actually ran, not just the merge.
    assert any(b.run and b.run.applied for b in outcome.batches)


def test_the_rescue_ends_lighter_than_its_own_peak(tmp_path):
    """The whole premise: never hold the finished scene's worth of memory."""
    source = _villa(tmp_path)
    candidates = candidates_from(xray(str(source)).nodes.nodes)
    library = {
        c.name: NodeFacts(
            handle=6000 + i, name=c.name, class_name="Editable_Poly",
            super_class="GeomObject", faces=400_000,
        )
        for i, c in enumerate(candidates)
    }
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
    runner = BatchRunner(
        context=ctx,
        governor=Governor(ram_budget=1 * GB),
        source_path=str(source),
        output_path=str(tmp_path / "out.max"),
        config=PlanConfig(),
    )
    list(runner.run(candidates))
    outcome = runner.outcome
    assert outcome.memory_after.rss_mb <= outcome.peak_rss_mb


def test_the_calibration_ends_measured_rather_than_assumed(tmp_path):
    source = _villa(tmp_path)
    candidates = candidates_from(xray(str(source)).nodes.nodes)
    library = {
        c.name: NodeFacts(
            handle=7000 + i, name=c.name, class_name="Editable_Poly",
            super_class="GeomObject", faces=200_000,
        )
        for i, c in enumerate(candidates)
    }
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
    governor = Governor(ram_budget=1 * GB)
    runner = BatchRunner(
        context=ctx, governor=governor, source_path=str(source),
        output_path=str(tmp_path / "out.max"), config=PlanConfig(),
    )
    list(runner.run(candidates))
    assert governor.calibration.observed
    assert "measured" in governor.calibration.describe()


# ---------------------------------------------------------------------------
# the refusal path
# ---------------------------------------------------------------------------


def test_a_file_with_a_known_payload_is_flagged_before_anything_runs(tmp_path):
    """The rescue script refuses on this verdict — merging a compromised scene
    into a clean session would spread it."""
    report = xray(str(_villa(tmp_path, malware=True)))
    assert report.verdict == Verdict.MALWARE
    assert report.malware


def test_scatter_objects_survive_a_full_pipeline_run(tmp_path):
    """Forest Pack geometry is generated, not stored; converting it destroys the
    system. It must come through untouched."""
    source = _villa(tmp_path)
    candidates = candidates_from(xray(str(source)).nodes.nodes)
    library = {
        c.name: NodeFacts(
            handle=8000 + i,
            name=c.name,
            class_name="Forest_Pro" if c.name == "Trees_Forest" else "Editable_Poly",
            super_class="GeomObject",
            faces=500_000,
        )
        for i, c in enumerate(candidates)
    }
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
    runner = BatchRunner(
        context=ctx, governor=Governor(ram_budget=2 * GB), source_path=str(source),
        output_path=str(tmp_path / "out.max"), config=PlanConfig(),
    )
    list(runner.run(candidates))

    survivors = {n.name for n in ctx.scene.nodes.values()}
    assert "Trees_Forest" in survivors
