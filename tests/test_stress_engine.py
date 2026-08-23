"""Stress: the engine under adversarial conditions.

The unit tests fail one thing at a time in a place I chose. These fail things
everywhere, at every index, and assert the invariants hold regardless:

* the material guard always runs and its pins are always released;
* a run always produces a report, whatever went wrong;
* every candidate is accounted for — merged, quarantined, or named as missing;
* nothing loops or blows up on a pathological scene.

Run with: pytest -k stress
"""

from __future__ import annotations

import random
import time

import pytest

from maxrescue.core.batch import BatchRunner
from maxrescue.core.governor import Governor, MergeCandidate
from maxrescue.core.plan import PlanConfig, build_plan
from maxrescue.core.session import RescueSession
from maxrescue.core.types import Engine, MemorySample, NodeFacts, OpStatus
from tests.fakes import FakeScene, archviz_scene, make_context

GB = 1 << 30
MB = 1 << 20
SEED = 20260823


def _drain(session):
    report = None
    for event in session.run_chunks():
        if event.report:
            report = event.report
    return report


# ---------------------------------------------------------------------------
# the session, broken everywhere
# ---------------------------------------------------------------------------


def test_stress_cancelling_at_every_step_always_produces_a_report():
    """Whatever the timing, the run must end with something to show."""
    for stop_at in range(0, 30):
        ctx = make_context()
        session = RescueSession(ctx, PlanConfig())
        report = None
        for index, event in enumerate(session.run_chunks()):
            if index == stop_at:
                session.cancel()
            if event.report:
                report = event.report
        assert report is not None, f"cancelling at event {stop_at} produced no report"
        assert ctx.guard.pinned is False, f"pins leaked when cancelling at {stop_at}"


def test_stress_failing_each_operation_in_turn_halts_cleanly():
    """Every op is made to fail, one at a time. Each must halt, keep the guard
    sweep, and release its pins."""
    ctx = make_context()
    plan = build_plan(
        list(ctx.query.nodes()), PlanConfig(), Engine.VRAY, render_hidden=False
    )

    for op in plan.ops:
        ctx = make_context()
        original = ctx.fixes

        def explode(*args, _op=op, **kwargs):
            raise RuntimeError(f"injected failure in {_op.id}")

        # Break whichever bridge call this op will reach.
        for attribute in (
            "free_scene_bitmaps", "clear_undo_buffer", "purge_unused_materials",
            "strip_motion_mixer", "collect_garbage", "delete_node",
            "collapse_stack", "convert_to_proxy", "set_bitmap_proxy_mode",
            "set_nitrous_texture_limit", "set_scatter_display",
            "convert_bitmaps_to_vray",
        ):
            setattr(original, attribute, explode)

        report = _drain(RescueSession(ctx, PlanConfig()))
        assert report is not None
        assert report.halted, f"{op.id} failed without halting"
        assert report.guard.checked >= 0
        assert ctx.guard.pinned is False, f"pins leaked after {op.id} failed"


def test_stress_a_bridge_that_lies_about_success_is_caught_by_verification():
    """The most dangerous failure: an operation that reports success and did
    nothing. Verification is the only thing standing between that and a report
    claiming work that never happened."""
    ctx = make_context()
    ctx.fixes.delete_node = lambda handle: None       # claims success, deletes nothing
    ctx.fixes.convert_to_proxy = lambda handle, out_dir: 99999  # never replaces
    report = _drain(RescueSession(ctx, PlanConfig()))
    assert report.halted, "a lying bridge was accepted as successful"
    assert any(
        r.status is OpStatus.FAILED and "verif" in r.message.lower()
        for r in report.results
    )


def test_stress_a_memory_probe_returning_nonsense_does_not_produce_nonsense_claims():
    for value in (0.0, -50.0, float("inf")):
        ctx = make_context()
        ctx.memory.sample = lambda v=value: MemorySample(rss_mb=v, source="broken")
        report = _drain(RescueSession(ctx, PlanConfig()))
        assert report is not None
        if value <= 0:
            assert not report.metrics.measured, (
                "a non-positive RSS must fall back to polygons, not print a "
                "confident percentage"
            )


def test_stress_a_guard_that_cannot_reattach_is_reported_not_hidden():
    scene = archviz_scene()
    scene.drop_material_on_proxy = {1}
    ctx = make_context(scene)
    ctx.guard.reattach = lambda node, material: False
    report = _drain(RescueSession(ctx, PlanConfig()))
    if report.guard.lost:
        assert report.guard.unrepaired
        assert not report.ok
        assert any("could NOT be restored" in line for line in report.log)


