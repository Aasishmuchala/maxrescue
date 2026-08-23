"""Render every screen of the app offscreen, as PNGs.

A layout argued in prose is a layout nobody has looked at. This draws each phase
with real data and saves it, so the design can be reviewed — and so a change
that wrecks the spacing is visible in a diff rather than discovered by a user.

    python scripts/ui_proof.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from maxrescue.ui.presenter import AppState, Outcome, Progress  # noqa: E402
from maxrescue.ui.window import MaxRescueWindow  # noqa: E402
from maxrescue.xray.report import xray  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def _scene(tmp: str, **kw) -> str:
    from tests.test_integration import _villa
    import pathlib

    return str(_villa(pathlib.Path(tmp), **kw))


def main() -> int:
    import tempfile

    app = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp()
    window = MaxRescueWindow()
    window.resize(760, 660)

    path = _scene(tmp)
    survey = xray(path)
    surveyed = AppState(path=path, survey=survey)

    screens = {
        "ui_1_empty": AppState(),
        "ui_2_surveyed": surveyed,
        "ui_3_running": AppState(
            path=path,
            survey=survey,
            progress=Progress(
                fraction=0.42,
                message="batch 5 of 12 — merging 1,240 objects",
                resident_mb=38_400,
            ),
        ),
        "ui_4_done": AppState(
            path=path,
            survey=survey,
            outcome=Outcome(
                output_path=r"D:\jobs\villa\villa_rescued.max",
                open_rss_mb=42 * 1024,
                peak_rss_mb=50 * 1024,
                vram_mb=9 * 1024,
                objects_merged=47_213,
                objects_requested=47_213,
                textures_reduced=118,
                verified=True,
            ),
        ),
        "ui_5_unverified": AppState(
            path=path,
            survey=survey,
            outcome=Outcome(
                output_path=r"D:\jobs\villa\villa_rescued.max",
                open_rss_mb=61 * 1024,
                peak_rss_mb=74 * 1024,
                objects_merged=47_000,
                objects_requested=47_213,
                textures_reduced=118,
                verified=None,
            ),
        ),
    }

    os.makedirs(OUT, exist_ok=True)
    for name, state in screens.items():
        window.apply(state)
        window.show()
        app.processEvents()
        target = os.path.join(OUT, f"{name}.png")
        window.grab().save(target)
        print("wrote", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
