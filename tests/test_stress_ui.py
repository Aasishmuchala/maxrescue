"""Stress: the app under conditions a long run actually creates.

A rescue can take hours. The window is redrawn every time progress ticks, and
the presenter is handed whatever the on-box script managed to write before it
died. So the bar here is:

* redrawing thousands of times must not accumulate anything;
* no value from a report — however broken — may crash the window or produce a
  sentence that misleads;
* every phase must be reachable from every other, in any order.

Run with: pytest -k stress
"""

from __future__ import annotations

import gc
import json
import os
import random

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from maxrescue.core.requirements import assess  # noqa: E402
from maxrescue.ui.presenter import (  # noqa: E402
    AppState,
    Outcome,
    Phase,
    Progress,
    present,
)
from maxrescue.ui.runner import parse_result  # noqa: E402
from maxrescue.ui.window import MaxRescueWindow  # noqa: E402
from maxrescue.xray.report import xray  # noqa: E402
from tests.test_integration import _villa  # noqa: E402

SEED = 20260823


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def survey(tmp_path_factory):
    return xray(str(_villa(tmp_path_factory.mktemp("scene"))))


def _state(survey, **kw) -> AppState:
    return AppState(path=r"D:\jobs\villa.max", survey=survey, **kw)


# ---------------------------------------------------------------------------
# the window redraws for hours
# ---------------------------------------------------------------------------


def test_stress_redrawing_a_thousand_times_does_not_accumulate_widgets(app, survey):
    """The controller redraws every 900 ms. A four-hour rescue is ~16,000
    redraws; if each one orphans its fact rows instead of destroying them, the
    process grows all afternoon and nobody connects it to the progress bar."""
    window = MaxRescueWindow()
    window.apply(_state(survey))
    gc.collect()
    before = len(window.findChildren(QLabel))

    for tick in range(1_000):
        window.apply(
            _state(
                survey,
                progress=Progress(
                    fraction=tick / 1_000,
                    message=f"batch {tick} — merging",
                    resident_mb=30_000 + tick,
                ),
            )
        )
    app.processEvents()
    gc.collect()
    after = len(window.findChildren(QLabel))

    assert after - before < 50, (
        f"labels grew from {before} to {after} over 1,000 redraws — the fact "
        "rows are being orphaned rather than destroyed"
    )
    window.deleteLater()


def test_stress_every_phase_transition_in_any_order(app, survey):
    """Phases are not a pipeline: a user can drop a new file mid-run, a run can
    fail back to the diagnosis, and the window must survive every order."""
    rng = random.Random(SEED)
    window = MaxRescueWindow()
    states = [
        AppState(),
        _state(survey),
        _state(survey, progress=Progress(0.3, "working", 30_000)),
        _state(survey, outcome=Outcome(output_path="x.max", verified=True)),
        _state(survey, outcome=Outcome(output_path="x.max", verified=False)),
        _state(survey, outcome=Outcome(output_path="x.max", verified=None)),
    ]
    for _ in range(400):
        window.apply(rng.choice(states))
    app.processEvents()
    window.deleteLater()


def test_stress_a_very_long_progress_message_does_not_wreck_the_window(app, survey):
    window = MaxRescueWindow()
    window.apply(_state(survey, progress=Progress(0.5, "x" * 20_000, 1)))
    app.processEvents()
    assert window.width() < 4_000, "one long line must not stretch the window"
    window.deleteLater()


def test_stress_warnings_never_survive_a_transition(app, survey):
    """A warning from a failed run still on screen after a clean one would be
    read as belonging to the clean one."""
    window = MaxRescueWindow()
    for _ in range(200):
        window.apply(_state(survey, outcome=Outcome(verified=False)))
        assert window.warnings_box.count() >= 1
        window.apply(_state(survey, outcome=Outcome(verified=True)))
        assert window.warnings_box.count() == 0
    window.deleteLater()


# ---------------------------------------------------------------------------
# the presenter is handed whatever survived
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"open_rss_mb": -1, "peak_rss_mb": -1},
        {"open_rss_mb": 0, "peak_rss_mb": 0},
        {"open_rss_mb": float("inf"), "peak_rss_mb": float("inf")},
        {"open_rss_mb": 1e18, "peak_rss_mb": 1e18},
        {"objects_merged": -5, "objects_requested": 10},
        {"objects_merged": 10, "objects_requested": 0},
        {"textures_reduced": -1},
        {"output_path": ""},
        {"output_path": "\x00\x01\x02"},
        {"vram_mb": -100},
        {"problems": tuple(f"problem {i}" for i in range(500))},
    ],
    ids=lambda v: str(v)[:40],
)
def test_stress_a_broken_outcome_still_renders_something_sane(app, survey, kwargs):
    view = present(_state(survey, outcome=Outcome(**kwargs)))
    assert view.phase is Phase.DONE
    assert isinstance(view.headline, str) and view.headline
    window = MaxRescueWindow()
    window.apply(_state(survey, outcome=Outcome(**kwargs)))
    window.deleteLater()


def test_stress_a_negative_shortfall_is_never_reported_as_missing_objects(app, survey):
    """More objects arriving than were asked for is odd, but it is not a loss —
    reporting '-5 objects never arrived' would be nonsense."""
    view = present(
        _state(survey, outcome=Outcome(objects_merged=100, objects_requested=90))
    )
    assert not any("-" in w and "never arrived" in w for w in view.warnings)


def test_stress_an_absurd_memory_figure_does_not_recommend_an_absurd_machine(app, survey):
    view = present(
        _state(survey, outcome=Outcome(open_rss_mb=1e15, peak_rss_mb=1e15))
    )
    joined = " ".join(f"{f.label} {f.value}" for f in view.facts)
    assert "1024 GB" in joined or "TB" in joined, joined


