"""PySide6 chat panel v3 for the eee_agent — runs INSIDE Houdini (thin client).

Dark "control-surface" theme tuned for Houdini: a live status dot + monospace
telemetry strip header, a compact plan list, a streaming chat log with role
labels and monospace tool lines, and a Send/Stop toggle.

Spawns the agent process (`.venv\\python.exe -m eee_agent.cli stdio`) via QProcess
and talks JSON-lines. Houdini's Python needs only its bundled PySide6.

Open from the EEE Agent menu (installed via houdini_side/install_menu.py), or:
    exec(open(r"<repo>/houdini_side/launch.py").read())
"""
from __future__ import annotations

import json
import os

from PySide6 import QtCore, QtGui, QtWidgets

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")

# ---- design tokens (dark "control-surface" theme, Houdini-native) ----------
BG_WINDOW = "#17181b"   # window base
BG_PANEL = "#1e2024"    # elevated surfaces
BG_INSET = "#15161a"    # recessed fields (chat, input, list)
BORDER = "#2e3137"      # hairlines
TEXT = "#e6e7ea"        # primary text
TEXT_DIM = "#8b9099"    # secondary / meta / eyebrows
ACCENT = "#ff8a3d"      # Houdini orange — the single chromatic accent
OK = "#57c785"          # success / done
ACTIVE = "#ff8a3d"      # in-flight
ERROR = "#e55952"       # failure

SANS = "'Segoe UI', Arial, sans-serif"
MONO = "'Consolas', 'Courier New', monospace"
SZ_BODY = "13px"
SZ_META = "12px"
SZ_EYE = "11px"         # eyebrows / role labels (uppercase)

# dot glyph + color per agent state
_DOT_STYLE = {
    "idle": ("○", TEXT_DIM),
    "thinking": ("◐", ACTIVE),
    "tool": ("▸", ACCENT),
    "done": ("●", OK),
    "error": ("✗", ERROR),
}
_STATE_COLOR = {"idle": TEXT_DIM, "thinking": ACTIVE, "tool": ACCENT,
                "done": OK, "error": ERROR}

_QSS = f"""
QWidget#ChatPanel {{
    background: {BG_WINDOW};
    color: {TEXT};
    font-family: {SANS};
    font-size: {SZ_BODY};
}}
QFrame#sep {{ background: {BORDER}; max-height: 1px; border: none; }}
QLabel {{ background: transparent; color: {TEXT}; }}
QLabel#title {{ color: {TEXT}; font-size: {SZ_BODY}; font-weight: 700; }}
QLabel#eyebrow {{ color: {TEXT_DIM}; font-size: {SZ_EYE}; font-weight: 600; }}
QLabel#telemetry {{ color: {TEXT_DIM}; font-family: {MONO}; font-size: {SZ_META}; }}
QListWidget#todos {{
    background: {BG_INSET}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 4px; color: {TEXT}; outline: 0;
}}
QListWidget#todos::item {{ padding: 2px 2px; border: none; }}
QPlainTextEdit#chat {{
    background: {BG_INSET}; border: 1px solid {BORDER}; border-radius: 4px;
    color: {TEXT}; padding: 8px; selection-background-color: {ACCENT};
}}
QLineEdit#input {{
    background: {BG_INSET}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 8px; color: {TEXT}; selection-background-color: {ACCENT};
}}
QLineEdit#input:focus {{ border: 1px solid {ACCENT}; }}
"""

_BTN_BASE = (f"font-family: {SANS}; font-size: {SZ_BODY}; font-weight: 600; "
             f"border-radius: 4px; padding: 8px 18px; min-width: 74px;")
