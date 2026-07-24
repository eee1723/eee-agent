"""Pi-style folding panels that keep the conversation timeline clean.

The conversation flow shows only user messages, the final assistant reply, and
structural cards (proposals, vision, artifacts, notices). The agent's
intermediate steps — tool calls, tool results, and the assistant text segments
between them — live here in two folding panels anchored BELOW the timeline:

- ActivityPanel: an ordered trace of every tool call / result and assistant
  text segment for the run in flight (or the last run). Collapsed by default;
  auto-expands on the first step. Each step is a card showing name + status
  glyph + an expandable body (tool args / result, or the assistant segment).

- ThinkingPanel: the model's reasoning stream, coalesced and char-capped so a
  long thinking trace never monopolizes the main thread.

Both panels update incrementally (keyed by step ref) instead of rebuilding on
every event, so they stay cheap during token streaming.
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_side.runtime_panel import theme

# Folded header height (matches Pi's collapsed panel rows).
_COLLAPSED_HEIGHT = 26
_EXPANDED_HEIGHT = 220
# Status glyphs + tones for step cards.
_STATUS_GLYPH = {
    "pending": "⏳",
    "streaming": "▍",
    "done": "✓",
    "error": "✗",
}
_STATUS_TONE = {
    "pending": theme.STATUS_WARN,
    "streaming": theme.PRIMARY,
    "done": theme.STATUS_OK,
    "error": theme.STATUS_ERROR,
}
_KIND_GLYPH = {
    "tool_call": "🔧",
    "tool_result": "↩",
    "assistant_text": "✍",
}
# Cap a step body when collapsed (full body still available on expand).
_PREVIEW_CHARS = 120
_THINKING_RENDER_INTERVAL_MS = 120
_THINKING_RENDER_CHAR_CAP = 2000


class _StepCard(QtWidgets.QFrame):
    """One step in the ActivityPanel: a tool call/result or an assistant
    text segment. Collapsed it shows glyph + title + a one-line preview;
    expanded it shows the full body."""

    def __init__(self, step: dict, parent=None) -> None:
        super().__init__(parent)
        self._ref = step.get("ref", "")
        self._kind = step.get("kind", "")
        self.setObjectName("StepCard")
        self.setStyleSheet(
            f"QFrame#StepCard {{ border-left: 2px solid {theme.DIVIDER};"
            f" background: {theme.SURFACE_1}; border-radius: 2px; }}")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)
        kind_glyph = _KIND_GLYPH.get(self._kind, "·")
        self._kind_label = QtWidgets.QLabel(kind_glyph)
        self._kind_label.setStyleSheet(f"color: {theme.FG_DIM};")
        self._title_label = QtWidgets.QLabel(str(step.get("title", "")))
        self._title_label.setObjectName("ProminentLabel")
        self._status_label = QtWidgets.QLabel()
        self._status_label.setStyleSheet(f"color: {theme.FG_DIM};")
        header.addWidget(self._kind_label)
        header.addWidget(self._title_label, 1)
        header.addWidget(self._status_label)
        layout.addLayout(header)

        # Preview line (collapsed) — hidden when expanded.
        self._preview_label = QtWidgets.QLabel()
        self._preview_label.setObjectName("DimLabel")
        self._preview_label.setWordWrap(False)
        self._preview_label.setStyleSheet(f"color: {theme.FG_DIMMER};")
        layout.addWidget(self._preview_label)

        # Full body (expanded) — shown only on click.
        self._body_label = QtWidgets.QLabel()
        self._body_label.setWordWrap(True)
        self._body_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self._body_label.setStyleSheet(
            f"color: {theme.FG}; font-family: {theme.mono_font_family()};")
        self._body_label.hide()
        layout.addWidget(self._body_label)

        self._expanded = False
        self.update_step(step)

    def update_step(self, step: dict) -> None:
        """Refresh title/status/body from an updated step dict (same ref)."""
        title = str(step.get("title", ""))
        if title:
            self._title_label.setText(title)
        status = str(step.get("status", "pending"))
        glyph = _STATUS_GLYPH.get(status, "·")
        tone = _STATUS_TONE.get(status, theme.FG_DIM)
        self._status_label.setText(glyph)
        self._status_label.setStyleSheet(f"color: {tone};")
        # Body source differs by kind: tool_call/result use 'result' (paired)
        # or 'body'; assistant_text uses 'body'.
        body = ""
        if self._kind in ("tool_call", "tool_result"):
            result = step.get("result")
            body = str(result) if result else str(step.get("body", ""))
        else:
            body = str(step.get("body", ""))
        self._body_label.setText(body if body else "(empty)")
        preview = body[:_PREVIEW_CHARS]
        if len(body) > _PREVIEW_CHARS:
            preview += "…"
        self._preview_label.setText(preview)
        self._preview_label.setVisible(bool(body) and not self._expanded)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._toggle_expand()
        super().mousePressEvent(event)

    def _toggle_expand(self) -> None:
        self._expanded = not self._expanded
        self._body_label.setVisible(self._expanded)
        self._preview_label.setVisible(
            bool(self._preview_label.text()) and not self._expanded)


class ActivityPanel(QtWidgets.QFrame):
    """Folding panel listing the ordered steps for the shown run.

    Steps are fed incrementally via set_steps(); each step's ref (call_id or
    synthetic) deduplicates so re-feeding the same list on every snapshot does
    not rebuild the cards. Auto-expands on the first step, collapses again on
    run boundaries (reset_for_run).
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ActivityPanel")
        self.setStyleSheet(
            f"QFrame#ActivityPanel {{ border-top: 1px solid {theme.DIVIDER};"
            f" background: {theme.VIEW_SURFACE}; }}")
        self.setFixedHeight(_COLLAPSED_HEIGHT)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(2)

        self._toggle = QtWidgets.QToolButton()
        self._toggle.setText("▸ Activity")
        self._toggle.setCheckable(True)
        self._toggle.setStyleSheet("QToolButton { border: none; }")
        self._toggle.toggled.connect(self._on_toggle)
        layout.addWidget(self._toggle)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self._host = QtWidgets.QWidget()
        self._flow = QtWidgets.QVBoxLayout(self._host)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setSpacing(4)
        self._flow.addStretch(1)
        self._scroll.setWidget(self._host)
        self._scroll.hide()
        layout.addWidget(self._scroll)

        self._cards: dict[str, _StepCard] = {}
        self._order: list[str] = []
        self._auto_expanded = False

    def reset_for_run(self, run_id: str | None) -> None:
        """Clear the step list when the active run changes.

        Collapses the panel back to its header so a new run does not inherit
        the previous run's expanded state.
        """
        for card in self._cards.values():
            self._flow.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        self._order.clear()
        self._auto_expanded = False
        self._toggle.setChecked(False)
        self._update_header()

    def set_steps(self, steps) -> None:
        """Incrementally update the step cards from a list of step dicts.

        Existing refs are updated in place; new refs are appended (preserving
        order). Never rebuilds cards that already exist, so this is cheap to
        call on every coalesced snapshot.
        """
        if not isinstance(steps, (list, tuple)):
            return
        for step in steps:
            if type(step) is not dict:
                continue
            ref = str(step.get("ref", ""))
            if not ref:
                continue
            if ref in self._cards:
                self._cards[ref].update_step(step)
            else:
                card = _StepCard(step)
                self._cards[ref] = card
                self._order.append(ref)
                self._flow.insertWidget(self._flow.count() - 1, card)
        self._update_header()
        # Auto-expand on the first step so the user sees the agent working
        # without having to click.
        if self._order and not self._auto_expanded:
            self._auto_expanded = True
            self._toggle.setChecked(True)

    def _update_header(self) -> None:
        n = len(self._order)
        tools = sum(1 for r in self._order if self._cards[r]._kind in
                    ("tool_call", "tool_result"))
        arrow = "▼" if self._toggle.isChecked() else "▸"
        if n:
            self._toggle.setText(f"{arrow} Activity · {n} steps ({tools} tool)")
        else:
            self._toggle.setText(f"{arrow} Activity")

    def _on_toggle(self, on: bool) -> None:
        self.setFixedHeight(_EXPANDED_HEIGHT if on else _COLLAPSED_HEIGHT)
        self._scroll.setVisible(on)
        self._update_header()


