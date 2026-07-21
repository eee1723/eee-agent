"""Dockable bounded Runtime control panel for Houdini 21.

The widget is a thin authenticated client for Session/Run control, bounded
ChangeSet approval summaries, and typed scene inspection. It does not open
SQLite, import the agent graph, start Runtime, expose Apply, or call HOM
directly.
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtNetwork, QtWebSockets, QtWidgets

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from eee_agent.panel.client_state import (  # noqa: E402
    PanelClientError,
    RuntimeCursorBook,
    build_command,
    choose_active_session,
    load_runtime_credentials,
    parse_runtime_message,
    runtime_state_dir,
    snapshot_boundary,
)
from eee_agent.panel.runtime_state import (  # noqa: E402
    RuntimePanelState,
    append_artifact_summary,
    append_vision_summary,
    approval_is_actionable,
    artifact_refresh_required,
    changeset_refresh_required,
    parse_artifact_event,
    parse_changeset_list,
    parse_vision_event,
    vision_refresh_required,
)
from houdini_side.secure_bridge_host import (  # noqa: E402
    SelectionQueryError,
    query_selection,
    run_background_async,
)

from houdini_side.runtime_panel.client import (  # noqa: E402
    RunRequestEdit,
    RuntimeObserverClient,
    SelectionQueryWorker,
    SessionTitleDialog,
    _configure_ime,
    _load_preferred_session_id,
    _QSS,
    _save_preferred_session_id,
)


class RuntimePanel(QtWidgets.QWidget):
    """Docked Runtime control, approval gate, and typed scene observer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EEEAgentRuntimePanel")
        self.setMinimumWidth(340)
        self._developer_details_visible = False
        self._build_ui()
        self.setStyleSheet(_QSS)
        self._client = RuntimeObserverClient(self)
        self._client.connectionChanged.connect(self._set_connection)
        self._client.sessionChanged.connect(self._set_session)
        self._client.sessionsChanged.connect(self._set_sessions)
        self._client.runtimeSnapshotChanged.connect(self._set_runtime_snapshot)
        self._client.changesetsChanged.connect(self._set_changesets)
        self._client.artifactObserved.connect(self._artifact_observed)
        self._client.visionObserved.connect(self._vision_observed)
        self._client.commandSucceeded.connect(self._command_succeeded)
        self._client.commandFailed.connect(self._command_failed)
        self._client.eventObserved.connect(self._set_event)
        self._runtime_online = False
        self._active_run_id: str | None = None
        self._active_run_status = ""
        self._changesets = ()
        self._artifacts = ()
        self._visions = ()
        self._decision_busy = False
        self._session_title_dialog: SessionTitleDialog | None = None
        self._selection_worker = SelectionQueryWorker(self)
        self._selection_worker.queryStarted.connect(self._selection_started)
        self._selection_worker.querySucceeded.connect(self._selection_succeeded)
        self._selection_worker.queryFailed.connect(self._selection_failed)
        self._client.start()
        QtCore.QTimer.singleShot(250, self._selection_worker.refresh)

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        kicker = QtWidgets.QLabel("EEE / PROCEDURAL MODELING")
        kicker.setObjectName("Kicker")
        layout.addWidget(kicker)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Modeling assistant")
        title.setObjectName("Title")
        header.addWidget(title)
        header.addStretch(1)
        self.runtime_state = QtWidgets.QLabel("CONNECTING")
        self.runtime_state.setObjectName("RuntimeState")
        header.addWidget(self.runtime_state)
        header.addSpacing(10)
        self.bridge_state = QtWidgets.QLabel("BRIDGE WAITING")
        self.bridge_state.setObjectName("BridgeState")
        header.addWidget(self.bridge_state)
        header.addSpacing(8)
        self.developer_toggle = QtWidgets.QToolButton()
        self.developer_toggle.setObjectName("DeveloperToggle")
        self.developer_toggle.setText("Details")
        self.developer_toggle.setCheckable(True)
        self.developer_toggle.setToolTip(
            "Show Runtime, scene, and Workspace diagnostics"
        )
        self.developer_toggle.toggled.connect(
            self._toggle_developer_details
        )
        header.addWidget(self.developer_toggle)
        layout.addLayout(header)

        self.status_text = QtWidgets.QLabel("Reading Runtime identity")
        self.status_text.setObjectName("Meta")
        layout.addWidget(self.status_text)
        self.context_text = QtWidgets.QLabel("HIP —  /  Session —  /  seq 0")
        self.context_text.setObjectName("Meta")
        self.context_text.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        layout.addWidget(self.context_text)
        layout.addWidget(self._divider())

        self.rail = QtWidgets.QFrame()
        self.rail.setObjectName("Rail")
        rail_layout = QtWidgets.QGridLayout(self.rail)
        rail_layout.setContentsMargins(10, 8, 10, 8)
        rail_layout.setHorizontalSpacing(14)
        rail_layout.setVerticalSpacing(3)
        self._rail_item(rail_layout, 0, "INSTANCE", "—", "instance_value")
        self._rail_item(rail_layout, 1, "EPOCH", "—", "epoch_value")
        self._rail_item(rail_layout, 2, "REVISION", "—", "revision_value")
        self.rail.setVisible(False)
        layout.addWidget(self.rail)

        self.tabs = QtWidgets.QTabWidget()
        self.model_tab_index = self.tabs.addTab(
            self._build_run_tab(), "MODEL"
        )
        self.review_tab_index = self.tabs.addTab(
            self._build_approvals_tab(), "REVIEW"
        )
        self.scene_tab_index = self.tabs.addTab(
            self._build_scene_tab(), "SCENE"
        )
        self.workspace_tab_index = self.tabs.addTab(
            self._build_workspace_tab(), "WORKSPACE"
        )
        self.artifacts_tab_index = self.tabs.addTab(
            self._build_artifacts_tab(), "ARTIFACTS"
        )
        self.tabs.setTabVisible(self.scene_tab_index, False)
        self.tabs.setTabVisible(self.workspace_tab_index, False)
        self.tabs.setTabVisible(self.artifacts_tab_index, False)
        layout.addWidget(self.tabs, 1)

        self.last_event = QtWidgets.QLabel("No Runtime events observed")
        self.last_event.setObjectName("Meta")
        self.last_event.setVisible(False)
        layout.addWidget(self.last_event)

    def _build_run_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        session_row = QtWidgets.QHBoxLayout()
        session_label = QtWidgets.QLabel("SESSION")
        session_label.setObjectName("Kicker")
        session_row.addWidget(session_label)
        self.session_combo = QtWidgets.QComboBox()
        self.session_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.session_combo.setMinimumContentsLength(12)
        self.session_combo.currentIndexChanged.connect(self._session_selected)
        session_row.addWidget(self.session_combo, 1)
        self.new_session_button = QtWidgets.QPushButton("New")
        self.new_session_button.clicked.connect(self._new_session)
        session_row.addWidget(self.new_session_button)
        layout.addLayout(session_row)

        lane = QtWidgets.QFrame()
        lane.setObjectName("RunLane")
        lane_layout = QtWidgets.QVBoxLayout(lane)
        lane_layout.setContentsMargins(11, 9, 11, 9)
        lane_layout.setSpacing(3)
        self.run_state_label = QtWidgets.QLabel("NO ACTIVE RUN")
        self.run_state_label.setObjectName("RunState")
        lane_layout.addWidget(self.run_state_label)
        self.run_meta_label = QtWidgets.QLabel(
            "Describe what you want to build. EEE will prepare a reviewable plan."
        )
        self.run_meta_label.setObjectName("Meta")
        self.run_meta_label.setWordWrap(True)
        lane_layout.addWidget(self.run_meta_label)
        layout.addWidget(lane)

        prompt_label = QtWidgets.QLabel("DESCRIBE THE MODEL")
        prompt_label.setObjectName("Kicker")
        layout.addWidget(prompt_label)
        self.run_prompt = RunRequestEdit()
        self.run_prompt.setFixedHeight(38)
        layout.addWidget(self.run_prompt)

        actions = QtWidgets.QHBoxLayout()
        self.start_run_button = QtWidgets.QPushButton("Plan model")
        self.start_run_button.clicked.connect(self._start_run)
        actions.addWidget(self.start_run_button)
        actions.addStretch(1)
        self.stop_run_button = QtWidgets.QPushButton("Stop")
        self.stop_run_button.setObjectName("DangerButton")
        self.stop_run_button.clicked.connect(self._stop_run)
        self.stop_run_button.setEnabled(False)
        actions.addWidget(self.stop_run_button)
        self.force_stop_button = QtWidgets.QPushButton("Force stop")
        self.force_stop_button.setObjectName("DangerButton")
        self.force_stop_button.clicked.connect(self._force_stop_run)
        self.force_stop_button.setVisible(False)
        actions.addWidget(self.force_stop_button)
        layout.addLayout(actions)

        self.review_banner = QtWidgets.QFrame()
        self.review_banner.setObjectName("ApprovalGate")
        review_layout = QtWidgets.QHBoxLayout(self.review_banner)
        review_layout.setContentsMargins(11, 8, 11, 8)
        self.review_banner_text = QtWidgets.QLabel(
            "A model plan is ready for review."
        )
        review_layout.addWidget(self.review_banner_text, 1)
        self.review_banner_button = QtWidgets.QPushButton("Review plan")
        self.review_banner_button.setObjectName("GateButton")
        self.review_banner_button.clicked.connect(self._show_review)
        review_layout.addWidget(self.review_banner_button)
        self.review_banner.setVisible(False)
        layout.addWidget(self.review_banner)

        output_label = QtWidgets.QLabel("ASSISTANT")
        output_label.setObjectName("Kicker")
        layout.addWidget(output_label)
        self.run_output = QtWidgets.QPlainTextEdit()
        self.run_output.setReadOnly(True)
        self.run_output.setPlaceholderText(
            "Planning notes and the final response will appear here."
        )
        layout.addWidget(self.run_output, 1)
        self.run_activity = QtWidgets.QLabel("No tool activity")
        self.run_activity.setObjectName("Meta")
        self.run_activity.setWordWrap(True)
        layout.addWidget(self.run_activity)
        return tab

    def _build_approvals_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        header = QtWidgets.QHBoxLayout()
        label = QtWidgets.QLabel("MODEL PLANS")
        label.setObjectName("Kicker")
        header.addWidget(label)
        self.approval_count = QtWidgets.QLabel("00")
        self.approval_count.setObjectName("Meta")
        header.addWidget(self.approval_count)
        header.addStretch(1)
        self.refresh_approvals_button = QtWidgets.QPushButton("Refresh")
        self.refresh_approvals_button.clicked.connect(
            self._refresh_approvals
        )
        header.addWidget(self.refresh_approvals_button)
        layout.addLayout(header)

        self.approval_list = QtWidgets.QTreeWidget()
        self.approval_list.setObjectName("SelectionTable")
        self.approval_list.setColumnCount(3)
        self.approval_list.setHeaderLabels(["STATE", "PERMISSION", "OPS"])
        self.approval_list.setRootIsDecorated(False)
        self.approval_list.setAlternatingRowColors(True)
        self.approval_list.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.approval_list.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.approval_list.header().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.approval_list.currentItemChanged.connect(
            self._approval_selected
        )
        self.approval_list.setMaximumHeight(210)
        layout.addWidget(self.approval_list, 1)

        self.approval_gate = QtWidgets.QFrame()
        self.approval_gate.setObjectName("ApprovalGate")
        gate_layout = QtWidgets.QVBoxLayout(self.approval_gate)
        gate_layout.setContentsMargins(11, 9, 11, 9)
        gate_layout.setSpacing(5)
        self.gate_state = QtWidgets.QLabel("NO PLAN TO REVIEW")
        self.gate_state.setObjectName("GateState")
        gate_layout.addWidget(self.gate_state)
        self.gate_summary = QtWidgets.QLabel(
            "New model plans will appear here before Houdini is changed."
        )
        self.gate_summary.setWordWrap(True)
        gate_layout.addWidget(self.gate_summary)
        self.gate_paths = QtWidgets.QPlainTextEdit()
        self.gate_paths.setReadOnly(True)
        self.gate_paths.setFixedHeight(72)
        self.gate_paths.setVisible(False)
        gate_layout.addWidget(self.gate_paths)
        decision_row = QtWidgets.QHBoxLayout()
        self.reject_button = QtWidgets.QPushButton("Reject")
        self.reject_button.setObjectName("DangerButton")
        self.reject_button.clicked.connect(self._reject_changeset)
        self.reject_button.setEnabled(False)
        decision_row.addWidget(self.reject_button)
        decision_row.addStretch(1)
        self.approve_button = QtWidgets.QPushButton("Approve and build")
        self.approve_button.setObjectName("GateButton")
        self.approve_button.clicked.connect(self._approve_changeset)
        self.approve_button.setEnabled(False)
        decision_row.addWidget(self.approve_button)
        gate_layout.addLayout(decision_row)
        layout.addWidget(self.approval_gate)
        return tab

    def _build_scene_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        section = QtWidgets.QHBoxLayout()
        selected = QtWidgets.QLabel("SELECTED")
        selected.setObjectName("Kicker")
        section.addWidget(selected)
        self.selection_count = QtWidgets.QLabel("00")
        self.selection_count.setObjectName("Meta")
        section.addWidget(self.selection_count)
        section.addStretch(1)
        self.refresh_button = QtWidgets.QPushButton("Refresh selection")
        self.refresh_button.clicked.connect(self._refresh_selection)
        section.addWidget(self.refresh_button)
        layout.addLayout(section)

        self.selection_stack = QtWidgets.QStackedWidget()
        self.empty_selection = QtWidgets.QFrame()
        self.empty_selection.setObjectName("Empty")
        empty_layout = QtWidgets.QVBoxLayout(self.empty_selection)
        empty_layout.setContentsMargins(12, 14, 12, 14)
        empty_layout.setSpacing(5)
        self.empty_title = QtWidgets.QLabel("No Houdini nodes selected")
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_body = QtWidgets.QLabel(
            "Select one or more nodes, then refresh. Inspection is read-only "
            "and does not bind a Workspace."
        )
        self.empty_body.setObjectName("EmptyBody")
        self.empty_body.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_body)
        self.selection_stack.addWidget(self.empty_selection)

        self.selection_table = QtWidgets.QTreeWidget()
        self.selection_table.setObjectName("SelectionTable")
        self.selection_table.setColumnCount(4)
        self.selection_table.setHeaderLabels(
            ["NODE", "TYPE", "GEOMETRY", "LOCK"]
        )
        self.selection_table.setRootIsDecorated(False)
        self.selection_table.setAlternatingRowColors(True)
        self.selection_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.selection_table.header().setStretchLastSection(False)
        self.selection_table.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.selection_table.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.selection_table.header().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.selection_table.header().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.selection_stack.addWidget(self.selection_table)
        layout.addWidget(self.selection_stack, 1)
        return tab

    def _build_workspace_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        title = QtWidgets.QLabel("TRUSTED WORKSPACE")
        title.setObjectName("Kicker")
        layout.addWidget(title)
        self.workspace_status = QtWidgets.QLabel("NO WORKSPACE INSPECTED")
        self.workspace_status.setObjectName("RunState")
        layout.addWidget(self.workspace_status)

        note = QtWidgets.QLabel(
            "EEE creates and binds a trusted Workspace automatically after the "
            "first approved model. These manual controls are developer recovery "
            "tools for inspecting or rebinding an existing owned graph."
        )
        note.setObjectName("Meta")
        note.setWordWrap(True)
        layout.addWidget(note)

        actions = QtWidgets.QHBoxLayout()
        self.create_workspace_button = QtWidgets.QPushButton("Create from selection")
        self.create_workspace_button.clicked.connect(self._create_workspace)
        actions.addWidget(self.create_workspace_button)
        self.inspect_workspace_button = QtWidgets.QPushButton("Inspect")
        self.inspect_workspace_button.clicked.connect(self._inspect_workspace)
        actions.addWidget(self.inspect_workspace_button)
        layout.addLayout(actions)

        self.workspace_id_edit = QtWidgets.QLineEdit()
        self.workspace_id_edit.setPlaceholderText("Workspace ID: ws_...")
        layout.addWidget(self.workspace_id_edit)
        self.workspace_revision_edit = QtWidgets.QLineEdit()
        self.workspace_revision_edit.setPlaceholderText("Manifest revision (64 hex)")
        layout.addWidget(self.workspace_revision_edit)
        self.bind_workspace_button = QtWidgets.QPushButton("Bind / refresh selection")
        self.bind_workspace_button.clicked.connect(self._bind_workspace)
        layout.addWidget(self.bind_workspace_button)

        self.workspace_summary = QtWidgets.QPlainTextEdit()
        self.workspace_summary.setReadOnly(True)
        self.workspace_summary.setPlaceholderText(
            "Workspace lifecycle and health summaries will appear here."
        )
        layout.addWidget(self.workspace_summary, 1)
        return tab

    def _build_artifacts_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        title = QtWidgets.QLabel("CAPTURED ARTIFACTS")
        title.setObjectName("Kicker")
        layout.addWidget(title)
        self.artifacts_status = QtWidgets.QLabel("NO ARTIFACTS OBSERVED")
        self.artifacts_status.setObjectName("RunState")
        layout.addWidget(self.artifacts_status)

        note = QtWidgets.QLabel(
            "Read-only capture metadata from durable Runtime events. The "
            "listed SHA-256 is the registered content digest: the bytes shown "
            "to you and any later vision input hash-identically match it."
        )
        note.setObjectName("Meta")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.artifact_list = QtWidgets.QTreeWidget()
        self.artifact_list.setObjectName("SelectionTable")
        self.artifact_list.setColumnCount(4)
        self.artifact_list.setHeaderLabels(
            ["ARTIFACT", "MEDIA", "BYTES", "SHA-256"]
        )
        self.artifact_list.setRootIsDecorated(False)
        self.artifact_list.setAlternatingRowColors(True)
        self.artifact_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.artifact_list.header().setStretchLastSection(False)
        self.artifact_list.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self.artifact_list.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.artifact_list.header().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.artifact_list.header().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self.artifact_list, 1)

        layout.addWidget(self._divider())
        vision_title = QtWidgets.QLabel("VISION EVALUATIONS")
        vision_title.setObjectName("Kicker")
        layout.addWidget(vision_title)
        self.vision_status = QtWidgets.QLabel("NO VISION EVALUATIONS OBSERVED")
        self.vision_status.setObjectName("RunState")
        layout.addWidget(self.vision_status)
        vision_note = QtWidgets.QLabel(
            "Advisory evaluation of the captured bytes. It can reject a "
            "delivery but can never override a failed deterministic check."
        )
        vision_note.setObjectName("Meta")
        vision_note.setWordWrap(True)
        layout.addWidget(vision_note)
        self.vision_list = QtWidgets.QTreeWidget()
        self.vision_list.setObjectName("SelectionTable")
        self.vision_list.setColumnCount(4)
        self.vision_list.setHeaderLabels(
            ["STATUS", "DECISION", "ADVISORY", "SUMMARY"]
        )
        self.vision_list.setRootIsDecorated(False)
        self.vision_list.setAlternatingRowColors(True)
        self.vision_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.vision_list.header().setStretchLastSection(False)
        self.vision_list.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.vision_list.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.vision_list.header().setSectionResizeMode(
            2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self.vision_list.header().setSectionResizeMode(
            3, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.vision_list, 1)
        return tab

    def _rail_item(self, layout, column, key, value, attr) -> None:
        key_label = QtWidgets.QLabel(key)
        key_label.setObjectName("RailKey")
        value_label = QtWidgets.QLabel(value)
        value_label.setObjectName("RailValue")
        layout.addWidget(key_label, 0, column)
        layout.addWidget(value_label, 1, column)
        setattr(self, attr, value_label)

    @staticmethod
    def _divider() -> QtWidgets.QFrame:
        divider = QtWidgets.QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        return divider

    @QtCore.Slot(bool)
    def _toggle_developer_details(self, visible: bool) -> None:
        self._developer_details_visible = bool(visible)
        self.rail.setVisible(self._developer_details_visible)
        self.last_event.setVisible(self._developer_details_visible)
        self.tabs.setTabVisible(
            self.scene_tab_index, self._developer_details_visible
        )
        self.tabs.setTabVisible(
            self.workspace_tab_index, self._developer_details_visible
        )
        self.tabs.setTabVisible(
            self.artifacts_tab_index, self._developer_details_visible
        )
        if (
            not self._developer_details_visible
            and self.tabs.currentIndex()
            in (self.scene_tab_index, self.workspace_tab_index, self.artifacts_tab_index)
        ):
            self.tabs.setCurrentIndex(self.model_tab_index)
        current = self._current_changeset()
        if current is not None:
            self._render_approval(current)

    @QtCore.Slot()
    def _show_review(self) -> None:
        self.tabs.setCurrentIndex(self.review_tab_index)

    @QtCore.Slot(object, str)
    def _set_sessions(self, sessions, selected_id: str) -> None:
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        selected_index = -1
        for index, session in enumerate(sessions):
            session_id = session.get("session_id", "")
            self.session_combo.addItem(session.get("title", session_id), session_id)
            self.session_combo.setItemData(
                index, session_id, QtCore.Qt.ItemDataRole.ToolTipRole
            )
            if session_id == selected_id:
                selected_index = index
        if selected_index >= 0:
            self.session_combo.setCurrentIndex(selected_index)
        self.session_combo.blockSignals(False)
        self._update_run_controls()

    @QtCore.Slot(int)
    def _session_selected(self, index: int) -> None:
        if index < 0:
            return
        session_id = self.session_combo.itemData(index)
        if type(session_id) is str and session_id:
            self._client.select_session(session_id)

    @QtCore.Slot()
    def _new_session(self) -> None:
        existing = self._session_title_dialog
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            existing.title_edit.setFocus(
                QtCore.Qt.FocusReason.OtherFocusReason
            )
            return
        dialog = SessionTitleDialog(self)
        self._session_title_dialog = dialog
        dialog.accepted.connect(
            lambda current=dialog: self._submit_new_session(current.title())
        )
        dialog.finished.connect(
            lambda _result, current=dialog: self._release_session_dialog(
                current
            )
        )
        dialog.open()

    @QtCore.Slot()
    def _create_workspace(self) -> None:
        self.create_workspace_button.setEnabled(False)
        self.workspace_status.setText("CREATING WORKSPACE")
        self._client.create_workspace()

    @QtCore.Slot()
    def _inspect_workspace(self) -> None:
        self.inspect_workspace_button.setEnabled(False)
        self.workspace_status.setText("INSPECTING WORKSPACE")
        workspace_id = self.workspace_id_edit.text().strip() or None
        self._client.inspect_workspace(workspace_id)

    @QtCore.Slot()
    def _bind_workspace(self) -> None:
        workspace_id = self.workspace_id_edit.text().strip()
        revision = self.workspace_revision_edit.text().strip()
        if not workspace_id or not revision:
            self.workspace_status.setText(
                "Enter Workspace ID and manifest revision before binding."
            )
            return
        self.bind_workspace_button.setEnabled(False)
        self.workspace_status.setText("BINDING WORKSPACE")
        self._client.bind_workspace(workspace_id, revision)

    def _submit_new_session(self, title: str) -> None:
        if title:
            self.new_session_button.setEnabled(False)
            self.status_text.setText("Creating Runtime Session...")
            self.run_state_label.setText("CREATING SESSION")
            self.run_meta_label.setText(
                "Waiting for Runtime to create and select the Session."
            )
            self._client.create_session(title)

    def _release_session_dialog(self, dialog: SessionTitleDialog) -> None:
        if self._session_title_dialog is dialog:
            self._session_title_dialog = None
        dialog.deleteLater()

    @QtCore.Slot()
    def _start_run(self) -> None:
        prompt = self.run_prompt.text().strip()
        if not prompt:
            self.run_meta_label.setText("Enter a request before starting a Run.")
            return
        self.start_run_button.setEnabled(False)
        self.run_state_label.setText("STARTING")
        self._client.start_run(prompt)

    @QtCore.Slot()
    def _stop_run(self) -> None:
        if self._active_run_id:
            self.stop_run_button.setEnabled(False)
            self._client.stop_run(self._active_run_id, force=False)

    @QtCore.Slot()
    def _force_stop_run(self) -> None:
        if not self._active_run_id:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Force stop Runtime Run",
            "Force stop the active Run? A started ChangeSet Apply is not "
            "interrupted by this action.",
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self.force_stop_button.setEnabled(False)
            self._client.stop_run(self._active_run_id, force=True)

    @QtCore.Slot()
    def _refresh_approvals(self) -> None:
        self.refresh_approvals_button.setEnabled(False)
        self._client.refresh_changesets()

    @QtCore.Slot(object)
    def _set_runtime_snapshot(self, snapshot) -> None:
        if type(snapshot) is not dict and not hasattr(snapshot, "get"):
            return
        if not snapshot:
            self._active_run_id = None
            self._active_run_status = ""
            self.run_state_label.setText("NO SESSION")
            self.run_meta_label.setText(
                "Create or select a Session before starting a Run."
            )
            self.run_output.clear()
            self._update_run_controls()
            return
        active = snapshot.get("active_run")
        selected = snapshot.get("selected_run")
        self._active_run_id = (
            active.get("run_id") if type(active) is dict else None
        )
        shown = active if type(active) is dict else selected
        if type(shown) is dict:
            status = shown.get("status", "Unknown")
            if type(active) is dict:
                self._active_run_status = str(status)
            else:
                self._active_run_status = ""
            self.run_state_label.setText(self._humanize(str(status)).upper())
            prompt = shown.get("user_input", "")
            run_id = shown.get("run_id", "")
            self.run_meta_label.setText(
                f"{self._short(str(run_id), 24)}  /  {self._short(str(prompt), 100)}"
            )
        else:
            self._active_run_status = ""
            self.run_state_label.setText("READY")
            self.run_meta_label.setText(
                "No Run history in this Session."
            )
        output = snapshot.get("output", "")
        if type(output) is str and output != self.run_output.toPlainText():
            self.run_output.setPlainText(output)
            cursor = self.run_output.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            self.run_output.setTextCursor(cursor)
        activity = snapshot.get("activity", ())
        if activity:
            item = activity[-1]
            name = item.get("name", "tool")
            detail = item.get("detail", "")
            suffix = f" — {self._short(detail, 160)}" if detail else ""
            self.run_activity.setText(f"{name}{suffix}")
        else:
            self.run_activity.setText("No tool activity")
        self._update_run_controls()

    @QtCore.Slot(object)
    def _set_changesets(self, changesets) -> None:
        self.refresh_approvals_button.setEnabled(True)
        self._decision_busy = False
        self._changesets = tuple(changesets)
        self.approval_count.setText(f"{len(self._changesets):02d}")
        self.approval_list.clear()
        preferred = -1
        actionable_count = 0
        for index, summary in enumerate(self._changesets):
            risk = summary["risk"]
            item = QtWidgets.QTreeWidgetItem(
                [
                    summary["state"],
                    self._humanize(summary["required_permission"]),
                    str(risk["operation_count"]),
                ]
            )
            item.setText(0, self._humanize(summary["state"]))
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, index)
            item.setToolTip(0, summary["change_id"])
            self.approval_list.addTopLevelItem(item)
            if preferred < 0 and approval_is_actionable(summary):
                preferred = index
            if approval_is_actionable(summary):
                actionable_count += 1
        self.review_banner.setVisible(actionable_count > 0)
        self.review_banner_text.setText(
            "A model plan is ready for review."
            if actionable_count == 1
            else f"{actionable_count} model plans are ready for review."
        )
        if self._changesets:
            self.approval_list.setCurrentItem(
                self.approval_list.topLevelItem(
                    preferred if preferred >= 0 else 0
                )
            )
        else:
            self._render_approval(None)

    @QtCore.Slot(object, object)
    def _approval_selected(self, current, _previous) -> None:
        if current is None:
            self._render_approval(None)
            return
        index = current.data(0, QtCore.Qt.ItemDataRole.UserRole)
        summary = (
            self._changesets[index]
            if type(index) is int and 0 <= index < len(self._changesets)
            else None
        )
        self._render_approval(summary)

    def _render_approval(self, summary) -> None:
        if summary is None:
            self.gate_state.setText("NO PLAN TO REVIEW")
            self.gate_state.setStyleSheet(f"color:{DIM};")
            self.gate_summary.setText(
                "New model plans will appear here before Houdini is changed."
            )
            self.gate_paths.clear()
            self.gate_paths.setVisible(False)
            self.approve_button.setEnabled(False)
            self.reject_button.setEnabled(False)
            return
        risk = summary["risk"]
        approval = summary["approval"]
        receipt = summary["receipt"]
        state = summary["state"]
        flags = []
        if risk["changes_wiring"]:
            flags.append("wiring")
        if risk["touches_external_nodes"]:
            flags.append("external")
        if risk["requires_backup"]:
            flags.append("backup")
        flag_text = " / ".join(flags) if flags else "bounded"
        self.gate_state.setText(f"REVIEW / {self._humanize(state).upper()}")
        gate_color = RED if state == "CriticalRecovery" else AMBER
        if state in ("Applied", "RolledBack"):
            gate_color = CYAN
        self.gate_state.setStyleSheet(f"color:{gate_color};")
        decision = approval["decision"] if approval else "No approval"
        receipt_text = f" / result {receipt['status']}" if receipt else ""
        self.gate_summary.setText(
            f"{risk['operation_count']} planned changes / {flag_text}\n"
            f"{decision}{receipt_text}"
        )
        paths = list(risk["affected_paths"])
        if risk["affected_paths_truncated"]:
            paths.append(
                f"+ {risk['affected_path_count'] - len(paths)} more paths"
            )
        self.gate_paths.setPlainText("\n".join(paths))
        self.gate_paths.setVisible(
            bool(paths) and self._developer_details_visible
        )
        actionable = approval_is_actionable(summary) and not self._decision_busy
        self.approve_button.setEnabled(actionable)
        self.reject_button.setEnabled(actionable)
        self.approval_gate.setToolTip(
            f"{summary['change_id']}\n{summary['changeset_digest']}"
        )

    def _current_changeset(self):
        item = self.approval_list.currentItem()
        if item is None:
            return None
        index = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if type(index) is not int or not 0 <= index < len(self._changesets):
            return None
        return self._changesets[index]

    @QtCore.Slot()
    def _approve_changeset(self) -> None:
        self._decide_current_changeset(True)

    @QtCore.Slot()
    def _reject_changeset(self) -> None:
        self._decide_current_changeset(False)

    def _decide_current_changeset(self, approve: bool) -> None:
        summary = self._current_changeset()
        if summary is None or not approval_is_actionable(summary):
            return
        self._decision_busy = True
        self.approve_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.gate_state.setText("REVIEW / APPROVING")
        self._client.decide_changeset(
            summary["change_id"],
            summary["changeset_digest"],
            approve=approve,
        )

    @QtCore.Slot(str, object)
    def _command_succeeded(self, purpose: str, result) -> None:
        if purpose.startswith("workspace."):
            self.create_workspace_button.setEnabled(True)
            self.inspect_workspace_button.setEnabled(True)
            self.bind_workspace_button.setEnabled(True)
            if type(result) is dict:
                workspace = result.get("workspace")
                if type(workspace) is dict:
                    workspace_id = workspace.get("workspace_id")
                    revision = workspace.get("revision")
                    if type(workspace_id) is str:
                        self.workspace_id_edit.setText(workspace_id)
                    if type(revision) is str:
                        self.workspace_revision_edit.setText(revision)
                self.workspace_summary.setPlainText(
                    json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
                )
                if purpose == "workspace.inspect":
                    self.workspace_status.setText(
                        f"WORKSPACE {str(result.get('status', 'Unknown')).upper()}"
                    )
                else:
                    self.workspace_status.setText("WORKSPACE READY")
            return
        if purpose == "session.create":
            self.new_session_button.setEnabled(True)
            self.status_text.setText("Session created")
        elif purpose == "run.start":
            self.run_prompt.clear()
            self.run_meta_label.setText("Run accepted by Runtime")
        elif purpose in ("run.stop", "run.force_stop"):
            self.run_meta_label.setText("Stop requested")
        elif purpose == "changeset.approve":
            self.gate_summary.setText(
                "Approved. Houdini is building and validating the model."
            )
        elif purpose == "changeset.reject":
            self.gate_summary.setText("Plan rejected. The scene was not changed.")

    @QtCore.Slot(str, str, str, bool, bool)
    def _command_failed(
        self,
        purpose: str,
        code: str,
        message: str,
        requires_action: bool,
        scene_changed: bool,
    ) -> None:
        self.new_session_button.setEnabled(True)
        self._decision_busy = False
        self.status_text.setText(message)
        self.status_text.setToolTip(code)
        if purpose.startswith("run."):
            self.run_meta_label.setText(message)
        if purpose.startswith("changeset."):
            suffix = " Scene may have changed." if scene_changed else ""
            self.gate_summary.setText(message + suffix)
            self.gate_state.setText(
                "REVIEW / ACTION REQUIRED"
                if requires_action
                else "REVIEW / BLOCKED"
            )
            self.gate_state.setStyleSheet(f"color:{RED};")
        if purpose.startswith("workspace."):
            self.create_workspace_button.setEnabled(True)
            self.inspect_workspace_button.setEnabled(True)
            self.bind_workspace_button.setEnabled(True)
            self.workspace_status.setText("WORKSPACE BLOCKED")
            self.workspace_summary.setPlainText(message)
        self._update_run_controls()

    def _update_run_controls(self) -> None:
        has_session = bool(getattr(self, "_session_id", ""))
        active = bool(self._active_run_id)
        self.new_session_button.setEnabled(self._runtime_online)
        self.session_combo.setEnabled(self._runtime_online)
        self.refresh_approvals_button.setEnabled(self._runtime_online)
        self.start_run_button.setEnabled(
            self._runtime_online and has_session and not active
        )
        self.stop_run_button.setEnabled(self._runtime_online and active)
        show_force = active and self._active_run_status in (
            "StopRequested",
            "Stopping",
        )
        self.force_stop_button.setVisible(show_force)
        self.force_stop_button.setEnabled(self._runtime_online and show_force)

    @QtCore.Slot()
    def _refresh_selection(self) -> None:
        self._selection_worker.refresh()

    @QtCore.Slot(str, str)
    def _set_connection(self, state: str, message: str) -> None:
        labels = {
            "online": ("ONLINE", CYAN),
            "connecting": ("CONNECTING", AMBER),
            "offline": ("OFFLINE", DIM),
            "error": ("ERROR", RED),
        }
        label, color = labels.get(state, ("OFFLINE", DIM))
        self._runtime_online = state == "online"
        self.runtime_state.setText(label)
        self.runtime_state.setStyleSheet(f"color:{color};")
        self.status_text.setText(message)
        self._update_run_controls()

    @QtCore.Slot(str, str, int)
    def _set_session(self, session_id: str, title: str, cursor: int) -> None:
        if session_id != getattr(self, "_session_id", ""):
            self._artifacts = ()
            self._render_artifacts()
            self._visions = ()
            self._render_visions()
        self._session_id = session_id
        self._session_title = title if session_id else "—"
        self._cursor = cursor
        self._refresh_context()
        self._update_run_controls()

    @QtCore.Slot(object)
    def _artifact_observed(self, summary) -> None:
        self._artifacts = append_artifact_summary(self._artifacts, summary)
        self._render_artifacts()

    def _render_artifacts(self) -> None:
        self.artifact_list.clear()
        count = len(self._artifacts)
        self.artifacts_status.setText(
            "NO ARTIFACTS OBSERVED" if count == 0 else f"{count:02d} ARTIFACTS"
        )
        for summary in self._artifacts:
            if summary["kind"] == "captured":
                state = str(summary.get("state", "available")).upper()
                item = QtWidgets.QTreeWidgetItem(
                    [
                        f"{state}  {summary['artifact_id']}",
                        summary["media_type"],
                        str(summary["size_bytes"]),
                        summary["sha256"][:12],
                    ]
                )
                item.setToolTip(
                    0,
                    f"{summary['relative_path']}\nsha256: {summary['sha256']}",
                )
                item.setToolTip(3, summary["sha256"])
            elif summary["kind"] == "lifecycle":
                state = str(summary["state"]).upper()
                item = QtWidgets.QTreeWidgetItem(
                    [f"{state}  {summary['artifact_id']}", "LIFECYCLE", "—", "NOT VIEWABLE"]
                )
                item.setToolTip(3, "Artifact lifecycle state; bytes are not available for viewing.")
            else:
                item = QtWidgets.QTreeWidgetItem(
                    ["CAPTURE FAILED", "—", "—", summary["code"]]
                )
                item.setToolTip(0, summary["change_id"])
                item.setToolTip(3, summary["code"])
            self.artifact_list.addTopLevelItem(item)

    @QtCore.Slot(object)
    def _vision_observed(self, summary) -> None:
        self._visions = append_vision_summary(self._visions, summary)
        self._render_visions()

    def _render_visions(self) -> None:
        self.vision_list.clear()
        count = len(self._visions)
        self.vision_status.setText(
            "NO VISION EVALUATIONS OBSERVED"
            if count == 0
            else f"{count:02d} VISION EVALUATIONS"
        )
        for summary in self._visions:
            status = str(summary["status"]).upper()
            decision = "ACCEPTED" if summary["accepted"] else "REJECTED"
            advisory = summary["advisory_passed"]
            advisory_text = (
                "-" if advisory is None else ("PASS" if advisory else "FAIL")
            )
            item = QtWidgets.QTreeWidgetItem(
                [status, decision, advisory_text, str(summary["summary"])]
            )
            details = [
                f"deterministic valid: {summary['deterministic_valid']}",
                f"observations: {summary['observation_count']}",
                f"artifacts: {summary['artifact_count']}",
                f"changeset: {summary['changeset_digest']}",
            ]
            if summary["report_summary"] is not None:
                details.insert(0, f"report: {summary['report_summary']}")
            item.setToolTip(0, "\n".join(details))
            item.setToolTip(3, str(summary["summary"]))
            self.vision_list.addTopLevelItem(item)

    @QtCore.Slot(str, int)
    def _set_event(self, event_type: str, seq: int) -> None:
        self.last_event.setText(f"Last event  {event_type}  /  seq {seq}")

    @QtCore.Slot()
    def _selection_started(self) -> None:
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Reading…")
        self._set_bridge_state("READING", AMBER)

    @QtCore.Slot(object)
    def _selection_succeeded(self, result) -> None:
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh selection")
        self._set_bridge_state("BRIDGE READY", CYAN)
        binding = result.binding
        self._hip_path = binding.hip_path or "unsaved"
        self.instance_value.setText(self._short(binding.instance_id, 18))
        self.instance_value.setToolTip(binding.instance_id)
        self.epoch_value.setText(str(binding.scene_epoch))
        revision = binding.observed_revision.removeprefix("sha256:")
        self.revision_value.setText(self._short(revision, 12))
        self.revision_value.setToolTip(binding.observed_revision)
        self._refresh_context()

        nodes = tuple(result.selected_nodes)
        self.selection_count.setText(f"{len(nodes):02d}")
        self.selection_table.clear()
        if not nodes:
            self.empty_title.setText("No Houdini nodes selected")
            self.empty_body.setText(
                "Select one or more nodes, then refresh. Inspection is "
                "read-only and does not bind a Workspace."
            )
            self.selection_stack.setCurrentWidget(self.empty_selection)
            return
        for node in nodes:
            item = QtWidgets.QTreeWidgetItem(
                [
                    node.path,
                    node.node_type,
                    self._geometry_text(node.geometry_stats),
                    "LOCKED" if node.is_locked else "—",
                ]
            )
            item.setToolTip(0, f"{node.display_name}\nParent: {node.parent_path}")
            if node.geometry_stats is not None:
                item.setToolTip(2, str(dict(node.geometry_stats)))
            self.selection_table.addTopLevelItem(item)
        self.selection_stack.setCurrentWidget(self.selection_table)

    @QtCore.Slot(str, str, bool)
    def _selection_failed(
        self, code: str, message: str, retryable: bool
    ) -> None:
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText("Refresh selection")
        state = "BRIDGE OFFLINE" if retryable else "BRIDGE ERROR"
        self._set_bridge_state(state, DIM if retryable else RED)
        self.selection_count.setText("00")
        self.empty_title.setText("Selection is unavailable")
        self.empty_body.setText(message)
        self.empty_body.setToolTip(code)
        self.selection_stack.setCurrentWidget(self.empty_selection)

    def _set_bridge_state(self, label: str, color: str) -> None:
        self.bridge_state.setText(label)
        self.bridge_state.setStyleSheet(f"color:{color};")

    def _refresh_context(self) -> None:
        hip = getattr(self, "_hip_path", "—")
        session = getattr(self, "_session_title", "—")
        cursor = getattr(self, "_cursor", 0)
        self.context_text.setText(
            f"HIP {hip}  /  Session {session}  /  seq {cursor}"
        )
        self.context_text.setToolTip(getattr(self, "_session_id", ""))

    @staticmethod
    def _short(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[:limit] + "…"

    @staticmethod
    def _humanize(value: str) -> str:
        words: list[str] = []
        current = ""
        for char in value:
            if char.isupper() and current:
                words.append(current)
                current = char
            else:
                current += char
        if current:
            words.append(current)
        return " ".join(words)

    @staticmethod
    def _geometry_text(stats) -> str:
        if stats is None:
            return "—"
        points = stats.get("points")
        prims = stats.get("primitives")
        if type(points) is int and type(prims) is int:
            return f"{points} pts · {prims} prims"
        return "geometry"

    def closeEvent(self, event) -> None:
        self._selection_worker.detach()
        self._client.stop()
        super().closeEvent(event)


def create_panel() -> RuntimePanel:
    return RuntimePanel()


def open_panel():
    """Open the registered Python Panel interface in a floating dockable tab."""
    import hou

    panel_file = REPO / "python_panels" / "EEEAgentRuntime.pypanel"
    interface = hou.pypanel.interfaceByName("eee_agent_runtime")
    if interface is None:
        hou.pypanel.installFile(str(panel_file))
        interface = hou.pypanel.interfaceByName("eee_agent_runtime")
    if interface is None:
        raise RuntimeError("EEE Agent Runtime Python Panel is not installed.")
    desktop = hou.ui.curDesktop()
    pane_tab = desktop.createFloatingPaneTab(hou.paneTabType.PythonPanel)
    pane_tab.setActiveInterface(interface)
    return pane_tab


__all__ = [
    "RuntimeObserverClient",
    "RuntimePanel",
    "RunRequestEdit",
    "SessionTitleDialog",
    "create_panel",
    "open_panel",
]
