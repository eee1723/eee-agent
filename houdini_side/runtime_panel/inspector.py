"""Inspector pane: structured Run / Workspace / Artifacts tabs.

Replaces the legacy key:value text dump (one QPlainTextEdit per tab) with
real Qt widgets built from the view_models dataclasses. All formatting and
normalization lives in view_models (Qt-free, unit-tested); this module only
assembles widgets and reads the structured fields.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme, view_models as vm

# Tone -> stylesheet color token. theme.py already exports these as module
# constants; the QSS engine needs the resolved hex string at runtime.
_TONE_COLORS = {
    "ok": theme.STATUS_OK,
    "warn": theme.STATUS_WARN,
    "error": theme.STATUS_ERROR,
    "normal": theme.FG_DIM,
    "gate": theme.HIGHLIGHT,
}


def _tone_label(text: str, tone: str, *, prominent: bool = False) -> QtWidgets.QLabel:
    """A QLabel whose text color reflects a semantic tone.

    Uses setStyleSheet with theme tokens because QSS does not pick up
    objectName-driven colors for inline status badges.
    """
    label = QtWidgets.QLabel(text)
    color = _TONE_COLORS.get(tone, theme.FG_DIM)
    weight = "600" if prominent else "400"
    label.setStyleSheet(
        f"color: {color}; font-weight: {weight}; background: transparent;"
    )
    label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    return label


def _dim_label(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setObjectName("DimLabel")
    label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    return label


def _prominent_label(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setObjectName("ProminentLabel")
    label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    return label


def _wrap_in_scroll(widget: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    """Wrap a content widget in a non-frame scroll area."""
    scroll = QtWidgets.QScrollArea()
    scroll.setWidget(widget)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    return scroll


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes}m {rest:.0f}s"


class _RunViewWidget(QtWidgets.QFrame):
    """The structured Run tab content widget (rebuilt on every snapshot)."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(8)
        # Placeholder rows; repopulated by update_view().
        self._status_row: QtWidgets.QHBoxLayout | None = None
        self._status_badge: QtWidgets.QLabel | None = None
        self._time_grid: QtWidgets.QGridLayout | None = None
        self._env_widget: QtWidgets.QWidget | None = None
        self._deps_widget: QtWidgets.QWidget | None = None
        self._activity_widget: QtWidgets.QWidget | None = None
        self._outcome_widget: QtWidgets.QWidget | None = None
        self._empty_label = _dim_label("No run selected.")
        self._layout.addWidget(self._empty_label)

    def update_view(self, run_view: vm.RunView | None) -> None:
        # Clear previous content.
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if run_view is None:
            self._layout.addWidget(_dim_label("No run selected."))
            return
        self._build_header(run_view)
        self._build_time_block(run_view)
        if run_view.todos:
            self._build_todos_block(run_view.todos)
        if run_view.environment is not None:
            self._build_environment_block(run_view.environment)
        if run_view.dependencies:
            self._build_dependencies_block(run_view.dependencies)
        if run_view.apply_outcome is not None:
            self._build_outcome_block(run_view.apply_outcome)
        self._build_activity_block(run_view.activity)
        self._layout.addStretch(1)

    def _build_header(self, rv: vm.RunView) -> None:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(_prominent_label(rv.run_id_short), 1)
        self._status_badge = _tone_label(rv.status, rv.status_tone, prominent=True)
        # Badge gets a subtle background tint so the tone reads at a glance.
        bg = _TONE_COLORS.get(rv.status_tone, theme.FG_DIM)
        self._status_badge.setStyleSheet(
            self._status_badge.styleSheet()
            + f" padding: 1px 8px; border-radius: 3px;"
            f" background: {bg}33;"  # 33 = ~20% alpha hex suffix
        )
        row.addWidget(self._status_badge)
        container = QtWidgets.QWidget()
        container.setLayout(row)
        self._layout.addWidget(container)

    def _build_time_block(self, rv: vm.RunView) -> None:
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)
        grid.addWidget(_dim_label("Started"), 0, 0)
        grid.addWidget(_dim_label(rv.started_at), 1, 0)
        grid.addWidget(_dim_label("Finished"), 0, 1)
        grid.addWidget(_dim_label(rv.finished_at), 1, 1)
        grid.addWidget(_dim_label("Duration"), 0, 2)
        grid.addWidget(_prominent_label(_format_duration(rv.duration_seconds)), 1, 2)
        container = QtWidgets.QWidget()
        container.setLayout(grid)
        self._layout.addWidget(container)

    def _build_environment_block(self, env: vm.EnvironmentView) -> None:
        self._layout.addWidget(_dim_label("ENVIRONMENT"))
        form = QtWidgets.QFormLayout()
        form.setSpacing(2)
        form.addRow("eee agent:", _dim_label(env.eee_agent))
        form.addRow("python:", _dim_label(env.python))
        form.addRow("platform:", _dim_label(env.platform))
        form.addRow("houdini build:", _dim_label(env.houdini_build))
        form.addRow("kb schema:", _dim_label(env.kb_schema_version))
        form.addRow("knowledge:", _tone_label(env.knowledge_status, "normal"))
        container = QtWidgets.QWidget()
        container.setLayout(form)
        self._layout.addWidget(container)

    def _build_dependencies_block(
        self, deps: tuple[tuple[str, str], ...]
    ) -> None:
        header_row = QtWidgets.QHBoxLayout()
        header_row.addWidget(_dim_label(f"DEPENDENCIES ({len(deps)})"))
        header_row.addStretch(1)
        header = QtWidgets.QWidget()
        header.setLayout(header_row)
        self._layout.addWidget(header)
        # A 2-column table; QTableWidget gives sortable headers for free.
        table = QtWidgets.QTableWidget(len(deps), 2)
        table.setHorizontalHeaderLabels(["package", "version"])
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        for row, (name, version) in enumerate(deps):
            name_item = QtWidgets.QTableWidgetItem(name)
            name_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            version_item = QtWidgets.QTableWidgetItem(version)
            version_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            table.setItem(row, 0, name_item)
            table.setItem(row, 1, version_item)
        # Fixed height so the table does not eat the whole tab.
        table.setFixedHeight(min(len(deps), 8) * 22 + 30)
        self._layout.addWidget(table)

    def _build_outcome_block(self, outcome: vm.ApplyOutcomeView) -> None:
        self._layout.addWidget(_dim_label("LATEST APPLY OUTCOME"))
        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        bg = _TONE_COLORS.get(outcome.tone, theme.FG_DIM)
        card.setStyleSheet(
            f"QFrame#Card {{ border-left: 3px solid {bg};"
            f" background: {bg}18; border-radius: 3px; }}"
        )
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 6)
        card_layout.setSpacing(2)
        header_row = QtWidgets.QHBoxLayout()
        header_row.addWidget(
            _tone_label(outcome.receipt_status, outcome.tone, prominent=True))
        header_row.addWidget(_dim_label(
            f"{outcome.applied_op_count} op(s) applied"), 1)
        card_layout.addLayout(header_row)
        if outcome.error_code:
            card_layout.addWidget(_tone_label(
                f"code: {outcome.error_code}", outcome.tone))
        if outcome.error_message:
            msg = QtWidgets.QLabel(outcome.error_message)
            msg.setWordWrap(True)
            msg.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            msg.setStyleSheet(f"color: {_TONE_COLORS.get(outcome.tone)};")
            card_layout.addWidget(msg)
        card_layout.addWidget(_dim_label(f"change: {outcome.change_id}"), 0,
                              QtCore.Qt.AlignmentFlag.AlignRight)
        self._layout.addWidget(card)

    def _build_todos_block(self, todos: tuple[vm.TodoItemView, ...]) -> None:
        # D-3: surface the agent's TodoList plan with one row per item and a
        # status marker. The block is rendered above environment/deps so the
        # user sees what the agent is doing before runtime metadata.
        done = sum(1 for t in todos if t.status == "completed")
        active = sum(1 for t in todos if t.status == "in_progress")
        header_row = QtWidgets.QHBoxLayout()
        header_row.addWidget(_dim_label(
            f"PLAN ({done}/{len(todos)} done"
            + (f", {active} active" if active else "")
            + ")"))
        header_row.addStretch(1)
        header = QtWidgets.QWidget()
        header.setLayout(header_row)
        self._layout.addWidget(header)
        for todo in todos:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(6)
            if todo.status == "completed":
                marker, tone = "✓", "ok"
            elif todo.status == "in_progress":
                marker, tone = "▶", "warn"
            else:
                marker, tone = "○", "normal"
            row.addWidget(_tone_label(marker, tone))
            content_label = QtWidgets.QLabel(todo.content)
            content_label.setWordWrap(True)
            content_label.setTextInteractionFlags(
                QtCore.Qt.TextSelectableByMouse)
            # Completed items read as dim (struck-through visually via color);
            # in-progress items read prominent; pending items normal.
            color = _TONE_COLORS.get(tone, theme.FG_DIM)
            weight = "600" if todo.status == "in_progress" else "400"
            content_label.setStyleSheet(
                f"color: {color}; font-weight: {weight}; background: transparent;"
            )
            row.addWidget(content_label, 1)
            line = QtWidgets.QWidget()
            line.setLayout(row)
            self._layout.addWidget(line)

    def _build_activity_block(self, steps: tuple[vm.ActivityStep, ...]) -> None:
        header_row = QtWidgets.QHBoxLayout()
        header_row.addWidget(_dim_label(
            f"ACTIVITY ({len(steps)} step{'s' if len(steps) != 1 else ''})"))
        header_row.addStretch(1)
        header = QtWidgets.QWidget()
        header.setLayout(header_row)
        self._layout.addWidget(header)
        if not steps:
            self._layout.addWidget(_dim_label("(no tool activity yet)"))
            return
        for step in steps:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(6)
            marker = "▶" if step.kind == "tool.started" else "✓"
            tone = "normal" if step.kind == "tool.started" else "ok"
            row.addWidget(_tone_label(marker, tone))
            row.addWidget(_dim_label(step.name), 0)
            if step.detail:
                detail = _dim_label(step.detail[:100])
                row.addWidget(detail, 1)
            else:
                row.addStretch(1)
            line = QtWidgets.QWidget()
            line.setLayout(row)
            self._layout.addWidget(line)


