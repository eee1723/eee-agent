"""Qt client layer for the Runtime panel package.

Hosts the WebSocket RuntimeObserverClient, the selection query worker,
and the small dialog/IME helpers used by the legacy RuntimePanel widget.
"""

from __future__ import annotations

import itertools
import sys
import threading
from pathlib import Path

from PySide6 import QtCore, QtNetwork, QtWebSockets, QtWidgets

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from eee_agent.panel.client_state import (  # noqa: E402
    PanelClientError,
    RuntimeCursorBook,
    build_command,
    choose_active_session,
    choose_empty_placeholder,
    load_runtime_credentials,
    parse_runtime_message,
    runtime_state_dir,
    snapshot_boundary,
)
from eee_agent.panel.runtime_state import (  # noqa: E402
    RuntimePanelState,
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


GRAPHITE = "#17191D"
SLATE = "#20242A"
INSET = "#14161A"
IRON = "#343A43"
TEXT = "#E7E9EC"
DIM = "#9097A1"
AMBER = "#FF7A1A"
CYAN = "#63C7C9"
RED = "#E45B55"
_SETTINGS_ORGANIZATION = "EEEAgent"
_SETTINGS_APPLICATION = "RuntimePanel"
_PREFERRED_SESSION_KEY = "preferred_session_id"
_AUTO_EXECUTE_KEY = "auto_execute_changesets"

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
QComboBox, QLineEdit, QPlainTextEdit {{
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
QToolButton#DeveloperToggle {{
    color: {DIM};
    border: none;
    padding: 3px 5px;
    font-family: "Consolas";
    font-size: 10px;
}}
QToolButton#DeveloperToggle:checked {{ color: {CYAN}; }}
"""


def _configure_ime(widget, *, multiline: bool) -> None:
    """Make Qt input-method support explicit inside Houdini's Python Panel."""
    widget.setAttribute(
        QtCore.Qt.WidgetAttribute.WA_InputMethodEnabled, True
    )
    hints = (
        QtCore.Qt.InputMethodHint.ImhMultiLine
        if multiline
        else QtCore.Qt.InputMethodHint.ImhNone
    )
    widget.setInputMethodHints(hints)
    widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)


def _load_preferred_session_id() -> str | None:
    value = QtCore.QSettings(
        _SETTINGS_ORGANIZATION, _SETTINGS_APPLICATION
    ).value(_PREFERRED_SESSION_KEY, "")
    return value if type(value) is str and value else None


def _save_preferred_session_id(session_id: str) -> None:
    settings = QtCore.QSettings(
        _SETTINGS_ORGANIZATION, _SETTINGS_APPLICATION
    )
    settings.setValue(_PREFERRED_SESSION_KEY, session_id)
    settings.sync()


def _load_auto_execute() -> bool:
    value = QtCore.QSettings(
        _SETTINGS_ORGANIZATION, _SETTINGS_APPLICATION
    ).value(_AUTO_EXECUTE_KEY, False)
    # QSettings may return a str ("true"/"false") or bool depending on backend;
    # normalize defensively.
    if type(value) is bool:
        return value
    if type(value) is str:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _save_auto_execute(enabled: bool) -> None:
    settings = QtCore.QSettings(
        _SETTINGS_ORGANIZATION, _SETTINGS_APPLICATION
    )
    settings.setValue(_AUTO_EXECUTE_KEY, bool(enabled))
    settings.sync()


