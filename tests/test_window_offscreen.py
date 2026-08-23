"""The window, rendered offscreen.

A GUI nobody can run in CI rots. This drives the real widget with
`QT_QPA_PLATFORM=offscreen` — the pattern MaxSlim proved, where it caught a
panel that opened expanded and an `isVisible()`/`isVisibleTo()` trap.

The checks are about what a user would notice: does the diagnosis appear, does
the button say the right thing, can it be pressed twice, does a warning show.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from maxrescue.ui.presenter import AppState, Outcome, Progress  # noqa: E402
from maxrescue.ui.window import MaxRescueWindow  # noqa: E402
from maxrescue.xray.report import xray  # noqa: E402
from tests.test_integration import _villa  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    widget = MaxRescueWindow()
    yield widget
    widget.deleteLater()


def _surveyed(tmp_path, **kw) -> AppState:
    path = str(_villa(tmp_path, **kw))
    return AppState(path=path, survey=xray(path))


def _labels(window) -> str:
    from PySide6.QtWidgets import QLabel

    return " ".join(child.text() for child in window.findChildren(QLabel))


# ---------------------------------------------------------------------------
# empty
# ---------------------------------------------------------------------------


def test_it_opens_asking_for_a_file_and_offering_nothing_else(window):
    assert "Drop a .max file here" in _labels(window)
    assert not window.button.isVisible() or not window.button.isEnabled()


def test_the_facts_panel_is_hidden_before_there_is_anything_to_say(window):
    assert window.facts_panel.isVisibleTo(window) is False


# ---------------------------------------------------------------------------
# surveyed
# ---------------------------------------------------------------------------


def test_dropping_a_scene_shows_the_diagnosis_immediately(window, tmp_path):
    window.apply(_surveyed(tmp_path))
    text = _labels(window)
    assert "One object is most of this scene" in text
    assert "Villa_Hero" in text
    assert "ForestPackPro.dlo" in text


def test_the_button_becomes_pressable_once_a_scene_is_loaded(window, tmp_path):
    window.apply(_surveyed(tmp_path))
    assert window.button.isEnabled()
    assert "Rescue" in window.button.text()


def test_pressing_rescue_asks_for_the_run_with_the_chosen_settings(window, tmp_path):
    state = _surveyed(tmp_path)
    window.apply(state)
    window.ceiling.setValue(96)
    captured = []
    window.rescue_requested.connect(lambda *args: captured.append(args))
    window.button.click()
    assert captured == [(state.path, 96, 4096)]


def test_the_texture_control_cannot_be_set_below_the_agreed_floor(window):
    """4K is the limit of what may be lost, so the control itself enforces it —
    the guarantee is visible, not merely applied somewhere out of sight."""
    window.texture_floor.setValue(512)
    assert window.texture_floor.value() == 4096


def test_an_infected_file_cannot_be_started(window, tmp_path):
    window.apply(_surveyed(tmp_path, malware=True))
    assert window.button.isEnabled() is False
    assert "malicious" in _labels(window).lower()


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def test_while_running_the_button_will_not_start_a_second_run(window, tmp_path):
    state = _surveyed(tmp_path)
    window.apply(
        AppState(
            path=state.path,
            survey=state.survey,
            progress=Progress(fraction=0.35, message="batch 3 of 12", resident_mb=38_000),
        )
    )
    assert window.button.isEnabled() is False
    assert window.progress.value() == 35
    assert "batch 3 of 12" in _labels(window)


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


def _finished(tmp_path, **kw) -> AppState:
    state = _surveyed(tmp_path)
    defaults = dict(
        output_path=r"D:\jobs\villa\villa_rescued.max",
        open_rss_mb=42 * 1024,
        peak_rss_mb=50 * 1024,
        vram_mb=9 * 1024,
        objects_merged=47_213,
        objects_requested=47_213,
        textures_reduced=118,
        verified=True,
    )
    defaults.update(kw)
    return AppState(path=state.path, survey=state.survey, outcome=Outcome(**defaults))


def test_the_finished_screen_answers_what_machine_this_runs_on(window, tmp_path):
    window.apply(_finished(tmp_path))
    text = _labels(window)
    assert "128 GB RAM" in text
    assert "42.0 GB" in text
    assert "villa_rescued.max" in text


def test_a_verified_run_says_so_and_shows_no_warning(window, tmp_path):
    window.apply(_finished(tmp_path, verified=True))
    assert "identical" in _labels(window).lower()
    assert window.warnings_box.count() == 0


def test_an_unverified_run_carries_a_visible_caveat(window, tmp_path):
    window.apply(_finished(tmp_path, verified=None))
    assert window.warnings_box.count() >= 1
    assert "not verified" in _labels(window).lower()


def test_the_button_resets_for_the_next_scene(window, tmp_path):
    window.apply(_finished(tmp_path))
    assert "another" in window.button.text().lower()
    window.button.click()
    assert "Drop a .max file here" in _labels(window)


def test_warnings_do_not_survive_a_reset(window, tmp_path):
    window.apply(_finished(tmp_path, verified=False))
    assert window.warnings_box.count() >= 1
    window.apply(AppState())
    assert window.warnings_box.count() == 0
