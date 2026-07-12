"""PySide6 chat panel for the eee_agent — runs INSIDE Houdini (thin client).

It spawns the agent process (`.venv\\python.exe -m eee_agent.cli stdio`) via
QProcess and talks JSON-lines:
  panel -> agent stdin:  {"type":"user","text":"..."}
  agent -> panel stdout: {"type":"ready"|"token"|"tool"|"done"|"error", ...}

Houdini's Python needs ONLY its bundled PySide6 — no extra deps installed into
Houdini. The heavy agent stack lives in .venv.

Open from a Houdini shelf / Python Source Editor:
    import sys
    sys.path.insert(0, r"Z:\\EEE_Project\\EEEProceduralModeling\\houdini_side")
    import chat_panel, start_rpc
    start_rpc.start()        # ensure the bridge is up (idempotent-ish)
    chat_panel.open_panel()
"""
from __future__ import annotations

import json
import os

from PySide6 import QtCore, QtGui, QtWidgets

# houdini_side/ -> repo root
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class ChatPanel(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("EEE Procedural Modeling Agent")
        self.resize(560, 640)
        # Force an independent floating top-level window even when parented to
        # Houdini's main window (otherwise it renders as an embedded child widget
        # and overlaps the Houdini UI). Still lifecycle-tied to Houdini.
        self.setWindowFlag(QtCore.Qt.Window)
        self.proc: QtCore.QProcess | None = None
        self._awaiting_agent = False
        self._build_ui()
        self._start_agent()

    # ---- UI ---------------------------------------------------------------
    def _build_ui(self) -> None:
        lay = QtWidgets.QVBoxLayout(self)

        self.view = QtWidgets.QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        lay.addWidget(self.view)

        row = QtWidgets.QHBoxLayout()
        self.input = QtWidgets.QLineEdit()
        self.input.setPlaceholderText("Ask the agent to build something…  (Enter to send)")
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        self.send_btn = QtWidgets.QPushButton("Send")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.send_btn)
        lay.addLayout(row)

        self.status = QtWidgets.QLabel("starting agent…")
        lay.addWidget(self.status)

    # ---- agent process ----------------------------------------------------
    def _start_agent(self) -> None:
        if not os.path.exists(VENV_PY):
            self.status.setText(f"venv python not found: {VENV_PY}")
            self.send_btn.setEnabled(False)
            return
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessChannelMode(QtCore.QProcess.SeparateChannels)
        self.proc.setWorkingDirectory(REPO)  # so .env (load_dotenv) is found
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        # Inherit Houdini's env; the child also loads <repo>/.env via config.py.
        self.proc.setProcessEnvironment(QtCore.QProcessEnvironment.systemEnvironment())
        self.proc.start(VENV_PY, ["-m", "eee_agent.cli", "stdio"])

    def _send(self) -> None:
        if self.proc is None or self.proc.state() != QtCore.QProcess.Running:
            self.status.setText("agent not running")
            return
        text = self.input.text().strip()
        if not text:
            return
        self.view.appendHtml("<b>you:</b> " + _esc(text))
        self.input.clear()
        self._awaiting_agent = True
        self.proc.write((json.dumps({"type": "user", "text": text}) + "\n").encode("utf-8"))

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
            self.status.setText("ready")
        elif kind == "token":
            if self._awaiting_agent:
                self.view.appendPlainText("")  # new line
                self._awaiting_agent = False
            self.view.moveCursor(QtGui.QTextCursor.End)
            self.view.insertPlainText(msg.get("text", ""))
        elif kind == "tool":
            self.view.appendHtml(
                "<i style='color:#2a7'>▸ " + _esc(msg.get("name", "tool")) + "</i>"
            )
        elif kind == "done":
            self.view.appendPlainText("")
            self.status.setText("done")
        elif kind == "error":
            self.view.appendHtml(
                "<span style='color:#c00'>[error] " + _esc(msg.get("text", "")) + "</span>"
            )

    def _on_stderr(self) -> None:
        assert self.proc is not None
        data = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        if data.strip():
            # LangSmith/langchain warnings land here; surface a short tail.
            self.status.setText("stderr: " + " ".join(data.split())[:120])

    def _on_finished(self, code, _status) -> None:
        self.status.setText(f"agent exited (code {code})")
        self.send_btn.setEnabled(False)

    # ---- teardown ---------------------------------------------------------
    def closeEvent(self, event):
        if self.proc and self.proc.state() == QtCore.QProcess.Running:
            self.proc.kill()
        super().closeEvent(event)


_panel: ChatPanel | None = None


def _main_window():
    """Best-effort Houdini main window as a Qt parent.

    hython (headless) has no UI at all, so we can't introspect the exact GUI API
    there. Instead we scan QApplication top-level widgets for a QMainWindow
    (robust across Houdini versions), then fall back to hou.qt.mainWindow(), then
    None. Parenting is cosmetic — None still works as a free-floating window.
    """
    try:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for w in app.topLevelWidgets():
                if w.inherits("QMainWindow") and w.isVisible():
                    return w
    except Exception:
        pass
    try:
        import hou
        if hasattr(hou, "qt") and hasattr(hou.qt, "mainWindow"):
            return hou.qt.mainWindow()
    except Exception:
        pass
    return None


def open_panel() -> ChatPanel:
    """Create (or reuse) and show the chat panel, parented to Houdini's main window."""
    global _panel
    parent = _main_window()
    if _panel is None:
        _panel = ChatPanel(parent)
    _panel.show()
    _panel.raise_()
    _panel.activateWindow()
    return _panel


if __name__ == "__main__":
    # Standalone test (outside Houdini) — needs a display.
    import sys
    app = QtWidgets.QApplication(sys.argv)
    w = ChatPanel()
    w.show()
    sys.exit(app.exec())
