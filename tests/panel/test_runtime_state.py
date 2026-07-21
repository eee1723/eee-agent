from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eee_agent.panel.client_state import PanelClientError
from eee_agent.panel.runtime_state import (
    RuntimePanelState,
    append_artifact_summary,
    append_vision_summary,
    approval_is_actionable,
    artifact_refresh_required,
    changeset_refresh_required,
    parse_artifact_event,
    parse_changeset_list,
    parse_session_snapshot,
    parse_vision_event,
    vision_refresh_required,
)

SID = "ses_" + "a" * 32
RID = "run_" + "b" * 32
CHG = "chg_" + "c" * 32
APR = "apr_" + "d" * 32
NOW = "2026-07-16T12:00:00+00:00"


def _run(*, status: str = "Planning", final: str | None = None) -> dict:
    return {
        "run_id": RID,
        "session_id": SID,
        "status": status,
        "user_input": "Inspect the selected node",
        "final_response": final,
        "created_at": NOW,
        "started_at": NOW,
        "finished_at": None,
        "failure_json": None,
        "model_snapshot_json": {"model": "fake"},
        "todos": [],
    }


def _snapshot(*, active: dict | None = None, seq: int = 5) -> dict:
    run = _run()
    return {
        "session": {
            "session_id": SID,
            "title": "Inspection",
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
            "last_seq": seq,
            "replay_floor_seq": 0,
        },
        "runs": [run],
        "active_run": active,
        "snapshot_seq": seq,
        "has_earlier_runs": False,
        "earliest_included_run_id": RID,
        "version_report": {"eee_agent": "test"},
    }


def _event(seq: int, event_type: str, payload: dict, *, run_id=RID) -> dict:
    return {
        "kind": "event",
        "session_id": SID,
        "run_id": run_id,
        "seq": seq,
        "timestamp": NOW,
        "type": event_type,
        "payload": payload,
    }


def _changeset(*, decision: str = "Pending", state: str = "AwaitingApproval"):
    return {
        "change_id": CHG,
        "run_id": RID,
        "state": state,
        "changeset_digest": "e" * 64,
        "created_at": NOW,
        "required_permission": "OwnedWorkspace",
        "risk": {
            "operation_count": 3,
            "touches_external_nodes": False,
            "changes_wiring": True,
            "requires_backup": True,
            "effect_count": 3,
            "effect_names": ["node.create", "parm.set", "wire.connect"],
            "affected_path_count": 14,
            "affected_paths": [f"/obj/ws/n{i}" for i in range(12)],
            "affected_paths_truncated": True,
        },
        "approval": {
            "approval_id": APR,
            "decision": decision,
            "expires_at": "2026-07-16T12:05:00+00:00",
            "decided_at": None,
            "approved_instance_id": None,
            "approved_scene_epoch": None,
        },
        "receipt": None,
    }


def test_parse_session_snapshot_validates_exact_shape() -> None:
    parsed = parse_session_snapshot(_snapshot(active=_run()))
    assert parsed["snapshot_seq"] == 5
    assert parsed["active_run"]["run_id"] == RID
    bad = _snapshot()
    bad["extra"] = True
    with pytest.raises(PanelClientError):
        parse_session_snapshot(bad)


def test_runtime_panel_state_recovers_and_applies_ordered_events() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run()))
    assert state.snapshot()["active_run"]["status"] == "Planning"
    assert state.apply_event(
        _event(6, "model.text_delta", {"text": "Hello "})
    )
    assert state.apply_event(
        _event(
            7,
            "tool.started",
            {"call_id": "call_1", "name": "inspect_node", "index": 0},
        )
    )
    assert state.apply_event(
        _event(8, "model.text_delta", {"text": "world"})
    )
    assert state.snapshot()["output"] == "Hello world"
    assert state.snapshot()["activity"][0]["name"] == "inspect_node"
    assert state.apply_event(
        _event(9, "run.state_changed", {"from": "Planning", "to": "Completed"})
    )
    assert state.snapshot()["active_run"] is None
    assert state.snapshot()["selected_run"]["status"] == "Completed"
    assert state.apply_event(
        _event(8, "model.text_delta", {"text": "duplicate"})
    ) is False
    assert state.snapshot()["output"] == "Hello world"


