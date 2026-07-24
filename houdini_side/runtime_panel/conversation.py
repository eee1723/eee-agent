"""Conversation pane: bounded card flow plus an IME-safe composer.

Cards are rendered by ``kind`` so the user, the assistant, and structural
events (proposals, artifacts, notices) each get a distinct visual weight —
that contrast is what keeps the flow from reading as one undifferentiated
block. Assistant replies stream in place (``update_body``) with a blinking
cursor while a run is in flight, and optional thinking text is shown in a
collapsible block.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme
from houdini_side.runtime_panel.client import RunRequestEdit
from houdini_side.runtime_panel.view_models import MAX_MESSAGES, MessageItem

_TONE_COLORS = {
    "normal": theme.PRIMARY,
    "ok": theme.STATUS_OK,
    "warn": theme.STATUS_WARN,
    "error": theme.STATUS_ERROR,
    "gate": theme.HIGHLIGHT,
}

# Blinking streaming-cursor cadence (ms).
_CURSOR_BLINK_MS = 500
_CURSOR = "▍"
# Coalesced autoscroll interval: instead of one zero-delay QTimer per token
# (which forces a relayout + scrollbar update on every token and piles up
# during fast streaming), batch scrolls to ~16x/sec.
_SCROLL_FLUSH_MS = 60


class _Card(QtWidgets.QFrame):
    """One message card. Rendering branches on ``item.kind``.

    - user: a right-aligned bubble (no title, accent surface)
    - assistant / assistant_streaming: a left-aligned wide card with an
      optional collapsible thinking block; streaming cards update in place
    - everything else: a structural card (proposal/vision/artifact/notice)
      keyed off its tone stripe
    """

    def __init__(self, item: MessageItem, parent=None) -> None:
        super().__init__(parent)
        self._kind = item.kind
        self._streaming = item.kind == "assistant_streaming"
        self._cursor_on = False

        if item.kind == "user":
            self.setObjectName("UserBubble")
        elif item.kind in ("assistant", "assistant_streaming"):
            self.setObjectName("AssistantCard")
        elif item.tone == "gate":
            self.setObjectName("GateCard")
        else:
            self.setObjectName("Card")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        # Structural cards keep their title; user/assistant bubbles omit it
        # (the sender is conveyed by alignment + surface, not a label).
        if item.kind not in ("user", "assistant", "assistant_streaming"):
            title = QtWidgets.QLabel(item.title)
            title.setObjectName("ProminentLabel")
            layout.addWidget(title)

        self.body_label = QtWidgets.QLabel(self._render_body(item))
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        if item.mono:
            self.body_label.setFont(QtGui.QFont(theme.mono_font_family()))
        layout.addWidget(self.body_label)

        # The model's reasoning stream is rendered by the dedicated
        # ThinkingPanel anchored below the timeline, not duplicated as a
        # collapsible block inside each assistant card. These stay None so any
        # legacy reference degrades gracefully.
        self._thinking_toggle = None
        self._thinking_label = None

        # Tone stripe + surface are set per kind via objectName + stylesheet.
        if item.kind not in ("user", "assistant", "assistant_streaming"):
            color = _TONE_COLORS[item.tone]
            self.setStyleSheet(
                f"QFrame#{self.objectName()} {{ border-left: 3px solid {color}; }}")

        if self._streaming:
            self._cursor_timer = QtCore.QTimer(self)
            self._cursor_timer.setInterval(_CURSOR_BLINK_MS)
            self._cursor_timer.timeout.connect(self._blink_cursor)
            self._cursor_timer.start()
        else:
            self._cursor_timer = None

    def update_body(self, text: str, *, thinking: str = "") -> None:
        """Update an in-flight assistant card in place (streaming).

        ``thinking`` is accepted for signature compatibility but ignored: the
        reasoning stream is shown in the ThinkingPanel below the timeline, not
        inside the card.
        """
        if self._kind not in ("assistant", "assistant_streaming"):
            return
        self.body_label.setText(self._render_body(MessageItem(
            self._kind, "", text, "normal")))

    def stop_streaming(self) -> None:
        """Freeze the cursor animation once the run reaches a terminal state."""
        if self._cursor_timer is not None:
            self._cursor_timer.stop()
            self._cursor_on = False
            self._streaming = False
            # Strip any trailing cursor from the displayed text.
            self.body_label.setText(self.body_label.text().rstrip(_CURSOR))

    def _render_body(self, item: MessageItem) -> str:
        text = item.body
        if self._streaming and self._cursor_on:
            return text + _CURSOR
        return text

    def _blink_cursor(self) -> None:
        self._cursor_on = not self._cursor_on
        self.body_label.setText(self._render_body(MessageItem(
            self._kind, "", self.body_label.text().rstrip(_CURSOR), "normal")))


class ConversationView(QtWidgets.QWidget):
    """Center pane: bounded card flow plus composer."""

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
        self.flow.setSpacing(8)
        self.flow.addStretch(1)
        self.scroll.setWidget(self.flow_host)
        layout.addWidget(self.scroll, 1)

        # Pi-style folding panels anchored below the timeline: the agent's
        # intermediate steps (tool calls/results + assistant text segments) and
        # the reasoning stream live here so the conversation flow stays a clean
        # user → assistant dialogue. They update incrementally (keyed by step
        # ref) and never rebuild on token deltas.
        from houdini_side.runtime_panel.activity_panel import (
            ActivityPanel, ThinkingPanel)
        self.activity_panel = ActivityPanel(self)
        self.thinking_panel = ThinkingPanel(self)
        layout.addWidget(self.activity_panel)
        layout.addWidget(self.thinking_panel)

        composer = QtWidgets.QHBoxLayout()
        self.input = RunRequestEdit()
        self.send_button = QtWidgets.QPushButton("Send")
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.setAutoDefault(False)
        self.send_button.clicked.connect(self._send)
        # Ctrl+Enter in the multi-line editor also sends (bare Enter just
        # inserts a newline — IME candidate confirmation stays safe).
        self.input.submitRequested.connect(self._send)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setObjectName("DangerButton")
        self.stop_button.setAutoDefault(False)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stopRequested)
        composer.addWidget(self.input, 1)
        composer.addWidget(self.send_button)
        composer.addWidget(self.stop_button)
        layout.addLayout(composer)
        self._cards: list[_Card] = []
        # Coalesced autoscroll: only one timer is ever pending, so fast token
        # streams don't queue dozens of relayout-forcing zero-delay timers.
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.setInterval(_SCROLL_FLUSH_MS)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._flush_scroll)

    def _schedule_scroll(self) -> None:
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _flush_scroll(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_item(self, item: MessageItem) -> None:
        card = _Card(item)
        # User bubbles right-align; everything else stays left.
        if item.kind == "user":
            row = self._wrapped_right(card)
            self.flow.insertWidget(self.flow.count() - 1, row)
        else:
            self.flow.insertWidget(self.flow.count() - 1, card)
        self._cards.append(card)
        while len(self._cards) > MAX_MESSAGES:
            old = self._cards.pop(0)
            self.flow.removeWidget(old)
            old.deleteLater()
        # Layout activation is deferred to the next event-loop pass; scroll on
        # the coalesced timer once layout has settled.
        self._schedule_scroll()

    def replace_last_assistant(self, item: MessageItem) -> None:
        """Swap the trailing streaming/assistant card for a final one.

        Used when a run terminates: the in-place streaming card becomes a
        static assistant_message so the cursor stops and the final text settles.
        """
        if not self._cards:
            self.append_item(item)
            return
        last = self._cards[-1]
        if last._kind in ("assistant", "assistant_streaming"):
            last.stop_streaming()
            last.deleteLater()
            self._cards.pop()
            self.flow.removeWidget(last)
        self.append_item(item)

    def update_streaming(self, text: str, *, thinking: str = "") -> bool:
        """Update the trailing assistant card in place; create it if absent.

        Returns True if a streaming card was updated or created.
        """
        from houdini_side.runtime_panel.view_models import streaming_assistant

        if self._cards and self._cards[-1]._kind == "assistant_streaming":
            self._cards[-1].update_body(text, thinking=thinking)
            self._schedule_scroll()
            return True
        self.append_item(streaming_assistant(text, thinking=thinking))
        return True

    @staticmethod
    def _wrapped_right(card: _Card) -> QtWidgets.QWidget:
        host = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(card, 0)
        return host

    def clear_items(self) -> None:
        """Drop every card so a different Session's history doesn't mix in."""
        for card in self._cards:
            self.flow.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

    def set_composer_state(self, state: str) -> None:
        """state: idle | running | stopping | stopping-forceable."""
        self.send_button.setEnabled(state == "idle")
        self.input.setEnabled(state == "idle")
        self.stop_button.setEnabled(state in {"running", "stopping-forceable"})
        if state == "stopping-forceable":
            self.stop_button.setText("Force stop")
        else:
            self.stop_button.setText(
                "Stopping…" if state == "stopping" else "Stop")

    def focus_composer(self) -> None:
        """Focus the Run-request editor so the user can start typing."""
        self.input.setFocus()

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.sendRequested.emit(text)