class InspectorPane(QtWidgets.QWidget):
    """Right pane: structured read-only views of runtime state."""

    createWorkspaceRequested = QtCore.Signal()
    inspectWorkspaceRequested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InspectorPane")
        self._artifacts: tuple = ()
        self._visions: tuple = ()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs)

        # RUN tab: structured widgets, no more QPlainTextEdit key:value dump.
        self._run_widget = _RunViewWidget(self)
        self.tabs.addTab(_wrap_in_scroll(self._run_widget), "RUN")

        # WORKSPACE tab: key/value table plus action buttons.
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
        self.workspace_table = QtWidgets.QTableWidget(0, 2)
        self.workspace_table.setHorizontalHeaderLabels(["key", "value"])
        self.workspace_table.verticalHeader().setVisible(False)
        self.workspace_table.horizontalHeader().setStretchLastSection(True)
        self.workspace_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.workspace_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.workspace_table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        workspace_layout.addWidget(self.workspace_table, 1)
        self.tabs.addTab(workspace_tab, "WORKSPACE")

        # ARTIFACTS tab: artifacts + vision evaluations in one table.
        self.artifacts_table = QtWidgets.QTableWidget(0, 4)
        self.artifacts_table.setHorizontalHeaderLabels(
            ["state", "type", "path / status", "size / decision"])
        self.artifacts_table.verticalHeader().setVisible(False)
        self.artifacts_table.horizontalHeader().setStretchLastSection(True)
        self.artifacts_table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.artifacts_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.artifacts_table.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.tabs.addTab(self.artifacts_table, "ARTIFACTS")

    # -- public API (preserved signatures) ---------------------------------

    def set_run_snapshot(self, snapshot, activity=(), *, apply_outcomes=()) -> None:
        """Rebuild the RUN tab from a structured RunView.

        Builds via view_models.run_view, which never raises on malformed
        snapshots; None surfaces the empty-state row.
        """
        run_view = vm.run_view(snapshot, activity, apply_outcomes)
        self._run_widget.update_view(run_view)

    def set_workspace_facts(self, facts) -> None:
        rows = vm.workspace_rows(facts)
        self.workspace_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            name_item = QtWidgets.QTableWidgetItem(row.key)
            name_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            value_item = QtWidgets.QTableWidgetItem(row.value)
            value_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            color = _TONE_COLORS.get(row.tone, theme.FG)
            value_item.setForeground(QtGui.QColor(color))
            self.workspace_table.setItem(i, 0, name_item)
            self.workspace_table.setItem(i, 1, value_item)

    def render_artifacts(self, summaries) -> None:
        # Artifacts and visions share the ARTIFACTS tab (plan Task 9: no fifth
        # tab). Cache both and re-render the combined table.
        self._artifacts = tuple(summaries) if isinstance(summaries, (list, tuple)) else ()
        self._render_artifacts_tab()

    def render_visions(self, summaries) -> None:
        self._visions = tuple(summaries) if isinstance(summaries, (list, tuple)) else ()
        self._render_artifacts_tab()

    def _render_artifacts_tab(self) -> None:
        arts = vm.artifact_rows(self._artifacts)
        visions = vm.vision_rows(self._visions)
        total = len(arts) + len(visions)
        self.artifacts_table.setRowCount(total)
        for i, art in enumerate(arts):
            self._set_artifact_row(i, art)
        offset = len(arts)
        for j, vis in enumerate(visions):
            self._set_vision_row(offset + j, vis)

    def _set_artifact_row(self, row: int, art: vm.ArtifactRow) -> None:
        state_item = QtWidgets.QTableWidgetItem(art.state)
        state_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        state_item.setForeground(QtGui.QColor(_TONE_COLORS.get(art.tone, theme.FG)))
        type_item = QtWidgets.QTableWidgetItem("artifact")
        type_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        path_item = QtWidgets.QTableWidgetItem(art.relative_path)
        path_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        size_item = QtWidgets.QTableWidgetItem(art.size_text)
        size_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        self.artifacts_table.setItem(row, 0, state_item)
        self.artifacts_table.setItem(row, 1, type_item)
        self.artifacts_table.setItem(row, 2, path_item)
        self.artifacts_table.setItem(row, 3, size_item)

    def _set_vision_row(self, row: int, vis: vm.VisionRow) -> None:
        state_item = QtWidgets.QTableWidgetItem(vis.status)
        state_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        state_item.setForeground(QtGui.QColor(_TONE_COLORS.get(vis.tone, theme.FG)))
        type_item = QtWidgets.QTableWidgetItem("vision")
        type_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        report_item = QtWidgets.QTableWidgetItem(vis.report_summary or "(no report)")
        report_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        decision_text = "accepted" if vis.accepted else "rejected"
        decision_item = QtWidgets.QTableWidgetItem(decision_text)
        decision_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        decision_item.setForeground(QtGui.QColor(_TONE_COLORS.get(vis.tone, theme.FG)))
        self.artifacts_table.setItem(row, 0, state_item)
        self.artifacts_table.setItem(row, 1, type_item)
        self.artifacts_table.setItem(row, 2, report_item)
        self.artifacts_table.setItem(row, 3, decision_item)
