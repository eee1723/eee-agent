"""Dockable bounded Runtime control panel for Houdini 21.

The widget is a thin authenticated client for Session/Run control, bounded
ChangeSet approval summaries, and typed scene inspection. It does not open
SQLite, import the agent graph, start Runtime, expose Apply, or call HOM
directly.
"""

from __future__ import annotations

import itertools
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtNetwork, QtWebSockets, QtWidgets

REPO = Path(__file__).resolve().parent.parent
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
    approval_is_actionable,
    changeset_refresh_required,
    parse_changeset_list,
)
from houdini_side.secure_bridge_host import (  # noqa: E402
    SelectionQueryError,
    query_selection,
    run_background_async,
)

GRAPHITE = "#17191D"
SLATE = "#20242A"
INSET = "#14161A"
IRON = "#343A43"
TEXT = "#E7E9EC"
DIM = "#9097A1"
AMBER = "#FF7A1A"
CYAN = "#63C7C9"
RED = "#E45B55"

_QSS = f"""
QWidget#EEEAgentRuntimePanel {{
    background: {GRAPHITE};
    color: {TEXT};
    font-family: "Segoe UI";
    font-size: 12px;
}}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#Kicker {{
    color: {DIM};
    font-family: "Consolas";
    font-size: 10px;
    font-weight: 700;
}}
QLabel#Title {{
    color: {TEXT};
    font-family: "Bahnschrift SemiCondensed", "Segoe UI";
    font-size: 17px;
    font-weight: 600;
}}
QLabel#Meta {{
    color: {DIM};
    font-family: "Consolas";
    font-size: 11px;
}}
QLabel#RuntimeState {{
    color: {AMBER};
    font-family: "Consolas";
    font-size: 11px;
    font-weight: 700;
}}
QLabel#BridgeState {{
    color: {DIM};
    font-family: "Consolas";
    font-size: 11px;
    font-weight: 700;
}}
QFrame#Rail {{
    background: {SLATE};
    border: 1px solid {IRON};
    border-radius: 3px;
}}
QLabel#RailKey {{
    color: {DIM};
    font-family: "Consolas";
    font-size: 9px;
}}
QLabel#RailValue {{
    color: {CYAN};
    font-family: "Consolas";
    font-size: 11px;
}}
QFrame#Divider {{
    background: {IRON};
    border: none;
    max-height: 1px;
}}
QFrame#Empty {{
    background: {INSET};
    border: 1px solid {IRON};
    border-radius: 3px;
}}
QLabel#EmptyTitle {{
    color: {TEXT};
    font-weight: 600;
}}
QLabel#EmptyBody {{
    color: {DIM};
}}
QTreeWidget#SelectionTable {{
    background: {INSET};
    alternate-background-color: {SLATE};
    border: 1px solid {IRON};
    border-radius: 3px;
    color: {TEXT};
    outline: 0;
    font-family: "Segoe UI";
}}
QTreeWidget#SelectionTable::item {{
    padding: 5px 4px;
    border: none;
}}
QTreeWidget#SelectionTable::item:selected {{
    background: #29353A;
    color: {TEXT};
}}
QHeaderView::section {{
    background: {SLATE};
    color: {DIM};
    border: none;
    border-bottom: 1px solid {IRON};
    padding: 5px 4px;
    font-family: "Consolas";
    font-size: 10px;
    font-weight: 700;
}}
QTabWidget::pane {{
    border: 1px solid {IRON};
    border-radius: 3px;
    background: {INSET};
    top: -1px;
}}
QTabBar::tab {{
    background: {GRAPHITE};
    color: {DIM};
    border: 1px solid {IRON};
    border-bottom: none;
    padding: 7px 12px;
    font-family: "Consolas";
    font-size: 10px;
    font-weight: 700;
}}
QTabBar::tab:selected {{
    color: {CYAN};
    background: {INSET};
}}
QComboBox, QPlainTextEdit {{
    background: {INSET};
    color: {TEXT};
    border: 1px solid {IRON};
    border-radius: 3px;
    selection-background-color: #29353A;
}}
QComboBox {{
    padding: 5px 8px;
}}
QComboBox QAbstractItemView {{
    background: {SLATE};
    color: {TEXT};
    border: 1px solid {IRON};
    selection-background-color: #29353A;
}}
QPlainTextEdit {{
    padding: 7px;
    font-family: "Segoe UI";
}}
QFrame#RunLane {{
    background: {SLATE};
    border: 1px solid {IRON};
    border-left: 3px solid {CYAN};
    border-radius: 3px;
}}
QFrame#ApprovalGate {{
    background: {SLATE};
    border: 1px solid {IRON};
    border-left: 4px solid {AMBER};
    border-radius: 3px;
}}
QLabel#RunState {{
    color: {CYAN};
    font-family: "Bahnschrift SemiCondensed", "Segoe UI";
    font-size: 16px;
    font-weight: 600;
}}
QLabel#GateState {{
    color: {AMBER};
    font-family: "Consolas";
    font-size: 11px;
    font-weight: 700;
}}
QPushButton#DangerButton {{
    color: {RED};
    border-color: {RED};
}}
QPushButton#DangerButton:hover {{ background: #3B2526; }}
QPushButton#GateButton {{
    color: {AMBER};
    border-color: {AMBER};
}}
QPushButton#GateButton:hover {{ background: #3B2B20; }}
QPushButton {{
    background: transparent;
    color: {CYAN};
    border: 1px solid {CYAN};
    border-radius: 3px;
    padding: 6px 12px;
    font-weight: 600;
}}
QPushButton:hover {{ background: #26383C; }}
QPushButton:disabled {{ color: {DIM}; border-color: {IRON}; }}
"""