class ThinkingPanel(QtWidgets.QFrame):
    """Folding panel for the model's reasoning stream.

    The thinking text is accumulated off-panel and rendered here on a coalesced
    timer (120ms) with a char cap on the live tail, mirroring the Pi project's
    approach so a long thinking trace never monopolizes the main thread.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ThinkingPanel")
        self.setStyleSheet(
            f"QFrame#ThinkingPanel {{ border-top: 1px solid {theme.DIVIDER};"
            f" background: {theme.THINKING_SURFACE}; }}")
        self.setFixedHeight(_COLLAPSED_HEIGHT)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(2)

        self._toggle = QtWidgets.QToolButton()
        self._toggle.setText("▸ Thinking")
        self._toggle.setCheckable(True)
        self._toggle.setStyleSheet("QToolButton { border: none; }")
        self._toggle.toggled.connect(self._on_toggle)
        layout.addWidget(self._toggle)

        self._view = QtWidgets.QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(
            f"QTextEdit {{ background: {theme.THINKING_SURFACE};"
            f" color: {theme.FG_DIM}; border: none; }}")
        self._view.hide()
        layout.addWidget(self._view)

        self._buffer = ""
        self._dirty = False
        self._render_timer = QtCore.QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(_THINKING_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._flush_render)
        self._auto_expanded = False

    def set_thinking(self, text: str) -> None:
        """Accumulate reasoning text and schedule a coalesced render."""
        if not isinstance(text, str):
            return
        self._buffer = text
        self._dirty = True
        if not self._render_timer.isActive():
            self._render_timer.start()
        # Auto-expand on first non-empty thinking so the user sees reasoning.
        if text and not self._auto_expanded:
            self._auto_expanded = True
            self._toggle.setChecked(True)

    def reset_for_run(self, run_id: str | None) -> None:
        self._buffer = ""
        self._view.clear()
        self._dirty = False
        self._render_timer.stop()
        self._auto_expanded = False
        self._toggle.setChecked(False)
        self._update_header()

    def _flush_render(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        # Cap the rendered tail so a very long trace does not trigger an
        # expensive QTextEdit relayout every 120ms.
        text = self._buffer[-_THINKING_RENDER_CHAR_CAP:]
        self._view.setPlainText(text)
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._update_header()

    def _update_header(self) -> None:
        arrow = "▼" if self._toggle.isChecked() else "▸"
        if self._buffer:
            paragraphs = self._buffer.count("\n\n") + 1
            self._toggle.setText(f"{arrow} Thinking · {paragraphs} ¶")
        else:
            self._toggle.setText(f"{arrow} Thinking")

    def _on_toggle(self, on: bool) -> None:
        self.setFixedHeight(_EXPANDED_HEIGHT if on else _COLLAPSED_HEIGHT)
        self._view.setVisible(on)
        self._update_header()
