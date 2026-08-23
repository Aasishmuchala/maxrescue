"""What the window says, as pure data.

Every word the user reads and every button state is decided here, with no Qt in
sight, so the wording of a failure can be tested as carefully as the logic that
produced it. The Qt layer only draws this.
"""

from __future__ import annotations

from maxrescue.ui.presenter import AppState, Phase, Progress, present


def test_with_no_file_it_asks_for_one_and_offers_nothing_else():
    view = present(AppState())
    assert view.phase is Phase.EMPTY
    assert "drop" in view.headline.lower()
    assert ".max" in view.headline.lower() or ".max" in view.detail.lower()
    assert view.can_rescue is False
    assert view.facts == ()


# --------------------------------------------------------------------------
# a file has been surveyed
# --------------------------------------------------------------------------


def _surveyed(tmp_path, **kw):
    """A real XrayReport from a real file — never a stand-in, so the wording is
    tested against what the X-ray actually produces."""
    from maxrescue.xray.report import xray
    from tests.test_integration import _villa

    return AppState(path=str(_villa(tmp_path, **kw)), survey=xray(str(_villa(tmp_path, **kw))))


def test_a_surveyed_file_names_the_problem_in_plain_language(tmp_path):
    view = present(_surveyed(tmp_path))
    assert view.phase is Phase.SURVEYED
    # The user is an artist, not a parser author: no verdict codes on screen.
    assert "single-monster-object" not in view.headline
    assert "one object" in view.headline.lower()
    assert "Villa_Hero" in view.detail


def test_a_surveyed_file_can_be_rescued(tmp_path):
    view = present(_surveyed(tmp_path))
    assert view.can_rescue is True
    assert "rescue" in view.rescue_label.lower()


def test_the_facts_answer_what_an_artist_would_ask_first(tmp_path):
    view = present(_surveyed(tmp_path))
    labels = [f.label.lower() for f in view.facts]
    assert any("object" in label for label in labels)
    assert any("plugin" in label for label in labels)
    assert any("size" in label or "disk" in label for label in labels)


def test_required_plugins_are_listed_by_name(tmp_path):
    view = present(_surveyed(tmp_path))
    plugins = next(f for f in view.facts if "plugin" in f.label.lower())
    assert "ForestPackPro.dlo" in plugins.value


def test_a_file_with_a_known_payload_refuses_to_be_rescued(tmp_path):
    view = present(_surveyed(tmp_path, malware=True))
    assert view.can_rescue is False
    assert view.warnings
    assert "malicious" in " ".join(view.warnings).lower()


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def test_while_running_it_shows_what_it_is_doing_and_refuses_a_second_start(tmp_path):
    state = _surveyed(tmp_path)
    view = present(
        AppState(
            path=state.path,
            survey=state.survey,
            progress=Progress(fraction=0.4, message="batch 3 — 1,240 objects",
                              resident_mb=38_000),
        )
    )
    assert view.phase is Phase.RUNNING
    assert view.can_rescue is False, "starting twice would merge everything twice"
    assert "batch 3" in view.detail
    assert view.progress == 0.4


def test_live_memory_is_shown_while_it_runs(tmp_path):
    state = _surveyed(tmp_path)
    view = present(
        AppState(path=state.path, survey=state.survey,
                 progress=Progress(fraction=0.5, message="merging",
                                   resident_mb=38_000))
    )
    live = [f for f in view.facts if "memory" in f.label.lower()]
    assert live, "the number the whole exercise is about should be on screen"
    assert "37" in live[0].value or "38" in live[0].value


# --------------------------------------------------------------------------
# finished — the payoff screen
# --------------------------------------------------------------------------


def _finished(tmp_path, **kw):
    from maxrescue.ui.presenter import Outcome

    state = _surveyed(tmp_path)
    defaults = dict(
        output_path=r"D:\jobs\villa\villa_rescued.max",
        open_rss_mb=42 * 1024,
        peak_rss_mb=50 * 1024,
        vram_mb=9 * 1024,
        objects_merged=47_213,
        objects_requested=47_213,
        textures_reduced=118,
        verified=None,
        problems=(),
    )
    defaults.update(kw)
    return AppState(path=state.path, survey=state.survey, outcome=Outcome(**defaults))


def test_when_finished_it_answers_what_machine_this_runs_on(tmp_path):
    view = present(_finished(tmp_path))
    assert view.phase is Phase.DONE
    # What is on screen, not which half of the row it sits in.
    onscreen = " ".join(f"{f.label} {f.value}" for f in view.facts)
    assert "RAM" in onscreen
    assert "128 GB" in onscreen


def test_it_states_the_measured_figure_separately_from_the_recommendation(tmp_path):
    view = present(_finished(tmp_path))
    joined = " ".join(f"{f.label} {f.value}" for f in view.facts)
    assert "42" in joined, "the measured open figure"
    assert "128" in joined, "the recommended machine"


def test_the_output_file_is_named_so_it_can_be_found(tmp_path):
    view = present(_finished(tmp_path))
    assert "villa_rescued.max" in view.detail or any(
        "villa_rescued.max" in f.value for f in view.facts
    )


def test_texture_reductions_are_disclosed_because_they_change_the_render(tmp_path):
    view = present(_finished(tmp_path))
    joined = " ".join(f"{f.label} {f.value}" for f in view.facts).lower()
    assert "texture" in joined
    assert "118" in joined


def test_a_verified_run_says_so_prominently(tmp_path):
    view = present(_finished(tmp_path, verified=True))
    assert "identical" in view.headline.lower()
    assert not view.warnings


def test_an_unverified_run_does_not_claim_to_be_verified(tmp_path):
    view = present(_finished(tmp_path, verified=None))
    assert "identical" not in view.headline.lower()
    assert any("not" in w.lower() and "verif" in w.lower() for w in view.warnings)


def test_a_failed_verification_is_the_loudest_thing_on_screen(tmp_path):
    view = present(_finished(tmp_path, verified=False))
    assert "differ" in view.headline.lower()
    assert view.warnings


def test_objects_that_never_arrived_are_reported_not_buried(tmp_path):
    view = present(_finished(tmp_path, objects_merged=47_000, objects_requested=47_213))
    assert any("213" in w for w in view.warnings)


def test_a_clean_complete_run_has_nothing_to_warn_about(tmp_path):
    view = present(_finished(tmp_path, verified=True))
    assert view.warnings == ()
