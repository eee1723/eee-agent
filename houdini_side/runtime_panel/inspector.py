"""Inspector pane: Run / Workspace / Artifacts tabs."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme

_MAX_ROWS = 200


def _text_view(parent: QtWidgets.QWidget) -> QtWidgets.QPlainTextEdit:
    view = QtWidgets.QPlainTextEdit()
    view.setReadOnly(True)
    view.setFont(QtGui.QFont(theme.mono_font_family()))
    return view


class InspectorPane(QtWidgets.QWidget):
    """Right pane: structured read-only views of runtime state."""

    createWorkspaceRequested = QtCore.Signal()
    inspectWorkspaceRequested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InspectorPane")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)

        self.run_view = _text_view(self)
        self.tabs.addTab(self.run_view, "RUN")
        self.workspace_view = _text_view(self)
        workspace_tab = QtWidgets.QWidget()
        workspace_layout = QtWidgets.QVBoxLayout(workspace_tab)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        actions = QtWidgets.QHBoxLayout()
        self.create_workspace_button = QtWidgets.QPushButton("Create workspace")
        self.create_workspace_button.setAutoDefault(False)
        self.create_workspace_button.clicked.connect(
            self.createWorkspaceRequested)
        self.inspect_workspace_button = QtWidgets.QPushButton("Inspect")
        self.inspect_workspace_button.setAutoDefault(False)
        self.inspect_workspace_button.clicked.connect(
            self.inspectWorkspaceRequested)
        actions.addWidget(self.create_workspace_button)
        actions.addWidget(self.inspect_workspace_button)
        actions.addStretch(1)
        workspace_layout.addLayout(actions)
        workspace_layout.addWidget(self.workspace_view, 1)
        self.tabs.addTab(workspace_tab, "WORKSPACE")
        self.artifacts_view = _text_view(self)
        self.tabs.addTab(self.artifacts_view, "ARTIFACTS")

    @staticmethod
    def _dump(view: QtWidgets.QPlainTextEdit, rows: list[str]) -> None:
        view.setPlainText("\n".join(rows[:_MAX_ROWS]) or "No data.")

    def set_run_snapshot(self, snapshot, activity=()) -> None:
        if not snapshot:
            self._dump(self.run_view, [])
            return
        # Keys follow _validate_run in eee_agent.panel.runtime_state
        # (status / model_snapshot_json / finished_at, not state / model / usage).
        rows = [
            f"run_id: {snapshot.get('run_id', '-')}",
            f"status: {snapshot.get('status', '-')}",
            f"model: {snapshot.get('model_snapshot_json') or '-'}",
            f"started: {snapshot.get('started_at', '-')}",
            f"finished: {snapshot.get('finished_at', '-')}",
        ]
        # Mirror legacy run_activity: show the most recent tool step. Activity
        # items are {type, name, detail} dicts from RuntimePanelState.
        latest = activity[-1] if activity else None
        if type(latest) is dict:
            name = latest.get("name") or "tool"
            detail = latest.get("detail") or ""
            suffix = f" — {detail[:160]}" if detail else ""
            rows.append(f"activity: {name}{suffix}")
        else:
            rows.append("activity: (none)")
        self._dump(self.run_view, rows)

    def set_workspace_facts(self, facts) -> None:
        if not facts:
            self._dump(self.workspace_view, [])
            return
        rows = [f"{key}: {value}" for key, value in sorted(facts.items())]
        self._dump(self.workspace_view, rows)

    def render_artifacts(self, summaries) -> None:
        rows = [
            f"{s.get('state', '-'):16} {s.get('relative_path', '-')}"
            for s in summaries
        ]
        self._dump(self.artifacts_view, rows)

    def render_visions(self, summaries) -> None:
        # Keys follow parse_vision_event in eee_agent.panel.runtime_state
        # (status / accepted, matching the legacy _render_visions).
        rows = [
            f"[vision] {s.get('status', '-')} / "
            f"{'accepted' if s.get('accepted') else 'rejected'}: "
            f"{s.get('report_summary') or '-'}"
            for s in summaries
        ]
        self._dump(self.artifacts_view, rows)
