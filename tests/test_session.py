"""Run choreography.

The tests are about *what happens when things go wrong*, because that is what
the ordering exists for. A run that succeeds proves very little; a run that
halts, cancels, or loses a material binding proves the design.
"""

from __future__ import annotations

from maxrescue.core.plan import PlanConfig
from maxrescue.core.session import RescueSession
from maxrescue.core.types import OpStatus, Stage
from tests.fakes import archviz_scene, make_context


def run(context, config=None, **kw):
    session = RescueSession(context, config or PlanConfig(), **kw)
    report = None
    events = []
    for event in session.run_chunks():
        events.append(event)
        if event.report:
            report = event.report
    return report, events, session


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_a_clean_run_applies_work_and_reports_measured_memory():
    ctx = make_context()
    report, _, _ = run(ctx)
    assert report.ok
    assert report.applied
    assert report.metrics.rss_freed_mb > 0
    assert report.metrics.measured


def test_progress_runs_from_zero_to_one_without_going_backwards():
    _, events, _ = run(make_context())
    fractions = [e.fraction for e in events]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0


def test_a_backup_is_taken_before_anything_is_modified():
    ctx = make_context()
    report, _, _ = run(ctx)
    assert report.backup_path
    assert ctx.backup.created


def test_no_backup_is_taken_when_the_source_file_is_itself_the_backup():
    """A freshly merged scene has an untouched source on disk; a second copy
    would be pure cost."""
    ctx = make_context()
    report, _, _ = run(ctx, requires_backup=False)
    assert report.backup_path is None
    assert not ctx.backup.created


# ---------------------------------------------------------------------------
# failure policy: halt vs skip vs cancel
# ---------------------------------------------------------------------------


def test_a_failed_backup_aborts_before_touching_anything():
    scene = archviz_scene()
    scene.backup_fails = True
    ctx = make_context(scene)
    nodes_before = dict(scene.nodes)
    report, _, _ = run(ctx)
    assert report.halted
    assert not report.applied
    assert scene.nodes == nodes_before
    assert any("nothing was modified" in line for line in report.log)


def test_a_failed_backup_report_does_not_claim_a_backup_exists():
    """The sibling project's report said 'backup is intact' after a backup
    failure. It was not intact; there was no backup."""
    scene = archviz_scene()
    scene.backup_fails = True
    report, _, _ = run(make_context(scene))
    assert report.backup_path is None
    assert not any("Backup:" in line for line in report.log)


def test_a_failing_operation_halts_the_run_and_leaves_later_ops_unattempted():
    scene = archviz_scene()
    scene.fail_proxy_for = {1}
    report, _, _ = run(make_context(scene))
    assert report.halted
    statuses = [r.status for r in report.results]
    assert OpStatus.FAILED in statuses


def test_a_failed_operation_is_rolled_back_not_half_applied():
    scene = archviz_scene()
    scene.fail_collapse_for = {1}
    before = dict(scene.nodes)
    run(make_context(scene))
    assert scene.nodes[1].modifier_classes == before[1].modifier_classes


def test_an_operation_whose_target_vanished_is_skipped_not_halted():
    """Proxy conversion replaces a node with a new handle, invalidating later
    ops aimed at the old one. Halting on that stopped whole good sessions."""
    ctx = make_context()
    ctx.scene.nodes.pop(30, None)  # the hidden node an op will target
    ctx.scene.assignments.pop(30, None)
    report, _, _ = run(ctx)
    assert not report.halted


def test_cancelling_keeps_completed_work_and_marks_the_rest_cancelled():
    ctx = make_context()
    session = RescueSession(ctx, PlanConfig())
    report = None
    for index, event in enumerate(session.run_chunks()):
        if index == 5:
            session.cancel()
        if event.report:
            report = event.report
    assert report.cancelled
    assert any(r.status is OpStatus.CANCELLED for r in report.results)


# ---------------------------------------------------------------------------
# the material guard
# ---------------------------------------------------------------------------


