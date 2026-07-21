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
from houdini_side.runtime_panel.client import (
    RuntimeObserverClient,
    SelectionQueryWorker,
)
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
        self._active_run_status = ""
        # Run output is append-only in the card flow (unlike legacy's single
        # text box), so render each run's final output exactly once.
        self._shown_output_run_id: str | None = None
        self._current_session_id = ""
        self._expired_notice_id = None
        self._pending_changeset = None
        self._artifacts: tuple = ()
        self._visions: tuple = ()
        # Launch worker state: a one-slot result box plus a done event,
        # polled by a QTimer so the blocking launcher never touches the
        # Houdini UI thread. _closing lets the worker reap a backend it
        # spawned after the panel has already closed.
        self._launch_done = threading.Event()
        self._launch_result: list = []
        self._launch_timer: QtCore.QTimer | None = None
        self._closing = threading.Event()
        self._selection_worker = SelectionQueryWorker(self)
        self._selection_worker.queryStarted.connect(self._selection_started)
        self._selection_worker.querySucceeded.connect(self._selection_succeeded)
        self._selection_worker.queryFailed.connect(self._selection_failed)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.context_bar = ContextBar(self)
        layout.addWidget(self.context_bar)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.session_sidebar = SessionSidebar(self)
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
        QtCore.QTimer.singleShot(250, self._selection_worker.refresh)

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
        c.commandSucceeded.connect(self._on_command_succeeded)
        c.commandFailed.connect(self._on_command_failed)

        self.session_sidebar.sessionChosen.connect(c.select_session)
        self.session_sidebar.newSessionRequested.connect(self._new_session)
        self.conversation.sendRequested.connect(self._send_run)
        self.conversation.stopRequested.connect(self._stop_run)
        self.inspector.createWorkspaceRequested.connect(self._create_workspace)
        self.inspector.inspectWorkspaceRequested.connect(self._inspect_workspace)
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
        if self._closing.is_set():
            # Panel closed mid-launch: reap a spawned backend here; the UI
            # thread will never poll the result box.
            if result.process is not None:
                backend_launcher.terminate(result.process)
            return
        self._launch_result.append(result)
        self._launch_done.set()
        if self._closing.is_set() and result.process is not None:
            # closeEvent may have completed between the first check and the
            # append; reap here too. terminate() is idempotent on exited
            # processes, so a double-reap against closeEvent is harmless.
            backend_launcher.terminate(result.process)

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
        if state == "online":
            self._selection_worker.refresh()
        elif state in {"offline", "unavailable", "error"}:
            # A dropped connection must not wedge the composer: client._send
            # silently drops commands while offline, so an optimistic "running"
            # state could leave Send/Input disabled with no running run. Return
            # to idle unless a Run is actually still executing server-side —
            # the next snapshot will re-assert the correct state.
            if self._active_run_id is None:
                self.conversation.set_composer_state("idle")
        self._refresh_context_bar()

    def _on_sessions(self, sessions, selected_id: str) -> None:
        self.session_sidebar.set_sessions(sessions, selected_id)

    def _on_session(self, session_id: str, title: str, cursor: int) -> None:
        if session_id and session_id != self._current_session_id:
            if self._current_session_id:
                # A different Session's history must never mix into this view:
                # reset the conversation flow and inspector caches together.
                self._artifacts = ()
                self._visions = ()
                self._shown_output_run_id = None
                self.inspector.render_artifacts(())
                self.inspector.render_visions(())
                self.conversation.clear_items()
                self.conversation.append_item(view_models.notice_card(
                    f"Switched to session {title or session_id}.",
                    tone="normal"))
            self._current_session_id = session_id
        self._session_title = title
        self._refresh_context_bar()

    def _on_snapshot(self, snapshot) -> None:
        # Mirrors legacy _set_runtime_snapshot: runs live under
        # active_run / selected_run, never a flat "run" key.
        if type(snapshot) is not dict and not hasattr(snapshot, "get"):
            return
        if not snapshot:
            self._active_run_id = None
            self._active_run_status = ""
            self._shown_output_run_id = None
            self._run_state = "idle"
            self.conversation.set_composer_state("idle")
            self.inspector.set_run_snapshot(None, ())
            self._refresh_context_bar()
            return
        active = snapshot.get("active_run")
        self._active_run_id = (
            active.get("run_id") if type(active) is dict else None)
        shown = active if type(active) is dict else snapshot.get("selected_run")
        status = active.get("status") if type(active) is dict else None
        self._active_run_status = status if type(status) is str else ""
        if self._active_run_status in _STOPPING_RUN_STATES:
            # Stop was already requested; the remaining action is force stop.
            self.conversation.set_composer_state("stopping-forceable")
        elif type(active) is dict and (
                self._active_run_status not in _TERMINAL_RUN_STATES):
            self.conversation.set_composer_state("running")
        else:
            self.conversation.set_composer_state("idle")
        if type(shown) is dict:
            shown_status = shown.get("status")
            if type(shown_status) is str:
                self._run_state = shown_status
        else:
            self._run_state = "idle"
        self.inspector.set_run_snapshot(
            shown if type(shown) is dict else None,
            snapshot.get("activity") if hasattr(snapshot, "get") else (),
            apply_outcomes=(
                snapshot.get("apply_outcomes") if hasattr(snapshot, "get") else ()
            ),
        )
        self._maybe_render_output(snapshot, shown)
        self._refresh_context_bar()

    def _maybe_render_output(self, snapshot, shown) -> None:
        # Stream the run's text + thinking into the trailing assistant card,
        # then settle it into a final assistant_message on termination.
        run_id = None
        is_terminal = False
        if type(shown) is dict:
            run_id = shown.get("run_id")
            status = shown.get("status")
            is_terminal = (
                type(status) is str and status in _TERMINAL_RUN_STATES)

        output = snapshot.get("output") if hasattr(snapshot, "get") else None
        output = output if type(output) is str else ""
        thinking = snapshot.get("thinking") if hasattr(snapshot, "get") else None
        thinking = thinking if type(thinking) is str else ""

        if not is_terminal and type(run_id) is str:
            # Run in flight: update the streaming card in place (creates it
            # on the first token so the user sees the reply forming).
            if output or thinking:
                self.conversation.update_streaming(output, thinking=thinking)
            return

        if (not is_terminal or type(run_id) is not str
                or run_id == self._shown_output_run_id):
            return
        # Run terminated: swap the streaming card for the final reply exactly
        # once. If there was no streamed output, still emit a final card so the
        # user sees the run produced something (or an explicit empty marker).
        self.conversation.replace_last_assistant(
            view_models.assistant_message(output))
        self._shown_output_run_id = run_id

    def _on_changesets(self, changesets) -> None:
        for summary in changesets:
            if summary.get("state") != "AwaitingApproval":
                continue
            if not approval_is_actionable(summary):
                # Expired gate: the drawer would be dead, so say so once
                # per change_id in the flow instead.
                if summary.get("change_id") != self._expired_notice_id:
                    self._expired_notice_id = summary.get("change_id")
                    self.conversation.append_item(
                        view_models.approval_result_card(False, expired=True))
                continue
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

    def _on_command_succeeded(self, purpose: str, result) -> None:
        # An approval decision (approve/reject) is acknowledged with a card so
        # the user sees the outcome in the flow, not just a vanishing drawer.
        if purpose == "changeset.approve":
            self.conversation.append_item(
                view_models.approval_result_card(True))
            return
        if purpose == "changeset.reject":
            self.conversation.append_item(
                view_models.approval_result_card(False))
            return
        if not purpose.startswith("workspace.") or type(result) is not dict:
            return
        workspace = result.get("workspace")
        if type(workspace) is dict:
            workspace_id = workspace.get("workspace_id")
            if type(workspace_id) is str:
                self._workspace_id = workspace_id
        self.inspector.set_workspace_facts(result)
        self._refresh_context_bar()

    def _on_command_failed(self, purpose, code, message, retryable, fatal):
        if purpose == "run.start":
            # The Run never started; release the optimistic composer lock.
            self.conversation.set_composer_state("idle")
        self.conversation.append_item(
            view_models.notice_card(f"{purpose} failed: {message}",
                                    tone="error"))

    # -- bridge state (SelectionQueryWorker, mirrors legacy) -----------------

    def _selection_started(self) -> None:
        self._bridge = "connecting"
        self._refresh_context_bar()

    def _selection_succeeded(self, result) -> None:
        self._bridge = "ready"
        self._refresh_context_bar()

    def _selection_failed(self, code, message, retryable) -> None:
        self._bridge = "unavailable"
        self._refresh_context_bar()

    # -- user intents -------------------------------------------------------

    def _send_run(self, text: str) -> None:
        if self._connection != "online":
            # The client silently drops commands while offline; never let
            # the composer wedge in "running" for a Run that was not sent.
            self.conversation.append_item(view_models.notice_card(
                "Runtime is offline; the Run was not sent.", tone="error"))
            self.conversation.set_composer_state("idle")
            return
        self.conversation.append_item(view_models.user_message(text))
        self._client.start_run(text)
        self.conversation.set_composer_state("running")

    def _stop_run(self) -> None:
        if self._connection != "online":
            self.conversation.append_item(view_models.notice_card(
                "Runtime is offline; the stop request was not sent.",
                tone="error"))
            self.conversation.set_composer_state("idle")
            return
        if not self._active_run_id:
            return
        # Legacy shows a separate Force stop button once stopping; the
        # composer reuses its one Stop button for both.
        force = self._active_run_status in _STOPPING_RUN_STATES
        self.conversation.set_composer_state("stopping")
        self._client.stop_run(self._active_run_id, force=force)

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

    def _create_workspace(self) -> None:
        self._client.create_workspace()

    def _inspect_workspace(self) -> None:
        self._client.inspect_workspace(self._workspace_id)

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
        self._closing.set()
        if self._launch_timer is not None and self._launch_timer.isActive():
            self._launch_timer.stop()
        if (self._spawned_process is None and self._launch_result
                and self._launch_result[0].process is not None):
            # The worker finished between its closing check and the timer
            # stop; the unpolled spawned backend is still ours to reap.
            backend_launcher.terminate(self._launch_result[0].process)
        if self._spawned_process is not None:
            backend_launcher.terminate(self._spawned_process)
            self._spawned_process = None
        self._selection_worker.detach()
        self._client.stop()
        super().closeEvent(event)