class SelectionQueryWorker(QtCore.QObject):
    """Runs short-lived Bridge network I/O off the Houdini UI thread."""

    queryStarted = QtCore.Signal()
    querySucceeded = QtCore.Signal(object)
    queryFailed = QtCore.Signal(str, str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._busy = False
        self._active = True

    def refresh(self) -> None:
        with self._lock:
            if not self._active or self._busy:
                return
            self._busy = True
        self.queryStarted.emit()
        threading.Thread(
            target=self._run,
            name="EEE-SelectionQuery",
            daemon=True,
        ).start()

    def detach(self) -> None:
        with self._lock:
            self._active = False

    def _run(self) -> None:
        try:
            result = run_background_async(
                lambda: query_selection(runtime_state_dir())
            )
        except SelectionQueryError as exc:
            self._emit_failure(exc.code, str(exc), exc.retryable)
        except PanelClientError as exc:
            self._emit_failure("bridge.not_available", str(exc), True)
        except Exception:
            self._emit_failure(
                "bridge.internal_failure",
                "The selection could not be inspected.",
                False,
            )
        else:
            with self._lock:
                active = self._active
            if active:
                try:
                    self.querySucceeded.emit(result)
                except RuntimeError:
                    pass
        finally:
            with self._lock:
                self._busy = False

    def _emit_failure(self, code: str, message: str, retryable: bool) -> None:
        with self._lock:
            active = self._active
        if active:
            try:
                self.queryFailed.emit(code, message, retryable)
            except RuntimeError:
                pass


class RuntimeObserverClient(QtCore.QObject):
    """Qt WebSocket client for the bounded Runtime panel command subset."""

    connectionChanged = QtCore.Signal(str, str)
    sessionChanged = QtCore.Signal(str, str, int)
    sessionsChanged = QtCore.Signal(object, str)
    runtimeSnapshotChanged = QtCore.Signal(object)
    changesetsChanged = QtCore.Signal(object)
    commandSucceeded = QtCore.Signal(str, object)
    commandFailed = QtCore.Signal(str, str, str, bool, bool)
    eventObserved = QtCore.Signal(str, int)

    _DELAYS_MS = (250, 500, 1000, 2000, 5000)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._socket: QtWebSockets.QWebSocket | None = None
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._connect)
        self._changeset_timer = QtCore.QTimer(self)
        self._changeset_timer.setSingleShot(True)
        self._changeset_timer.timeout.connect(self.refresh_changesets)
        self._request_counter = itertools.count(1)
        self._pending: dict[str, str] = {}
        self._cursors = RuntimeCursorBook()
        self._runtime_state = RuntimePanelState()
        self._sessions: dict[str, dict] = {}
        self._preferred_session_id: str | None = None
        self._current_session_id: str | None = None
        self._current_session_title = ""
        self._attempt = 0
        self._stopping = False

    def start(self) -> None:
        self._stopping = False
        self._schedule(0)

    def stop(self) -> None:
        self._stopping = True
        self._timer.stop()
        self._changeset_timer.stop()
        socket = self._socket
        self._socket = None
        if socket is not None:
            socket.abort()
            socket.deleteLater()

    def reconnect_now(self) -> None:
        self._attempt = 0
        socket = self._socket
        self._socket = None
        if socket is not None:
            socket.abort()
            socket.deleteLater()
        self._timer.stop()
        self._schedule(0)

    def create_session(self, title: str) -> None:
        self._send("session.create", {"title": title}, "session.create")

    def select_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        self._preferred_session_id = session_id
        if session_id != self._current_session_id:
            # A connection has no unsubscribe command. Reconnect so switching
            # Sessions never leaves hidden live subscriptions behind.
            self.reconnect_now()

    def start_run(self, user_input: str) -> None:
        session_id = self._current_session_id
        if session_id is None:
            self.commandFailed.emit(
                "run.start",
                "runtime.session_required",
                "Create or select a Session before starting a Run.",
                True,
                False,
            )
            return
        self._send(
            "run.start",
            {"session_id": session_id, "user_input": user_input},
            "run.start",
        )

    def stop_run(self, run_id: str, *, force: bool = False) -> None:
        command = "run.force_stop" if force else "run.stop"
        self._send(command, {"run_id": run_id}, command)

    def decide_changeset(
        self, change_id: str, changeset_digest: str, *, approve: bool
    ) -> None:
        command = "changeset.approve" if approve else "changeset.reject"
        self._send(
            command,
            {
                "change_id": change_id,
                "changeset_digest": changeset_digest,
            },
            command,
        )

    def refresh_changesets(self) -> None:
        session_id = self._current_session_id
        if session_id is None:
            self.changesetsChanged.emit(())
            return
        self._send(
            "changeset.list",
            {"session_id": session_id, "limit": 50},
            "changeset.list",
        )

    def _schedule(self, delay_ms: int | None = None) -> None:
        if self._stopping or self._timer.isActive():
            return
        if delay_ms is None:
            index = min(self._attempt, len(self._DELAYS_MS) - 1)
            delay_ms = self._DELAYS_MS[index]
            self._attempt += 1
        self._timer.start(delay_ms)

    def _connect(self) -> None:
        if self._stopping:
            return
        try:
            credentials = load_runtime_credentials(runtime_state_dir())
        except PanelClientError as exc:
            self.connectionChanged.emit("offline", str(exc))
            self._schedule()
            return

        old = self._socket
        self._socket = None
        if old is not None:
            old.abort()
            old.deleteLater()
        socket = QtWebSockets.QWebSocket(
            "EEE Agent Runtime Panel",
            QtWebSockets.QWebSocketProtocol.VersionLatest,
            self,
        )
        self._socket = socket
        self._pending.clear()
        socket.connected.connect(self._on_connected)
        socket.disconnected.connect(self._on_disconnected)
        socket.textMessageReceived.connect(self._on_message)
        socket.errorOccurred.connect(self._on_error)

        request = QtNetwork.QNetworkRequest(QtCore.QUrl(credentials.websocket_url))
        request.setRawHeader(b"Authorization", ("Bearer " + credentials.token).encode())
        self.connectionChanged.emit("connecting", "Connecting to Runtime")
        socket.open(request)

    @QtCore.Slot()
    def _on_connected(self) -> None:
        if self.sender() is not self._socket:
            return
        self._attempt = 0
        self.connectionChanged.emit("online", "Runtime connected")
        self._send("runtime.ping", {}, "ping")
        self._send("session.list", {"include_archived": False}, "session.list")

    @QtCore.Slot()
    def _on_disconnected(self) -> None:
        if self.sender() is not self._socket:
            return
        if self._stopping:
            return
        self.connectionChanged.emit("offline", "Runtime disconnected")
        self._pending.clear()
        self._schedule()

    @QtCore.Slot(object)
    def _on_error(self, _error) -> None:
        if self.sender() is not self._socket:
            return
        if not self._stopping:
            self.connectionChanged.emit("error", "Runtime connection failed")

    def _send(self, command_type: str, payload: dict, purpose: str) -> None:
        socket = self._socket
        if socket is None or socket.state() != QtNetwork.QAbstractSocket.ConnectedState:
            return
        request_id = f"panel_{next(self._request_counter)}"
        try:
            text = build_command(request_id, command_type, payload)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        self._pending[request_id] = purpose
        socket.sendTextMessage(text)

    @QtCore.Slot(str)
    def _on_message(self, raw: str) -> None:
        if self.sender() is not self._socket:
            return
        try:
            message = parse_runtime_message(raw)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            socket = self._socket
            if socket is not None:
                socket.abort()
            return
        if message["kind"] == "response":
            self._handle_response(message)
            return
        self._handle_event(message)

    def _handle_response(self, message) -> None:
        request_id = message["request_id"]
        purpose = self._pending.pop(request_id, None)
        if message["ok"] is not True:
            error = message.get("error")
            if type(error) is dict:
                text = error.get("message_for_user", "Runtime request failed")
                code = error.get("code", "runtime.request_failed")
                requires_action = bool(error.get("requires_user_action", False))
                scene_changed = bool(error.get("scene_may_have_changed", False))
            else:
                text = "Runtime request failed"
                code = "runtime.request_failed"
                requires_action = False
                scene_changed = False
            if purpose in ("ping", "session.list", None):
                self.connectionChanged.emit("error", text)
            self.commandFailed.emit(
                purpose or "runtime.request",
                code,
                text,
                requires_action,
                scene_changed,
            )
            return
        result = message.get("result")
        if purpose == "session.create":
            if type(result) is dict and type(result.get("session_id")) is str:
                self._preferred_session_id = result["session_id"]
            self.commandSucceeded.emit(purpose, result)
            self.reconnect_now()
            return
        if purpose == "session.snapshot":
            try:
                self._runtime_state.load_snapshot(result)
                snapshot = self._runtime_state.snapshot()
                self._cursors.advance(
                    snapshot["session_id"], snapshot["last_seq"]
                )
            except PanelClientError as exc:
                self.connectionChanged.emit("error", str(exc))
                return
            self.runtimeSnapshotChanged.emit(snapshot)
            return
        if purpose == "changeset.list":
            try:
                items = parse_changeset_list(result)
            except PanelClientError as exc:
                self.connectionChanged.emit("error", str(exc))
                return
            self.changesetsChanged.emit(items)
            return
        if purpose in (
            "run.start",
            "run.stop",
            "run.force_stop",
            "changeset.approve",
            "changeset.reject",
        ):
            self.commandSucceeded.emit(purpose, result)
            if purpose.startswith("run."):
                self._request_snapshot()
            else:
                self._schedule_changeset_refresh()
            return
        if purpose != "session.list":
            return
        sessions = result.get("sessions") if type(result) is dict else None
        try:
            selected = choose_active_session(sessions, self._preferred_session_id)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        self._sessions = {
            item["session_id"]: dict(item)
            for item in sessions
            if type(item) is dict and type(item.get("session_id")) is str
        }
        if selected is None:
            self._current_session_id = None
            self._current_session_title = ""
            self.sessionChanged.emit("", "No active Session", 0)
            self.sessionsChanged.emit(tuple(self._sessions.values()), "")
            self.runtimeSnapshotChanged.emit({})
            self.changesetsChanged.emit(())
            return
        self.sessionsChanged.emit(
            tuple(self._sessions.values()), selected["session_id"]
        )
        self._activate_session(selected)

    def _activate_session(self, selected) -> None:
        session_id = selected["session_id"]
        title = selected["title"]
        self._preferred_session_id = session_id
        self._current_session_id = session_id
        self._current_session_title = title
        cursor = self._cursors.last_seq(session_id)
        self.sessionChanged.emit(session_id, title, cursor)
        self._send(
            "session.subscribe",
            {"session_id": session_id, "last_seq": cursor},
            "session.subscribe",
        )
        self._request_snapshot()
        self.refresh_changesets()

    def _request_snapshot(self) -> None:
        if self._current_session_id is None:
            return
        self._send(
            "session.snapshot",
            {"session_id": self._current_session_id},
            "session.snapshot",
        )

    def _schedule_changeset_refresh(self) -> None:
        if not self._changeset_timer.isActive():
            self._changeset_timer.start(80)

    def _handle_event(self, message) -> None:
        try:
            snapshot = snapshot_boundary(message)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        session_id = message["session_id"]
        if snapshot is not None:
            _snapshot_session_id, snapshot_seq = snapshot
            self._cursors.advance(session_id, snapshot_seq)
            try:
                self._runtime_state.load_snapshot(message["payload"])
            except PanelClientError as exc:
                self.connectionChanged.emit("error", str(exc))
                return
            self.runtimeSnapshotChanged.emit(self._runtime_state.snapshot())
            if session_id == self._current_session_id:
                self.sessionChanged.emit(
                    session_id,
                    self._current_session_title,
                    self._cursors.last_seq(session_id),
                )
            return
        try:
            state_advanced = self._runtime_state.apply_event(message)
            advanced = self._cursors.observe(message)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        if advanced:
            seq = self._cursors.last_seq(session_id)
            if state_advanced:
                self.runtimeSnapshotChanged.emit(
                    self._runtime_state.snapshot()
                )
            if changeset_refresh_required(message):
                self._schedule_changeset_refresh()
            self.eventObserved.emit(message["type"], seq)
            if session_id == self._current_session_id:
                self.sessionChanged.emit(
                    session_id, self._current_session_title, seq
                )


