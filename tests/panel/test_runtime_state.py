from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eee_agent.panel.client_state import PanelClientError
from eee_agent.panel.runtime_state import (
    RuntimePanelState,
    append_artifact_summary,
    approval_is_actionable,
    artifact_refresh_required,
    changeset_refresh_required,
    parse_artifact_event,
    parse_changeset_list,
    parse_session_snapshot,
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


def test_parse_capture_failed_event_returns_bounded_summary() -> None:
    summary = parse_artifact_event(_failed_message())
    assert summary == {
        "kind": "failed",
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