def test_runtime_panel_state_bounds_output_and_activity() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=0))
    state.apply_event(_event(1, "model.text_delta", {"text": "x" * 40_000}))
    for seq in range(2, 122):
        state.apply_event(
            _event(
                seq,
                "tool.started",
                {"call_id": str(seq), "name": f"tool_{seq}", "index": 0},
            )
        )
    assert len(state.snapshot()["output"]) == 32_000
    assert len(state.snapshot()["activity"]) == 100


def test_reasoning_delta_accumulates_into_separate_thinking_buffer() -> None:
    # model.reasoning_delta is the thinking stream; it must accumulate into a
    # buffer distinct from the output text and surface via snapshot["thinking"].
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=5))
    state.apply_event(_event(6, "model.reasoning_delta", {"text": "Let me think "}))
    state.apply_event(_event(7, "model.reasoning_delta", {"text": "about this."}))
    snap = state.snapshot()
    assert snap["thinking"] == "Let me think about this."
    # Output stays untouched — thinking and reply are separate channels.
    assert snap["output"] == ""


def test_reasoning_delta_is_bounded_like_output() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=0))
    state.apply_event(_event(1, "model.reasoning_delta", {"text": "y" * 40_000}))
    assert len(state.snapshot()["thinking"]) == 32_000


def test_reasoning_delta_buffer_cleared_on_terminal_run() -> None:
    # Thinking is OPERATIONAL: once the run terminates the live buffer is
    # dropped (the streaming card is swapped for the final reply).
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=5))
    state.apply_event(_event(6, "model.reasoning_delta", {"text": "thinking..."}))
    assert state.snapshot()["thinking"] == "thinking..."
    state.apply_event(
        _event(7, "run.state_changed", {"from": "Planning", "to": "Completed"})
    )
    # selected_run is now the completed run; its thinking buffer is gone.
    assert state.snapshot()["thinking"] == ""


def test_load_snapshot_resets_thinking_buffer() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=5))
    state.apply_event(_event(6, "model.reasoning_delta", {"text": "mid-stream"}))
    assert state.snapshot()["thinking"] == "mid-stream"
    # A fresh snapshot means we are not mid-stream; thinking is not replayed.
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    assert state.snapshot()["thinking"] == ""


def test_runtime_panel_state_bounds_recovered_final_output() -> None:
    state = RuntimePanelState()
    state.load_snapshot(
        _snapshot(active=None, seq=5)
        | {"runs": [_run(status="Completed", final="x" * 40_000)]}
    )
    assert len(state.snapshot()["output"]) == 32_000


def test_runtime_panel_state_does_not_advance_on_invalid_event() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=5))
    with pytest.raises(PanelClientError):
        state.apply_event(
            _event(
                6,
                "run.state_changed",
                {"from": "Planning", "to": "NotARealState"},
            )
        )
    assert state.snapshot()["last_seq"] == 5
    assert state.snapshot()["active_run"]["status"] == "Planning"


def test_changeset_list_and_actionability_are_strict() -> None:
    parsed = parse_changeset_list({"changesets": [_changeset()]})
    assert parsed[0]["risk"]["affected_paths_truncated"] is True
    assert approval_is_actionable(
        parsed[0],
        now=datetime(2026, 7, 16, 12, 1, tzinfo=timezone.utc),
    )
    assert not approval_is_actionable(
        parsed[0],
        now=datetime(2026, 7, 16, 12, 6, tzinfo=timezone.utc),
    )
    bad = _changeset()
    bad["risk"]["affected_paths"].append("/obj/ws/overflow")
    with pytest.raises(PanelClientError):
        parse_changeset_list({"changesets": [bad]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "Unknown"),
        ("required_permission", "Everything"),
    ],
)
def test_changeset_list_rejects_unknown_enums(field: str, value: str) -> None:
    bad = _changeset()
    bad[field] = value
    with pytest.raises(PanelClientError):
        parse_changeset_list({"changesets": [bad]})


