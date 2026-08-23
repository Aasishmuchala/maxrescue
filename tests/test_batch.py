"""The batch loop.

What matters here is the accounting. A rescue that quietly drops 3,000 objects
and reports success is worse than one that fails loudly, so most of these tests
are about whether the run can tell you what it did *not* do.
"""

from __future__ import annotations

from maxrescue.core.batch import BatchRunner
from maxrescue.core.governor import Governor, MergeCandidate
from maxrescue.core.plan import PlanConfig
from maxrescue.core.types import BatchOutcome, NodeFacts
from tests.fakes import FakeScene, make_context

GB = 1 << 30
MB = 1 << 20


def _library(count: int = 12, faces: int = 200_000) -> dict[str, NodeFacts]:
    return {
        f"Obj_{i:03d}": NodeFacts(
            handle=1000 + i,
            name=f"Obj_{i:03d}",
            class_name="Editable_Poly",
            super_class="GeomObject",
            faces=faces,
        )
        for i in range(count)
    }


def _candidates(library: dict[str, NodeFacts], weight: int = 40 * MB):
    return [
        MergeCandidate(name=name, weight=weight, position=i)
        for i, name in enumerate(library)
    ]


def _runner(ctx, budget_gb: float = 8.0, **kw) -> BatchRunner:
    return BatchRunner(
        context=ctx,
        governor=Governor(ram_budget=int(budget_gb * GB)),
        source_path=r"C:\jobs\villa\villa.max",
        output_path=r"C:\jobs\villa\villa_rescued.max",
        config=PlanConfig(),
        **kw,
    )


def _run(ctx, **kw):
    library = kw.pop("library", None) or _library()
    ctx.merge.library = library
    runner = _runner(ctx, **kw)
    list(runner.run(_candidates(library)))
    return runner.outcome


def _empty_context(library=None):
    scene = FakeScene(rss_mb=3000.0)
    ctx = make_context(scene, library=library)
    return ctx


# ---------------------------------------------------------------------------
# the whole scene gets rebuilt
# ---------------------------------------------------------------------------


def test_every_object_is_merged_across_the_batches():
    library = _library(12)
    outcome = _run(_empty_context(library), library=library)
    assert outcome.merged == 12
    assert outcome.requested == 12
    assert outcome.complete


def test_the_scene_is_reset_before_the_first_merge():
    """Merging into whatever happened to be open would silently contaminate the
    output."""
    ctx = _empty_context()
    _run(ctx)
    assert ctx.merge.resets >= 1


def test_the_output_is_saved_after_every_batch():
    """A crash at batch 40 of 50 must not throw away the first 39."""
    library = _library(12)
    ctx = _empty_context(library)
    ctx.merge.library = library
    runner = _runner(ctx, budget_gb=6.0)
    list(runner.run(_candidates(library)))
    assert len(runner.outcome.batches) > 1
    assert ctx.merge.saved_to == r"C:\jobs\villa\villa_rescued.max"


def test_reduction_runs_inside_the_loop_not_only_at_the_end():
    """Converting a tree while only its batch is resident is the entire point."""
    library = _library(4, faces=900_000)
    ctx = _empty_context(library)
    outcome = _run(ctx, library=library)
    assert any(b.run and b.run.applied for b in outcome.batches)


def test_no_backup_is_taken_per_batch():
    """The untouched source file is the backup; a copy per batch is pure cost."""
    ctx = _empty_context()
    _run(ctx)
    assert not ctx.backup.created


# ---------------------------------------------------------------------------
# accounting — the run must be able to say what it did not do
# ---------------------------------------------------------------------------


def test_a_shortfall_is_counted_not_hidden():
    library = _library(6)
    ctx = _empty_context(library)
    ctx.scene.merge_shortfall = 2
    outcome = _run(ctx, library=library)
    assert outcome.merged < outcome.requested
    assert not outcome.complete
    assert any(b.shortfall for b in outcome.batches)


