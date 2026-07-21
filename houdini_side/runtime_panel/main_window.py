"""Three-pane Runtime panel: assembly, responsive drawers, client wiring."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from eee_agent.panel.client_state import runtime_state_dir
from eee_agent.panel.runtime_state import (
    append_artifact_summary,
    append_vision_summary,
    approval_is_actionable,
)
from houdini_side.runtime_panel import backend_launcher, theme, view_models
from houdini_side.runtime_panel.approval_drawer import ApprovalDrawer
from houdini_side.runtime_panel.client import RuntimeObserverClient
from houdini_side.runtime_panel.context_bar import ContextBar
from houdini_side.runtime_panel.conversation import ConversationView
from houdini_side.runtime_panel.inspector import InspectorPane
from houdini_side.runtime_panel.session_sidebar import SessionSidebar

_WIDE_MIN_WIDTH = 900
_MEDIUM_MIN_WIDTH = 700
_DRAWER_WIDTH = 320
_DRAWER_MARGIN = 8
_TERMINAL_RUN_STATES = frozenset({"Completed", "Cancelled", "Failed"})
_STOPPING_RUN_STATES = frozenset({"StopRequested", "Stopping"})


class RuntimePanel(QtWidgets.QWidget):
    """Three-pane agent panel. Thin shell: all logic stays in the client
    and the Qt-free view models / launcher."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EEEAgentRuntimePanel")
        self.setStyleSheet(theme.build_qss())

        self._client = RuntimeObserverClient(self)
        self._spawned_process = None
        self._connection = "offline"
        self._bridge = "unavailable"
        self._run_state = "idle"
        self._session_title = ""
        self._workspace_id: str | None = None
        self._active_run_id: str | None = None
        self._pending_changeset = None
        self._artifacts: tuple = ()
        self._visions: tuple = ()
        # Launch worker state: a one-slot result box plus a done event,
        # polled by a QTimer so the blocking launcher never touches the
        # Houdini UI thread.
        self._launch_done = threading.Event()
        self._launch_result: list = []
        self._launch_timer: QtCore.QTimer | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.context_bar = ContextBar(self)
        layout.addWidget(self.context_bar)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.session_sidebar = SessionSidebar(self._client, self)
        self.conversation = ConversationView(self)
        self.inspector = InspectorPane(self)
        self.splitter.addWidget(self.session_sidebar)
        self.splitter.addWidget(self.conversation)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([220, 600, 300])
        layout.addWidget(self.splitter, 1)

        # Right-anchored overlay inside the conversation pane; positioned in
        # _position_drawer, which runs on every conversation resize.
        self.approval_drawer = ApprovalDrawer(self.conversation)
        self.approval_drawer.hide()
        self.conversation.installEventFilter(self)

        self._wire()
        self._start_backend_and_connect()

    # -- wiring ---------------------------------------------------------

    def _wire(self) -> None:
        c = self._client
        c.connectionChanged.connect(self._on_connection)
        c.sessionsChanged.connect(self._on_sessions)
        c.sessionChanged.connect(self._on_session)
        c.runtimeSnapshotChanged.connect(self._on_snapshot)
        c.changesetsChanged.connect(self._on_changesets)
        c.artifactObserved.connect(self._on_artifact)
        c.visionObserved.connect(self._on_vision)
        c.commandFailed.connect(self._on_command_failed)

        self.session_sidebar.sessionChosen.connect(c.select_session)
        self.session_sidebar.newSessionRequested.connect(self._new_session)
        self.conversation.sendRequested.connect(self._send_run)
        self.conversation.stopRequested.connect(self._stop_run)
        self.approval_drawer.approved.connect(
            lambda: self._decide_changeset(True))
        self.approval_drawer.rejected.connect(
            lambda: self._decide_changeset(False))
        self.context_bar.sidebarToggled.connect(
            self.session_sidebar.setVisible)
        self.context_bar.inspectorToggled.connect(self.inspector.setVisible)

    # -- backend auto-start ----------------------------------------------

    def _start_backend_and_connect(self) -> None:
        try:
            state_dir = runtime_state_dir()
        except Exception as exc:  # bounded: misconfigured EEE_RUNTIME_HOME
            self.conversation.append_item(
                view_models.notice_card(
                    f"Runtime home is unavailable: {exc}", tone="error"))
            return
        repo_root = Path(os.environ.get("EEE_PATH", ".")).resolve()
        # ensure_runtime blocks up to its timeout; run it off the UI thread
        # and poll for completion so the panel stays responsive.
        self._connection = "connecting"
        self._refresh_context_bar()
        threading.Thread(
            target=self._launch_worker,
            args=(repo_root, state_dir),
            name="EEE-BackendLaunch",
            daemon=True,
        ).start()
        self._launch_timer = QtCore.QTimer(self)
        self._launch_timer.setInterval(100)
        self._launch_timer.timeout.connect(self._poll_launch)
        self._launch_timer.start()

    def _launch_worker(self, repo_root: Path, state_dir: Path) -> None:
        result = backend_launcher.ensure_runtime(repo_root, state_dir)
        self._launch_result.append(result)
        self._launch_done.set()

    def _poll_launch(self) -> None:
        if not self._launch_done.is_set():
            return
        self._launch_timer.stop()
        result = self._launch_result[0]
        if result.status in {"ready", "spawned"}:
            self._spawned_process = (
                result.process if result.status == "spawned" else None)
            self._client.start()
            return
        # Whoever spawns, reaps: a still-running timeout process is owned by
        # this panel and is terminated in closeEvent.
        if result.status == "timeout" and result.process is not None:
            self._spawned_process = result.process
        self._connection = "offline"
        self._refresh_context_bar()
        detail = result.detail
        if result.log_path is not None:
            detail += f" Log: {result.log_path}"
        self.conversation.append_item(
            view_models.notice_card(
                f"Runtime backend did not start ({result.status}). {detail}",
                tone="error"))

    # -- client signal handlers -------------------------------------------

    def _on_connection(self, state: str, message: str) -> None:
        self._connection = state
        self._refresh_context_bar()

    def _on_sessions(self, sessions, selected_id: str) -> None:
        self.session_sidebar.set_sessions(sessions, selected_id)

    def _on_session(self, session_id: str, title: str, cursor: int) -> None:
        self._session_title = title
        self._refresh_context_bar()

    def _on_snapshot(self, snapshot) -> None:
        # Mirrors legacy _set_runtime_snapshot: runs live under
        # active_run / selected_run, never a flat "run" key.
        if type(snapshot) is not dict and not hasattr(snapshot, "get"):
            return
        if not snapshot:
            self._active_run_id = None
            self._run_state = "idle"
            self.conversation.set_composer_state("idle")
            self.inspector.set_run_snapshot(None)
            self._refresh_context_bar()
            return
        active = snapshot.get("active_run")
        self._active_run_id = (
            active.get("run_id") if type(active) is dict else None)
        shown = active if type(active) is dict else snapshot.get("selected_run")
        if type(active) is dict:
            status = active.get("status")
            if status in _STOPPING_RUN_STATES:
                self.conversation.set_composer_state("stopping")
            elif status not in _TERMINAL_RUN_STATES:
                self.conversation.set_composer_state("running")
            else:
                self.conversation.set_composer_state("idle")
        else:
            self.conversation.set_composer_state("idle")
        if type(shown) is dict:
            status = shown.get("status")
            if type(status) is str:
                self._run_state = status
        else:
            self._run_state = "idle"
        self.inspector.set_run_snapshot(
            shown if type(shown) is dict else None)
        self._refresh_context_bar()

    def _on_changesets(self, changesets) -> None:
        for summary in changesets:
            if summary.get("state") == "AwaitingApproval":
                # Store the exact summary so the decision forwards the same
                # change_id / changeset_digest the gate rendered.
                self._pending_changeset = summary
                self.approval_drawer.show_changeset(summary)
                self._position_drawer()
                return
        self._pending_changeset = None
        self.approval_drawer.hide_drawer()

    def _on_artifact(self, summary) -> None:
        self.conversation.append_item(view_models.artifact_card(summary))
        self._artifacts = append_artifact_summary(self._artifacts, summary)
        self.inspector.render_artifacts(self._artifacts)

    def _on_vision(self, summary) -> None:
        self.conversation.append_item(view_models.vision_card(summary))
        self._visions = append_vision_summary(self._visions, summary)
        self.inspector.render_visions(self._visions)

    def _on_command_failed(self, purpose, code, message, retryable, fatal):
        if purpose == "run.start":
            # The Run never started; release the optimistic composer lock.
            self.conversation.set_composer_state("idle")
        self.conversation.append_item(
            view_models.notice_card(f"{purpose} failed: {message}",
                                    tone="error"))

    # -- user intents -------------------------------------------------------

    def _send_run(self, text: str) -> None:
        self.conversation.append_item(view_models.user_message(text))
        self._client.start_run(text)
        self.conversation.set_composer_state("running")

    def _stop_run(self) -> None:
        if not self._active_run_id:
            return
        self.conversation.set_composer_state("stopping")
        self._client.stop_run(self._active_run_id, force=False)

    def _decide_changeset(self, approve: bool) -> None:
        summary = self._pending_changeset
        if summary is None or not approval_is_actionable(summary):
            return
        self.approval_drawer.hide_drawer()
        self._pending_changeset = None
        self._client.decide_changeset(
            summary["change_id"],
            summary["changeset_digest"],
            approve=approve,
        )

    def _new_session(self) -> None:
        from houdini_side.runtime_panel.client import SessionTitleDialog

        dialog = SessionTitleDialog(self)
        # exec_(): the source boundary forbids the plain builtin-named call.
        if dialog.exec_() == QtWidgets.QDialog.DialogCode.Accepted:
            self._client.create_session(dialog.title())

    # -- responsive layout ---------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = self.width()
        self.inspector.setVisible(width >= _WIDE_MIN_WIDTH)
        self.session_sidebar.setVisible(width >= _MEDIUM_MIN_WIDTH)
        self.context_bar.inspector_button.setChecked(width >= _WIDE_MIN_WIDTH)
        self.context_bar.sidebar_button.setChecked(width >= _MEDIUM_MIN_WIDTH)
        self._position_drawer()

    def eventFilter(self, watched, event) -> bool:
        if (watched is self.conversation
                and event.type() == QtCore.QEvent.Type.Resize):
            self._position_drawer()
        return super().eventFilter(watched, event)

    def _position_drawer(self) -> None:
        """Anchor the approval drawer to the conversation's right edge."""
        width = min(_DRAWER_WIDTH, self.conversation.width())
        if width <= 0:
            return
        self.approval_drawer.setGeometry(
            self.conversation.width() - width - _DRAWER_MARGIN,
            _DRAWER_MARGIN,
            width,
            max(0, self.conversation.height() - 2 * _DRAWER_MARGIN),
        )
        self.approval_drawer.raise_()

    def _refresh_context_bar(self) -> None:
        hip = "-"
        try:
            import hou  # type: ignore

            hip = hou.hipFile.basename() or "untitled.hip"
        except Exception:
            pass
        self.context_bar.set_status(view_models.context_status(
            hip=hip, session_title=self._session_title,
            workspace_id=self._workspace_id, connection=self._connection,
            bridge=self._bridge, run_state=self._run_state,
        ))

    # -- teardown -------------------------------------------------------------

    def closeEvent(self, event) -> None:
        if self._launch_timer is not None and self._launch_timer.isActive():
            self._launch_timer.stop()
        if self._spawned_process is not None:
            backend_launcher.terminate(self._spawned_process)
            self._spawned_process = None
        self._client.stop()
        super().closeEvent(event)