class RuntimePanel(QtWidgets.QWidget):
    """Docked Runtime control, approval gate, and typed scene observer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EEEAgentRuntimePanel")
        self.setMinimumWidth(340)
        self._build_ui()
        self.setStyleSheet(_QSS)
        self._client = RuntimeObserverClient(self)
        self._client.connectionChanged.connect(self._set_connection)
        self._client.sessionChanged.connect(self._set_session)
        self._client.sessionsChanged.connect(self._set_sessions)
        self._client.runtimeSnapshotChanged.connect(self._set_runtime_snapshot)
        self._client.changesetsChanged.connect(self._set_changesets)
        self._client.commandSucceeded.connect(self._command_succeeded)
        self._client.commandFailed.connect(self._command_failed)
        self._client.eventObserved.connect(self._set_event)
        self._runtime_online = False
        self._active_run_id: str | None = None
        self._active_run_status = ""
        self._changesets = ()
        self._decision_busy = False
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

        kicker = QtWidgets.QLabel("EEE / CONTROL PLANE")
        kicker.setObjectName("Kicker")
        layout.addWidget(kicker)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Runtime control")
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

        rail = QtWidgets.QFrame()
        rail.setObjectName("Rail")
        rail_layout = QtWidgets.QGridLayout(rail)
        rail_layout.setContentsMargins(10, 8, 10, 8)
        rail_layout.setHorizontalSpacing(14)
        rail_layout.setVerticalSpacing(3)
        self._rail_item(rail_layout, 0, "INSTANCE", "—", "instance_value")
        self._rail_item(rail_layout, 1, "EPOCH", "—", "epoch_value")
        self._rail_item(rail_layout, 2, "REVISION", "—", "revision_value")
        layout.addWidget(rail)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_run_tab(), "RUN")
        self.tabs.addTab(self._build_approvals_tab(), "APPROVALS")
        self.tabs.addTab(self._build_scene_tab(), "SCENE")
        layout.addWidget(self.tabs, 1)

        self.last_event = QtWidgets.QLabel("No Runtime events observed")
        self.last_event.setObjectName("Meta")
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
            "Create or select a Session, then start a read-only Run."
        )
        self.run_meta_label.setObjectName("Meta")
        self.run_meta_label.setWordWrap(True)
        lane_layout.addWidget(self.run_meta_label)
        layout.addWidget(lane)

        prompt_label = QtWidgets.QLabel("RUN REQUEST")
        prompt_label.setObjectName("Kicker")
        layout.addWidget(prompt_label)
        self.run_prompt = QtWidgets.QPlainTextEdit()
        self.run_prompt.setPlaceholderText(
            "Ask the Runtime to inspect or reason about the current scene…"
        )
        self.run_prompt.setMaximumBlockCount(120)
        self.run_prompt.setFixedHeight(76)
        layout.addWidget(self.run_prompt)

        actions = QtWidgets.QHBoxLayout()
        self.start_run_button = QtWidgets.QPushButton("Start run")
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

        output_label = QtWidgets.QLabel("RUN OUTPUT")
        output_label.setObjectName("Kicker")
        layout.addWidget(output_label)
        self.run_output = QtWidgets.QPlainTextEdit()
        self.run_output.setReadOnly(True)
        self.run_output.setPlaceholderText("Run output will stream here.")
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
        label = QtWidgets.QLabel("CHANGESET GATES")
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
        self.gate_state = QtWidgets.QLabel("NO PENDING APPROVAL")
        self.gate_state.setObjectName("GateState")
        gate_layout.addWidget(self.gate_state)
        self.gate_summary = QtWidgets.QLabel(
            "Trusted ChangeSet proposals will appear here."
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
        self.approve_button = QtWidgets.QPushButton("Approve bound")
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
        title, accepted = QtWidgets.QInputDialog.getText(
            self,
            "New Runtime Session",
            "Session title",
        )
        if accepted and title.strip():
            self.new_session_button.setEnabled(False)
            self._client.create_session(title.strip())

    @QtCore.Slot()
    def _start_run(self) -> None:
        prompt = self.run_prompt.toPlainText().strip()
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
            self.gate_state.setText("NO PENDING APPROVAL")
            self.gate_state.setStyleSheet(f"color:{DIM};")
            self.gate_summary.setText(
                "Trusted ChangeSet proposals will appear here."
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
        flag_text = " · ".join(flags) if flags else "bounded"
        self.gate_state.setText(f"GATE / {self._humanize(state).upper()}")
        gate_color = RED if state == "CriticalRecovery" else AMBER
        if state in ("Applied", "RolledBack"):
            gate_color = CYAN
        self.gate_state.setStyleSheet(f"color:{gate_color};")
        decision = approval["decision"] if approval else "No approval"
        receipt_text = f" · receipt {receipt['status']}" if receipt else ""
        self.gate_summary.setText(
            f"{self._humanize(summary['required_permission'])} · "
            f"{risk['operation_count']} "
            f"operations · {flag_text}\n{decision}{receipt_text}"
        )
        paths = list(risk["affected_paths"])
        if risk["affected_paths_truncated"]:
            paths.append(
                f"+ {risk['affected_path_count'] - len(paths)} more paths"
            )
        self.gate_paths.setPlainText("\n".join(paths))
        self.gate_paths.setVisible(bool(paths))
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
        self.gate_state.setText("GATE / DECIDING")
        self._client.decide_changeset(
            summary["change_id"],
            summary["changeset_digest"],
            approve=approve,
        )

    @QtCore.Slot(str, object)
    def _command_succeeded(self, purpose: str, _result) -> None:
        if purpose == "session.create":
            self.new_session_button.setEnabled(True)
            self.status_text.setText("Session created")
        elif purpose == "run.start":
            self.run_prompt.clear()
            self.run_meta_label.setText("Run accepted by Runtime")
        elif purpose in ("run.stop", "run.force_stop"):
            self.run_meta_label.setText("Stop requested")
        elif purpose in ("changeset.approve", "changeset.reject"):
            self.gate_summary.setText("Decision committed; refreshing gate state.")

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
                "GATE / ACTION REQUIRED" if requires_action else "GATE / BLOCKED"
            )
            self.gate_state.setStyleSheet(f"color:{RED};")
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
        self._session_id = session_id
        self._session_title = title if session_id else "—"
        self._cursor = cursor
        self._refresh_context()
        self._update_run_controls()

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


__all__ = ["RuntimeObserverClient", "RuntimePanel", "create_panel", "open_panel"]
