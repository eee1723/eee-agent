"""Message-item and status aggregation contracts (Qt-free)."""

from __future__ import annotations

import pytest

from houdini_side.runtime_panel import view_models as vm


def test_user_message_is_bounded() -> None:
    item = vm.user_message("x" * 5000)
    assert item.kind == "user"
    assert len(item.body) == vm.MAX_BODY_CHARS
    assert item.tone == "normal"


def test_assistant_message_is_bounded_and_wraps() -> None:
    item = vm.assistant_message("y" * 5000)
    assert item.kind == "assistant"
    assert item.title == "Assistant"
    assert len(item.body) == vm.MAX_BODY_CHARS
    assert item.tone == "normal"
    assert item.mono is False  # prose wraps, not monospace


def test_streaming_assistant_carries_thinking_and_allows_empty_body() -> None:
    # Just started: no tokens yet, only thinking is streaming.
    empty = vm.streaming_assistant("", thinking="planning the approach")
    assert empty.kind == "assistant_streaming"
    assert empty.body == ""
    assert empty.thinking == "planning the approach"

    # Mid-stream: both body and thinking accumulate, each hard-capped.
    big = vm.streaming_assistant("z" * 5000, thinking="w" * 5000)
    assert len(big.body) == vm.MAX_BODY_CHARS
    assert len(big.thinking) == vm.MAX_BODY_CHARS
    # A final assistant_message has no thinking field by default.
    assert vm.assistant_message("done").thinking == ""


def test_proposal_card_summarizes_operations() -> None:
    item = vm.proposal_card(
        {"operation_count": 14, "permission_mode": "ProjectChange",
         "changeset_digest": "ab" * 32}
    )
    assert item.kind == "proposal"
    assert "14" in item.title
    assert "ProjectChange" in item.body
    assert item.body.count("ab" * 8) == 1  # digest shown as 16-char prefix
    assert item.tone == "gate"


def test_vision_card_maps_status_to_tone() -> None:
    # Summaries follow parse_vision_event: status / accepted / report_summary.
    ok = vm.vision_card({"status": "completed", "accepted": True,
                         "report_summary": "matches brief"})
    assert ok.tone == "ok"
    failed = vm.vision_card({"status": "failed",
                             "accepted": False,
                             "report_summary": None})
    assert failed.tone == "warn"
    assert failed.body  # bounded fallback text, never empty


def test_vision_card_body_respects_hard_cap() -> None:
    # The status/decision prefix is structural; report_summary must take the
    # remainder so the whole body never exceeds MAX_BODY_CHARS.
    item = vm.vision_card({"status": "completed", "accepted": True,
                           "report_summary": "x" * 5000})
    assert len(item.body) == vm.MAX_BODY_CHARS
    assert item.body.startswith("Status: completed\nDecision: accepted\n")


def test_artifact_card_lists_state() -> None:
    item = vm.artifact_card(
        {"artifact_id": "art_" + "0" * 32, "relative_path": "shots/apply.png",
         "state": "available", "size_bytes": 2048}
    )
    assert item.kind == "artifact"
    assert "shots/apply.png" in item.title
    assert "available" in item.body


def test_append_bounded_enforces_cap() -> None:
    items: list[vm.MessageItem] = []
    for _ in range(vm.MAX_MESSAGES + 10):
        vm.append_bounded(items, vm.notice_card("n"))
    assert len(items) == vm.MAX_MESSAGES


def test_context_status_aggregates() -> None:
    status = vm.context_status(
        hip="untitled.hip", session_title="Table session",
        workspace_id=None, connection="online", bridge="ready",
        run_state="Planning",
    )
    assert status.runtime == "online"
    assert status.workspace == "no workspace"
    assert status.run_state == "Planning"


# --------------------------------------------------------------------------
# Stage A: structured Run/Workspace/Artifacts view models (Qt-free)
# --------------------------------------------------------------------------


def _full_snapshot(**overrides):
    """A snapshot mirroring the reported run_441a83009b shape."""
    snap = {
        "run_id": "run_0123456789abcdef0123456789abcdef",
        "status": "Completed",
        "started_at": "2026-07-21T13:33:52.336248+00:00",
        "finished_at": "2026-07-21T13:34:23.760368+00:00",
        "model_snapshot_json": {
            "eee_agent": "0.1.0",
            "python": "3.11.7",
            "platform": "Windows-10-10.0.26200-SP0",
            "houdini_build": None,
            "kb_schema_version": None,
            "knowledge_status": "missing",
            "dependencies": {"langchain": "1.3.13", "openai": "2.45.0"},
        },
    }
    snap.update(overrides)
    return snap


