"""Conversation pane: bounded card flow plus an IME-safe composer."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme
from houdini_side.runtime_panel.client import RunRequestEdit
from houdini_side.runtime_panel.view_models import MAX_MESSAGES, MessageItem

_TONE_COLORS = {
    "normal": theme.DIVIDER,
    "ok": theme.STATUS_OK,
    "warn": theme.STATUS_WARN,
    "error": theme.STATUS_ERROR,
    "gate": theme.HIGHLIGHT,
}


class _Card(QtWidgets.QFrame):
    def __init__(self, item: MessageItem, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("GateCard" if item.tone == "gate" else "Card")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        title = QtWidgets.QLabel(item.title)
        title.setObjectName("ProminentLabel")
        layout.addWidget(title)
        body = QtWidgets.QLabel(item.body)
        body.setWordWrap(True)
        body.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        if item.mono:
            body.setFont(QtGui.QFont(theme.mono_font_family()))
        layout.addWidget(body)
        # Left tone stripe via stylesheet token reference.
        color = _TONE_COLORS[item.tone]
        self.setStyleSheet(
            f"QFrame#{self.objectName()} {{ border-left: 3px solid {color}; }}")


class ConversationView(QtWidgets.QWidget):
    """Center pane: append-only bounded card flow plus composer."""

    sendRequested = QtCore.Signal(str)
    stopRequested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConversationView")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.flow_host = QtWidgets.QWidget()
        self.flow = QtWidgets.QVBoxLayout(self.flow_host)
        self.flow.setContentsMargins(2, 2, 2, 2)
        self.flow.addStretch(1)
        self.scroll.setWidget(self.flow_host)
        layout.addWidget(self.scroll, 1)

        composer = QtWidgets.QHBoxLayout()
        self.input = RunRequestEdit()
        self.input.setPlaceholderText("Describe what to build…")
        self.send_button = QtWidgets.QPushButton("Send")
        self.send_button.setAutoDefault(False)
        self.send_button.clicked.connect(self._send)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setAutoDefault(False)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stopRequested)
        composer.addWidget(self.input, 1)
        composer.addWidget(self.send_button)
        composer.addWidget(self.stop_button)
        layout.addLayout(composer)
        self._cards: list[_Card] = []

    def append_item(self, item: MessageItem) -> None:
        card = _Card(item)
        self.flow.insertWidget(self.flow.count() - 1, card)
        self._cards.append(card)
        while len(self._cards) > MAX_MESSAGES:
            old = self._cards.pop(0)
            self.flow.removeWidget(old)
            old.deleteLater()
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_composer_state(self, state: str) -> None:
        """state: idle | running | stopping (mirrors Run state machine)."""
        self.send_button.setEnabled(state == "idle")
        self.input.setEnabled(state == "idle")
        self.stop_button.setEnabled(state == "running")
        self.stop_button.setText("Stopping…" if state == "stopping" else "Stop")

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.sendRequested.emit(text)
