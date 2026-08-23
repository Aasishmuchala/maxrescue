"""Texture reduction — the one quality trade the user explicitly authorised.

The rule is a floor, not a target: nothing goes below 4K, and anything already
at or under it is left alone. An 8K map becomes 4K; a 4K map is untouched. This is the only stage that may make the render
look different, so every decision it takes is reported per texture.

What it must NEVER do is lose one. A downscale that deletes the original, or
relinks to a file that was not written, turns a memory problem into a missing-
asset problem — which is strictly worse, because the original is gone.
"""

from __future__ import annotations

from maxrescue.core.plan import PlanConfig, texture_reason
from maxrescue.core.types import TextureFacts


def texture(**kw) -> TextureFacts:
    base = dict(
        handle=1,
        path=r"C:\jobs\villa\textures\brick_diffuse.png",
        width=8192,
        height=8192,
        loader="Bitmaptexture",
        exists=True,
    )
    base.update(kw)
    return TextureFacts(**base)


CONFIG = PlanConfig()


def test_an_8k_texture_is_reduced_to_the_4k_floor():
    assert texture_reason(texture(), CONFIG) is None


def test_a_texture_already_at_the_floor_is_left_alone():
    reason = texture_reason(texture(width=4096, height=4096), CONFIG)
    assert reason is not None and "4,096" in reason


def test_a_texture_below_the_floor_is_never_enlarged_or_touched():
    reason = texture_reason(texture(width=1024, height=1024), CONFIG)
    assert reason is not None


def test_a_missing_texture_is_refused_rather_than_relinked_to_nothing():
    """Relinking a texture that is not on disk turns a memory problem into a
    missing-asset problem, and the original reference is then gone."""
    reason = texture_reason(texture(exists=False), CONFIG)
    assert reason is not None and "not on disk" in reason


def test_a_texture_inside_an_xref_is_never_touched():
    reason = texture_reason(texture(in_xref=True), CONFIG)
    assert reason is not None and "xref" in reason.lower()


def test_the_floor_is_configurable_but_never_below_4k():
    """4K is the agreed limit of what may be lost. A settings file asking for
    less must not be honoured silently — nobody would see it happen."""
    for asked in (256, 512, 1024, 2048):
        config = PlanConfig(texture_floor_px=asked)
        assert config.effective_texture_floor == 4096, asked


def test_a_larger_floor_is_honoured():
    """Asking to keep MORE quality is always allowed."""
    config = PlanConfig(texture_floor_px=8192)
    assert config.effective_texture_floor == 8192
    assert texture_reason(texture(width=8192, height=8192), config) is not None


def test_non_square_textures_are_judged_by_their_longest_edge():
    assert texture_reason(texture(width=16384, height=1024), CONFIG) is None
    assert texture_reason(texture(width=4096, height=256), CONFIG) is not None


# --------------------------------------------------------------------------
# the stage actually runs
# --------------------------------------------------------------------------


def test_texture_ops_are_not_skipped_as_missing_nodes():
    """A texture op targets a BITMAP LOADER, not a node. Checking those handles
    with `node_exists` reported every one as vanished, so the whole stage was
    silently skipped while the log said "target no longer exists" — the feature
    looked implemented and did nothing."""
    from maxrescue.core.session import RescueSession
    from maxrescue.core.types import NodeFacts, OpStatus, Stage
    from tests.fakes import FakeScene, make_context

    scene = FakeScene(rss_mb=9000.0)
    for index in range(5):
        scene.textures[index] = TextureFacts(
            handle=index, path=f"C:/t/{index}.png", width=8192, height=8192
        )
    scene.add(
        NodeFacts(
            handle=1, name="Hero", class_name="Editable_Poly",
            super_class="GeomObject", faces=500_000,
        ),
        material=1,
    )
    ctx = make_context(scene)

    report = None
    for event in RescueSession(ctx, PlanConfig()).run_chunks():
        if event.report:
            report = event.report

    results = [r for r in report.results if r.stage is Stage.TEXTURES]
    assert results, "no texture ops were planned at all"
    assert all(r.status is OpStatus.APPLIED for r in results), [
        (r.status.value, r.message) for r in results
    ]
    assert len(ctx.fixes.reduced_textures) == 5


def test_a_texture_loader_that_really_vanished_is_skipped_not_halted():
    from maxrescue.core.plan import PlanConfig as _Config
    from maxrescue.core.session import RescueSession
    from maxrescue.core.types import OpStatus, Stage
    from tests.fakes import FakeScene, make_context

    scene = FakeScene(rss_mb=9000.0)
    scene.textures[7] = TextureFacts(
        handle=7, path="C:/t/7.png", width=8192, height=8192
    )
    ctx = make_context(scene)
    original = ctx.query.textures
    # planned, then gone by the time it runs
    ctx.query.textures = lambda: tuple(original())
    scene.textures.pop(7)
    scene.textures[7] = TextureFacts(handle=7, path="x", width=8192, height=8192)

    report = None
    for event in RescueSession(ctx, _Config()).run_chunks():
        if event.report:
            report = event.report
    assert not report.halted