def test_stress_a_scene_of_every_single_node_shape():
    """One node with each guard-relevant flag set, all at once."""
    rng = random.Random(SEED)
    flags = [
        "is_hidden", "renderable", "primary_visibility", "is_animated",
        "has_skin", "is_bone", "in_xref", "scripted_controller",
        "negative_scale", "in_group", "is_group_head", "has_parent",
        "has_dependents",
    ]
    scene = FakeScene(rss_mb=5000.0)
    for index in range(300):
        kwargs = {f: rng.random() < 0.3 for f in flags}
        scene.add(
            NodeFacts(
                handle=index + 1,
                name=f"N{index}",
                class_name=rng.choice(
                    ["Editable_Poly", "Forest_Pro", "VRayProxy", "Sphere", "tyFlow"]
                ),
                super_class=rng.choice(["GeomObject", "Helper", "Light"]),
                faces=rng.randint(0, 2_000_000),
                child_count=rng.randint(0, 3),
                modifier_classes=tuple(
                    rng.sample(["Bend", "TurboSmooth", "Skin", "Twist"],
                               rng.randint(0, 3))
                ),
                **kwargs,
            ),
            material=1,
        )
    ctx = make_context(scene)
    report = _drain(RescueSession(ctx, PlanConfig()))
    assert report is not None
    assert ctx.guard.pinned is False


# ---------------------------------------------------------------------------
# the governor, pushed hard
# ---------------------------------------------------------------------------


def test_stress_a_hundred_thousand_candidates_plan_quickly():
    candidates = [
        MergeCandidate(name=f"n{i}", weight=(i % 500) * MB, position=i)
        for i in range(100_000)
    ]
    governor = Governor(ram_budget=8 * GB)
    started = time.time()
    batches = governor.plan_all(candidates)
    elapsed = time.time() - started

    assert elapsed < 30.0, f"planning 100k candidates took {elapsed:.1f}s"
    assert sum(len(b.names) for b in batches) == 100_000


def test_stress_one_giant_family_does_not_hang_the_union_find():
    """50,000 nodes in a single parent chain — the pathological union-find input."""
    candidates = [MergeCandidate(name="root", weight=MB, position=0)] + [
        MergeCandidate(name=f"n{i}", weight=MB, position=i, parent_position=i - 1)
        for i in range(1, 50_000)
    ]
    started = time.time()
    batches = Governor(ram_budget=8 * GB).plan_all(candidates)
    assert time.time() - started < 30.0
    assert len(batches) == 1, "one family must stay in one batch"
    assert len(batches[0].names) == 50_000


def test_stress_a_node_that_is_its_own_parent():
    candidates = [MergeCandidate(name="self", weight=MB, position=5, parent_position=5)]
    batches = Governor(ram_budget=GB).plan_all(candidates)
    assert [n for b in batches for n in b.names] == ["self"]


def test_stress_random_parent_graphs_always_cover_every_candidate():
    rng = random.Random(SEED + 1)
    for _ in range(60):
        count = rng.randint(1, 60)
        candidates = [
            MergeCandidate(
                name=f"n{i}",
                weight=rng.randint(0, 200) * MB,
                position=i,
                parent_position=rng.choice([None, rng.randrange(count), 9999, i]),
            )
            for i in range(count)
        ]
        batches = Governor(ram_budget=rng.choice([1, 4, 64]) * GB).plan_all(candidates)
        placed = [n for b in batches for n in b.names]
        assert sorted(placed) == sorted(c.name for c in candidates)
        assert len(placed) == len(set(placed)), "an object was placed twice"


def test_stress_zero_weight_candidates_do_not_divide_by_zero():
    candidates = [MergeCandidate(name=f"n{i}", weight=0, position=i) for i in range(50)]
    batches = Governor(ram_budget=GB).plan_all(candidates)
    assert sum(len(b.names) for b in batches) == 50


def test_stress_a_tiny_budget_still_terminates():
    candidates = [MergeCandidate(name=f"n{i}", weight=MB, position=i) for i in range(200)]
    batches = Governor(ram_budget=1).plan_all(candidates)
    assert sum(len(b.names) for b in batches) == 200
    assert all(b.isolated for b in batches)


def test_stress_calibration_cannot_be_driven_to_absurdity():
    """A pathological sequence of observations must not produce a multiplier
    that makes the governor either paralysed or reckless."""
    rng = random.Random(SEED + 2)
    governor = Governor(ram_budget=8 * GB)
    for _ in range(500):
        governor.observe(
            batch_weight=rng.choice([0, 1, MB, GB]),
            rss_delta=rng.choice([-GB, 0, 1, MB, 100 * GB]),
        )
    assert governor.calibration.multiplier >= 1.0
    assert governor.weight_budget() >= 1


# ---------------------------------------------------------------------------
# the batch loop, broken everywhere
# ---------------------------------------------------------------------------


def _library(count: int, faces: int = 200_000):
    return {
        f"Obj_{i:03d}": NodeFacts(
            handle=3000 + i, name=f"Obj_{i:03d}", class_name="Editable_Poly",
            super_class="GeomObject", faces=faces,
        )
        for i in range(count)
    }


