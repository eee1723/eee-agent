"""Approval gate drawer: interrupts the conversation for a pending ChangeSet."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from eee_agent.panel.runtime_state import approval_is_actionable
from houdini_side.runtime_panel import theme

_MAX_PATH_ROWS = 8


class ApprovalDrawer(QtWidgets.QFrame):
    """Right-anchored amber gate; Approve stays bound to the exact digest."""

    approved = QtCore.Signal()
    rejected = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ApprovalDrawer")
        self.setStyleSheet(
            f"QFrame#ApprovalDrawer {{ background: {theme.SURFACE_2};"
            f" border: 1px solid {theme.HIGHLIGHT}; border-radius: 5px; }}")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        self.headline = QtWidgets.QLabel("GATE / AWAITING APPROVAL")
        self.headline.setObjectName("ProminentLabel")
        self.headline.setStyleSheet(f"color: {theme.HIGHLIGHT};")
        layout.addWidget(self.headline)

        self.summary = QtWidgets.QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.paths = QtWidgets.QLabel("")
        self.paths.setFont(QtGui.QFont(theme.mono_font_family()))
        self.paths.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.paths)

        buttons = QtWidgets.QHBoxLayout()
        self.reject_button = QtWidgets.QPushButton("Reject")
        self.reject_button.setAutoDefault(False)
        self.reject_button.clicked.connect(
            lambda: self._emit_if_actionable(self.rejected))
        self.approve_button = QtWidgets.QPushButton("Approve and build")
        self.approve_button.setObjectName("GateApprove")
        self.approve_button.setAutoDefault(False)
        self.approve_button.clicked.connect(
            lambda: self._emit_if_actionable(self.approved))
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)
        self._actionable = False
        self._summary = None
        self.hide()

    def show_changeset(self, summary) -> None:
        """Render one actionable ChangeSet summary from the client."""
        self._summary = summary
        self._actionable = approval_is_actionable(summary)
        risk = summary.get("risk") or {}
        title = summary.get("title") or "ChangeSet"
        count = risk.get("operation_count")
        mode = summary.get("required_permission") or "unknown"
        self.summary.setText(f"{title} · {count} operations · {mode}")
        paths = list(risk.get("affected_paths") or [])
        rows = [str(p) for p in paths[:_MAX_PATH_ROWS]]
        if risk.get("affected_paths_truncated"):
            rows.append(
                f"+ {risk.get('affected_path_count', 0) - len(paths)} more paths")
        self.paths.setText("\n".join(rows))
        self.approve_button.setEnabled(self._actionable)
        self.reject_button.setEnabled(self._actionable)
        self.show()
        self.raise_()

    def hide_drawer(self) -> None:
        self._actionable = False
        self._summary = None
        self.hide()

    def _emit_if_actionable(self, signal) -> None:
        """Re-check the stored summary at click time before emitting."""
        summary = self._summary
        if not self._actionable or summary is None:
            return
        if not approval_is_actionable(summary):
            return
        signal.emit()

    @property
    def actionable(self) -> bool:
        return self._actionable
