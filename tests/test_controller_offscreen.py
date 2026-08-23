"""The controller, driven offscreen.

The wiring between a click and a finished scene. What matters is that nothing
here can leave the window looking like it worked when it did not.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from maxrescue.ui.app import Controller  # noqa: E402
from maxrescue.ui.presenter import Outcome, Phase, present  # noqa: E402
from tests.test_integration import _villa  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(app):
    made = Controller()
    yield made
    made.window.deleteLater()


def _labels(window) -> str:
    return " ".join(child.text() for child in window.findChildren(QLabel))


def test_opening_a_scene_surveys_it_and_shows_the_diagnosis(controller, tmp_path):
    controller.open_scene(str(_villa(tmp_path)))
    controller._survey.wait(10_000)
    app_ = QApplication.instance()
    app_.processEvents()
    assert present(controller.state).phase is Phase.SURVEYED
    assert "Villa_Hero" in _labels(controller.window)


def test_an_unreadable_file_leaves_the_window_empty_not_half_loaded(
    controller, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "maxrescue.ui.app.QMessageBox.warning", lambda *a, **k: None
    )
    bad = tmp_path / "notamax.max"
    bad.write_bytes(b"definitely not a compound file")
    controller.open_scene(str(bad))
    controller._survey.wait(10_000)
    QApplication.instance().processEvents()
    assert present(controller.state).phase is Phase.EMPTY


def test_rescuing_without_3ds_max_says_so_rather_than_failing_silently(
    controller, tmp_path, monkeypatch
):
    shown = []
    monkeypatch.setattr("maxrescue.ui.app.find_max_installs", lambda: [])
    monkeypatch.setattr(
        "maxrescue.ui.app.QMessageBox.warning",
        lambda parent, title, text: shown.append((title, text)),
    )
    controller.start_rescue(str(_villa(tmp_path)), 70, 4096)
    assert shown and "not found" in shown[0][0].lower()
    assert "needed no 3ds Max" in shown[0][1]


def test_a_failed_run_returns_to_the_diagnosis_never_to_a_success_screen(
    controller, tmp_path, monkeypatch
):
    """The failure this wiring exists to prevent."""
    from maxrescue.xray.report import xray
    from maxrescue.ui.presenter import AppState

    path = str(_villa(tmp_path))
    controller.state = AppState(path=path, survey=xray(path))
    monkeypatch.setattr(
        "maxrescue.ui.app.QMessageBox.critical", lambda *a, **k: None
    )
    controller._on_failed("3ds Max ran out of memory")
    view = present(controller.state)
    assert view.phase is Phase.SURVEYED
    assert "Done" not in view.headline


def test_progress_is_read_from_the_report_the_script_flushes(controller, tmp_path):
    from maxrescue.xray.report import xray
    from maxrescue.ui.presenter import AppState

    path = str(_villa(tmp_path))
    controller.state = AppState(path=path, survey=xray(path))
    controller._work_dir = str(tmp_path)
    (tmp_path / "rescue_report.json").write_text(
        json.dumps(
            {
                "log": ["batch 4: merged 1,200/1,200 (+3,400 MB)"],
                "batches": [{"peak_rss_mb": 38_400}] * 4,
            }
        )
    )
    controller._read_progress()
    view = present(controller.state)
    assert view.phase is Phase.RUNNING
    assert "batch 4" in view.detail
    assert view.progress and 0 < view.progress < 1


def test_progress_never_runs_backwards_as_batches_accumulate(controller, tmp_path):
    """A bar that jumps back reads as a fault even when nothing is wrong."""
    from maxrescue.xray.report import xray
    from maxrescue.ui.presenter import AppState

    path = str(_villa(tmp_path))
    controller.state = AppState(path=path, survey=xray(path))
    controller._work_dir = str(tmp_path)
    seen = []
    for count in range(1, 9):
        (tmp_path / "rescue_report.json").write_text(
            json.dumps({"log": ["working"], "batches": [{}] * count})
        )
        controller._read_progress()
        seen.append(present(controller.state).progress)
    assert seen == sorted(seen)


def test_a_missing_report_does_not_disturb_the_current_view(controller, tmp_path):
    controller._work_dir = str(tmp_path / "nothing-here")
    before = controller.state
    controller._read_progress()
    assert controller.state is before


def test_a_finished_run_reaches_the_payoff_screen(controller, tmp_path):
    from maxrescue.xray.report import xray
    from maxrescue.ui.presenter import AppState

    path = str(_villa(tmp_path))
    controller.state = AppState(path=path, survey=xray(path))
    controller._on_finished(
        Outcome(
            output_path=r"D:\villa_rescued.max",
            open_rss_mb=42 * 1024,
            peak_rss_mb=50 * 1024,
            objects_merged=100,
            objects_requested=100,
            verified=True,
        )
    )
    assert present(controller.state).phase is Phase.DONE
    assert "128 GB RAM" in _labels(controller.window)