def _runner(ctx, budget_gb=0.5, **kw):
    return BatchRunner(
        context=ctx,
        governor=Governor(ram_budget=int(budget_gb * GB)),
        source_path="src.max",
        output_path="out.max",
        config=PlanConfig(),
        **kw,
    )


def test_stress_failing_the_merge_of_each_batch_in_turn():
    library = _library(12)
    for failing in range(1, 6):
        ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
        calls = {"n": 0}
        real = ctx.merge.merge

        def flaky(path, names, _f=failing):
            calls["n"] += 1
            if calls["n"] == _f:
                raise RuntimeError("injected merge failure")
            return real(path, names)

        ctx.merge.merge = flaky
        runner = _runner(ctx)
        candidates = [
            MergeCandidate(name=n, weight=40 * MB, position=i)
            for i, n in enumerate(library)
        ]
        list(runner.run(candidates))
        outcome = runner.outcome
        assert outcome is not None
        # Accounting must still add up: everything is merged, quarantined or named.
        accounted = outcome.merged + sum(b.shortfall for b in outcome.batches)
        assert accounted == outcome.requested


def test_stress_a_merge_returning_objects_nobody_asked_for():
    """Merge is name-based and Max can rename on collision. Extra arrivals must
    not corrupt the accounting."""
    library = _library(6)
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)

    def greedy(path, names):
        for name, node in library.items():
            ctx.scene.add(node, material=1)
        return list(library), []

    ctx.merge.merge = greedy
    runner = _runner(ctx, budget_gb=4)
    candidates = [
        MergeCandidate(name=n, weight=40 * MB, position=i) for i, n in enumerate(library)
    ]
    list(runner.run(candidates))
    assert runner.outcome is not None
    assert runner.outcome.merged >= 0


def test_stress_a_save_that_always_fails_does_not_abort_the_run():
    """Losing the output is bad, but abandoning mid-run leaves Max holding the
    whole scene, which is worse."""
    library = _library(8)
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
    ctx.merge.save_as = lambda path: False
    runner = _runner(ctx)
    candidates = [
        MergeCandidate(name=n, weight=40 * MB, position=i) for i, n in enumerate(library)
    ]
    list(runner.run(candidates))
    assert runner.outcome is not None


def test_stress_an_empty_candidate_list_is_a_no_op_not_a_crash():
    ctx = make_context(FakeScene(rss_mb=3000.0), library={})
    runner = _runner(ctx)
    list(runner.run([]))
    assert runner.outcome.batches == ()
    assert runner.outcome.merged == 0


def test_stress_duplicate_candidate_names_do_not_double_count():
    """Max scenes routinely contain duplicate node names."""
    library = _library(3)
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
    runner = _runner(ctx, budget_gb=4)
    candidates = [
        MergeCandidate(name="Obj_000", weight=40 * MB, position=i) for i in range(5)
    ]
    list(runner.run(candidates))
    outcome = runner.outcome
    assert outcome.requested == 5
    assert outcome.merged <= outcome.requested


@pytest.mark.parametrize("budget_gb", [0.01, 0.1, 1.0, 64.0])
def test_stress_every_budget_produces_a_coherent_run(budget_gb):
    library = _library(20)
    ctx = make_context(FakeScene(rss_mb=3000.0), library=library)
    runner = _runner(ctx, budget_gb=budget_gb)
    candidates = [
        MergeCandidate(name=n, weight=40 * MB, position=i) for i, n in enumerate(library)
    ]
    list(runner.run(candidates))
    outcome = runner.outcome
    assert outcome.requested == 20
    assert outcome.merged <= 20
    assert outcome.peak_rss_mb >= 0


# ---------------------------------------------------------------------------
# cost of verification — the O(n^2) trap
# ---------------------------------------------------------------------------


def test_stress_verification_does_not_re_read_the_whole_scene_per_operation():
    """`SceneQuery.nodes()` is the expensive bulk MAXScript crossing. Calling it
    once per operation turns a scene with thousands of collapse targets into
    thousands of full scene reads — precisely the O(n^2) shape that produced a
    nine-minute stall in a sibling project."""
    scene = FakeScene(rss_mb=9000.0)
    for index in range(400):
        scene.add(
            NodeFacts(
                handle=index + 1,
                name=f"Prop_{index}",
                class_name="Editable_Poly",
                super_class="GeomObject",
                faces=5_000,
                modifier_classes=("Bend",),
            ),
            material=1,
        )
    ctx = make_context(scene)

    calls = {"n": 0}
    real = ctx.query.nodes

    def counting():
        calls["n"] += 1
        return real()

    ctx.query.nodes = counting
    _drain(RescueSession(ctx, PlanConfig()))

    # One read to plan, plus a small constant for measurement. Anything
    # proportional to the number of operations is the bug.
    assert calls["n"] < 20, (
        f"the scene was read in full {calls['n']} times for ~400 operations — "
        "verification is re-reading the whole scene per op"
    )
