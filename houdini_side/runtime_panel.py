"""Dockable Task 17-A Runtime observer for Houdini 21.

The widget is a thin authenticated client. It reads Runtime discovery/token
handoff files, connects with QtWebSockets, lists/subscribes to Sessions, and
keeps reconnect cursors in memory. It does not open SQLite, import the agent
graph, start Runtime, or expose any write command.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtNetwork, QtWebSockets, QtWidgets

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
from houdini_side.secure_bridge_host import (  # noqa: E402
    SelectionQueryError,
    query_selection,
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
            result = asyncio.run(query_selection(runtime_state_dir()))
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
    """Qt WebSocket client for Task 17-A's existing read-only command subset."""

    connectionChanged = QtCore.Signal(str, str)
    sessionChanged = QtCore.Signal(str, str, int)
    eventObserved = QtCore.Signal(str, int)

    _DELAYS_MS = (250, 500, 1000, 2000, 5000)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._socket: QtWebSockets.QWebSocket | None = None
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._connect)
        self._request_counter = itertools.count(1)
        self._pending: dict[str, str] = {}
        self._cursors = RuntimeCursorBook()
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
            text = (
                error.get("message_for_user", "Runtime request failed")
                if type(error) is dict
                else "Runtime request failed"
            )
            self.connectionChanged.emit("error", text)
            return
        if purpose != "session.list":
            return
        result = message.get("result")
        sessions = result.get("sessions") if type(result) is dict else None
        try:
            selected = choose_active_session(sessions, self._preferred_session_id)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        if selected is None:
            self._current_session_id = None
            self._current_session_title = ""
            self.sessionChanged.emit("", "No active Session", 0)
            return
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
            if session_id == self._current_session_id:
                self.sessionChanged.emit(
                    session_id,
                    self._current_session_title,
                    self._cursors.last_seq(session_id),
                )
            return
        try:
            advanced = self._cursors.observe(message)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        if advanced:
            seq = self._cursors.last_seq(session_id)
            self.eventObserved.emit(message["type"], seq)
            if session_id == self._current_session_id:
                self.sessionChanged.emit(
                    session_id, self._current_session_title, seq
                )


class RuntimePanel(QtWidgets.QWidget):
    """Docked read-only Runtime and typed Houdini selection observer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EEEAgentRuntimePanel")
        self.setMinimumWidth(340)
        self._build_ui()
        self.setStyleSheet(_QSS)
        self._client = RuntimeObserverClient(self)
        self._client.connectionChanged.connect(self._set_connection)
        self._client.sessionChanged.connect(self._set_session)
        self._client.eventObserved.connect(self._set_event)
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
        title = QtWidgets.QLabel("Live scene observer")
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

        self.last_event = QtWidgets.QLabel("No Runtime events observed")
        self.last_event.setObjectName("Meta")
        layout.addWidget(self.last_event)

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
        self.runtime_state.setText(label)
        self.runtime_state.setStyleSheet(f"color:{color};")
        self.status_text.setText(message)

    @QtCore.Slot(str, str, int)
    def _set_session(self, session_id: str, title: str, cursor: int) -> None:
        self._session_id = session_id
        self._session_title = title if session_id else "—"
        self._cursor = cursor
        self._refresh_context()

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
