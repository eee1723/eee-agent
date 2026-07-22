"""C: behavior-level tests for _replay_history without instantiating Qt.

PySide6 is unavailable in the test venv, so we extract the method source
and exec it against a fake ``self`` + fake ``conversation``. This verifies
the actual replay logic (which runs are emitted, which are skipped, the
order, and the _shown_output_run_id side effect) instead of just the
source-level contracts in test_main_window_history.py.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
MAIN_WINDOW_SRC = (ROOT / "houdini_side" / "runtime_panel" / "main_window.py").read_text(
    encoding="utf-8"
)

# Extract just the _replay_history method source so we can exec it in isolation
# without importing the Qt-dependent module.
_tree = ast.parse(MAIN_WINDOW_SRC)
_methods = {n.name: n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)}
_REPLAY_SRC = textwrap.dedent(ast.get_source_segment(MAIN_WINDOW_SRC, _methods["_replay_history"]))

# Pull the module-level _TERMINAL_RUN_STATES set the method depends on.
_TERMINAL_RUN_STATES = frozenset({"Completed", "Cancelled", "Failed"})


class _FakeViewModels:
    """Stand-in for houdini_side.runtime_panel.view_models.

    Records the cards built so the test can assert on kinds and bodies.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def user_message(self, text: str) -> SimpleNamespace:
        self.calls.append(("user", text))
        return SimpleNamespace(kind="user", body=text)

    def assistant_message(self, text: str) -> SimpleNamespace:
        self.calls.append(("assistant", text))
        return SimpleNamespace(kind="assistant", body=text)


class _FakeConversation:
    """Stand-in for the ConversationView Qt widget."""

    def __init__(self, view_models: _FakeViewModels) -> None:
        self._vm = view_models
        self.appended: list[SimpleNamespace] = []
        self.streaming_seeded = 0

    def append_item(self, item: SimpleNamespace) -> None:
        self.appended.append(item)

    def update_streaming(self, text: str, *, thinking: str = "") -> bool:
        # In real ConversationView this appends when no streaming card exists;
        # in the fake we just count the seed calls so we can assert the
        # active-run path was taken.
        self.streaming_seeded += 1
        return True


def _make_replay(view_models_obj):
    """Build a standalone _replay_history callable bound to fakes.

    The extracted method source references the module-level ``view_models``
    import; exec it in a namespace that supplies a fake view_models plus the
    _TERMINAL_RUN_STATES frozenset the method reads.
    """
    ns: dict = {
        "_TERMINAL_RUN_STATES": _TERMINAL_RUN_STATES,
        "view_models": view_models_obj,
    }
    exec(_REPLAY_SRC, ns)
    return ns["_replay_history"]


def _run_replay(snapshot, *, active_run_id=None, shown_output_run_id=None):
    """Run the standalone replay against fakes; return (calls, appended, seeded, shown)."""
    vm = _FakeViewModels()
    convo = _FakeConversation(vm)
    fake_self = SimpleNamespace(
        conversation=convo,
        _active_run_id=active_run_id,
        _shown_output_run_id=shown_output_run_id,
    )
    replay = _make_replay(vm)
    replay(fake_self, snapshot)
    return vm.calls, convo.appended, convo.streaming_seeded, fake_self._shown_output_run_id


def test_replay_emits_user_and_assistant_for_each_terminal_run() -> None:
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "创建一个box",
             "status": "Completed", "final_response": "box 已创建"},
            {"run_id": "r2", "user_input": "再加一个",
             "status": "Completed", "final_response": "已加"},
        ]
    }
    calls, appended, seeded, shown = _run_replay(snapshot)
    # 2 user + 2 assistant cards, in chronological order, no streaming seed.
    assert calls == [
        ("user", "创建一个box"),
        ("assistant", "box 已创建"),
        ("user", "再加一个"),
        ("assistant", "已加"),
    ]
    assert seeded == 0
    assert shown is None  # no active run


def test_replay_skips_active_non_terminal_run_and_seeds_streaming() -> None:
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "hi", "status": "Completed",
             "final_response": "hello"},
            {"run_id": "r2", "user_input": "do something",
             "status": "Planning", "final_response": None},
        ]
    }
    calls, appended, seeded, shown = _run_replay(snapshot, active_run_id="r2")
    # r1 fully emitted; r2 emits the user prompt then seeds streaming (no
    # assistant card for r2 — the live path owns it).
    assert calls == [
        ("user", "hi"),
        ("assistant", "hello"),
        ("user", "do something"),
    ]
    assert seeded == 1
    # Active run is non-terminal -> _shown_output_run_id left untouched.
    assert shown is None


def test_replay_emits_assistant_for_terminal_active_run_and_marks_shown() -> None:
    # If the active run is already terminal (e.g. just completed), replay
    # emits its final reply AND marks it shown so _maybe_render_output does
    # not double-emit on the same snapshot.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "q", "status": "Completed",
             "final_response": "answer"},
        ]
    }
    calls, appended, seeded, shown = _run_replay(snapshot, active_run_id="r1")
    assert calls == [("user", "q"), ("assistant", "answer")]
    assert seeded == 0
    assert shown == "r1"


def test_replay_emits_user_card_even_when_final_response_missing() -> None:
    # A run that crashed before producing a reply still has a prompt the user
    # typed; the timeline must show it (Failed is terminal, so an empty
    # assistant card follows).
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "broken prompt",
             "status": "Failed", "final_response": None},
        ]
    }
    calls, appended, seeded, shown = _run_replay(snapshot)
    assert calls == [("user", "broken prompt"), ("assistant", "")]


def test_replay_skips_run_with_empty_user_input() -> None:
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "", "status": "Completed",
             "final_response": "orphan reply"},
        ]
    }
    calls, appended, seeded, shown = _run_replay(snapshot)
    # No user card (empty prompt); assistant card still emitted because the
    # run is terminal.
    assert calls == [("assistant", "orphan reply")]


def test_replay_handles_missing_runs_key() -> None:
    # A snapshot without a "runs" key must not raise.
    calls, appended, seeded, shown = _run_replay({})
    assert calls == []
    assert seeded == 0


def test_replay_ignores_non_dict_run_entries() -> None:
    snapshot = {"runs": ["junk", None, {"run_id": "r1", "user_input": "ok",
                                        "status": "Completed", "final_response": "done"}]}
    calls, appended, seeded, shown = _run_replay(snapshot)
    assert calls == [("user", "ok"), ("assistant", "done")]
