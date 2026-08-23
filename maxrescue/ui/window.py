"""The window.

It draws a :class:`ViewModel` and does nothing else — no decisions, no wording,
no branching on scene contents. That is what lets every sentence the user reads
be tested without Qt, and what lets this file be checked by rendering it
offscreen rather than by clicking it.

Layout follows the shape of the task rather than the shape of the data:

    ┌ drop zone ────────────────┐   large when empty, a thin strip once loaded
    ├ headline + detail ────────┤   what is wrong, in plain language
    ├ facts ────────────────────┤   size, objects, plugins, heaviest
    ├ warnings ─────────────────┤   only when there are any
    ├ settings ─────────────────┤   two numbers, both with sane defaults
    └ one button ───────────────┘   the whole job

One window, one button, no tabs, no wizard. The scene is dropped and the
diagnosis appears immediately — the X-ray needs no 3ds Max, so there is no
reason to make anyone wait or press anything to see what is wrong.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from maxrescue.ui.presenter import AppState, Phase, ViewModel, present

__all__ = ["MaxRescueWindow", "STYLE"]

INK = "#e9e6f5"
MUTED = "#8f8aa6"
ACCENT = "#c6bfff"
WARN = "#ffb4a2"
BACKGROUND = "#141118"
PANEL = "#1c1824"
EDGE = "#2a2436"

STYLE = f"""
QWidget {{
    background: {BACKGROUND};
    color: {INK};
    font-family: 'Segoe UI', 'Inter', 'Manrope', sans-serif;
    font-size: 13px;
}}
QLabel {{
    /* Without this, every label paints the window background over whatever
       panel it sits in, and each row reads as a grey block. */
    background: transparent;
}}
QLabel#headline {{
    font-size: 25px;
    font-weight: 600;
    color: {INK};
}}
QLabel#detail  {{ color: {MUTED}; font-size: 13px; }}
QLabel#factLabel {{ color: {MUTED}; }}
QLabel#factValue {{ color: {INK}; font-weight: 500; }}
QLabel#warning {{
    color: {WARN};
    background: rgba(255, 180, 162, 0.09);
    border-radius: 6px;
    border-left: 2px solid {WARN};
    padding: 9px 12px;
}}
QFrame#drop {{
    background: {PANEL};
    border: 1px dashed {EDGE};
    border-radius: 10px;
}}
QFrame#panel {{
    background: {PANEL};
    border: 1px solid {EDGE};
    border-radius: 10px;
}}
QPushButton#primary {{
    background: {ACCENT};
    color: #1a1526;
    border: none;
    border-radius: 8px;
    padding: 15px;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