def test_changeset_list_rejects_invalid_receipt_revision_and_epoch() -> None:
    bad = _changeset(state="Applied")
    bad["approval"] = None
    bad["receipt"] = {
        "status": "Applied",
        "instance_id": "houdini-test",
        "scene_epoch": 0,
        "before_revision": "not-a-revision",
        "after_revision": "f" * 64,
        "applied_operation_count": 3,
        "scene_may_have_changed": False,
        "completed_at": NOW,
    }
    with pytest.raises(PanelClientError):
        parse_changeset_list({"changesets": [bad]})


@pytest.mark.parametrize(
    "event_type",
    [
        "changeset.proposed",
        "approval.requested",
        "approval.approved",
        "approval.rejected",
        "approval.expired",
        "changeset.state_changed",
        "changeset.applied",
        "changeset.rolled_back",
        "recovery.critical",
    ],
)
def test_changeset_events_require_authoritative_refresh(event_type: str) -> None:
    assert changeset_refresh_required(
        {"kind": "event", "type": event_type}
    )
    assert not changeset_refresh_required(
        {"kind": "event", "type": "model.text_delta"}
    )


ART = "art_" + "e" * 32


def _artifact_message(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "kind": "event",
        "type": "modeling.artifact_captured",
        "seq": 7,
        "payload": {
            "change_id": CHG,
            "changeset_digest": "a" * 64,
            "artifact": {
                "artifact_id": ART,
                "relative_path": f"{SID}/{RID}/{ART}.png",
                "sha256": "b" * 64,
                "media_type": "image/png",
                "size_bytes": 4096,
                "schema_version": 1,
            },
            "framing": {
                "adjustments_used": 1,
                "margin_left": 0.16,
                "margin_right": 0.17,
                "margin_bottom": 0.12,
                "margin_top": 0.12,
                "longest_axis_ratio": 0.78,
                "center_offset": 0.002,
            },
        },
    }
    message.update(overrides)
    return message


def _failed_message(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "kind": "event",
        "type": "modeling.capture_failed",
        "seq": 9,
        "payload": {
            "change_id": CHG,
            "changeset_digest": "a" * 64,
            "code": "capture.framing_failed",
        },
    }
    message.update(overrides)
    return message


@pytest.mark.parametrize(
    "event_type",
    ["modeling.artifact_captured", "modeling.capture_failed"],
)
def test_artifact_events_require_authoritative_refresh(event_type: str) -> None:
    assert artifact_refresh_required({"kind": "event", "type": event_type})
    assert not artifact_refresh_required({"kind": "event", "type": "model.text_delta"})
    assert not artifact_refresh_required({"kind": "command", "type": event_type})


def test_parse_artifact_captured_event_returns_bounded_summary() -> None:
    summary = parse_artifact_event(_artifact_message())
    assert summary["kind"] == "captured"
    assert summary["artifact_id"] == ART
    assert summary["relative_path"] == f"{SID}/{RID}/{ART}.png"
    assert summary["sha256"] == "b" * 64
    assert summary["media_type"] == "image/png"
    assert summary["size_bytes"] == 4096
    assert summary["seq"] == 7
    assert summary["state"] == "available"
    assert summary["viewable"] is True


@pytest.mark.parametrize("state", ["pending", "pending_eviction", "evicted", "missing", "failed"])
def test_parse_artifact_captured_event_exposes_non_viewable_lifecycle_state(state: str) -> None:
    message = _artifact_message()
    message["payload"]["artifact"]["artifact_state"] = state  # type: ignore[index]
    summary = parse_artifact_event(message)
    assert summary["state"] == state
    assert summary["viewable"] is False


