"""C: source-contract tests for the timeline history replay.

PySide6 is not a project dependency, so the RuntimePanel Qt widget is
verified by source contracts here. The behavior under test: the panel
used to render only the latest reply because _on_session cleared the
card flow and _on_snapshot only painted the active/selected run. The
fix rebuilds the conversation from snapshot["runs"] on session activation.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN_WINDOW = (ROOT / "houdini_side" / "runtime_panel" / "main_window.py").read_text(
    encoding="utf-8"
)


def test_main_window_defines_replay_history_method() -> None:
    # The new _replay_history method is the single entry point that turns
    # snapshot["runs"] back into conversation cards.
    assert "def _replay_history(self, snapshot) -> None:" in MAIN_WINDOW


def test_main_window_has_history_needs_replay_flag() -> None:
    # The flag gates replay so it runs once per session activation, not on
    # every snapshot poll.
    assert "self._history_needs_replay" in MAIN_WINDOW


def test_on_session_sets_replay_flag_on_session_change() -> None:
    # Find the _on_session method body and confirm it sets the flag in both
    # the "switched from another session" and "first activation" branches.
    tree = ast.parse(MAIN_WINDOW)
    methods = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    on_session = methods.get("_on_session")
    assert on_session is not None, "_on_session method must exist"
    src = ast.get_source_segment(MAIN_WINDOW, on_session) or ""
    assert "_history_needs_replay = True" in src, src


def test_on_snapshot_consumes_replay_flag_once() -> None:
    tree = ast.parse(MAIN_WINDOW)
    methods = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    on_snapshot = methods.get("_on_snapshot")
    assert on_snapshot is not None
    src = ast.get_source_segment(MAIN_WINDOW, on_snapshot) or ""
    assert "if self._history_needs_replay:" in src
    assert "self._replay_history(snapshot)" in src
    assert "self._history_needs_replay = False" in src


def test_replay_history_iterates_runs_in_order() -> None:
    tree = ast.parse(MAIN_WINDOW)
    methods = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    replay = methods.get("_replay_history")
    assert replay is not None
    src = ast.get_source_segment(MAIN_WINDOW, replay) or ""
    # It must read the runs list from the snapshot...
    assert 'snapshot.get("runs")' in src
    # ...iterate it...
    assert "for run in runs:" in src
    # ...and append a user_message card for each run's user_input.
    assert "view_models.user_message(user_input)" in src
    # A-3: terminal runs delegate to the unified selector instead of building
    # a card inline. The selector decides Completed/Cancelled/Failed shape and
    # returns the items in render order; replay appends each one.
    assert "view_models.terminal_result_items(run, final_text)" in src
    # Replay must NOT re-implement the terminal business rules itself.
    assert "view_models.assistant_message(final_text)" not in src


def test_replay_history_skips_active_non_terminal_run() -> None:
    # The active run is owned by the live streaming path; replay must not
    # double-emit it. It should seed an empty streaming card instead.
    tree = ast.parse(MAIN_WINDOW)
    methods = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    replay = methods.get("_replay_history")
    src = ast.get_source_segment(MAIN_WINDOW, replay) or ""
    assert "active_run_id" in src
    assert "is_terminal" in src
    assert "continue" in src  # the skip branch


def test_maybe_render_output_uses_unified_terminal_selector() -> None:
    # A-3: the live settling path must delegate terminal rendering to the same
    # selector as history replay, render the returned items in order (first
    # via replace_last_assistant, the rest appended), and set the idempotency
    # marker afterwards. It must not build an assistant_message inline.
    tree = ast.parse(MAIN_WINDOW)
    methods = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    render = methods.get("_maybe_render_output")
    assert render is not None, "_maybe_render_output method must exist"
    src = ast.get_source_segment(MAIN_WINDOW, render) or ""
    assert "view_models.terminal_result_items(shown, output)" in src
    assert "view_models.assistant_message(output)" not in src
    # Non-terminal runs still stream; the selector is only for terminal runs.
    assert "update_streaming(output" in src
    # The idempotency guard and marker set are preserved.
    assert "self._shown_output_run_id" in src
    # First item reuses the streaming slot; later items append.
    assert "replace_last_assistant(item)" in src
    assert "append_item(item)" in src


def test_first_activation_also_clears_flow_and_requests_replay() -> None:
    # The first time a session activates after connect, _current_session_id
    # is "" so the "switched from another session" reset branch would not
    # run. The fix adds an else branch that clears the boot notice and
    # requests replay.
    tree = ast.parse(MAIN_WINDOW)
    methods = {
        n.name: n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    on_session = methods.get("_on_session")
    src = ast.get_source_segment(MAIN_WINDOW, on_session) or ""
    # The else branch (first activation) must clear and set the flag.
    assert "else:" in src
    assert "self.conversation.clear_items()" in src
