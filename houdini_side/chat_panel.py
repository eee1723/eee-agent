"""PySide6 chat panel v2 for the eee_agent — runs INSIDE Houdini (thin client).

Shows runtime info: streaming tokens, thinking, tool-call cards (name+args+result),
the live plan (todos), and metrics (step / tokens / elapsed). Send button toggles to
Stop while a turn is running (cancels the current turn; no pause/HITL).

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

_STATUS_ICON = {"idle": "○", "thinking": "◐", "tool": "▸", "done": "●", "error": "✗"}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ChatPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("EEE Procedural Modeling Agent")
        self.resize(720, 760)
        self.setWindowFlag(QtCore.Qt.Window)
        self.proc: QtCore.QProcess | None = None
        self._running = False
        self._awaiting_agent = False
        self._build_ui()
        self._start_agent()

    # ---- UI ---------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QtWidgets.QVBoxLayout(self)

        # top status bar (state + metrics)
        self.status = QtWidgets.QLabel("starting agent…")
        self.status.setStyleSheet("color:#444; padding:2px;")
        lay.addWidget(self.status)

        # plan / todos (compact)
        todo_box = QtWidgets.QGroupBox("Plan")
        tb = QtWidgets.QVBoxLayout(todo_box)
        self.todo_list = QtWidgets.QListWidget()
        self.todo_list.setMaximumHeight(110)
        self.todo_list.setStyleSheet("QListWidget { font-size: 11px; }")
        tb.addWidget(self.todo_list)
        lay.addWidget(todo_box)

        # chat view
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)

        # input + send/stop
        row = QtWidgets.QHBoxLayout()
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText("Ask the agent to build something…  (Enter to send)")
        self.input.returnPressed.connect(self._on_send_click)
        row.addWidget(self.input, 1)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._on_send_click)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

    # ---- agent process ----------------------------------------------------
    def _start_agent(self) -> None:
        if not os.path.exists(VENV_PY):
            self.status.setText(f"venv python not found: {VENV_PY}")
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

    # ---- send / stop toggle ----------------------------------------------
    def _on_send_click(self) -> None:
        if self._running:
            # Stop: cancel the current turn
            self._write({"type": "stop"})
            self._set_running(False)
            return
        text = self.input.text().strip()
        if not text or self.proc is None or self.proc.state() != QtCore.QProcess.Running:
            return
        self.view.appendHtml("<b>you:</b> " + _esc(text))
        self.input.clear()
        self._awaiting_agent = True
        self._write({"type": "user", "text": text})
        self._set_running(True)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.send_btn.setText("Stop" if running else "Send")
        self.send_btn.setStyleSheet("background:#c33; color:white;" if running else "")
        self.input.setEnabled(not running)

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
            self._set_status("ready", "ready")
        elif kind == "token":
            if self._awaiting_agent:
                self.view.appendPlainText("")
                self._awaiting_agent = False
            self.view.moveCursor(QtGui.QTextCursor.End)
            self.view.insertPlainText(msg.get("text", ""))
        elif kind == "thinking":
            self.view.appendHtml("<span style='color:#888; font-style:italic;'>"
                                 + _esc(msg.get("text", "")) + "</span>")
        elif kind == "tool_call":
            self._set_status("tool", f"tool: {msg.get('name','')}")
            args = msg.get("args", "")
            self.view.appendHtml("<span style='color:#2a7;'>▸ "
                                 + _esc(msg.get("name", "tool")) + "</span> "
                                 + ("<span style='color:#888;'>" + _esc(str(args)[:160]) + "</span>" if args else ""))
        elif kind == "tool_result":
            self.view.appendHtml("<span style='color:#555;'>  → "
                                 + _esc(str(msg.get("content", ""))[:200]) + "</span>")
        elif kind == "todo":
            self._update_todos(msg.get("tasks", []))
        elif kind == "metric":
            self._metric = msg
            self._refresh_status()
        elif kind == "done":
            self.view.appendPlainText("")
            self._set_running(False)
            self._set_status("done", "done")
        elif kind == "cancelled":
            self.view.appendHtml("<span style='color:#a70;'>[stopped]</span>")
            self._set_running(False)
            self._set_status("idle", "stopped")
        elif kind == "error":
            self.view.appendHtml("<span style='color:#c00;'>[error] "
                                 + _esc(msg.get("text", "")) + "</span>")
            self._set_running(False)
            self._set_status("error", "error")

    def _update_todos(self, tasks: list) -> None:
        self.todo_list.clear()
        for t in tasks:
            title = t.get("title", "?") if isinstance(t, dict) else str(t)
            status = t.get("status", "") if isinstance(t, dict) else ""
            mark = {"completed": "✓", "in_progress": "▶", "pending": "○"}.get(status, "○")
            it = QtWidgets.QListWidgetItem(f"{mark}  {title}")
            if status == "completed":
                it.setForeground(QtGui.QColor("#888"))
            elif status == "in_progress":
                it.setForeground(QtGui.QColor("#2a7"))
            self.todo_list.addItem(it)

    def _set_status(self, icon_key: str, label: str) -> None:
        self._status_label = label
        self._status_icon = icon_key
        self._refresh_status()

    def _refresh_status(self) -> None:
        icon = _STATUS_ICON.get(getattr(self, "_status_icon", "idle"), "○")
        label = getattr(self, "_status_label", "—")
        m = getattr(self, "_metric", {}) or {}
        if m:
            label = (f"{label}   step {m.get('step','?')}   "
                     f"tok {m.get('tokens_in',0)}↓/{m.get('tokens_out',0)}↑   "
                     f"{m.get('elapsed',0)}s")
        self.status.setText(f"{icon}  {label}")

    def _on_stderr(self) -> None:
        assert self.proc is not None
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        if data.strip():
            self.status.setText("stderr: " + " ".join(data.split())[:120])

    def _on_finished(self, code, _status) -> None:
        self._set_running(False)
        self._set_status("idle", f"agent exited ({code})")
        self.send_btn.setEnabled(False)

    def closeEvent(self, event):
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