def test_parse_artifact_lifecycle_event_is_not_a_captured_image() -> None:
    summary = parse_artifact_event(
        {
            "kind": "event",
            "type": "modeling.artifact_state_changed",
            "seq": 12,
            "payload": {"artifact_id": ART, "state": "evicted"},
        }
    )
    assert summary["kind"] == "lifecycle"
    assert summary["state"] == "evicted"
    assert summary["viewable"] is False


def test_parse_capture_failed_event_returns_bounded_summary() -> None:
    summary = parse_artifact_event(_failed_message())
    assert summary == {
        "kind": "failed",
        "state": "failed",
        "viewable": False,
        "code": "capture.framing_failed",
        "change_id": CHG,
        "seq": 9,
    }


def test_parse_artifact_event_rejects_non_artifact_types() -> None:
    with pytest.raises(PanelClientError):
        parse_artifact_event({"kind": "event", "type": "model.text_delta", "seq": 1, "payload": {}})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m["payload"].pop("framing"),
        lambda m: m["payload"].__setitem__("extra", True),
        lambda m: m["payload"]["artifact"].__setitem__("artifact_id", RID),
        lambda m: m["payload"]["artifact"].__setitem__("sha256", "B" * 64),
        lambda m: m["payload"]["artifact"].__setitem__("size_bytes", -1),
        lambda m: m["payload"]["artifact"].__setitem__("schema_version", 2),
        lambda m: m["payload"]["artifact"].__setitem__("relative_path", "../escape.png"),
        lambda m: m["payload"]["artifact"].__setitem__("relative_path", "a\\b.png"),
        lambda m: m["payload"]["artifact"].__setitem__("media_type", ""),
        lambda m: m["payload"]["framing"].__setitem__("adjustments_used", 3),
        lambda m: m["payload"]["framing"].__setitem__("margin_left", float("nan")),
        lambda m: m["payload"].__setitem__("changeset_digest", "not-a-digest"),
        lambda m: m.__setitem__("seq", 0),
    ],
)
def test_parse_artifact_captured_event_is_strict(mutation) -> None:
    message = _artifact_message()
    mutation(message)
    with pytest.raises(PanelClientError):
        parse_artifact_event(message)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m["payload"].pop("code"),
        lambda m: m["payload"].__setitem__("framing", {}),
        lambda m: m["payload"].__setitem__("code", ""),
        lambda m: m["payload"].__setitem__("change_id", "bad id"),
    ],
)
def test_parse_capture_failed_event_is_strict(mutation) -> None:
    message = _failed_message()
    mutation(message)
    with pytest.raises(PanelClientError):
        parse_artifact_event(message)


def test_append_artifact_summary_bounds_to_newest_fifty() -> None:
    items: tuple = ()
    for index in range(55):
        items = append_artifact_summary(
            items, {"kind": "failed", "code": f"c{index}", "change_id": CHG, "seq": index}
        )
    assert len(items) == 50
    # Newest first; the five oldest were dropped.
    assert items[0]["code"] == "c54"
    assert items[-1]["code"] == "c5"


# --------------------------------------------------------------------------
# Vision evaluation events
# --------------------------------------------------------------------------

VISION_ART = "art_" + "f" * 32


def _vision_artifact() -> dict[str, object]:
    return {
        "artifact_id": VISION_ART,
        "relative_path": f"{SID}/{RID}/{VISION_ART}.png",
        "sha256": "c" * 64,
        "media_type": "image/png",
        "size_bytes": 4096,
        "schema_version": 1,
    }


def _vision_message(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "kind": "event",
        "type": "vision.evaluation_completed",
        "seq": 11,
        "payload": {
            "brief": "make a box",
            "spec": "operations=1; effects=parm.set; targets=/obj/ws/box1",
            "changeset_digest": "a" * 64,
            "approval": "Consumed:local_user:apr_" + "d" * 32,
            "receipt": "applied:" + "b" * 64,
            "validation_report": ["Cook:Passed:modeling.cook.ok"],
            "artifact_refs": [_vision_artifact()],
            "artifact_status": ["available"],
            "knowledge_manifest_sha256": "e" * 64,
            "vision_status": "completed",
            "vision_report": {
                "summary": "silhouette matches",
                "observations": ["bounded observation"],
                "confidence": 0.9,
                "advisory_passed": True,
            },
            "final_decision": {
                "status": "completed",
                "accepted": True,
                "deterministic_valid": True,
                "summary": "silhouette matches",
            },
            "recovery_evidence": [],
        },
    }
    message.update(overrides)
    return message