BTN_SEND_STYLE = (
    f"QPushButton {{ background: transparent; color: {ACCENT}; "
    f"border: 1px solid {ACCENT}; {_BTN_BASE} }}"
    f"QPushButton:hover {{ background: {ACCENT}; color: {BG_WINDOW}; }}"
    f"QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BORDER}; }}"
)
BTN_STOP_STYLE = (
    f"QPushButton {{ background: {ERROR}; color: white; "
    f"border: 1px solid {ERROR}; {_BTN_BASE} }}"
    f"QPushButton:hover {{ background: #c0453f; border-color: #c0453f; }}"
)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_tok(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ---- rich-text block builders (Qt-supported inline-CSS subset only) --------
def _h_role(name: str) -> str:
    return (f"<span style='color:{TEXT_DIM};font-family:{SANS};"
            f"font-size:{SZ_EYE};font-weight:600;'>{name}</span>")


def _h_thinking(text: str) -> str:
    return (f"<span style='color:{TEXT_DIM};font-family:{SANS};"
            f"font-size:{SZ_BODY};font-style:italic;'>{_esc(text)}</span>")


def _h_tool_call(name: str, args) -> str:
    head = (f"<span style='color:{ACCENT};font-family:{MONO};font-size:{SZ_META};"
            f"font-weight:600;'>▸ {_esc(name or 'tool')}</span>")
    if args:
        head += (f"  <span style='color:{TEXT_DIM};font-family:{MONO};"
                 f"font-size:{SZ_META};'>{_esc(str(args)[:160])}</span>")
    return head


def _h_tool_result(content) -> str:
    return (f"<span style='color:{TEXT_DIM};font-family:{MONO};font-size:{SZ_META};'>"
            f"→ {_esc(str(content)[:200])}</span>")


def _h_stopped() -> str:
    return (f"<span style='color:{ACTIVE};font-family:{SANS};font-size:{SZ_BODY};'>"
            f"[stopped]</span>")


def _h_error(text: str) -> str:
    return (f"<span style='color:{ERROR};font-family:{SANS};font-size:{SZ_BODY};'>"
            f"[error] {_esc(text)}</span>")


class ChatPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatPanel")
        self.setWindowTitle("EEE Procedural Modeling Agent")
        self.resize(720, 760)
        self.setMinimumSize(560, 640)
        self.setWindowFlag(QtCore.Qt.Window)
        self.proc: QtCore.QProcess | None = None
        self._running = False
        self._awaiting_agent = False
        self._streaming = False
        self._status_icon = "idle"
        self._status_label = "starting agent…"
        self._metric: dict = {}
        self._build_ui()
        self.setStyleSheet(_QSS)
        self._start_agent()

    # ---- UI ---------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        # --- header: dot + title .... status-word / monospace telemetry -------
        header = QtWidgets.QFrame()
        hl = QtWidgets.QVBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(8)
        self.dot = QtWidgets.QLabel("○")
        self.dot.setMinimumWidth(14)
        self.dot.setAlignment(QtCore.Qt.AlignCenter)
        self.title = QtWidgets.QLabel("EEE PROCEDURAL MODELING AGENT")
        self.title.setObjectName("title")
        row1.addWidget(self.dot)
        row1.addWidget(self.title)
        row1.addStretch(1)
        self.status_word = QtWidgets.QLabel(self._status_label)
        self.status_word.setObjectName("statusWord")
        row1.addWidget(self.status_word)
        hl.addLayout(row1)

        self.telemetry = QtWidgets.QLabel("step —    0↓ / 0↑    0.0s")
        self.telemetry.setObjectName("telemetry")
        self.telemetry.setIndent(22)   # sit under the title, past the dot
        hl.addWidget(self.telemetry)
        lay.addWidget(header)

        # gentle "live" pulse on the status dot while a turn runs
        self._dot_effect = QtWidgets.QGraphicsOpacityEffect(self.dot)
        self._dot_effect.setOpacity(1.0)
        self.dot.setGraphicsEffect(self._dot_effect)
        self._dot_anim = QtCore.QPropertyAnimation(self._dot_effect, b"opacity")
        self._dot_anim.setDuration(900)
        self._dot_anim.setKeyValueAt(0.0, 1.0)
        self._dot_anim.setKeyValueAt(0.5, 0.3)
        self._dot_anim.setKeyValueAt(1.0, 1.0)
        self._dot_anim.setLoopCount(-1)

        lay.addWidget(self._sep())

        # --- plan / todos (flat eyebrow + recessed list) ---------------------
        plan_label = QtWidgets.QLabel("PLAN")
        plan_label.setObjectName("eyebrow")
        lay.addWidget(plan_label)
        self.todo_list = QtWidgets.QListWidget()
        self.todo_list.setObjectName("todos")
        self.todo_list.setMaximumHeight(118)
        self.todo_list.setFocusPolicy(QtCore.Qt.NoFocus)
        lay.addWidget(self.todo_list)

        lay.addWidget(self._sep())

        # --- chat view (dark, recessed; streams tokens) ----------------------
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setObjectName("chat")
        self.view.setReadOnly(True)
        pal = self.view.palette()
        pal.setColor(QtGui.QPalette.Base, QtGui.QColor(BG_INSET))
        pal.setColor(QtGui.QPalette.Text, QtGui.QColor(TEXT))
        pal.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor(TEXT_DIM))
        self.view.setPalette(pal)
        lay.addWidget(self.view, 1)

        # --- input + send/stop ------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.input = QtWidgets.QLineEdit()
        self.input.setObjectName("input")
        self.input.setPlaceholderText("Ask the agent to build something…  (Enter to send)")
        self.input.returnPressed.connect(self._on_send_click)
        row.addWidget(self.input, 1)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.send_btn.setStyleSheet(BTN_SEND_STYLE)
        self.send_btn.clicked.connect(self._on_send_click)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

        self._refresh_status()

    def _sep(self) -> QtWidgets.QFrame:
        f = QtWidgets.QFrame()
        f.setObjectName("sep")
        f.setFixedHeight(1)
        return f

    # ---- agent process ----------------------------------------------------
    def _start_agent(self) -> None:
        if not os.path.exists(VENV_PY):
            self.status_word.setText(f"venv python not found: {VENV_PY}")
            self.status_word.setStyleSheet(
                f"color:{ERROR}; background: transparent;")
            self.send_btn.setEnabled(False)
            return
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessChannelMode(QtCore.QProcess.SeparateChannels)
        self.proc.setWorkingDirectory(REPO)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.setProcessEnvironment(QtCore.QProcessEnvironment.systemEnvironment())
        self.proc.start(VENV_PY, ["-m", "eee_agent.cli", "stdio"])

    def _write(self, obj: dict) -> None:
        if self.proc and self.proc.state() == QtCore.QProcess.Running:
            self.proc.write((json.dumps(obj) + "\n").encode("utf-8"))

    # ---- send / stop toggle -----------------------------------------------
    def _on_send_click(self) -> None:
        if self._running:
            self._write({"type": "stop"})   # cancel the current turn
            self._set_running(False)
            return
        text = self.input.text().strip()
        if not text or self.proc is None or self.proc.state() != QtCore.QProcess.Running:
            return
        self._streaming = False
        self.view.appendHtml(_h_role("YOU"))
        self.view.appendPlainText(text)
        self.input.clear()
        self._awaiting_agent = True
        self._write({"type": "user", "text": text})
        self._set_running(True)
        self._set_status("thinking", "thinking")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.send_btn.setText("Stop" if running else "Send")
        self.send_btn.setStyleSheet(BTN_STOP_STYLE if running else BTN_SEND_STYLE)
        self.input.setEnabled(not running)
        if running:
            self._dot_anim.start()
        else:
            self._dot_anim.stop()
            self._dot_effect.setOpacity(1.0)

    # ---- protocol parsing -------------------------------------------------
    def _on_stdout(self) -> None:
        assert self.proc is not None
        while self.proc.canReadLine():
            line = bytes(self.proc.readLine()).decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle(msg)

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ready":
            self._set_status("idle", "ready")
        elif kind == "token":
            self._begin_assistant_text()
            self.view.moveCursor(QtGui.QTextCursor.End)
            self.view.insertPlainText(msg.get("text", ""))
        elif kind == "thinking":
            self._begin_assistant_block()
            self._streaming = False
            self.view.appendHtml(_h_thinking(msg.get("text", "")))
        elif kind == "tool_call":
            self._set_status("tool", f"tool: {msg.get('name', '')}")
            self._streaming = False
            self.view.appendHtml(_h_tool_call(msg.get("name", "tool"),
                                              msg.get("args", "")))
        elif kind == "tool_result":
            self._streaming = False
            self.view.appendHtml(_h_tool_result(msg.get("content", "")))
        elif kind == "todo":
            self._update_todos(msg.get("tasks", []))
        elif kind == "metric":
            self._metric = msg
            self._refresh_status()
        elif kind == "done":
            self._streaming = False
            self.view.appendPlainText("")
            self._set_running(False)
            self._set_status("done", "done")
        elif kind == "cancelled":
            self._streaming = False
            self.view.appendHtml(_h_stopped())
            self._set_running(False)
            self._set_status("idle", "stopped")
        elif kind == "error":
            self._streaming = False
            self.view.appendHtml(_h_error(msg.get("text", "")))
            self._set_running(False)
            self._set_status("error", "error")

    def _begin_assistant_block(self) -> None:
        """Emit the ASSISTANT role label once per turn (before text/thinking)."""
        if self._awaiting_agent:
            self.view.appendHtml(_h_role("ASSISTANT"))
            self._awaiting_agent = False

    def _begin_assistant_text(self) -> None:
        """Ensure a fresh content line, then stream tokens into it."""
        self._begin_assistant_block()
        if not self._streaming:
            self.view.appendPlainText("")   # new content block
            self._streaming = True

    def _update_todos(self, tasks: list) -> None:
        self.todo_list.clear()
        for t in tasks:
            title = t.get("title", "?") if isinstance(t, dict) else str(t)
            status = t.get("status", "") if isinstance(t, dict) else ""
            mark, color = {
                "completed": ("✓", OK),
                "in_progress": ("▶", ACCENT),
                "pending": ("○", TEXT_DIM),
            }.get(status, ("○", TEXT_DIM))
            it = QtWidgets.QListWidgetItem(f"  {mark}   {title}")
            it.setForeground(QtGui.QColor(color))
            self.todo_list.addItem(it)

    def _set_status(self, icon_key: str, label: str) -> None:
        self._status_icon = icon_key
        self._status_label = label
        self._refresh_status()

    def _refresh_status(self) -> None:
        glyph, color = _DOT_STYLE.get(self._status_icon, ("○", TEXT_DIM))
        self.dot.setText(glyph)
        self.dot.setStyleSheet(f"color:{color}; background: transparent;")
        word_color = _STATE_COLOR.get(self._status_icon, TEXT_DIM)
        self.status_word.setText(self._status_label)
        self.status_word.setStyleSheet(
            f"color:{word_color}; background: transparent;")
        m = self._metric or {}
        self.telemetry.setText(
            f"step {m.get('step', '—')}    "
            f"{_fmt_tok(m.get('tokens_in', 0))}↓ / "
            f"{_fmt_tok(m.get('tokens_out', 0))}↑    "
            f"{m.get('elapsed', 0.0)}s"
        )

    def _on_stderr(self) -> None:
        assert self.proc is not None
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        if data.strip():
            self.status_word.setText("stderr: " + " ".join(data.split())[:120])
            self.status_word.setStyleSheet(f"color:{ERROR}; background: transparent;")

    def _on_finished(self, code, _status) -> None:
        self._set_running(False)
        self._set_status("idle", f"agent exited ({code})")
        self.send_btn.setEnabled(False)

    def closeEvent(self, event):
        self._dot_anim.stop()
        if self.proc and self.proc.state() == QtCore.QProcess.Running:
            self.proc.kill()
        super().closeEvent(event)


_panel: ChatPanel | None = None


def _main_window():
    try:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for w in app.topLevelWidgets():
                if w.inherits("QMainWindow") and w.isVisible():
                    return w
    except Exception:
        pass
    return None


def open_panel() -> ChatPanel:
    global _panel
    parent = _main_window()
    if _panel is None:
        _panel = ChatPanel(parent)
    else:
        _panel.show()
        _panel.raise_()
    return _panel


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    w = ChatPanel()
    w.show()
    sys.exit(app.exec())
