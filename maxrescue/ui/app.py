"""The controller: window ↔ worker.

Two jobs, both about not blocking the interface.

**The X-ray runs on a background thread.** It needs no 3ds Max and usually takes
under a second, but "usually" is doing work there — a damaged multi-gigabyte
`Scene` stream can take much longer, and a frozen window during it looks
identical to a crash.

**The rescue certainly does.** A sibling project ran its work on the UI thread
and froze Max on production-scale scenes. Here it runs in a `QThread` and
progress is read by polling the JSON report the on-box script flushes after
every batch — no IPC to get wrong, and the file survives a crash, so a run that
dies still leaves everything it had done.
"""

from __future__ import annotations

import json
import os
import tempfile

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from maxrescue.ui.presenter import AppState, Outcome, Progress
from maxrescue.ui.runner import (
    MaxInstall,
    RescueRequest,
    RunnerError,
    find_max_installs,
    run_rescue,
)
from maxrescue.ui.window import MaxRescueWindow
from maxrescue.xray.ole import MaxFileError
from maxrescue.xray.report import xray

__all__ = ["Controller", "RescueWorker", "SurveyWorker", "main"]

#: How often to re-read the progress report while a rescue runs.
POLL_MS = 900


class SurveyWorker(QThread):
    """Reads a `.max` off the UI thread."""

    surveyed = Signal(object)
    failed = Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            self.surveyed.emit(xray(self._path))
        except MaxFileError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - the window must hear about it
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class RescueWorker(QThread):
    """Runs one rescue to completion."""

    finished_with = Signal(object)
    failed = Signal(str)

    def __init__(self, request: RescueRequest, install: MaxInstall):
        super().__init__()
        self._request = request
        self._install = install

    def run(self) -> None:
        try:
            self.finished_with.emit(run_rescue(self._request, self._install))
        except RunnerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class Controller(QObject):
    """Owns the window and the workers, and nothing else."""

    def __init__(self, window: MaxRescueWindow | None = None):
        super().__init__()
        self.window = window or MaxRescueWindow()
        self.state = AppState()
        self._survey: SurveyWorker | None = None
        self._rescue: RescueWorker | None = None
        self._work_dir = ""

        self.window.file_chosen.connect(self.open_scene)
        self.window.rescue_requested.connect(self.start_rescue)

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._read_progress)

    # -- survey ------------------------------------------------------------

    def open_scene(self, path: str) -> None:
        self.state = AppState(path=path)
        self.window.apply(self.state)
        self._survey = SurveyWorker(path)
        self._survey.surveyed.connect(self._on_surveyed)
        self._survey.failed.connect(self._on_survey_failed)
        self._survey.start()

    def _on_surveyed(self, survey) -> None:
        self.state = AppState(path=self.state.path, survey=survey)
        self.window.apply(self.state)

    def _on_survey_failed(self, message: str) -> None:
        self.state = AppState()
        self.window.apply(self.state)
        QMessageBox.warning(self.window, "Could not read that file", message)

    # -- rescue ------------------------------------------------------------

    def start_rescue(self, path: str, ceiling_gb: int, texture_floor_px: int) -> None:
        installs = find_max_installs()
        if not installs:
            QMessageBox.warning(
                self.window,
                "3ds Max not found",
                "No 3ds Max installation with 3dsmaxbatch.exe was found.\n\n"
                "The diagnosis above needed no 3ds Max, but rescuing a scene "
                "does.",
            )
            return

        stem, _ = os.path.splitext(path)
        self._work_dir = os.path.join(
            tempfile.gettempdir(), f"maxrescue_{os.path.basename(stem)}"
        )
        request = RescueRequest(
            scene=path,
            output=f"{stem}_rescued.max",
            work_dir=self._work_dir,
            ceiling_gb=ceiling_gb,
            texture_floor_px=texture_floor_px,
        )

        self.state = AppState(
            path=self.state.path,
            survey=self.state.survey,
            progress=Progress(fraction=0.0, message=f"starting {installs[0].label}"),
        )
        self.window.apply(self.state)

        self._rescue = RescueWorker(request, installs[0])
        self._rescue.finished_with.connect(self._on_finished)
        self._rescue.failed.connect(self._on_failed)
        self._rescue.start()
        self._poll.start()

    def _read_progress(self) -> None:
        """Follow the report the on-box script flushes after each batch."""
        if not self._work_dir:
            return
        try:
            with open(
                os.path.join(self._work_dir, "rescue_report.json"), encoding="utf-8"
            ) as handle:
                payload = json.load(handle)
        except Exception:
            return

        log = payload.get("log") or []
        batches = payload.get("batches") or []
        message = log[-1] if log else "working"
        resident = 0.0
        if batches:
            resident = float(batches[-1].get("peak_rss_mb") or 0)

        # Deliberately not a percentage of anything: the batch count is not
        # known until the plan settles, and a bar that jumps backwards is worse
        # than one that only ever creeps forward.
        fraction = min(0.95, 0.05 + 0.05 * len(batches))
        self.state = AppState(
            path=self.state.path,
            survey=self.state.survey,
            progress=Progress(fraction=fraction, message=message, resident_mb=resident),
        )
        self.window.apply(self.state)

    def _on_finished(self, outcome: Outcome) -> None:
        self._poll.stop()
        self.state = AppState(
            path=self.state.path, survey=self.state.survey, outcome=outcome
        )
        self.window.apply(self.state)

    def _on_failed(self, message: str) -> None:
        self._poll.stop()
        # Back to the surveyed state, not to a success screen. A run that died
        # must never leave the window looking like it worked.
        self.state = AppState(path=self.state.path, survey=self.state.survey)
        self.window.apply(self.state)
        QMessageBox.critical(self.window, "The rescue did not finish", message)

    def show(self) -> None:
        self.window.show()


def main() -> int:  # pragma: no cover - the real entry point
    app = QApplication.instance() or QApplication([])
    controller = Controller()
    controller.show()
    return app.exec()