def _unavailable_vision_message() -> dict[str, object]:
    message = _vision_message()
    payload = message["payload"]  # type: ignore[index]
    payload["vision_status"] = "unavailable"
    payload["vision_report"] = None
    payload["final_decision"] = {
        "status": "unavailable",
        "accepted": True,
        "deterministic_valid": True,
        "summary": "Visual evaluation provider is unavailable.",
    }
    return message


def test_vision_events_require_authoritative_refresh() -> None:
    assert vision_refresh_required(
        {"kind": "event", "type": "vision.evaluation_completed"}
    )
    assert not vision_refresh_required({"kind": "event", "type": "model.text_delta"})
    assert not vision_refresh_required(
        {"kind": "command", "type": "vision.evaluation_completed"}
    )


def test_parse_vision_completed_event_returns_bounded_summary() -> None:
    summary = parse_vision_event(_vision_message())
    assert summary["kind"] == "vision"
    assert summary["status"] == "completed"
    assert summary["accepted"] is True
    assert summary["deterministic_valid"] is True
    assert summary["advisory_passed"] is True
    assert summary["summary"] == "silhouette matches"
    assert summary["report_summary"] == "silhouette matches"
    assert summary["observation_count"] == 1
    assert summary["artifact_count"] == 1
    assert summary["changeset_digest"] == "a" * 64
    assert summary["seq"] == 11


def test_parse_vision_unavailable_event_has_no_report() -> None:
    summary = parse_vision_event(_unavailable_vision_message())
    assert summary["kind"] == "vision"
    assert summary["status"] == "unavailable"
    assert summary["accepted"] is True
    assert summary["deterministic_valid"] is True
    assert summary["advisory_passed"] is None
    assert summary["summary"] == "Visual evaluation provider is unavailable."
    assert summary["report_summary"] is None
    assert summary["observation_count"] == 0
    assert summary["artifact_count"] == 1


def test_parse_vision_event_rejects_non_vision_types() -> None:
    with pytest.raises(PanelClientError):
        parse_vision_event(
            {"kind": "event", "type": "model.text_delta", "seq": 1, "payload": {}}
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m["payload"].pop("spec"),
        lambda m: m["payload"].__setitem__("extra", True),
        lambda m: m["payload"].__setitem__("brief", ""),
        lambda m: m["payload"].__setitem__("brief", "x" * 4097),
        lambda m: m["payload"].__setitem__("changeset_digest", "not-a-digest"),
        lambda m: m["payload"].__setitem__("approval", "x" * 513),
        lambda m: m["payload"].__setitem__("validation_report", "not-a-list"),
        lambda m: m["payload"].__setitem__("validation_report", ["x"] * 65),
        lambda m: m["payload"].__setitem__("validation_report", ["x" * 513]),
        lambda m: m["payload"].__setitem__("artifact_refs", [_vision_artifact()] * 17),
        lambda m: m["payload"]["artifact_refs"][0].__setitem__("size_bytes", 0),
        lambda m: m["payload"]["artifact_refs"][0].__setitem__("artifact_id", RID),
        lambda m: m["payload"].__setitem__("artifact_status", ["available", "available"]),
        lambda m: m["payload"].__setitem__("artifact_status", ["bogus"]),
        lambda m: m["payload"].__setitem__("knowledge_manifest_sha256", "zz"),
        lambda m: m["payload"].__setitem__("vision_status", "bogus"),
        lambda m: m["payload"]["vision_report"].__setitem__("confidence", float("nan")),
        lambda m: m["payload"]["vision_report"].__setitem__("confidence", 1.5),
        lambda m: m["payload"]["vision_report"].__setitem__("observations", ["x"] * 33),
        lambda m: m["payload"]["final_decision"].__setitem__("status", "unavailable"),
        lambda m: m["payload"]["final_decision"].__setitem__("accepted", "yes"),
        lambda m: m["payload"].__setitem__("recovery_evidence", ["x"] * 33),
        lambda m: m.__setitem__("seq", 0),
        # Completed without a report.
        lambda m: m["payload"].__setitem__("vision_report", None),
        # Accepted cannot contradict deterministic failure.
        lambda m: m["payload"]["final_decision"].__setitem__("deterministic_valid", False),
        # Accepted cannot contradict an advisory failure.
        lambda m: m["payload"]["vision_report"].__setitem__("advisory_passed", False),
    ],
)
def test_parse_vision_event_is_strict(mutation) -> None:
    message = _vision_message()
    mutation(message)
    with pytest.raises(PanelClientError):
        parse_vision_event(message)


