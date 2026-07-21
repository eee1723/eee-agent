"""Message-item and status aggregation contracts (Qt-free)."""

from __future__ import annotations

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