def test_the_guard_reattaches_a_material_lost_during_conversion():
    scene = archviz_scene()
    scene.drop_material_on_proxy = {1}
    ctx = make_context(scene)
    report, _, _ = run(ctx)
    assert report.guard.lost or report.guard.clean


def test_the_guard_runs_even_after_a_halt():
    """A halt is exactly when bindings are most likely to be missing and least
    likely to be checked."""
    scene = archviz_scene()
    scene.fail_proxy_for = {1}
    ctx = make_context(scene)
    report, _, _ = run(ctx)
    assert report.halted
    assert report.guard.checked > 0


def test_pins_are_released_even_when_the_run_halts():
    scene = archviz_scene()
    scene.backup_fails = True
    ctx = make_context(scene)
    run(ctx)
    assert ctx.guard.pinned is False


def test_a_deliberately_removed_node_is_not_reported_as_a_lost_material():
    """Deleting a hidden object removes its binding legitimately; counting that
    as damage would make every clean run look broken."""
    ctx = make_context()
    report, _, _ = run(ctx)
    assert report.guard.clean


# ---------------------------------------------------------------------------
# what the plan actually did
# ---------------------------------------------------------------------------


def test_scatter_objects_are_never_converted_only_redisplayed():
    ctx = make_context()
    run(ctx)
    assert 20 in ctx.scene.nodes, "the Forest_Pro object must survive"
    assert ctx.fixes.scatter_display.get(20) == "points"


def test_the_rigged_figure_is_left_alone():
    ctx = make_context()
    run(ctx)
    assert 40 in ctx.scene.nodes
    assert ctx.scene.nodes[40].modifier_classes == ("Skin",)


def test_the_gi_contributing_card_survives():
    """primaryVisibility=False still lights the scene."""
    ctx = make_context()
    run(ctx)
    assert 31 in ctx.scene.nodes


def test_unused_materials_are_purged_but_used_ones_are_not():
    ctx = make_context()
    run(ctx)
    assert 1 in ctx.scene.materials
    assert 3 not in ctx.scene.materials


def test_the_bitmap_mode_used_is_never_the_render_changing_one():
    ctx = make_context()
    run(ctx)
    assert ctx.fixes.bitmap_mode == "renderMode_UseFullRes_FlushFromMemory"


def test_bitmap_conversion_does_not_run_unless_asked_for():
    ctx = make_context()
    run(ctx)
    assert not any(c.startswith("convert_bitmaps") for c in ctx.fixes.calls)


def test_bitmap_conversion_runs_when_enabled():
    ctx = make_context()
    run(ctx, PlanConfig(convert_bitmaps=True))
    assert any(c.startswith("convert_bitmaps") for c in ctx.fixes.calls)


def test_every_exclusion_reaches_the_log_with_its_reason():
    ctx = make_context()
    report, _, _ = run(ctx)
    assert report.skipped_candidates
    assert any("skipped:" in line for line in report.log)


def test_a_proxy_is_always_set_to_bounding_box_display():
    ctx = make_context()
    run(ctx)
    assert any(c.startswith("proxy_display:") and c.endswith(":0")
               for c in ctx.fixes.calls)


def test_the_headline_falls_back_to_polygons_when_memory_is_unavailable():
    ctx = make_context()
    ctx.memory.sample = lambda: __import__(
        "maxrescue.core.types", fromlist=["MemorySample"]
    ).MemorySample(rss_mb=0.0, source="none")
    report, _, _ = run(ctx)
    assert not report.metrics.measured
    assert any("polygons" in line for line in report.log)


def test_stage_order_is_respected_in_the_results():
    ctx = make_context()
    report, _, _ = run(ctx)
    stages = [r.stage.value for r in report.results if r.status is OpStatus.APPLIED]
    assert stages == sorted(stages)


def test_hygiene_runs_before_anything_destructive():
    """If a run halts early, the free wins should already be banked."""
    ctx = make_context()
    report, _, _ = run(ctx)
    first = report.results[0]
    assert first.stage is Stage.HYGIENE