@pytest.mark.parametrize(
    "mutation",
    [
        # Non-completed statuses cannot carry a report.
        lambda m: m["payload"].__setitem__(
            "vision_report",
            {
                "summary": "s",
                "observations": [],
                "confidence": 0.5,
                "advisory_passed": True,
            },
        ),
        # Decision status must agree with the evaluation status.
        lambda m: m["payload"]["final_decision"].__setitem__("status", "completed"),
    ],
)
def test_parse_vision_unavailable_event_is_strict(mutation) -> None:
    message = _unavailable_vision_message()
    mutation(message)
    with pytest.raises(PanelClientError):
        parse_vision_event(message)


def test_append_vision_summary_bounds_to_newest_fifty() -> None:
    items: tuple = ()
    for index in range(55):
        items = append_vision_summary(
            items, {"kind": "vision", "status": "completed", "seq": index}
        )
    assert len(items) == 50
    assert items[0]["seq"] == 54
    assert items[-1]["seq"] == 5


# --------------------------------------------------------------------------
# Artifact summary lifecycle de-duplication
# --------------------------------------------------------------------------


def test_lifecycle_event_merges_into_existing_captured_row() -> None:
    items: tuple = ()
    captured = parse_artifact_event(_artifact_message())
    items = append_artifact_summary(items, captured)
    lifecycle = parse_artifact_event(
        {
            "kind": "event",
            "type": "modeling.artifact_state_changed",
            "seq": 12,
            "payload": {"artifact_id": ART, "state": "evicted"},
        }
    )
    items = append_artifact_summary(items, lifecycle)
    # One row, not two: the lifecycle event updates the captured row.
    assert len(items) == 1
    assert items[0]["kind"] == "captured"
    assert items[0]["artifact_id"] == ART
    assert items[0]["state"] == "evicted"
    assert items[0]["viewable"] is False
    assert items[0]["seq"] == 12


def test_lifecycle_event_for_unknown_artifact_keeps_its_own_row() -> None:
    items: tuple = ()
    lifecycle = parse_artifact_event(
        {
            "kind": "event",
            "type": "artifact.reconciled",
            "seq": 12,
            "payload": {"artifact_id": ART, "from_state": "pending", "state": "missing"},
        }
    )
    items = append_artifact_summary(items, lifecycle)
    assert len(items) == 1
    assert items[0]["kind"] == "lifecycle"
    assert items[0]["state"] == "missing"
    assert items[0]["viewable"] is False
    assert items[0]["recovery"] is True


