"""A-3: behavior-level tests for the unified terminal-render path.

PySide6 is unavailable in the test venv, so we extract the real method
sources (``_replay_history`` and ``_maybe_render_output``) from
``main_window.py`` and exec them against a fake ``self`` + fake
``conversation`` + fake ``view_models``. This verifies the actual logic
(which cards are emitted, in which order, the idempotency marker, and
that every terminal rendering decision is delegated to the unified
``view_models.terminal_result_items`` selector) instead of just the
source-level contracts in ``test_main_window_history.py``.
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

# Extract just the methods we drive in isolation, without importing the
# Qt-dependent module.
_tree = ast.parse(MAIN_WINDOW_SRC)
_methods = {n.name: n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)}
_REPLAY_SRC = textwrap.dedent(
    ast.get_source_segment(MAIN_WINDOW_SRC, _methods["_replay_history"])
)
_RENDER_SRC = textwrap.dedent(
    ast.get_source_segment(MAIN_WINDOW_SRC, _methods["_maybe_render_output"])
)

# Pull the module-level _TERMINAL_RUN_STATES set both methods depend on.
_TERMINAL_RUN_STATES = frozenset({"Completed", "Cancelled", "Failed"})


class _FakeViewModels:
    """Stand-in for houdini_side.runtime_panel.view_models.

    The terminal selector is faked so the test can both (a) assert that the
    main window delegates to it instead of re-implementing the business
    rules, and (b) control what recognizable items the selector yields. Each
    constructed card is a SimpleNamespace carrying ``kind`` / ``body`` so the
    ordering and "no empty assistant" invariants are checkable.
    """

    def __init__(self) -> None:
        # Record of every selector call: (run_arg, output_arg).
        self.selector_calls: list[tuple[object, object]] = []
        self.calls: list[tuple[str, str]] = []

    def user_message(self, text: str) -> SimpleNamespace:
        self.calls.append(("user", text))
        return SimpleNamespace(kind="user", body=text)

    def assistant_message(self, text: str) -> SimpleNamespace:
        # Still referenced by streaming_assistant indirectly? No — kept so a
        # stray direct call surfaces loudly in self.calls if the main window
        # stops delegating.
        self.calls.append(("assistant", text))
        return SimpleNamespace(kind="assistant", body=text)

    def terminal_result_items(
        self, run: object, output: object
    ) -> tuple[SimpleNamespace, ...]:
        self.selector_calls.append((run, output))
        if type(run) is not dict:
            return ()
        status = run.get("status")
        text = output if type(output) is str else ""
        # Mirrors the real selector's shape using recognizable kinds so order
        # and "no empty assistant" are verifiable without importing Qt types.
        if status == "Completed":
            if text:
                return (SimpleNamespace(kind="assistant", body=text),)
            return (SimpleNamespace(kind="notice", body="completed-no-response"),)
        if status == "Cancelled":
            if text:
                return (SimpleNamespace(kind="assistant", body=text),)
            return (SimpleNamespace(kind="notice", body="cancelled"),)
        if status == "Failed":
            error = SimpleNamespace(kind="error", body="failed")
            if text:
                return (SimpleNamespace(kind="assistant", body=text), error)
            return (error,)
        return ()


class _FakeConversation:
    """Stand-in for the ConversationView Qt widget.

    Records appended/replaced items so tests can assert the rendered sequence
    and which item replaced the streaming card.
    """

    def __init__(self, view_models: _FakeViewModels) -> None:
        self._vm = view_models
        self.appended: list[SimpleNamespace] = []
        self.streaming_seeded = 0
        # replace_last_assistant pops a trailing assistant/streaming card and
        # appends the replacement; track the replaced count to assert that the
        # first terminal item reuses the streaming slot.
        self.replaced: list[SimpleNamespace] = []

    def append_item(self, item: SimpleNamespace) -> None:
        self.appended.append(item)

    def update_streaming(self, text: str, *, thinking: str = "") -> bool:
        # In real ConversationView this appends when no streaming card exists;
        # in the fake we just count the seed calls so we can assert the
        # active-run path was taken.
        self.streaming_seeded += 1
        return True

    def replace_last_assistant(self, item: SimpleNamespace) -> None:
        # Simulate the real method: if the trailing card is an assistant/
        # streaming card it is replaced, otherwise the item is appended.
        if self.appended and self.appended[-1].kind in (
            "assistant",
            "assistant_streaming",
        ):
            self.replaced.append(self.appended[-1])
            self.appended[-1] = item
        else:
            self.appended.append(item)


def _make_replay(view_models_obj):
    """Build a standalone _replay_history callable bound to fakes."""
    ns: dict = {
        "_TERMINAL_RUN_STATES": _TERMINAL_RUN_STATES,
        "view_models": view_models_obj,
    }
    exec(_REPLAY_SRC, ns)
    return ns["_replay_history"]


def _make_render(view_models_obj):
    """Build a standalone _maybe_render_output callable bound to fakes."""
    ns: dict = {
        "_TERMINAL_RUN_STATES": _TERMINAL_RUN_STATES,
        "view_models": view_models_obj,
    }
    exec(_RENDER_SRC, ns)
    return ns["_maybe_render_output"]


def _run_replay(snapshot, *, active_run_id=None, shown_output_run_id=None):
    """Run standalone replay against fakes.

    Returns (selector_calls, appended, seeded, shown_output_run_id).
    """
    vm = _FakeViewModels()
    convo = _FakeConversation(vm)
    fake_self = SimpleNamespace(
        conversation=convo,
        _active_run_id=active_run_id,
        _shown_output_run_id=shown_output_run_id,
    )
    replay = _make_replay(vm)
    replay(fake_self, snapshot)
    return (
        vm.selector_calls,
        convo.appended,
        convo.streaming_seeded,
        fake_self._shown_output_run_id,
    )


def _run_render(snapshot, shown, *, shown_output_run_id=None):
    """Run standalone _maybe_render_output against fakes.

    Returns (selector_calls, appended, replaced, seeded, shown_output_run_id).
    """
    vm = _FakeViewModels()
    convo = _FakeConversation(vm)
    fake_self = SimpleNamespace(
        conversation=convo,
        _shown_output_run_id=shown_output_run_id,
    )
    render = _make_render(vm)
    render(fake_self, snapshot, shown)
    return (
        vm.selector_calls,
        convo.appended,
        convo.replaced,
        convo.streaming_seeded,
        fake_self._shown_output_run_id,
    )


# --------------------------------------------------------------------------
# _replay_history behavior
# --------------------------------------------------------------------------

def test_replay_completed_with_output_emits_user_then_assistant() -> None:
    # Behavior 1: Completed + final output -> user item + assistant item.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "创建一个box",
             "status": "Completed", "final_response": "box 已创建"},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    assert [a.kind for a in appended] == ["user", "assistant"]
    assert appended[1].body == "box 已创建"
    assert seeded == 0
    assert shown is None  # no active run
    # The terminal branch delegated to the unified selector exactly once.
    assert len(selector_calls) == 1
    assert selector_calls[0][0]["run_id"] == "r1"
    assert selector_calls[0][1] == "box 已创建"


def test_replay_failed_no_output_emits_error_not_empty_assistant() -> None:
    # Behavior 2: Failed + no output -> error item; NO empty assistant.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "broken prompt",
             "status": "Failed", "final_response": None,
             "failure_json": {"code": "boom"}},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    assert [a.kind for a in appended] == ["user", "error"]
    # Crucially there is no assistant card at all for a failed-no-output run.
    assert not any(a.kind == "assistant" for a in appended)
    assert len(selector_calls) == 1


def test_replay_failed_partial_output_emits_assistant_then_error() -> None:
    # Behavior 3: Failed + partial output -> strict order assistant, error.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "q",
             "status": "Failed", "final_response": "partial answer",
             "failure_json": {"code": "boom"}},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    assert [a.kind for a in appended] == ["user", "assistant", "error"]
    assert appended[1].body == "partial answer"
    assert appended[2].kind == "error"
    assert len(selector_calls) == 1


def test_replay_cancelled_no_output_emits_notice_not_assistant() -> None:
    # Behavior 4: Cancelled + no output -> cancelled notice; no assistant.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "do thing",
             "status": "Cancelled", "final_response": None},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    assert [a.kind for a in appended] == ["user", "notice"]
    assert appended[1].body == "cancelled"
    assert not any(a.kind == "assistant" for a in appended)
    assert len(selector_calls) == 1


def test_replay_non_terminal_run_does_not_call_selector() -> None:
    # Behavior 5: non-terminal run seeds streaming and does NOT call the
    # terminal selector or emit a terminal item.
    snapshot = {
        "runs": [
            {"run_id": "r2", "user_input": "in flight",
             "status": "Planning", "final_response": None},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(
        snapshot, active_run_id="r2"
    )
    assert [a.kind for a in appended] == ["user"]
    assert seeded == 1
    # No terminal rendering for a non-terminal run.
    assert selector_calls == []
    assert shown is None  # active non-terminal run marker left untouched


def test_replay_two_terminal_runs_each_delegate_to_selector() -> None:
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "a",
             "status": "Completed", "final_response": "A"},
            {"run_id": "r2", "user_input": "b",
             "status": "Failed", "final_response": "B",
             "failure_json": {"code": "x"}},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    # Each terminal run calls the selector once, in chronological order.
    assert [call[0]["run_id"] for call in selector_calls] == ["r1", "r2"]
    # Full rendered sequence: user, assistant, user, assistant, error.
    assert [a.kind for a in appended] == [
        "user", "assistant", "user", "assistant", "error"]


def test_replay_terminal_active_run_marks_shown() -> None:
    # Behavior 7 (history side): a terminal active run is rendered by replay
    # AND marked shown so the live path cannot re-emit it.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "q", "status": "Completed",
             "final_response": "answer"},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(
        snapshot, active_run_id="r1"
    )
    assert [a.kind for a in appended] == ["user", "assistant"]
    assert seeded == 0
    assert shown == "r1"


def test_replay_skips_run_with_empty_user_input() -> None:
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "", "status": "Completed",
             "final_response": "orphan reply"},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    # No user card (empty prompt); assistant card still emitted via selector.
    assert [a.kind for a in appended] == ["assistant"]
    assert len(selector_calls) == 1


def test_replay_non_terminal_active_run_seeds_streaming_only() -> None:
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "hi", "status": "Completed",
             "final_response": "hello"},
            {"run_id": "r2", "user_input": "do something",
             "status": "Planning", "final_response": None},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(
        snapshot, active_run_id="r2"
    )
    # r1 fully emitted; r2 emits the user prompt then seeds streaming (no
    # terminal item for r2 — the live path owns it).
    assert [a.kind for a in appended] == ["user", "assistant", "user"]
    assert seeded == 1
    assert [call[0]["run_id"] for call in selector_calls] == ["r1"]
    assert shown is None


def test_replay_handles_missing_runs_key() -> None:
    # A snapshot without a "runs" key must not raise or call the selector.
    selector_calls, appended, seeded, shown = _run_replay({})
    assert selector_calls == []
    assert appended == []
    assert seeded == 0


def test_replay_ignores_non_dict_run_entries() -> None:
    snapshot = {"runs": ["junk", None, {"run_id": "r1", "user_input": "ok",
                                        "status": "Completed", "final_response": "done"}]}
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    assert [a.kind for a in appended] == ["user", "assistant"]
    assert len(selector_calls) == 1


def test_replay_user_card_preserved_when_failed_and_no_output() -> None:
    # Behavior 9: non-empty user_input must remain even when the run failed
    # and produced no output.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "remember me",
             "status": "Failed", "final_response": None,
             "failure_json": {"code": "x"}},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    assert appended[0].kind == "user"
    assert appended[0].body == "remember me"
    assert [a.kind for a in appended] == ["user", "error"]


def test_replay_delegates_terminal_decisions_not_its_own_business_rules() -> None:
    # Cross-cutting: the replay loop must NOT branch on Completed/Cancelled/
    # Failed itself — it hands the run + final_text to the selector and
    # renders whatever it returns.
    snapshot = {
        "runs": [
            {"run_id": "c", "user_input": "u1", "status": "Completed",
             "final_response": "ok"},
            {"run_id": "x", "user_input": "u2", "status": "Cancelled",
             "final_response": None},
            {"run_id": "f", "user_input": "u3", "status": "Failed",
             "final_response": None, "failure_json": {"code": "b"}},
        ]
    }
    selector_calls, appended, seeded, shown = _run_replay(snapshot)
    # All three terminal runs reach the selector with their exact final_text.
    assert [call[0]["run_id"] for call in selector_calls] == ["c", "x", "f"]
    assert selector_calls[0] == (
        {"run_id": "c", "user_input": "u1", "status": "Completed",
         "final_response": "ok"}, "ok")
    # Cancelled with no output -> output arg is "" (exact-str contract).
    assert selector_calls[1][1] == ""
    assert selector_calls[2][1] == ""


# --------------------------------------------------------------------------
# _maybe_render_output behavior
# --------------------------------------------------------------------------

def test_render_non_terminal_with_output_updates_streaming() -> None:
    # Behavior 5 (live side): a non-terminal run streams, never reaches the
    # terminal selector.
    shown = {"run_id": "r1", "status": "Planning"}
    snapshot = {"output": "forming", "thinking": "hmm"}
    selector_calls, appended, replaced, seeded, shown_id = _run_render(
        snapshot, shown)
    assert selector_calls == []
    assert appended == []
    assert seeded == 1
    assert shown_id is None


def test_render_non_terminal_without_output_does_not_emit_terminal() -> None:
    # No output/thinking and non-terminal -> nothing emitted, no selector call.
    shown = {"run_id": "r1", "status": "Planning"}
    snapshot = {"output": "", "thinking": ""}
    selector_calls, appended, replaced, seeded, shown_id = _run_render(
        snapshot, shown)
    assert selector_calls == []
    assert appended == []
    assert seeded == 0
    assert shown_id is None


def test_render_completed_replaces_streaming_card_once() -> None:
    # Terminal Completed: the streaming card (if present) is replaced by the
    # selector's first item, and the marker is set.
    shown = {"run_id": "r1", "status": "Completed"}
    snapshot = {"output": "final answer"}
    selector_calls, appended, replaced, seeded, shown_id = _run_render(
        snapshot, shown)
    assert [a.kind for a in appended] == ["assistant"]
    assert appended[0].body == "final answer"
    assert shown_id == "r1"
    assert len(selector_calls) == 1


def test_render_failed_partial_output_appends_assistant_then_error() -> None:
    # Behavior 3 (live): Failed + partial output -> first item replaces the
    # streaming card, second item (error) appends.
    shown = {"run_id": "r1", "status": "Failed",
             "failure_json": {"code": "boom"}}
    snapshot = {"output": "partial"}
    selector_calls, appended, replaced, seeded, shown_id = _run_render(
        snapshot, shown)
    assert [a.kind for a in appended] == ["assistant", "error"]
    assert appended[0].body == "partial"
    assert appended[1].kind == "error"
    assert shown_id == "r1"
    assert len(selector_calls) == 1


def test_render_failed_no_output_shows_error_not_empty_assistant() -> None:
    # Behavior 2 (live): Failed + no output -> single error item; no empty
    # assistant. When there is no trailing assistant card to replace, the
    # single item is appended.
    shown = {"run_id": "r1", "status": "Failed",
             "failure_json": {"code": "boom"}}
    snapshot = {"output": ""}
    selector_calls, appended, replaced, seeded, shown_id = _run_render(
        snapshot, shown)
    assert [a.kind for a in appended] == ["error"]
    assert not any(a.kind == "assistant" for a in appended)
    assert shown_id == "r1"


def test_render_cancelled_no_output_shows_notice() -> None:
    # Behavior 4 (live): Cancelled + no output -> single notice item.
    shown = {"run_id": "r1", "status": "Cancelled"}
    snapshot = {"output": ""}
    selector_calls, appended, replaced, seeded, shown_id = _run_render(
        snapshot, shown)
    assert [a.kind for a in appended] == ["notice"]
    assert appended[0].body == "cancelled"
    assert shown_id == "r1"


def test_render_does_not_double_emit_repeated_terminal_snapshot() -> None:
    # Behavior 6 (live): a second identical terminal snapshot for a run that
    # was already shown must not call the selector or append again.
    shown = {"run_id": "r1", "status": "Completed"}
    snapshot = {"output": "final"}
    # First arrival: emits and sets the marker.
    _, appended1, _, _, shown_id1 = _run_render(snapshot, shown)
    assert shown_id1 == "r1"
    # Second arrival for the same run_id: already shown -> no-op.
    selector_calls2, appended2, _, _, shown_id2 = _run_render(
        snapshot, shown, shown_output_run_id="r1")
    assert selector_calls2 == []
    assert appended2 == []
    assert shown_id2 == "r1"


def test_render_different_run_ids_are_independent() -> None:
    # Behavior 8: one run's terminal marker does not suppress a different
    # run's terminal render.
    shown_a = {"run_id": "r1", "status": "Completed"}
    snap_a = {"output": "A"}
    _, appended_a, _, _, shown_id_a = _run_render(
        snap_a, shown_a, shown_output_run_id=None)
    assert [a.body for a in appended_a] == ["A"]
    assert shown_id_a == "r1"
    # A different run id is not blocked by r1's marker.
    shown_b = {"run_id": "r2", "status": "Completed"}
    snap_b = {"output": "B"}
    selector_calls_b, appended_b, _, _, shown_id_b = _run_render(
        snap_b, shown_b, shown_output_run_id="r1")
    assert [a.body for a in appended_b] == ["B"]
    assert shown_id_b == "r2"
    assert len(selector_calls_b) == 1


def test_render_history_then_live_does_not_re_emit() -> None:
    # Behavior 7: history replay already showed a terminal run; the live path
    # must not re-render it. We simulate replay setting the marker, then the
    # live path receiving the same terminal run.
    snapshot = {
        "runs": [
            {"run_id": "r1", "user_input": "q", "status": "Completed",
             "final_response": "answer"},
        ]
    }
    replay_calls, replay_appended, _, replay_shown = _run_replay(
        snapshot, active_run_id="r1")
    assert replay_shown == "r1"
    # Now the live path sees the same terminal run. Because replay marked it,
    # the selector is NOT called again and nothing is appended.
    live_shown = {"run_id": "r1", "status": "Completed"}
    live_snap = {"output": "answer"}
    live_calls, live_appended, _, _, live_id = _run_render(
        live_snap, live_shown, shown_output_run_id=replay_shown)
    assert live_calls == []
    assert live_appended == []
    assert live_id == "r1"


def test_render_delegates_terminal_decisions_not_its_own_business_rules() -> None:
    # Cross-cutting: _maybe_render_output hands the run + output to the
    # selector for every terminal status and renders whatever it returns; it
    # does not branch on Completed/Cancelled/Failed itself.
    for status in ("Completed", "Cancelled", "Failed"):
        shown = {"run_id": "r1", "status": status}
        if status == "Failed":
            shown["failure_json"] = {"code": "x"}
        snapshot = {"output": "text"}
        selector_calls, _, _, _, _ = _run_render(snapshot, shown)
        assert len(selector_calls) == 1, status
        assert selector_calls[0][0] is shown
        assert selector_calls[0][1] == "text", status


def test_render_exact_str_output_contract() -> None:
    # Non-str output (None / int / list) must reach the selector as "" so the
    # selector's exact-str contract is preserved — never str(payload) or a
    # leaked repr.
    shown = {"run_id": "r1", "status": "Completed"}
    for bad in (None, 42, ["a"], {"k": "v"}):
        snapshot = {"output": bad}
        selector_calls, _, _, _, _ = _run_render(snapshot, shown)
        assert selector_calls[0][1] == "", bad