QPushButton#primary:disabled {{ background: {EDGE}; color: {MUTED}; }}
QPushButton#primary:hover:enabled {{ background: #d6d0ff; }}
QPushButton#ghost {{
    background: transparent;
    color: {MUTED};
    border: 1px solid {EDGE};
    border-radius: 7px;
    padding: 8px 15px;
}}
QSpinBox {{
    background: {BACKGROUND};
    border: 1px solid {EDGE};
    border-radius: 6px;
    padding: 6px 8px;
    color: {INK};
}}
QProgressBar {{
    background: {BACKGROUND};
    border: 1px solid {EDGE};
    border-radius: 6px;
    height: 8px;
    text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
"""


class MaxRescueWindow(QWidget):
    """One window. Drop a scene, read what is wrong, press one button."""

    #: (path, ceiling_gb, texture_floor_px)
    rescue_requested = Signal(str, int, int)
    file_chosen = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MaxRescue")
        self.setMinimumSize(720, 620)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE)

        self._state = AppState()
        self._build()
        self.apply(AppState())

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 26, 26, 26)
        outer.setSpacing(18)

        self.drop = QFrame(objectName="drop")
        drop_layout = QVBoxLayout(self.drop)
        drop_layout.setContentsMargins(20, 18, 20, 18)
        self.drop_text = QLabel("Drop a .max file here", alignment=Qt.AlignCenter)
        self.drop_text.setObjectName("headline")
        self.drop_hint = QLabel("or click to browse", alignment=Qt.AlignCenter)
        self.drop_hint.setObjectName("detail")
        drop_layout.addWidget(self.drop_text)
        drop_layout.addWidget(self.drop_hint)
        self.drop.mousePressEvent = lambda _event: self._browse()
        outer.addWidget(self.drop)

        self.headline = QLabel(objectName="headline", wordWrap=True)
        self.detail = QLabel(objectName="detail", wordWrap=True)
        outer.addWidget(self.headline)
        outer.addWidget(self.detail)

        self.facts_panel = QFrame(objectName="panel")
        self.facts_grid = QGridLayout(self.facts_panel)
        self.facts_grid.setContentsMargins(18, 16, 18, 16)
        self.facts_grid.setHorizontalSpacing(22)
        self.facts_grid.setVerticalSpacing(9)
        self.facts_grid.setColumnStretch(1, 1)
        outer.addWidget(self.facts_panel)

        self.warnings_box = QVBoxLayout()
        self.warnings_box.setSpacing(8)
        outer.addLayout(self.warnings_box)

        outer.addStretch(1)

        self.settings_panel = QWidget()
        self.settings_row = QHBoxLayout(self.settings_panel)
        self.settings_row.setContentsMargins(0, 0, 0, 0)
        self.settings_row.setSpacing(10)
        self.settings_row.addWidget(QLabel("Fit into"))
        self.ceiling = QSpinBox()
        self.ceiling.setRange(8, 2048)
        self.ceiling.setValue(70)
        self.ceiling.setSuffix(" GB")
        self.settings_row.addWidget(self.ceiling)
        self.settings_row.addSpacing(18)
        self.settings_row.addWidget(QLabel("Textures no smaller than"))
        self.texture_floor = QSpinBox()
        # 4096 is the agreed limit of what may be lost; the control cannot go
        # under it, so the guarantee is visible rather than only enforced.
        self.texture_floor.setRange(4096, 16384)
        self.texture_floor.setSingleStep(2048)
        self.texture_floor.setValue(4096)
        self.texture_floor.setSuffix(" px")
        self.settings_row.addWidget(self.texture_floor)
        self.settings_row.addStretch(1)
        outer.addWidget(self.settings_panel)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        outer.addWidget(self.progress)

        self.button = QPushButton("Rescue this scene", objectName="primary")
        self.button.clicked.connect(self._on_button)
        outer.addWidget(self.button)

    # -- rendering ---------------------------------------------------------

    def apply(self, state: AppState) -> None:
        """Draw a state. The only entry point — tests drive this directly."""
        self._state = state
        view: ViewModel = present(state)

        empty = view.phase is Phase.EMPTY
        self.drop.setVisible(True)
        self.drop_text.setVisible(empty)
        self.drop_hint.setText(
            "or click to browse"
            if empty
            else Path(state.path or "").name or "no file"
        )

        self.headline.setText(view.headline if not empty else "")
        self.headline.setVisible(not empty)
        self.detail.setText(view.detail)
        self.detail.setVisible(bool(view.detail))

        self._render_facts(view)
        self._render_warnings(view)

        running = view.phase is Phase.RUNNING
        # Visible while there is still a decision to make; gone once the run has
        # answered it, so the finished screen is only the result.
        self.settings_panel.setVisible(view.phase in (Phase.SURVEYED, Phase.RUNNING))
        self.settings_panel.setEnabled(view.phase is Phase.SURVEYED)
        self.progress.setVisible(running or view.phase is Phase.DONE)
        self.progress.setValue(int((view.progress or 0.0) * 100))

        self.button.setText(view.rescue_label or "Rescue this scene")
        self.button.setEnabled(view.can_rescue or view.phase is Phase.DONE)
        self.button.setVisible(not empty)

    def _clear(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

    def _render_facts(self, view: ViewModel) -> None:
        self._clear(self.facts_grid)
        for row, fact in enumerate(view.facts):
            label = QLabel(fact.label, objectName="factLabel")
            value = QLabel(fact.value, objectName="factValue", wordWrap=True)
            self.facts_grid.addWidget(label, row, 0, Qt.AlignTop)
            self.facts_grid.addWidget(value, row, 1)
        self.facts_panel.setVisible(bool(view.facts))

    def _render_warnings(self, view: ViewModel) -> None:
        self._clear(self.warnings_box)
        for text in view.warnings:
            self.warnings_box.addWidget(
                QLabel(text, objectName="warning", wordWrap=True)
            )

    # -- input -------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._dropped_path(event) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        path = self._dropped_path(event)
        if path:
            event.acceptProposedAction()
            self.file_chosen.emit(path)

    @staticmethod
    def _dropped_path(event) -> str | None:
        data = event.mimeData()
        if not data.hasUrls():
            return None
        for url in data.urls():
            path = url.toLocalFile()
            if path.lower().endswith(".max"):
                return path
        return None

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a 3ds Max scene", "", "3ds Max scenes (*.max)"
        )
        if path:
            self.file_chosen.emit(path)

    def _on_button(self) -> None:
        view = present(self._state)
        if view.phase is Phase.DONE:
            self.apply(AppState())
            return
        if self._state.path and view.can_rescue:
            self.rescue_requested.emit(
                self._state.path, self.ceiling.value(), self.texture_floor.value()
            )


def main() -> int:  # pragma: no cover - the real entry point
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 10))
    window = MaxRescueWindow()
    window.show()
    return app.exec()