def test_lifecycle_merge_does_not_touch_other_artifacts() -> None:
    items: tuple = ()
    items = append_artifact_summary(items, parse_artifact_event(_artifact_message()))
    other = _artifact_message()
    other["payload"]["artifact"]["artifact_id"] = "art_" + "9" * 32  # type: ignore[index]
    other["payload"]["artifact"]["relative_path"] = f"{SID}/{RID}/{'art_' + '9' * 32}.png"  # type: ignore[index]
    items = append_artifact_summary(items, parse_artifact_event(other))
    lifecycle = parse_artifact_event(
        {
            "kind": "event",
            "type": "modeling.artifact_state_changed",
            "seq": 13,
            "payload": {"artifact_id": ART, "state": "failed"},
        }
    )
    items = append_artifact_summary(items, lifecycle)
    assert len(items) == 2
    states = {item.get("artifact_id"): item["state"] for item in items}
    assert states[ART] == "failed"
    assert states["art_" + "9" * 32] == "available"


# --------------------------------------------------------------------------
# B-2: changeset.applied / changeset.rolled_back events carry apply outcomes
# --------------------------------------------------------------------------


def test_b2_changeset_rolled_back_event_records_error_outcome() -> None:
    """The reported symptom from ses_65fefe0a5d: a RolledBack with
    applied_op_ids=[] reached the LLM with no cause, so it guessed
    '似乎仅部分应用'. B-2 captures the receipt's error fields into the
    panel state so the inspector (and any LLM context layer) can read them.
    """
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    assert state.apply_event(
        _event(
            11,
            "changeset.rolled_back",
            {
                "change_id": CHG,
                "receipt_status": "RolledBack",
                "applied_op_ids": [],
                "scene_may_have_changed": False,
                "error_code": "houdini.operation_failed",
                "error_message": "Cannot create node 'copytopoints::2.0'",
            },
        )
    )
    snap = state.snapshot()
    outcomes = snap["apply_outcomes"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["change_id"] == CHG
    assert outcome["receipt_status"] == "RolledBack"
    assert outcome["error_code"] == "houdini.operation_failed"
    assert outcome["error_message"] == "Cannot create node 'copytopoints::2.0'"


def test_b2_changeset_applied_event_has_no_error_fields() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    assert state.apply_event(
        _event(
            11,
            "changeset.applied",
            {
                "change_id": CHG,
                "receipt_status": "Applied",
                "applied_op_ids": ["op_a", "op_b"],
                "scene_may_have_changed": False,
            },
        )
    )
    outcome = state.snapshot()["apply_outcomes"][0]
    assert outcome["receipt_status"] == "Applied"
    assert outcome["applied_op_ids"] == ["op_a", "op_b"]
    assert "error_code" not in outcome
    assert "error_message" not in outcome


def test_b2_load_snapshot_clears_apply_outcomes() -> None:
    """A snapshot from the server does not carry apply outcomes today; a
    fresh snapshot must clear stale outcomes from the previous session so
    they cannot leak into the new view."""
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    state.apply_event(
        _event(
            11,
            "changeset.rolled_back",
            {
                "change_id": CHG,
                "receipt_status": "RolledBack",
                "applied_op_ids": [],
                "scene_may_have_changed": False,
                "error_code": "houdini.operation_failed",
                "error_message": "boom",
            },
        )
    )
    assert len(state.snapshot()["apply_outcomes"]) == 1
    state.load_snapshot(_snapshot(active=_run(), seq=20))
    assert state.snapshot()["apply_outcomes"] == ()


def test_b2_apply_outcome_keyed_by_change_id_overwrites_on_replay() -> None:
    """Replaying the same change_id (e.g. after a reconnect) overwrites the
    previous entry instead of accumulating duplicates."""
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    payload = {
        "change_id": CHG,
        "receipt_status": "RolledBack",
        "applied_op_ids": [],
        "scene_may_have_changed": False,
        "error_code": "houdini.operation_failed",
        "error_message": "first",
    }
    state.apply_event(_event(11, "changeset.rolled_back", payload))
    payload["error_message"] = "second"
    state.apply_event(_event(12, "changeset.rolled_back", payload))
    outcomes = state.snapshot()["apply_outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["error_message"] == "second"


def test_b2_recovery_critical_event_also_recorded() -> None:
    """Partial/CriticalRecovery outcomes use the recovery.critical event type
    but should still be captured so the inspector can flag them."""
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    assert state.apply_event(
        _event(
            11,
            "recovery.critical",
            {
                "change_id": CHG,
                "receipt_status": "CriticalRecovery",
                "applied_op_ids": ["op_a"],
                "scene_may_have_changed": True,
                "error_code": "apply.unexpected_error",
                "error_message": "rollback crashed; scene state uncertain",
            },
        )
    )
    outcome = state.snapshot()["apply_outcomes"][0]
    assert outcome["receipt_status"] == "CriticalRecovery"
    assert outcome["scene_may_have_changed"] is True
    assert outcome["error_code"] == "apply.unexpected_error"


# --------------------------------------------------------------------------
# D-1/D-2: todos.updated events update run.todos
# --------------------------------------------------------------------------


def test_d1_d2_todos_updated_event_updates_run_todos() -> None:
    """The D-1 todos.updated event carries the new plan; the panel state must
    stash it on run['todos'] so the UI's TodoList widget can render it."""
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    assert state.apply_event(
        _event(
            11,
            "todos.updated",
            {"todos": [
                {"content": "Plan", "status": "completed"},
                {"content": "Apply", "status": "in_progress"},
            ]},
        )
    )
    snap = state.snapshot()
    selected = snap["selected_run"]
    assert selected["todos"] == [
        {"content": "Plan", "status": "completed"},
        {"content": "Apply", "status": "in_progress"},
    ]


def test_d1_d2_todos_updated_empty_list_clears_run_todos() -> None:
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    state.apply_event(_event(11, "todos.updated", {"todos": [
        {"content": "x", "status": "pending"}]}))
    assert state.snapshot()["selected_run"]["todos"] == [
        {"content": "x", "status": "pending"}]
    # Empty list signal clears it.
    state.apply_event(_event(12, "todos.updated", {"todos": []}))
    assert state.snapshot()["selected_run"]["todos"] == []


def test_d1_d2_todos_updated_rejects_non_list_payload() -> None:
    """A malformed event must not corrupt the run; reject it loudly so the
    server-side bug surfaces instead of silently dropping todos."""
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=_run(), seq=10))
    with pytest.raises(PanelClientError):
        state.apply_event(_event(11, "todos.updated", {"todos": "not a list"}))


