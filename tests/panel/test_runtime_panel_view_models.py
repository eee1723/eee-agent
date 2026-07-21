"""Message-item and status aggregation contracts (Qt-free)."""

from __future__ import annotations

from houdini_side.runtime_panel import view_models as vm


def test_user_message_is_bounded() -> None:
    item = vm.user_message("x" * 5000)
    assert item.kind == "user"
    assert len(item.body) == vm.MAX_BODY_CHARS
    assert item.tone == "normal"


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