class RunRequestEdit(QtWidgets.QPlainTextEdit):
    """Multi-line Run request editor resilient to Windows IME confirmation.

    Enter inserts a newline (so IME candidate confirmation never accidentally
    fires a Run); Ctrl+Enter is the explicit submit gesture. Exposes a
    QLineEdit-compatible ``text()``/``clear()`` so the composer does not need
    to know it is a plain-text widget.
    """

    submitRequested = QtCore.Signal()

    _MAX_CHARS = 16_000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RunRequestEdit")
        self.setPlaceholderText(
            "Ask the Runtime to inspect or reason about the current scene…"
            "  (Ctrl+Enter to send)"
        )
        self.setMaximumHeight(90)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        _configure_ime(self, multiline=True)

    def keyPressEvent(self, event) -> None:
        if event.key() in (
            QtCore.Qt.Key.Key_Return,
            QtCore.Qt.Key.Key_Enter,
        ):
            modifiers = event.modifiers()
            if modifiers & QtCore.Qt.KeyboardModifier.ControlModifier:
                # Explicit submit gesture: Ctrl+Enter. The modifier makes IME
                # candidate confirmation (a bare Enter) safe — it just inserts
                # a newline via the default handler below.
                event.accept()
                self.submitRequested.emit()
                return
            # Bare Enter/Return inserts a newline (default QPlainTextEdit
            # behavior); do NOT start a Run from it.
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        super().keyReleaseEvent(event)
        # Enforce the length cap on release so paste/drop are bounded too.
        text = self.toPlainText()
        if len(text) > self._MAX_CHARS:
            cursor = self.textCursor()
            pos = cursor.position()
            self.setPlainText(text[: self._MAX_CHARS])
            cursor.setPosition(min(pos, self._MAX_CHARS))
            self.setTextCursor(cursor)

    # QLineEdit-compatible surface so the composer stays widget-agnostic.

    def text(self) -> str:  # type: ignore[override]
        return self.toPlainText()

    def setText(self, value: str) -> None:  # noqa: N802 — QLineEdit compat
        self.setPlainText(value)


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
    artifactObserved = QtCore.Signal(object)
    visionObserved = QtCore.Signal(object)
    # Fired when New Session is requested but the current Session is already
    # the empty placeholder: no duplicate create is sent (idempotency holds),
    # but the panel needs a cue to acknowledge the click instead of looking dead.
    emptySessionFocused = QtCore.Signal()

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
        self._preferred_session_id = _load_preferred_session_id()
        # When enabled, a proposal auto-approves and applies immediately
        # (reusing the exact-digest approve path). Default off; persisted so the
        # user's choice survives a Houdini restart.
        self._auto_execute = _load_auto_execute()
        self._current_session_id: str | None = None
        self._current_session_title = ""
        # When the user sends a prompt with no active Session, we auto-create
        # one (placeholder title) and stash the prompt here to fire run.start
        # once the new Session activates after reconnect.
        self._pending_run_input: str | None = None
        self._session_create_inflight = False
        self._attempt = 0
        self._stopping = False
        # A freshly opened panel defaults to the empty placeholder rather than
        # restoring the last-used Session (Houdini sessions begin new work).
        # Only the FIRST session.list after panel start ignores the persisted
        # preferred id; every later reconnect honors it so an in-progress
        # conversation survives reconnect.
        self._bootstrap_complete = False

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
        self._current_session_id = None
        self._current_session_title = ""
        self.sessionChanged.emit("", "Loading Session", 0)
        socket = self._socket
        self._socket = None
        if socket is not None:
            socket.abort()
            socket.deleteLater()
        self._timer.stop()
        self._schedule(0)

    def is_auto_execute(self) -> bool:
        """Whether proposals auto-approve and apply without a manual gate."""
        return self._auto_execute

    def set_auto_execute(self, enabled: bool) -> None:
        """Persist the auto-execute preference (survives a Houdini restart)."""
        self._auto_execute = bool(enabled)
        _save_auto_execute(self._auto_execute)

    def create_unnamed_session(self) -> None:
        """Create or focus the one empty placeholder conversation."""
        if self._session_create_inflight:
            return
        current = self._sessions.get(self._current_session_id or "")
        if (
            type(current) is dict
            and current.get("title") == "New session"
            and not self._runtime_state.snapshot().get("runs")
        ):
            # Already on the empty placeholder. A duplicate create is not sent
            # (the server's find_empty_placeholder would return this same one),
            # but the click must be acknowledged so the panel doesn't look dead.
            self.emptySessionFocused.emit()
            return
        self._session_create_inflight = True
        self._send(
            "session.create",
            {"title": "New session"},
            "session.create",
        )

    def select_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        self._preferred_session_id = session_id
        _save_preferred_session_id(session_id)
        # An explicit selection ends the fresh-open bootstrap: honor this
        # choice even if the very first session.list has not resolved yet.
        self._bootstrap_complete = True
        if session_id != self._current_session_id:
            # A connection has no unsubscribe command. Reconnect so switching
            # Sessions never leaves hidden live subscriptions behind.
            self.reconnect_now()

    def delete_session(self, session_id: str) -> None:
        # The server cascades runs/events and removes artifacts for the Session.
        # The authoritative confirmation is the success response (the broadcast
        # session.deleted control event carries seq=None and is dropped by the
        # cursor); _handle_response drops it from the local cache and, if it was
        # the active Session, reconnects so the next session.list selects a valid
        # conversation.
        self._send("session.delete", {"session_id": session_id}, "session.delete")

    def archive_session(self, session_id: str) -> None:
        # Archived Sessions are hidden from the default list (include_archived
        # is False on session.list). _handle_response drops it from the local
        # cache and reconnects so the sidebar no longer offers it.
        self._send("session.archive", {"session_id": session_id}, "session.archive")

    def cached_session_title(self, session_id: str) -> str | None:
        # Read-only view of the cached Session title, for UI affordances (e.g.
        # an archive/delete confirmation). Returns None when the id is unknown
        # to this client so the caller can fall back to a generic label.
        session = self._sessions.get(session_id)
        if type(session) is dict and type(session.get("title")) is str:
            return session["title"]
        return None

    def start_run(self, user_input: str) -> None:
        session_id = self._current_session_id
        if session_id is None:
            # No active Session: auto-create one with a placeholder title and
            # hold the prompt until the new Session activates post-reconnect.
            # The backend renames it to a meaningful title once the first run
            # completes (see RuntimeService auto-title).
            self._pending_run_input = user_input
            self.create_unnamed_session()
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

    def recover_changeset(self, change_id: str) -> None:
        self._send("changeset.recover", {"change_id": change_id}, "changeset.recover")

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

    def create_workspace(self, expected_scene_epoch: int | None = None) -> None:
        session_id = self._current_session_id
        if session_id is None:
            self.commandFailed.emit(
                "workspace.create",
                "runtime.session_required",
                "Create or select a Session before creating a Workspace.",
                True,
                False,
            )
            return
        self._send(
            "workspace.create",
            {
                "session_id": session_id,
                "expected_scene_epoch": expected_scene_epoch,
            },
            "workspace.create",
        )

    def inspect_workspace(self, workspace_id: str | None = None) -> None:
        session_id = self._current_session_id
        if session_id is None:
            return
        self._send(
            "workspace.inspect",
            {
                "session_id": session_id,
                "workspace_id": workspace_id,
                "expected_scene_epoch": None,
            },
            "workspace.inspect",
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
        socket.textMessageReceived.connect(self._on_text_message)
        # Runtime builds through Task 17-B encoded JSON as binary WebSocket
        # frames. Accept both frame types so the panel can reconnect to an
        # already-running pre-fix Runtime while new servers send text frames.
        socket.binaryMessageReceived.connect(self._on_binary_message)
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
        self._current_session_id = None
        self._current_session_title = ""
        self.sessionChanged.emit("", "Loading Session", 0)
        self.connectionChanged.emit("online", "Runtime connected")
        self._send("runtime.ping", {}, "ping")
        self._send("session.list", {"include_archived": False}, "session.list")

    @QtCore.Slot()
    def _on_disconnected(self) -> None:
        if self.sender() is not self._socket:
            return
        if self._stopping:
            return
        self._current_session_id = None
        self._current_session_title = ""
        self._session_create_inflight = False
        self.sessionChanged.emit("", "Runtime disconnected", 0)
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
    def _on_text_message(self, raw: str) -> None:
        if self.sender() is not self._socket:
            return
        self._process_message(raw)

    @QtCore.Slot(QtCore.QByteArray)
    def _on_binary_message(self, raw) -> None:
        if self.sender() is not self._socket:
            return
        self._process_message(bytes(raw))

    def _process_message(self, raw: str | bytes) -> None:
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
            if purpose == "session.create":
                self._session_create_inflight = False
                # Auto-create failed: drop the stashed prompt so a later manual
                # retry isn't silently swallowed when a Session activates.
                self._pending_run_input = None
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
            self._session_create_inflight = False
            if type(result) is dict and type(result.get("session_id")) is str:
                self._preferred_session_id = result["session_id"]
                _save_preferred_session_id(result["session_id"])
            self.commandSucceeded.emit(purpose, result)
            self.reconnect_now()
            return
        if purpose in ("session.delete", "session.archive"):
            # Drop the affected Session from the local cache. The server already
            # removed (delete) or hid (archive) it, so the next session.list
            # reflects the new state. If it was the active/preferred Session,
            # clear the preference and reconnect so a valid conversation is
            # chosen; otherwise a lightweight sidebar refresh suffices.
            sid = (
                result.get("session_id")
                if type(result) is dict and type(result.get("session_id")) is str
                else None
            )
            if sid:
                self._sessions.pop(sid, None)
            self.commandSucceeded.emit(purpose, result)
            if sid == self._current_session_id or sid == self._preferred_session_id:
                self._preferred_session_id = None
                _save_preferred_session_id("")
                self.reconnect_now()
            else:
                self.sessionsChanged.emit(
                    tuple(self._sessions.values()),
                    self._current_session_id or "",
                )
            return
        if purpose in ("session.snapshot", "session.bootstrap_snapshot"):
            try:
                self._runtime_state.load_snapshot(result)
                snapshot = self._runtime_state.snapshot()
                self._cursors.advance(
                    snapshot["session_id"], snapshot["last_seq"]
                )
            except PanelClientError as exc:
                self.connectionChanged.emit("error", str(exc))
                return
            if purpose == "session.bootstrap_snapshot":
                session_id = snapshot["session_id"]
                if session_id != self._current_session_id:
                    return
                cursor = snapshot["last_seq"]
                self._send(
                    "session.subscribe",
                    {"session_id": session_id, "last_seq": cursor},
                    "session.subscribe",
                )
                self.sessionChanged.emit(
                    session_id, self._current_session_title, cursor
                )
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
            "workspace.create",
            "workspace.bind",
            "workspace.inspect",
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
        # A freshly opened panel ignores the persisted preferred id and opens
        # the empty placeholder ("New session"). Every later reconnect honors
        # the preferred id so an in-progress conversation survives.
        bootstrap = not self._bootstrap_complete
        self._bootstrap_complete = True
        preferred = None if bootstrap else self._preferred_session_id
        try:
            if bootstrap:
                selected = choose_empty_placeholder(sessions)
            else:
                selected = choose_active_session(sessions, preferred)
        except PanelClientError as exc:
            self.connectionChanged.emit("error", str(exc))
            return
        self._sessions = {
            item["session_id"]: dict(item)
            for item in sessions
            if type(item) is dict and type(item.get("session_id")) is str
        }
        if selected is None:
            if bootstrap:
                # No empty placeholder exists yet: create one. The create
                # result triggers a reconnect whose session.list will find it
                # (bootstrap is now complete, but create_unnamed_session sets
                # the preferred id so the next list activates it directly).
                self.create_unnamed_session()
                return
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
        _save_preferred_session_id(session_id)
        self._current_session_id = session_id
        self._current_session_title = (
            "未命名对话" if title == "New session" else title
        )
        # Bootstrap from one bounded snapshot, then subscribe from that exact
        # boundary. Events committed between the snapshot and subscribe are
        # replayed by the server, so this is gap-free without replaying an
        # entire high-volume Session from seq 0 into Qt.
        self.sessionChanged.emit("", f"Loading {self._current_session_title}", 0)
        self._request_snapshot(purpose="session.bootstrap_snapshot")
        self.refresh_changesets()
        # If the user sent a prompt that triggered auto-create, fire the run
        # now that the new Session is active. The server queues run events
        # until our subscribe catches up, so no prompt is lost.
        if self._pending_run_input is not None:
            prompt = self._pending_run_input
            self._pending_run_input = None
            self._send(
                "run.start",
                {"session_id": session_id, "user_input": prompt},
                "run.start",
            )

    def _request_snapshot(self, *, purpose: str = "session.snapshot") -> None:
        if self._current_session_id is None:
            return
        self._send(
            "session.snapshot",
            {"session_id": self._current_session_id},
            purpose,
        )

    def _apply_renamed_session(self, payload: object) -> None:
        # Update the cached Session title from a session.renamed event so the
        # sidebar reflects an auto-generated title without an extra list call.
        if type(payload) is not dict:
            return
        sid = payload.get("session_id")
        title = payload.get("title")
        if type(sid) is not str or type(title) is not str or not title:
            return
        cached = self._sessions.get(sid)
        if type(cached) is dict:
            cached["title"] = title
        if sid == self._current_session_id:
            self._current_session_title = title
            self.sessionChanged.emit(
                sid, title, self._cursors.last_seq(sid))
        self.sessionsChanged.emit(
            tuple(self._sessions.values()),
            self._current_session_id or "",
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
            # A renamed Session (e.g. auto-titled after the first run) updates
            # the cached title and the sidebar in place — no extra round-trip.
            if message.get("type") == "session.renamed":
                self._apply_renamed_session(message.get("payload"))
            if changeset_refresh_required(message):
                self._schedule_changeset_refresh()
            if artifact_refresh_required(message):
                try:
                    summary = parse_artifact_event(message)
                except PanelClientError as exc:
                    self.connectionChanged.emit("error", str(exc))
                    return
                self.artifactObserved.emit(summary)
            if vision_refresh_required(message):
                try:
                    vision_summary = parse_vision_event(message)
                except PanelClientError as exc:
                    self.connectionChanged.emit("error", str(exc))
                    return
                self.visionObserved.emit(vision_summary)
            if session_id == self._current_session_id:
                self.sessionChanged.emit(
                    session_id, self._current_session_title, seq
                )
