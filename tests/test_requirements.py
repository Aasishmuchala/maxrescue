"""What machine will this scene run on?

The answer has to separate two very different kinds of statement, because the
user will quote whichever one is bolder:

* what was MEASURED — the rescued scene occupied N GB when opened here;
* what is RECOMMENDED — a machine size derived from that measurement by a rule
  that is stated out loud, not smuggled in.

A measurement with no reasoning attached invites someone to buy 64 GB for a
scene that needs 128 the moment it renders.
"""

from __future__ import annotations

from maxrescue.core.requirements import MachineRequirements, assess

GB = 1024  # MB per GiB — 42_000 MB is 41 GB, which is exactly the kind of
           # rounding an assertion should not have to guess at.


def test_an_unmeasured_run_refuses_to_recommend_anything():
    """No sample, no advice. Silence beats a number nobody can trace."""
    result = assess(open_rss_mb=0, peak_rss_mb=0)
    assert result.measured is False
    assert result.recommended_ram_gb is None
    assert "not measured" in result.summary.lower()


def test_the_measured_figure_is_reported_as_measured():
    result = assess(open_rss_mb=42 * GB, peak_rss_mb=50 * GB)
    assert result.measured is True
    assert "42" in result.summary
    assert "measured" in result.summary.lower()


def test_the_recommendation_accounts_for_render_headroom_not_just_opening():
    """Opening a scene is the cheap part. V-Ray builds its own copy on top, so a
    machine sized to the open figure runs out during the first render."""
    result = assess(open_rss_mb=42 * GB, peak_rss_mb=42 * GB)
    assert result.recommended_ram_gb > 42
    assert "render" in result.reasoning.lower()


def test_the_reasoning_is_stated_rather_than_implied():
    result = assess(open_rss_mb=20 * GB, peak_rss_mb=20 * GB)
    assert result.reasoning
    assert "×" in result.reasoning or "x" in result.reasoning.lower()


def test_it_recommends_a_machine_size_that_exists():
    """Nobody buys 93 GB of RAM."""
    for open_mb in (8 * GB, 20 * GB, 42 * GB, 90 * GB, 200 * GB):
        result = assess(open_rss_mb=open_mb, peak_rss_mb=open_mb)
        assert result.recommended_ram_gb in (16, 32, 64, 128, 256, 512, 1024)


def test_a_scene_that_grew_during_the_run_is_sized_on_the_peak():
    """The peak is what has to fit, not the tidy figure at the end."""
    modest = assess(open_rss_mb=30 * GB, peak_rss_mb=30 * GB)
    spiky = assess(open_rss_mb=30 * GB, peak_rss_mb=120 * GB)
    assert spiky.recommended_ram_gb > modest.recommended_ram_gb


def test_vram_is_reported_only_when_it_was_actually_sampled():
    assert assess(open_rss_mb=10 * GB, peak_rss_mb=10 * GB).recommended_vram_gb is None
    with_gpu = assess(open_rss_mb=10 * GB, peak_rss_mb=10 * GB, vram_mb=9 * GB)
    assert with_gpu.recommended_vram_gb >= 12


def test_it_says_plainly_whether_a_named_machine_will_cope():
    result = assess(open_rss_mb=42 * GB, peak_rss_mb=50 * GB)
    assert result.fits_in(128) is True
    assert result.fits_in(64) is False, "64 GB leaves nothing for the render"


def test_the_verdict_for_a_target_machine_is_a_sentence_not_a_boolean():
    result = assess(open_rss_mb=42 * GB, peak_rss_mb=50 * GB)
    verdict = result.verdict_for(128)
    assert "128" in verdict
    assert "GB" in verdict


def test_a_machine_that_will_not_cope_is_told_so_directly():
    result = assess(open_rss_mb=180 * GB, peak_rss_mb=190 * GB)
    verdict = result.verdict_for(128)
    assert result.fits_in(128) is False
    assert "not" in verdict.lower() or "too" in verdict.lower()