def test_a_batch_that_merges_nothing_is_quarantined_and_the_run_continues():
    library = _library(6)
    ctx = _empty_context(library)
    ctx.merge.library = {}  # nothing resolves
    runner = _runner(ctx, budget_gb=4.5)
    list(runner.run(_candidates(library)))
    outcome = runner.outcome
    assert outcome.quarantined
    assert not outcome.halted, "one bad batch must not stop the rest"


def test_a_merge_that_raises_is_quarantined_with_its_reason():
    library = _library(4)
    ctx = _empty_context(library)
    ctx.merge.library = library

    def explode(path, names):
        raise RuntimeError("missing plugin: ForestPackPro.dlo")

    ctx.merge.merge = explode
    runner = _runner(ctx)
    list(runner.run(_candidates(library)))
    (batch,) = [b for b in runner.outcome.batches if b.outcome is BatchOutcome.QUARANTINED]
    assert "ForestPackPro" in batch.note


def test_a_halting_batch_stops_the_run_and_says_what_was_saved():
    library = _library(8, faces=900_000)
    ctx = _empty_context(library)
    ctx.merge.library = library
    ctx.scene.fail_proxy_for = {1000 + i for i in range(8)}
    runner = _runner(ctx, budget_gb=5.0)
    list(runner.run(_candidates(library)))
    outcome = runner.outcome
    assert outcome.halted
    assert any("re-run excluding" in line for line in outcome.log)


def test_the_outcome_reports_peak_memory_during_the_run_not_just_the_end():
    """The end figure is the point of the exercise; the peak is what decides
    whether the machine survives it."""
    library = _library(10, faces=500_000)
    outcome = _run(_empty_context(library), library=library, budget_gb=5.5)
    assert outcome.peak_rss_mb > 0


# ---------------------------------------------------------------------------
# the governor learns
# ---------------------------------------------------------------------------


def test_the_governor_is_calibrated_from_real_batches():
    """It starts on a declared prior; by the end it should be using measurement."""
    library = _library(12)
    ctx = _empty_context(library)
    ctx.merge.library = library
    runner = _runner(ctx, budget_gb=5.5)
    assert runner.governor.calibration.observed is False
    list(runner.run(_candidates(library)))
    assert runner.governor.calibration.observed is True
    assert runner.governor.calibration.samples >= 1


def test_a_quarantined_batch_does_not_teach_the_governor_anything():
    """A batch that merged nothing has no ratio to learn from."""
    library = _library(4)
    ctx = _empty_context(library)
    ctx.merge.library = {}
    runner = _runner(ctx)
    list(runner.run(_candidates(library)))
    assert runner.governor.calibration.observed is False


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


def test_cancelling_stops_between_batches_and_keeps_what_was_done():
    library = _library(12)
    ctx = _empty_context(library)
    ctx.merge.library = library
    runner = _runner(ctx, budget_gb=5.0)
    events = runner.run(_candidates(library))
    next(events)
    runner.cancel()
    for _ in events:
        pass
    assert any("cancelled" in line for line in runner.outcome.log)


def test_the_log_opens_with_the_plan_and_says_the_prior_is_unmeasured():
    outcome = _run(_empty_context())
    assert "batches" in outcome.log[0]
    assert "NOT measured" in outcome.log[0]


def test_calibration_uses_the_merge_cost_not_the_net_change():
    """Regression: feeding the governor the net delta measured after reduction
    conflates loading cost with reduction saving. It is usually negative, so the
    governor silently learned nothing and kept using the prior forever."""
    library = _library(8, faces=400_000)
    ctx = _empty_context(library)
    ctx.merge.library = library
    runner = _runner(ctx, budget_gb=5.5)
    list(runner.run(_candidates(library)))
    batch = runner.outcome.batches[0]
    assert batch.merge_cost_mb > 0, "merging must cost memory"
    assert batch.rss_delta_mb < batch.merge_cost_mb, "reduction must give some back"
    assert runner.governor.calibration.observed