def test_run_view_returns_none_for_missing_or_invalid_snapshot() -> None:
    assert vm.run_view(None) is None
    assert vm.run_view("not a dict") is None
    assert vm.run_view({}) is None
    assert vm.run_view({"status": "Completed"}) is None  # no run_id


def test_run_view_does_not_stringify_model_snapshot_dict() -> None:
    # The reported bug: model: {'dependencies': {...}} was the dict str()'d
    # by the legacy f-string. The structured view must NOT contain any dict
    # repr; environment fields are individual strings.
    rv = vm.run_view(_full_snapshot())
    assert rv is not None
    assert rv.environment is not None
    assert rv.environment.eee_agent == "0.1.0"
    assert rv.environment.python == "3.11.7"
    assert rv.environment.houdini_build == "-"  # None -> "-"
    assert rv.environment.knowledge_status == "missing"
    assert rv.dependencies == (("langchain", "1.3.13"), ("openai", "2.45.0"))


def test_run_view_short_id_is_bounded() -> None:
    rv = vm.run_view(_full_snapshot())
    assert rv.run_id_short == "run_01234567"
    assert len(rv.run_id_short) <= 12


def test_run_view_maps_status_to_tone() -> None:
    for status, tone in [
        ("Completed", "ok"),
        ("Failed", "error"),
        ("Cancelled", "warn"),
        ("Planning", "normal"),
        ("Stopping", "warn"),
    ]:
        rv = vm.run_view(_full_snapshot(status=status))
        assert rv.status_tone == tone, (status, rv.status_tone)


def test_run_view_duration_is_computed_in_seconds() -> None:
    rv = vm.run_view(_full_snapshot())
    assert rv.duration_seconds is not None
    assert abs(rv.duration_seconds - 31.42) < 0.1


def test_run_view_duration_none_when_timestamps_missing() -> None:
    rv = vm.run_view(_full_snapshot(started_at=None, finished_at=None))
    assert rv.duration_seconds is None


def test_run_view_captures_activity_steps_in_order() -> None:
    activity = [
        {"type": "tool.started", "name": "scene_status", "detail": ""},
        {"type": "tool.completed", "name": "scene_status", "detail": "healthy"},
        {"type": "tool.started", "name": "propose_modeling", "detail": ""},
    ]
    rv = vm.run_view(_full_snapshot(), activity=activity)
    assert len(rv.activity) == 3
    assert rv.activity[0].name == "scene_status"
    assert rv.activity[1].kind == "tool.completed"
    assert rv.activity[1].detail == "healthy"
    assert rv.activity[2].name == "propose_modeling"


def test_run_view_picks_latest_apply_outcome_and_maps_tone() -> None:
    outcomes = [
        {"change_id": "chg_a", "receipt_status": "Applied",
         "applied_op_ids": ["op1"], "scene_may_have_changed": False},
        {"change_id": "chg_b", "receipt_status": "RolledBack",
         "applied_op_ids": [],
         "error_code": "houdini.operation_failed",
         "error_message": "Cannot create node 'copytopoints::2.0'",
         "scene_may_have_changed": False},
    ]
    rv = vm.run_view(_full_snapshot(), apply_outcomes=outcomes)
    assert rv.apply_outcome is not None
    # Latest only.
    assert rv.apply_outcome.change_id == "chg_b"
    assert rv.apply_outcome.tone == "error"
    assert rv.apply_outcome.error_code == "houdini.operation_failed"
    assert rv.apply_outcome.applied_op_count == 0


def test_run_view_no_apply_outcome_when_list_empty() -> None:
    rv = vm.run_view(_full_snapshot(), apply_outcomes=[])
    assert rv.apply_outcome is None


def test_workspace_rows_map_value_tones() -> None:
    rows = vm.workspace_rows({
        "status": "healthy",
        "state": "stale",
        "workspace_id": "ws_12345678",
        "error": "failed",
    })
    tones = {r.key: r.tone for r in rows}
    assert tones["status"] == "ok"
    assert tones["state"] == "warn"
    assert tones["error"] == "error"
    assert tones["workspace_id"] == "normal"


def test_workspace_rows_handle_non_dict() -> None:
    assert vm.workspace_rows(None) == ()
    assert vm.workspace_rows("junk") == ()


def test_artifact_rows_map_state_to_tone() -> None:
    rows = vm.artifact_rows([
        {"state": "available", "relative_path": "a.png", "size_bytes": 100},
        {"state": "failed", "relative_path": "b.png", "size_bytes": 200},
        {"state": "missing", "relative_path": "c.png"},
    ])
    assert [r.tone for r in rows] == ["ok", "error", "error"]
    assert rows[2].size_text == "size unknown"


