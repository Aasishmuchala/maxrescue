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