def test_d2_snapshot_run_carries_todos_field() -> None:
    """The server-side RunRecord now emits todos via to_dict; the panel must
    accept a snapshot whose runs include the todos field."""
    state = RuntimePanelState()
    state.load_snapshot(_snapshot(active=None, seq=5) | {
        "runs": [_run() | {"todos": [
            {"content": "from server", "status": "completed"}
        ]}]
    })
    snap = state.snapshot()
    runs = snap["runs"]
    assert len(runs) == 1
    assert runs[0]["todos"] == [{"content": "from server", "status": "completed"}]


def test_d2_snapshot_run_accepts_missing_todos_field_for_backward_compat() -> None:
    """A snapshot from a pre-D-2 Runtime does not include the todos field.
    The panel must accept it (treating todos as empty) instead of going
    offline — that was the original 'Runtime offline after one message'
    regression when D-2 first shipped."""
    state = RuntimePanelState()
    legacy_run = _run()
    del legacy_run["todos"]
    state.load_snapshot(_snapshot(active=None, seq=5) | {"runs": [legacy_run]})
    runs = state.snapshot()["runs"]
    assert len(runs) == 1
    # Backfilled to [] so downstream consumers (inspector, history replay)
    # see a stable shape regardless of which Runtime version emitted it.
    assert runs[0]["todos"] == []


def test_d2_snapshot_run_rejects_extra_unknown_field() -> None:
    """Backward-compat for missing 'todos' is allowed; adding brand-new
    unknown fields still fails loudly so a server bug surfaces immediately."""
    state = RuntimePanelState()
    bad_run = {**_run(), "unknown_future_field": "x"}
    with pytest.raises(PanelClientError):
        state.load_snapshot(_snapshot(active=None, seq=5) | {"runs": [bad_run]})