def test_vision_rows_decide_tone_by_status_and_acceptance() -> None:
    rows = vm.vision_rows([
        {"status": "completed", "accepted": True, "report_summary": "ok"},
        {"status": "completed", "accepted": False, "report_summary": "no"},
        {"status": "failed", "accepted": False, "report_summary": None},
    ])
    assert [r.tone for r in rows] == ["ok", "warn", "warn"]
    assert rows[2].report_summary == ""


# --------------------------------------------------------------------------
# D-3: TodoList view models for the deepagents plan
# --------------------------------------------------------------------------


def test_d3_todo_items_returns_empty_for_non_list() -> None:
    assert vm.todo_items(None) == ()
    assert vm.todo_items("foo") == ()
    assert vm.todo_items({"a": 1}) == ()


def test_d3_todo_items_maps_status_to_tone() -> None:
    items = vm.todo_items([
        {"content": "Plan", "status": "completed"},
        {"content": "Apply", "status": "in_progress"},
        {"content": "Review", "status": "pending"},
    ])
    assert [i.tone for i in items] == ["ok", "warn", "normal"]
    assert [i.status for i in items] == ["completed", "in_progress", "pending"]


def test_d3_todo_items_filters_invalid_entries() -> None:
    items = vm.todo_items([
        {"content": "good", "status": "pending"},
        {"content": "", "status": "pending"},   # empty content
        {"content": "bad", "status": "NOPE"},   # bad status
        {"content": "no status"},               # missing status
        "not a dict",                           # non-dict
        {"content": "also good", "status": "completed"},
    ])
    assert len(items) == 2
    assert items[0].content == "good"
    assert items[1].status == "completed"


def test_d3_todo_items_bounds_content_length() -> None:
    items = vm.todo_items([{"content": "x" * 5000, "status": "pending"}])
    assert len(items[0].content) == vm.MAX_BODY_CHARS


def test_d3_run_view_picks_up_todos_from_snapshot() -> None:
    snap = _full_snapshot()
    snap["todos"] = [
        {"content": "Plan", "status": "completed"},
        {"content": "Apply", "status": "in_progress"},
    ]
    rv = vm.run_view(snap)
    assert rv is not None
    assert len(rv.todos) == 2
    assert rv.todos[0].content == "Plan"
    assert rv.todos[1].tone == "warn"


def test_d3_run_view_handles_missing_todos_field() -> None:
    # A snapshot from a pre-D-2 server (or a run that never wrote todos)
    # has no 'todos' key; run_view must surface an empty tuple.
    snap = _full_snapshot()
    snap.pop("todos", None)
    rv = vm.run_view(snap)
    assert rv is not None
    assert rv.todos == ()


def test_d3_run_view_handles_invalid_todos_field() -> None:
    # A malformed todos value must not crash the inspector; treat as empty.
    snap = _full_snapshot()
    snap["todos"] = "not a list"
    rv = vm.run_view(snap)
    assert rv is not None
    assert rv.todos == ()


# --------------------------------------------------------------------------
# Stage A / Task 2: FailureView, failure_view, failure_card, terminal items
# --------------------------------------------------------------------------
# Qt-free failure normalization + terminal-result selection. The view model
# must surface only the safe fields (code, message_for_user, retryable) and
# must never render technical_detail_ref, traceback, provider response,
# category internals, suggested_actions, or the raw mapping's str()/repr().


def _failure(**overrides):
    payload = {
        "code": "model.provider_failed",
        "category": "provider_transient",
        "message_for_user": "The model provider could not complete this run.",
        "technical_detail_ref": "traceback-and-secret-must-not-render",
        "retryable": True,
        "requires_user_action": False,
        "suggested_actions": ["internal suggestion must not render"],
    }
    payload.update(overrides)
    return payload


# 一、有效 failure payload
def test_failure_view_extracts_only_safe_bounded_fields() -> None:
    failure = vm.failure_view(_failure())
    assert failure.code == "model.provider_failed"
    assert failure.message == "The model provider could not complete this run."
    assert failure.retryable is True
    assert failure.tone == "error"
    assert "traceback-and-secret" not in repr(failure)
    assert "internal suggestion" not in repr(failure)


# 二、retryable=False 精确保留
def test_failure_view_preserves_exact_bool_false() -> None:
    failure = vm.failure_view(_failure(retryable=False))
    assert failure.retryable is False
    assert failure.tone == "error"


