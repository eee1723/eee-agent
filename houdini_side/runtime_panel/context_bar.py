"""Top context bar: HIP > Session > Workspace and Runtime|Bridge|Run state."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from houdini_side.runtime_panel import theme
from houdini_side.runtime_panel.view_models import ContextStatus

_STATUS_TONES = {
    "online": theme.STATUS_OK,
    "ready": theme.STATUS_OK,
    "connecting": theme.STATUS_WARN,
    "reconnecting": theme.STATUS_WARN,
    "unavailable": theme.STATUS_ERROR,
    "offline": theme.STATUS_ERROR,
    "error": theme.STATUS_ERROR,
}


class ContextBar(QtWidgets.QWidget):
    """Two-row status strip with sidebar/inspector drawer toggles."""

    sidebarToggled = QtCore.Signal(bool)
    inspectorToggled = QtCore.Signal(bool)
    autoExecuteToggled = QtCore.Signal(bool)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ContextBar")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 4)
        root.setSpacing(2)

        location = QtWidgets.QHBoxLayout()
        self.hip_label = QtWidgets.QLabel("no hip")
        self.hip_label.setObjectName("ProminentLabel")
        self.session_label = QtWidgets.QLabel("no session")
        self.workspace_label = QtWidgets.QLabel("no workspace")
        self.workspace_label.setObjectName("DimLabel")
        location.addWidget(self.hip_label)
        location.addWidget(QtWidgets.QLabel(">"))
        location.addWidget(self.session_label)
        location.addWidget(QtWidgets.QLabel(">"))
        location.addWidget(self.workspace_label)
        location.addStretch(1)
        self.sidebar_button = QtWidgets.QToolButton()
        self.sidebar_button.setText("Sessions")
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(True)
        self.sidebar_button.toggled.connect(self.sidebarToggled)
        self.inspector_button = QtWidgets.QToolButton()
        self.inspector_button.setText("Inspector")
        self.inspector_button.setCheckable(True)
        self.inspector_button.setChecked(True)
        self.inspector_button.toggled.connect(self.inspectorToggled)
        # Auto-execute: when checked, proposals approve+apply immediately
        # without the manual gate (exact-digest path is reused unchanged).
        self.auto_button = QtWidgets.QToolButton()
        self.auto_button.setText("Auto")
        self.auto_button.setToolTip(
            "Auto-execute: approve and apply proposals immediately "
            "(no manual gate). The exact-digest approval path is reused.")
        self.auto_button.setCheckable(True)
        self.auto_button.setChecked(False)
        self.auto_button.toggled.connect(self.autoExecuteToggled)
        location.addWidget(self.sidebar_button)
        location.addWidget(self.inspector_button)
        location.addWidget(self.auto_button)
        root.addLayout(location)

        states = QtWidgets.QHBoxLayout()
        states.setSpacing(14)
        self.runtime_label = QtWidgets.QLabel()
        self.bridge_label = QtWidgets.QLabel()
        self.run_label = QtWidgets.QLabel()
        # Block C: a model chip + live token total so the user can see which
        # model is driving the run and how many tokens it has used, without
        # opening the inspector. Mirrors the Pi project's context-panel model
        # row.
        self.model_label = QtWidgets.QLabel()
        self.model_label.setObjectName("StateLabel")
        self.tokens_label = QtWidgets.QLabel()
        self.tokens_label.setObjectName("DimLabel")
        for label in (self.runtime_label, self.bridge_label, self.run_label,
                      self.model_label, self.tokens_label):
            label.setObjectName("StateLabel")
            states.addWidget(label)
        states.addStretch(1)
        root.addLayout(states)

    def set_status(self, status: ContextStatus) -> None:
        self.hip_label.setText(status.hip)
        self.session_label.setText(status.session)
        self.workspace_label.setText(status.workspace)
        self._set_state(self.runtime_label, "Runtime", status.runtime)
        self._set_state(self.bridge_label, "Bridge", status.bridge)
        self.run_label.setText(f"Run: {status.run_state}")
        # Block C: model chip (hidden when no run selected) + token total.
        if status.model:
            self.model_label.setText(f"Model: {status.model}")
            self.model_label.show()
        else:
            self.model_label.hide()
        if status.total_tokens is not None:
            self.tokens_label.setText(f"Tokens: {status.total_tokens:,}")
            self.tokens_label.show()
        else:
            self.tokens_label.hide()

    @staticmethod
    def _set_state(label: QtWidgets.QLabel, name: str, value: str) -> None:
        color = _STATUS_TONES.get(value, theme.FG_DIM)
        label.setText(f'{name}: <span style="color:{color}">{value}</span>')