def test_stress_progress_outside_zero_to_one_is_clamped(app, survey):
    window = MaxRescueWindow()
    for fraction in (-5.0, -0.1, 1.5, 99.0, float("nan")):
        window.apply(_state(survey, progress=Progress(fraction, "working", 1)))
        assert 0 <= window.progress.value() <= 100, fraction
    window.deleteLater()


# ---------------------------------------------------------------------------
# the runner reads whatever the script managed to write
# ---------------------------------------------------------------------------


def test_stress_any_verdict_line_parses_without_raising(tmp_path):
    rng = random.Random(SEED + 1)
    report = tmp_path / "rescue_report.json"
    report.write_text("{}")
    lines = [
        "",
        "   ",
        "RESCUE_OK",
        "RESCUE_VERIFIED",
        "RESCUE_DIFFERENT",
        "RESCUE_HALTED",
        "RESCUE_PARTIAL",
        "RESCUE_REFUSED",
        "RESCUE_FAIL",
        "COMPLETE GIBBERISH",
        "RESCUE_" + "X" * 10_000,
        "\x00\x01\x02",
        "RESCUE_OK\nRESCUE_DIFFERENT",
    ] + ["".join(rng.choice("ABC_ 0123") for _ in range(40)) for _ in range(50)]

    for line in lines:
        outcome = parse_result(line, str(report))
        assert outcome.verified in (True, False, None)


def test_stress_a_report_with_hostile_values_never_crashes_the_parse(tmp_path):
    payloads = [
        {},
        {"outcome": None},
        {"outcome": []},
        {"outcome": {"merged": "many"}},
        {"outcome": {"merged": None, "requested": None}},
        {"outcome": {"rss_end_mb": "lots"}},
        {"outcome": {"merged": 1e400}},
        {"textures_reduced": "some"},
        {"outcome": {"output": None}},
    ]
    report = tmp_path / "rescue_report.json"
    for payload in payloads:
        report.write_text(json.dumps(payload))
        try:
            outcome = parse_result("RESCUE_OK", str(report))
        except (ValueError, TypeError) as exc:
            pytest.fail(f"{payload} raised {type(exc).__name__}: {exc}")
        assert outcome.objects_merged >= 0


def test_stress_a_verdict_that_only_looks_verified_is_not_trusted(tmp_path):
    """'RESCUE_VERIFIED_NOT' or a line merely containing the word must not be
    read as a proof of identity."""
    report = tmp_path / "r.json"
    report.write_text("{}")
    for line in (
        "the run was not RESCUE_VERIFIED",
        "RESCUE_FAIL could not run RESCUE_VERIFIED step",
    ):
        assert parse_result(line, str(report)).verified is not True


# ---------------------------------------------------------------------------
# requirements arithmetic
# ---------------------------------------------------------------------------


def test_stress_requirements_never_divide_by_zero_or_return_nonsense():
    rng = random.Random(SEED + 2)
    for _ in range(400):
        open_mb = rng.choice([0, -1, 1, 1e3, 1e6, 1e12, float("inf")])
        peak_mb = rng.choice([0, -1, 1, 1e3, 1e6, 1e12, float("inf")])
        result = assess(open_rss_mb=open_mb, peak_rss_mb=peak_mb)
        if result.measured:
            assert result.recommended_ram_gb in (16, 32, 64, 128, 256, 512, 1024)
            assert result.open_rss_gb >= 0


# ---------------------------------------------------------------------------
# the texture stage at production scale
# ---------------------------------------------------------------------------


def test_stress_textures_are_not_re_read_for_every_batch():
    """`SceneQuery.textures()` opens each bitmap to read its real dimensions.
    A scene with 10,000 maps rebuilt across 50 batches would open 500,000
    bitmaps if the sweep runs per batch — hours of pure overhead, and it would
    look like a hang."""
    from maxrescue.core.batch import BatchRunner
    from maxrescue.core.governor import Governor, MergeCandidate
    from maxrescue.core.plan import PlanConfig
    from maxrescue.core.types import NodeFacts, TextureFacts
    from tests.fakes import FakeScene, make_context

    scene = FakeScene(rss_mb=3000.0)
    for index in range(2_000):
        scene.textures[index] = TextureFacts(
            handle=index, path=f"C:/t/{index}.png", width=8192, height=8192
        )

    library = {
        f"Obj_{i:03d}": NodeFacts(
            handle=9000 + i, name=f"Obj_{i:03d}", class_name="Editable_Poly",
            super_class="GeomObject", faces=200_000,
        )
        for i in range(24)
    }
    ctx = make_context(scene, library=library)

    calls = {"n": 0}
    real = ctx.query.textures

    def counting():
        calls["n"] += 1
        return real()

    ctx.query.textures = counting

    runner = BatchRunner(
        context=ctx,
        governor=Governor(ram_budget=6 * (1 << 30)),
        source_path="src.max",
        output_path="out.max",
        config=PlanConfig(),
    )
    list(runner.run([
        MergeCandidate(name=name, weight=40 * (1 << 20), position=i)
        for i, name in enumerate(library)
    ]))

    assert calls["n"] <= 1, (
        f"the bitmap sweep ran {calls['n']} times for {len(runner.outcome.batches)} "
        "batches — it OPENS every texture file to read real dimensions, so it has "
        "to happen once per rescue, not once per batch"
    )
    assert len(ctx.fixes.reduced_textures) > 0, (
        "no texture was reduced at all — the stage is planned but never applied"
    )