# 三、畸形 payload 的安全 fallback
def test_failure_view_safe_fallback_for_non_dict_payloads() -> None:
    for payload in (None, "not a dict", [], {}):
        failure = vm.failure_view(payload)
        assert failure.code == "runtime.failed", payload
        assert failure.message == "The run failed before producing a response.", payload
        assert failure.retryable is False, payload
        assert failure.tone == "error", payload


def test_failure_view_safe_fallback_for_malformed_field_types() -> None:
    failure = vm.failure_view(
        {"code": 42, "message_for_user": [], "retryable": "yes"}
    )
    assert failure.code == "runtime.failed"
    assert failure.message == "The run failed before producing a response."
    assert failure.retryable is False
    # Malformed fields must not be str()'d into the visible repr.
    assert "42" not in repr(failure)
    assert "[]" not in repr(failure)


# 四、部分有效 payload
def test_failure_view_partial_valid_code_invalid_message_uses_safe_message() -> None:
    failure = vm.failure_view(
        {"code": "model.timeout", "message_for_user": None, "retryable": "true"}
    )
    # Valid bounded code is retained; message falls back; retryable only exact bool.
    assert failure.code == "model.timeout"
    assert failure.message == "The run failed before producing a response."
    assert failure.retryable is False


def test_failure_view_partial_valid_message_invalid_code_uses_safe_code() -> None:
    failure = vm.failure_view(
        {"code": "", "message_for_user": "Safe message", "retryable": 1}
    )
    # Empty code falls back; valid bounded message retained; retryable False.
    assert failure.code == "runtime.failed"
    assert failure.message == "Safe message"
    assert failure.retryable is False


# 五、边界裁剪
def test_failure_view_bounds_code_to_title_and_message_to_body() -> None:
    failure = vm.failure_view({
        "code": "a" * (vm.MAX_TITLE_CHARS + 50),
        "message_for_user": "b" * (vm.MAX_BODY_CHARS + 500),
        "retryable": False,
    })
    assert len(failure.code) <= vm.MAX_TITLE_CHARS
    assert len(failure.message) <= vm.MAX_BODY_CHARS


def test_failure_card_body_never_exceeds_cap_even_with_retry_hint() -> None:
    failure = vm.failure_view({
        "code": "model.timeout",
        "message_for_user": "m" * (vm.MAX_BODY_CHARS + 500),
        "retryable": True,
    })
    card = vm.failure_card(failure)
    # The retry hint must not push the whole body past the hard cap.
    assert len(card.body) <= vm.MAX_BODY_CHARS
    assert "Retry may succeed." in card.body


# 六、技术信息绝不泄漏
def test_failure_view_and_card_never_leak_technical_detail() -> None:
    payload = {
        "code": "runtime.failed",
        "message_for_user": "Safe message",
        "technical_detail_ref": "C:\\secret\\traceback.txt",
        "traceback": "API_KEY=secret",
        "provider_response": {"raw": "secret"},
        "retryable": True,
    }
    failure = vm.failure_view(payload)
    card = vm.failure_card(failure)
    forbidden = ("C:\\secret", "API_KEY", "provider_response", "raw")
    for blob in (repr(failure), failure.code, failure.message):
        for token in forbidden:
            assert token not in blob, (token, blob)
    for blob in (repr(card), card.title, card.body):
        for token in forbidden:
            assert token not in blob, (token, blob)


# 七、RunView failure 字段
def test_run_view_failure_field_only_present_for_failed_status() -> None:
    failed = vm.run_view(_full_snapshot(status="Failed", failure_json=_failure()))
    completed = vm.run_view(_full_snapshot(status="Completed", failure_json=_failure()))
    cancelled = vm.run_view(_full_snapshot(status="Cancelled", failure_json=_failure()))
    assert failed is not None
    assert failed.failure is not None
    assert completed is not None
    assert completed.failure is None
    assert cancelled is not None
    assert cancelled.failure is None


def test_run_view_failure_safe_fallback_when_missing_or_malformed() -> None:
    missing = vm.run_view(_full_snapshot(status="Failed", failure_json=None))
    assert missing is not None
    assert missing.failure is not None
    assert missing.failure.code == "runtime.failed"
    assert missing.failure.message == "The run failed before producing a response."
    assert missing.failure.retryable is False

    malformed = vm.run_view(_full_snapshot(status="Failed", failure_json="nope"))
    assert malformed is not None
    assert malformed.failure is not None
    assert malformed.failure.code == "runtime.failed"


