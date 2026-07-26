"""Read-only task graph summary block for the Runtime panel."""

from __future__ import annotations

from PySide6 import QtWidgets


class TaskGraphPanel(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._title = QtWidgets.QLabel("Task graph")
        layout.addWidget(self._title)
        self._view = QtWidgets.QTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumHeight(160)
        layout.addWidget(self._view)
        self.set_steps(())

    def set_steps(self, steps) -> None:
        from eee_agent.panel.runtime_state import format_task_graph_steps

        self._view.setPlainText(format_task_graph_steps(tuple(steps)))