# 八、failure_card
def test_failure_card_retryable_renders_safe_fields_and_hint() -> None:
    failure = vm.failure_view(_failure())
    card = vm.failure_card(failure)
    assert card.kind == "error"
    assert card.tone == "error"
    assert "model.provider_failed" in card.title  # bounded code in title
    assert "The model provider could not complete this run." in card.body
    assert "Retry may succeed." in card.body
    assert "traceback-and-secret" not in card.body
    assert "traceback-and-secret" not in card.title


def test_failure_card_non_retryable_omits_hint() -> None:
    failure = vm.failure_view(_failure(retryable=False))
    card = vm.failure_card(failure)
    assert card.kind == "error"
    assert card.tone == "error"
    assert "Retry may succeed." not in card.body


def test_failure_card_rejects_non_exact_failure_view() -> None:
    # The contract is exact-type, not isinstance: a raw mapping or None must
    # never slip past the typed boundary and get rendered as a card.
    with pytest.raises(TypeError, match="exact FailureView"):
        vm.failure_card(None)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="exact FailureView"):
        vm.failure_card({  # type: ignore[arg-type]
            "code": "runtime.failed",
            "message": "bad",
            "retryable": False,
        })


# 九、terminal_result_items
def test_terminal_result_completed_with_output_is_assistant_card() -> None:
    items = vm.terminal_result_items({"status": "Completed"}, "the final answer")
    assert len(items) == 1
    assert items[0].kind == "assistant"
    assert items[0].body == "the final answer"


def test_terminal_result_completed_without_output_is_normal_notice() -> None:
    items = vm.terminal_result_items({"status": "Completed"}, "")
    assert len(items) == 1
    assert items[0].kind == "notice"
    assert items[0].tone == "normal"
    assert items[0].body == "The run completed without a response."


def test_terminal_result_cancelled_without_output_is_warn_notice() -> None:
    items = vm.terminal_result_items({"status": "Cancelled"}, "")
    assert len(items) == 1
    assert items[0].kind == "notice"
    assert items[0].tone == "warn"
    assert items[0].body == "The run was cancelled."


def test_terminal_result_cancelled_with_output_preserves_assistant_only() -> None:
    items = vm.terminal_result_items({"status": "Cancelled"}, "partial work")
    assert [i.kind for i in items] == ["assistant"]
    assert items[0].body == "partial work"


def test_terminal_result_failed_without_output_emits_single_error_card() -> None:
    items = vm.terminal_result_items(
        {"status": "Failed", "failure_json": _failure()}, ""
    )
    assert len(items) == 1
    assert items[0].kind == "error"
    assert items[0].body  # never an empty assistant card
    assert items[0].body != ""
    assert "traceback-and-secret" not in items[0].body


def test_terminal_result_failed_with_partial_output_orders_assistant_then_error() -> None:
    items = vm.terminal_result_items(
        {"status": "Failed", "failure_json": _failure()}, "partial output"
    )
    assert [i.kind for i in items] == ["assistant", "error"]
    assert items[0].body == "partial output"
    assert items[1].body != ""  # error card present and non-empty


def test_terminal_result_failed_with_malformed_failure_uses_safe_fallback() -> None:
    items = vm.terminal_result_items(
        {"status": "Failed", "failure_json": "garbage"}, ""
    )
    assert len(items) == 1
    assert items[0].kind == "error"
    assert items[0].body == "The run failed before producing a response."


def test_terminal_result_failed_with_missing_failure_json_uses_safe_fallback() -> None:
    items = vm.terminal_result_items({"status": "Failed"}, "")
    assert len(items) == 1
    assert items[0].kind == "error"
    assert items[0].body == "The run failed before producing a response."


def test_terminal_result_non_terminal_statuses_return_empty() -> None:
    for status in ("Planning", "Finalizing", "StopRequested", "Created", "Retrying"):
        items = vm.terminal_result_items({"status": status}, "ignored")
        assert items == (), status


def test_terminal_result_unknown_status_returns_empty_without_raising() -> None:
    items = vm.terminal_result_items({"status": "Mystery"}, "anything")
    assert items == ()


def test_terminal_result_non_dict_run_returns_empty() -> None:
    assert vm.terminal_result_items(None, "x") == ()
    assert vm.terminal_result_items("nope", "x") == ()
    assert vm.terminal_result_items([], "x") == ()


def test_terminal_result_non_string_output_treated_as_empty() -> None:
    # output must be an exact str to count; otherwise treated as empty.
    items = vm.terminal_result_items({"status": "Completed"}, None)
    assert [i.kind for i in items] == ["notice"]
    assert items[0].tone == "normal"
