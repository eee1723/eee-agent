"""Pure bounded Run and ChangeSet state for the docked Runtime panel."""

from __future__ import annotations

import itertools
import math
import re
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping

from eee_agent.panel.client_state import PanelClientError
from eee_agent.runtime.models import TOOL_RESULT_PREVIEW_CHARS

_TERMINAL_RUN_STATES = frozenset({"Completed", "Cancelled", "Failed"})
_SESSION_STATES = frozenset({"active", "archived"})
_RUN_STATES = frozenset(
    {
        "Created",
        "PreparingContext",
        "Planning",
        "Finalizing",
        "Completed",
        "StopRequested",
        "Stopping",
        "Cancelled",
        "Retrying",
        "Failed",
    }
)
_CHANGESET_STATES = frozenset(
    {
        "Proposed",
        "AwaitingApproval",
        "Approved",
        "Applying",
        "Applied",
        "RolledBack",
        "CriticalRecovery",
        "Stale",
        "Rejected",
        "Expired",
    }
)
_PERMISSION_MODES = frozenset(
    {"OwnedWorkspace", "ScopedPatch", "ProjectChange"}
)
_APPROVAL_DECISIONS = frozenset(
    {"Pending", "Approved", "Rejected", "Consumed", "Expired"}
)
_RECEIPT_STATUSES = frozenset(
    {"Applied", "AlreadyApplied", "RolledBack", "Partial", "CriticalRecovery"}
)
_SESSION_ID_RE = re.compile(r"^ses_[0-9a-f]{32}$")
_RUN_ID_RE = re.compile(r"^run_[0-9a-f]{32}$")
_CHANGE_ID_RE = re.compile(r"^chg_[0-9a-f]{32}$")
_APPROVAL_ID_RE = re.compile(r"^apr_[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CHANGESET_REFRESH_EVENTS = frozenset(
    {
        "changeset.proposed",
        "approval.requested",
        "approval.approved",
        "approval.rejected",
        "approval.expired",
        "changeset.state_changed",
        "changeset.applied",
        "changeset.rolled_back",
        "recovery.critical",
    }
)
_ARTIFACT_REFRESH_EVENTS = frozenset(
    {
        "modeling.artifact_captured",
        "modeling.capture_failed",
        "modeling.artifact_state_changed",
        "artifact.reconciled",
    }
)
_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{32}$")
_ARTIFACT_CAPTURED_FIELDS = frozenset(
    {"change_id", "changeset_digest", "artifact", "framing"}
)
_CAPTURE_FAILED_FIELDS = frozenset({"change_id", "changeset_digest", "code"})
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_id",
        "relative_path",
        "sha256",
        "media_type",
        "size_bytes",
        "schema_version",
    }
)
_ARTIFACT_LIFECYCLE_STATES = frozenset(
    {"pending", "available", "pending_eviction", "evicted", "missing", "failed"}
)
_FRAMING_FIELDS = frozenset(
    {
        "adjustments_used",
        "margin_left",
        "margin_right",
        "margin_bottom",
        "margin_top",
        "longest_axis_ratio",
        "center_offset",
    }
)
_MAX_ARTIFACTS = 50
_VISION_REFRESH_EVENTS = frozenset({"vision.evaluation_completed"})
_VISION_EVALUATION_FIELDS = frozenset(
    {
        "brief",
        "spec",
        "changeset_digest",
        "approval",
        "receipt",
        "validation_report",
        "artifact_refs",
        "artifact_status",
        "knowledge_manifest_sha256",
        "vision_status",
        "vision_report",
        "final_decision",
        "vision_reason_code",
        "recovery_evidence",
    }
)
_VISION_STATUSES = frozenset({"completed", "unavailable", "waived", "failed"})
_VISION_REPORT_FIELDS = frozenset(
    {"summary", "observations", "confidence", "advisory_passed"}
)
_VISION_DECISION_FIELDS = frozenset(
    {"status", "accepted", "deterministic_valid", "summary"}
)
_MAX_VISION = 50
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "session_id",
        "status",
        "user_input",
        "final_response",
        "created_at",
        "started_at",
        "finished_at",
        "failure_json",
        "model_snapshot_json",
        # D-2: deepagents TodoList snapshot captured at run termination.
        # List of {content, status} dicts (possibly empty).
        "todos",
    }
)
_SESSION_FIELDS = frozenset(
    {
        "session_id",
        "title",
        "status",
        "created_at",
        "updated_at",
        "last_seq",
        "replay_floor_seq",
    }
)
_SNAPSHOT_FIELDS = frozenset(
    {
        "session",
        "runs",
        "active_run",
        "snapshot_seq",
        "has_earlier_runs",
        "earliest_included_run_id",
        "version_report",
    }
)
_CHANGESET_FIELDS = frozenset(
    {
        "change_id",
        "run_id",
        "state",
        "changeset_digest",
        "created_at",
        "required_permission",
        "risk",
        "approval",
        "receipt",
    }
)
_RISK_FIELDS = frozenset(
    {
        "operation_count",
        "touches_external_nodes",
        "changes_wiring",
        "requires_backup",
        "effect_count",
        "effect_names",
        "affected_path_count",
        "affected_paths",
        "affected_paths_truncated",
    }
)
_APPROVAL_FIELDS = frozenset(
    {
        "approval_id",
        "decision",
        "expires_at",
        "decided_at",
        "approved_instance_id",
        "approved_scene_epoch",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "status",
        "instance_id",
        "scene_epoch",
        "before_revision",
        "after_revision",
        "applied_operation_count",
        "scene_may_have_changed",
        "completed_at",
    }
)
_MAX_OUTPUT_CHARS = 32_000
_MAX_ACTIVITY = 100
_MAX_RUNS = 100
# Step list (per-run ordered trace of assistant text segments + tool calls +
# tool results). Bounded to the same magnitude as activity so a long agent
# loop cannot grow the run dict without limit.
_MAX_STEPS = 100
_MAX_STEP_BODY_CHARS = 4000
_STEP_KINDS = frozenset({"assistant_text", "tool_call", "tool_result"})
_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens"})


def _timestamp(value: object) -> str:
    if type(value) is not str:
        raise PanelClientError("Runtime timestamp is invalid.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PanelClientError("Runtime timestamp is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PanelClientError("Runtime timestamp is invalid.")
    return value


def _nullable_timestamp(value: object) -> str | None:
    return None if value is None else _timestamp(value)


def _exact_str(value: object) -> str:
    if type(value) is not str or not value:
        raise PanelClientError("Runtime state is invalid.")
    return value


def _matching(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise PanelClientError("Runtime identity is invalid.")
    return value


def _validate_run(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    # D-2: 'todos' is a new field added in schema v6 / RunRecord change. Older
    # Runtime processes still emit snapshots without it; treat a missing
    # 'todos' as an empty list instead of rejecting the whole snapshot, which
    # would otherwise wedge the panel offline against any pre-D-2 Runtime.
    keys = set(value)
    if "todos" not in keys:
        keys = keys | {"todos"}
        value = {**value, "todos": []}
    # 'steps' and 'usage' are panel-only projections: the server wire protocol
    # never carries them, but _validate_run is invoked twice on the load path
    # (once inside parse_session_snapshot, once in load_snapshot). Strip any
    # previously-synthesized copies before the strict _RUN_FIELDS check so the
    # re-validation is idempotent, then re-synthesize fresh defaults below.
    if "steps" in keys or "usage" in keys:
        keys = keys - {"steps", "usage"}
        value = {k: v for k, v in value.items() if k not in ("steps", "usage")}
    if keys != _RUN_FIELDS:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    value = {**value, "steps": [], "usage": None}
    run = dict(value)
    _matching(run["run_id"], _RUN_ID_RE)
    _matching(run["session_id"], _SESSION_ID_RE)
    if run["status"] not in _RUN_STATES:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    if type(run["user_input"]) is not str:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    if run["final_response"] is not None and type(run["final_response"]) is not str:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    _timestamp(run["created_at"])
    _nullable_timestamp(run["started_at"])
    _nullable_timestamp(run["finished_at"])
    if run["failure_json"] is not None and type(run["failure_json"]) is not dict:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    if type(run["model_snapshot_json"]) is not dict:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    # D-2: todos must be a list (possibly empty). Items are validated lazily
    # by the consumer; here we only enforce the outer shape so a malformed
    # snapshot cannot crash the panel state machine.
    if type(run["todos"]) is not list:
        raise PanelClientError("Runtime Run snapshot is invalid.")
    return run


def parse_session_snapshot(payload: object) -> Mapping[str, object]:
    """Validate and normalize one Runtime Session snapshot payload."""
    if type(payload) is not dict or set(payload) != _SNAPSHOT_FIELDS:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    session = payload["session"]
    if type(session) is not dict or set(session) != _SESSION_FIELDS:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    _matching(session.get("session_id"), _SESSION_ID_RE)
    _exact_str(session.get("title"))
    if session.get("status") not in _SESSION_STATES:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    _timestamp(session["created_at"])
    _timestamp(session["updated_at"])
    for field in ("last_seq", "replay_floor_seq"):
        if type(session[field]) is not int or session[field] < 0:
            raise PanelClientError("Runtime Session snapshot is invalid.")
    snapshot_seq = payload["snapshot_seq"]
    if type(snapshot_seq) is not int or snapshot_seq < 0:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    runs_raw = payload["runs"]
    if type(runs_raw) is not list or len(runs_raw) > 100:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    runs = [_validate_run(item) for item in runs_raw]
    if any(run["session_id"] != session["session_id"] for run in runs):
        raise PanelClientError("Runtime Session snapshot is invalid.")
    active_raw = payload["active_run"]
    active = None if active_raw is None else _validate_run(active_raw)
    if active is not None and active["session_id"] != session["session_id"]:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    if type(payload["has_earlier_runs"]) is not bool:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    earliest = payload["earliest_included_run_id"]
    if earliest is not None:
        _matching(earliest, _RUN_ID_RE)
    if type(payload["version_report"]) is not dict:
        raise PanelClientError("Runtime Session snapshot is invalid.")
    return MappingProxyType(
        {
            "session": dict(session),
            "runs": runs,
            "active_run": active,
            "snapshot_seq": snapshot_seq,
            "has_earlier_runs": payload["has_earlier_runs"],
            "earliest_included_run_id": earliest,
            "version_report": dict(payload["version_report"]),
        }
    )


# Monotonic counter for synthetic step refs (assistant_text segments and
# unpaired tool results have no natural id). Module-level so refs stay unique
# across runs within one panel process.
_synthetic_ref_counter = itertools.count(1)


def _synthetic_ref() -> str:
    return f"step_{next(_synthetic_ref_counter)}"


def _usage_from_payload(value: object) -> dict[str, int] | None:
    """Validate a token-usage dict as emitted by the runner.

    Accepts {"input_tokens", "output_tokens", "total_tokens"} with exact int
    values. Returns None for any missing/malformed value (callers treat None
    as 'no usage'). Never raises — a malformed usage must not wedge the run.
    """
    if type(value) is not dict:
        return None
    result: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        token = value.get(field)
        # bool is a subclass of int; reject it explicitly so True/False
        # cannot masquerade as a token count.
        if type(token) is not int or type(token) is bool:
            return None
        result[field] = token
    return result


def _accumulate_usage(
    current: object, payload: Mapping[str, object]
) -> dict[str, int] | None:
    """Add an incremental model.usage_updated delta onto the running total.

    current is the run's existing usage (dict or None). The delta payload
    carries the SAME keys as the full usage (input/output/total tokens). Per
    the runner, usage_updated fires with the delta for that chunk, so we add
    it. A malformed delta yields the current value unchanged.
    """
    base = current if type(current) is dict else {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    }
    result: dict[str, int] = {
        "input_tokens": int(base.get("input_tokens", 0)),
        "output_tokens": int(base.get("output_tokens", 0)),
        "total_tokens": int(base.get("total_tokens", 0)),
    }
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        delta = payload.get(field)
        if type(delta) is int and type(delta) is not bool:
            result[field] += delta
    return result


class RuntimePanelState:
    """In-memory Run projection rebuilt from snapshot plus ordered events."""

    def __init__(self) -> None:
        self._session_id = ""
        self._last_seq = 0
        self._runs: dict[str, dict[str, object]] = {}
        self._run_order: list[str] = []
        self._active_run_id: str | None = None
        self._output: dict[str, str] = {}
        # model.reasoning_delta is OPERATIONAL: streamed live, not replayed from
        # history. load_snapshot never repopulates this — it only carries the
        # active run's thinking while the panel is connected.
        self._thinking: dict[str, str] = {}
        self._activity: list[dict[str, str]] = []
        # B-2: outcomes of changeset apply events observed on this session.
        # Keyed by change_id so the UI can show "this proposal you approved
        # failed with code X" without rereading the event log. Cleared on
        # load_snapshot (snapshots do not carry apply outcomes today).
        self._apply_outcomes: dict[str, dict[str, object]] = {}

    def load_snapshot(self, payload: object) -> None:
        snap = parse_session_snapshot(payload)
        session = snap["session"]
        runs = snap["runs"]
        snapshot_seq = snap["snapshot_seq"]
        active = snap["active_run"]
        if (
            type(session) is not dict
            or type(runs) is not list
            or type(snapshot_seq) is not int
            or (active is not None and type(active) is not dict)
        ):
            raise PanelClientError("Runtime Session snapshot is invalid.")
        self._session_id = _matching(session.get("session_id"), _SESSION_ID_RE)
        self._last_seq = snapshot_seq
        parsed_runs = [_validate_run(run) for run in runs]
        self._runs = {
            _matching(run["run_id"], _RUN_ID_RE): dict(run) for run in parsed_runs
        }
        self._run_order = [
            _matching(run["run_id"], _RUN_ID_RE) for run in parsed_runs
        ]
        if active is not None:
            active_run = _validate_run(active)
            active_run_id = _matching(active_run["run_id"], _RUN_ID_RE)
            self._runs[active_run_id] = dict(active_run)
            if active_run_id not in self._run_order:
                self._run_order.append(active_run_id)
            self._active_run_id = active_run_id
        else:
            self._active_run_id = None
        self._output = {
            run_id: str(run.get("final_response") or "")[-_MAX_OUTPUT_CHARS:]
            for run_id, run in self._runs.items()
        }
        # Thinking is OPERATIONAL-only: a fresh snapshot means we are not mid-
        # stream on any run, so there is nothing to carry over.
        self._thinking = {}
        self._activity = []
        # B-2: snapshot does not carry apply outcomes today; clear any stale
        # entries from a previous session so they cannot leak into this view.
        self._apply_outcomes = {}

    def apply_event(self, message: Mapping[str, object]) -> bool:
        if message.get("kind") != "event":
            return False
        seq = message.get("seq")
        if type(seq) is not int or seq <= self._last_seq:
            return False
        session_id = message.get("session_id")
        if session_id != self._session_id:
            return False
        event_type = message.get("type")
        payload = message.get("payload")
        run_id = message.get("run_id")
        if type(event_type) is not str or type(payload) is not dict:
            raise PanelClientError("Runtime event is invalid.")
        timestamp = _timestamp(message.get("timestamp"))
        if run_id is not None:
            _matching(run_id, _RUN_ID_RE)
        if event_type == "run.created":
            rid = _matching(payload.get("run_id"), _RUN_ID_RE)
            if run_id != rid or payload.get("session_id") != self._session_id:
                raise PanelClientError("Runtime Run event is invalid.")
            user_input = payload.get("user_input")
            if type(user_input) is not str:
                raise PanelClientError("Runtime Run event is invalid.")
            created_run: dict[str, object] = {
                "run_id": rid,
                "session_id": self._session_id,
                "status": "Created",
                "user_input": user_input,
                "final_response": None,
                "created_at": timestamp,
                "started_at": None,
                "finished_at": None,
                "failure_json": None,
                "model_snapshot_json": {},
                # D-2: empty until a todos.updated event arrives or a fresh
                # snapshot repopulates it from RunRecord.todos.
                "todos": [],
                # Panel-only ordered trace of the run's steps (assistant text
                # segments, tool calls, tool results) and the latest token
                # usage. Populated by apply_event; reset on each new run.
                "steps": [],
                "usage": None,
            }
            self._runs[rid] = created_run
            if rid not in self._run_order:
                self._run_order.append(rid)
            self._active_run_id = rid
            self._output[rid] = ""
            self._thinking[rid] = ""
            self._activity = []
            self._trim_runs()
        elif type(run_id) is str and run_id in self._runs:
            run = self._runs[run_id]
            steps: list[dict[str, object]] = run["steps"]  # type: ignore[assignment]
            if event_type == "run.state_changed":
                target = _exact_str(payload.get("to"))
                if target not in _RUN_STATES:
                    raise PanelClientError("Runtime Run event is invalid.")
                run["status"] = target
                if target in _TERMINAL_RUN_STATES:
                    if self._active_run_id == run_id:
                        self._active_run_id = None
                    run["finished_at"] = timestamp
                    # Drop the live-thinking buffer for this run; the terminal
                    # snapshot swaps the streaming card for the final reply.
                    self._thinking.pop(run_id, None)
            elif event_type == "model.text_delta":
                text = payload.get("text")
                if type(text) is not str:
                    raise PanelClientError("Runtime output event is invalid.")
                combined = self._output.get(run_id, "") + text
                self._output[run_id] = combined[-_MAX_OUTPUT_CHARS:]
                # Step list: accumulate into the trailing assistant_text step,
                # opening a fresh one if the last step was a tool result or
                # none exists yet. This preserves each assistant segment
                # between tool calls instead of fusing them into one string.
                self._touch_assistant_step(steps)
                last = steps[-1]
                last["body"] = str(last.get("body", "")) + text
                last["body"] = str(last["body"])[-_MAX_STEP_BODY_CHARS:]
                last["status"] = "streaming"
            elif event_type == "model.reasoning_delta":
                # Thinking/reasoning stream. Same shape as text_delta, same
                # bound, separate buffer. OPERATIONAL — never replayed.
                text = payload.get("text")
                if type(text) is not str:
                    raise PanelClientError("Runtime output event is invalid.")
                combined = self._thinking.get(run_id, "") + text
                self._thinking[run_id] = combined[-_MAX_OUTPUT_CHARS:]
            elif event_type == "message.assistant_final":
                text = payload.get("text")
                if type(text) is not str:
                    raise PanelClientError("Runtime output event is invalid.")
                self._output[run_id] = text[-_MAX_OUTPUT_CHARS:]
                run["final_response"] = self._output[run_id]
                # Capture the terminal usage carried alongside the final text.
                # Previously this was read-and-discarded; persist it so the
                # inspector and context bar can show token counts. Only
                # overwrite when a valid usage dict is present, so an
                # assistant_final without usage preserves a running total
                # accumulated from model.usage_updated.
                final_usage = _usage_from_payload(payload.get("usage"))
                if final_usage is not None:
                    run["usage"] = final_usage
                # Settle the trailing assistant_text step to 'done' with the
                # final text so the activity panel shows the complete reply
                # rather than the last streamed fragment.
                self._settle_assistant_step(steps, text)
            elif event_type == "model.usage_updated":
                # Incremental token counts during the run. Accumulate into the
                # run's usage so the context bar can show live totals.
                run["usage"] = _accumulate_usage(run.get("usage"), payload)
            elif event_type == "model.completed":
                # DURABLE terminal usage; authoritative final count (survives
                # reconnect replay). Overwrites any accumulated running total.
                run["usage"] = _usage_from_payload(payload.get("usage"))
            elif event_type == "run.failed":
                error = payload.get("error")
                if type(error) is not dict:
                    raise PanelClientError("Runtime failure event is invalid.")
                run["failure_json"] = dict(error)
            elif event_type == "todos.updated":
                # D-1/D-2: deepagents' write_todos tool produced a new plan.
                # Stash the latest list on the run so the UI's TodoList widget
                # and the snapshot can render it. Items are normalized on the
                # producer side (agent_runner._normalize_todos); here we only
                # enforce the outer list shape so a malformed event cannot
                # crash the panel.
                todos = payload.get("todos")
                if type(todos) is not list:
                    raise PanelClientError("Runtime todos event is invalid.")
                run["todos"] = list(todos)
            elif event_type in (
                "changeset.applied",
                "changeset.rolled_back",
                "recovery.critical",
            ):
                # B-2: capture the apply outcome so the inspector and any
                # LLM-facing context can show why an approved proposal did not
                # reach the scene. The event payload (from
                # _receipt_event_payload) carries change_id, receipt_status,
                # applied_op_ids, and optional error_code/error_message.
                self._record_apply_outcome(payload)
            elif event_type in ("tool.started", "tool.completed"):
                name = payload.get("name")
                if type(name) is not str:
                    raise PanelClientError("Runtime tool event is invalid.")
                call_id = payload.get("call_id")
                call_id_text = call_id if type(call_id) is str else ""
                detail = ""
                status = "pending"
                if event_type == "tool.completed":
                    content = payload.get("content")
                    if type(content) is str:
                        detail = content[:TOOL_RESULT_PREVIEW_CHARS]
                    status = "done"
                self._activity.append(
                    {"type": event_type, "name": name, "detail": detail}
                )
                self._activity = self._activity[-_MAX_ACTIVITY:]
                # Step list: a tool_call opens a new step (closing any open
                # assistant_text segment); a tool_result pairs onto its
                # matching tool_call by call_id when present, else appends.
                if event_type == "tool.started":
                    steps.append({
                        "kind": "tool_call",
                        "ref": call_id_text or _synthetic_ref(),
                        "title": name,
                        "body": "",
                        "status": status,
                    })
                else:
                    paired = False
                    if call_id_text:
                        for step in reversed(steps):
                            if (step.get("kind") == "tool_call"
                                    and step.get("ref") == call_id_text):
                                step["status"] = status
                                step["result"] = detail
                                paired = True
                                break
                    if not paired:
                        steps.append({
                            "kind": "tool_result",
                            "ref": call_id_text or _synthetic_ref(),
                            "title": name,
                            "body": detail,
                            "status": status,
                        })
                run["steps"] = steps[-_MAX_STEPS:]
        self._last_seq = seq
        return True

    def _touch_assistant_step(
        self, steps: list[dict[str, object]]
    ) -> None:
        """Ensure the trailing step is an open assistant_text segment.

        Called on each model.text_delta: if the last step is an active
        assistant_text step, reuse it; otherwise open a new one. This is what
        separates the assistant's text between tool calls into distinct
        segments instead of concatenating them into a single blob.
        """
        if steps and steps[-1].get("kind") == "assistant_text" \
                and steps[-1].get("status") == "streaming":
            return
        steps.append({
            "kind": "assistant_text",
            "ref": _synthetic_ref(),
            "title": "",
            "body": "",
            "status": "streaming",
        })

    def _settle_assistant_step(
        self, steps: list[dict[str, object]], final_text: str
    ) -> None:
        """Mark the trailing assistant_text step done with the final reply.

        On message.assistant_final the run's authoritative text arrives; bake
        it into the last assistant_text segment (bounded) so the activity
        panel shows the complete reply, not the last streamed fragment.
        """
        if not steps or steps[-1].get("kind") != "assistant_text":
            steps.append({
                "kind": "assistant_text",
                "ref": _synthetic_ref(),
                "title": "",
                "body": final_text[-_MAX_STEP_BODY_CHARS:],
                "status": "done",
            })
            return
        last = steps[-1]
        last["body"] = final_text[-_MAX_STEP_BODY_CHARS:]
        last["status"] = "done"

    def _record_apply_outcome(self, payload: Mapping[str, object]) -> None:
        """B-2: stash the latest receipt outcome keyed by change_id.

        Defensive: ignores malformed payloads instead of raising, because a
        bad apply-outcome event must not wedge the live event stream. The
        inspector and any LLM context layer read from this dict by change_id.
        """
        if type(payload) is not dict:
            return
        change_id = payload.get("change_id")
        if type(change_id) is not str or not change_id:
            return
        outcome: dict[str, object] = {
            "change_id": change_id,
            "receipt_status": payload.get("receipt_status"),
            "applied_op_ids": list(payload.get("applied_op_ids") or []),
            "scene_may_have_changed": payload.get("scene_may_have_changed"),
        }
        if payload.get("error_code") is not None:
            outcome["error_code"] = payload.get("error_code")
        if payload.get("error_message") is not None:
            outcome["error_message"] = payload.get("error_message")
        # Cap the in-memory outcome log so a runaway session cannot grow it
        # without bound. Most recent 32 outcomes per session is plenty for UI
        # display and any next-run LLM context injection.
        self._apply_outcomes[change_id] = outcome
        if len(self._apply_outcomes) > 32:
            # Drop the oldest insertion-order entry (Python dict preserves it).
            oldest = next(iter(self._apply_outcomes))
            self._apply_outcomes.pop(oldest, None)

    def _trim_runs(self) -> None:
        while len(self._run_order) > _MAX_RUNS:
            oldest = self._run_order[0]
            if oldest == self._active_run_id:
                break
            self._run_order.pop(0)
            self._runs.pop(oldest, None)
            self._output.pop(oldest, None)
            self._thinking.pop(oldest, None)

    def snapshot(self) -> Mapping[str, object]:
        active = (
            dict(self._runs[self._active_run_id])
            if self._active_run_id in self._runs
            else None
        )
        selected_id = self._active_run_id
        if selected_id is None and self._run_order:
            selected_id = self._run_order[-1]
        selected = dict(self._runs[selected_id]) if selected_id else None
        output = self._output.get(selected_id or "", "")
        thinking = self._thinking.get(selected_id or "", "")
        return MappingProxyType(
            {
                "session_id": self._session_id,
                "last_seq": self._last_seq,
                "runs": tuple(dict(self._runs[rid]) for rid in self._run_order),
                "active_run": active,
                "selected_run": selected,
                "output": output,
                "thinking": thinking,
                "activity": tuple(dict(item) for item in self._activity),
                # B-2: apply outcomes keyed by change_id. Inspector and any
                # next-run LLM context layer can read this to see why an
                # approved proposal did not reach the scene.
                "apply_outcomes": tuple(dict(v) for v in self._apply_outcomes.values()),
            }
        )

    def streaming_delta(self) -> tuple[str | None, str, str]:
        """Return the selected run's live text + thinking without a full snapshot.

        Used by the panel's lightweight streaming path so token deltas update
        the trailing conversation card WITHOUT triggering the full-snapshot
        deep-copy + inspector rebuild. Mirrors the selection logic in
        ``snapshot()``: the active run if set, else the most recent run.
        """
        selected_id = self._active_run_id
        if selected_id is None and self._run_order:
            selected_id = self._run_order[-1]
        if not selected_id:
            return None, "", ""
        output = self._output.get(selected_id, "")
        thinking = self._thinking.get(selected_id, "")
        return selected_id, output, thinking


def parse_changeset_list(result: object) -> tuple[Mapping[str, object], ...]:
    """Validate the exact bounded `changeset.list` result."""
    if type(result) is not dict or set(result) != {"changesets"}:
        raise PanelClientError("Runtime ChangeSet list is invalid.")
    items = result["changesets"]
    if type(items) is not list or len(items) > 50:
        raise PanelClientError("Runtime ChangeSet list is invalid.")
    parsed: list[Mapping[str, object]] = []
    for item in items:
        if type(item) is not dict or set(item) != _CHANGESET_FIELDS:
            raise PanelClientError("Runtime ChangeSet list is invalid.")
        _matching(item["change_id"], _CHANGE_ID_RE)
        _matching(item["run_id"], _RUN_ID_RE)
        _matching(item["changeset_digest"], _DIGEST_RE)
        if item["state"] not in _CHANGESET_STATES:
            raise PanelClientError("Runtime ChangeSet list is invalid.")
        if item["required_permission"] not in _PERMISSION_MODES:
            raise PanelClientError("Runtime ChangeSet list is invalid.")
        _timestamp(item["created_at"])
        risk = item["risk"]
        if type(risk) is not dict or set(risk) != _RISK_FIELDS:
            raise PanelClientError("Runtime ChangeSet risk is invalid.")
        for field in ("operation_count", "effect_count", "affected_path_count"):
            if type(risk[field]) is not int or risk[field] < 0:
                raise PanelClientError("Runtime ChangeSet risk is invalid.")
        for field in (
            "touches_external_nodes",
            "changes_wiring",
            "requires_backup",
            "affected_paths_truncated",
        ):
            if type(risk[field]) is not bool:
                raise PanelClientError("Runtime ChangeSet risk is invalid.")
        for field in ("effect_names", "affected_paths"):
            values = risk[field]
            if (
                type(values) is not list
                or len(values) > 12
                or any(type(value) is not str for value in values)
            ):
                raise PanelClientError("Runtime ChangeSet risk is invalid.")
        if (
            risk["effect_count"] < len(risk["effect_names"])
            or risk["affected_path_count"] < len(risk["affected_paths"])
            or risk["affected_paths_truncated"]
            is not (risk["affected_path_count"] > len(risk["affected_paths"]))
        ):
            raise PanelClientError("Runtime ChangeSet risk is invalid.")
        approval = item["approval"]
        if approval is not None:
            if type(approval) is not dict or set(approval) != _APPROVAL_FIELDS:
                raise PanelClientError("Runtime approval summary is invalid.")
            _matching(approval["approval_id"], _APPROVAL_ID_RE)
            if approval["decision"] not in _APPROVAL_DECISIONS:
                raise PanelClientError("Runtime approval summary is invalid.")
            _timestamp(approval["expires_at"])
            _nullable_timestamp(approval["decided_at"])
            if approval["approved_instance_id"] is not None:
                _exact_str(approval["approved_instance_id"])
            epoch = approval["approved_scene_epoch"]
            if epoch is not None and (type(epoch) is not int or epoch < 1):
                raise PanelClientError("Runtime approval summary is invalid.")
        receipt = item["receipt"]
        if receipt is not None:
            if type(receipt) is not dict or set(receipt) != _RECEIPT_FIELDS:
                raise PanelClientError("Runtime receipt summary is invalid.")
            if receipt["status"] not in _RECEIPT_STATUSES:
                raise PanelClientError("Runtime receipt summary is invalid.")
            _exact_str(receipt["instance_id"])
            _matching(receipt["before_revision"], _DIGEST_RE)
            _matching(receipt["after_revision"], _DIGEST_RE)
            if (
                type(receipt["scene_epoch"]) is not int
                or receipt["scene_epoch"] < 1
            ):
                raise PanelClientError("Runtime receipt summary is invalid.")
            if (
                type(receipt["applied_operation_count"]) is not int
                or receipt["applied_operation_count"] < 0
                or type(receipt["scene_may_have_changed"]) is not bool
            ):
                raise PanelClientError("Runtime receipt summary is invalid.")
            _timestamp(receipt["completed_at"])
        parsed.append(MappingProxyType(dict(item)))
    return tuple(parsed)


_TASK_STEP_FIELDS = frozenset({"seq", "tool", "purpose", "status", "node_count"})
_TASK_STEP_STATUSES = frozenset({"open", "committed", "deleted"})


def parse_task_graph_list(result: object) -> tuple[Mapping[str, object], ...]:
    if type(result) is not dict or set(result) != {"steps"}:
        raise PanelClientError("Runtime task graph list is invalid.")
    items = result["steps"]
    if type(items) is not list or len(items) > 50:
        raise PanelClientError("Runtime task graph list is invalid.")
    parsed: list[Mapping[str, object]] = []
    for item in items:
        if type(item) is not dict or set(item) != _TASK_STEP_FIELDS:
            raise PanelClientError("Runtime task graph list is invalid.")
        if type(item["seq"]) is not int or item["seq"] < 1:
            raise PanelClientError("Runtime task graph list is invalid.")
        if type(item["tool"]) is not str or type(item["purpose"]) is not str:
            raise PanelClientError("Runtime task graph list is invalid.")
        if item["status"] not in _TASK_STEP_STATUSES:
            raise PanelClientError("Runtime task graph list is invalid.")
        if type(item["node_count"]) is not int or item["node_count"] < 0:
            raise PanelClientError("Runtime task graph list is invalid.")
        parsed.append(item)
    return tuple(parsed)


def format_task_graph_steps(
    steps: tuple[Mapping[str, object], ...],
) -> str:
    if not steps:
        return "No build steps recorded for this run."
    return "\n".join(
        f"{step['seq']}. [{step['status']}] {step['purpose']} "
        f"({step['node_count']} nodes)"
        for step in steps
    )


def changeset_refresh_required(message: Mapping[str, object]) -> bool:
    return (
        message.get("kind") == "event"
        and message.get("type") in _CHANGESET_REFRESH_EVENTS
    )


def artifact_refresh_required(message: Mapping[str, object]) -> bool:
    return (
        message.get("kind") == "event"
        and message.get("type") in _ARTIFACT_REFRESH_EVENTS
    )


def _finite_number(value: object) -> float:
    if type(value) is int:
        return float(value)
    if type(value) is float:
        if math.isfinite(value):
            return value
    raise PanelClientError("Runtime artifact event is invalid.")


def parse_artifact_event(message: Mapping[str, object]) -> Mapping[str, object]:
    """Validate one durable artifact event into a bounded panel summary.

    The summary carries metadata only (never image bytes): the artifact id,
    media type, size, sha256, relative path, and framing report of a capture,
    or the structured code of a capture failure. Byte identity stays
    inspectable because the listed sha256 is the registered content digest.
    """
    if not artifact_refresh_required(message):
        raise PanelClientError("Runtime artifact event is invalid.")
    seq = message.get("seq")
    if type(seq) is not int or seq <= 0:
        raise PanelClientError("Runtime artifact event is invalid.")
    payload = message.get("payload")
    if type(payload) is not dict:
        raise PanelClientError("Runtime artifact event is invalid.")
    if message["type"] == "modeling.capture_failed":
        if set(payload) != _CAPTURE_FAILED_FIELDS:
            raise PanelClientError("Runtime capture failure event is invalid.")
        _matching(payload["change_id"], _CHANGE_ID_RE)
        _matching(payload["changeset_digest"], _DIGEST_RE)
        code = _exact_str(payload["code"])
        return MappingProxyType(
            {
                "kind": "failed",
                "state": "failed",
                "viewable": False,
                "code": code,
                "change_id": payload["change_id"],
                "seq": seq,
            }
        )
    if message["type"] in {"modeling.artifact_state_changed", "artifact.reconciled"}:
        expected = frozenset({"artifact_id", "state"})
        recovery_fields = frozenset({"artifact_id", "from_state", "state"})
        if set(payload) not in (expected, recovery_fields):
            raise PanelClientError("Runtime artifact lifecycle event is invalid.")
        _matching(payload["artifact_id"], _ARTIFACT_ID_RE)
        state = payload["state"]
        if type(state) is not str or state not in _ARTIFACT_LIFECYCLE_STATES:
            raise PanelClientError("Runtime artifact lifecycle event is invalid.")
        return MappingProxyType(
            {
                "kind": "lifecycle",
                "artifact_id": payload["artifact_id"],
                "state": state,
                "viewable": state == "available",
                "recovery": message["type"] == "artifact.reconciled",
                "seq": seq,
            }
        )
    if set(payload) != _ARTIFACT_CAPTURED_FIELDS:
        raise PanelClientError("Runtime artifact event is invalid.")
    _matching(payload["change_id"], _CHANGE_ID_RE)
    _matching(payload["changeset_digest"], _DIGEST_RE)
    artifact = payload["artifact"]
    if type(artifact) is not dict or set(artifact) not in (_ARTIFACT_FIELDS, _ARTIFACT_FIELDS | {"artifact_state"}):
        raise PanelClientError("Runtime artifact event is invalid.")
    _matching(artifact["artifact_id"], _ARTIFACT_ID_RE)
    relative_path = artifact["relative_path"]
    if (
        type(relative_path) is not str
        or not relative_path
        or len(relative_path) > 1024
        or relative_path.startswith("/")
        or "\\" in relative_path
        or ".." in relative_path.split("/")
    ):
        raise PanelClientError("Runtime artifact event is invalid.")
    _matching(artifact["sha256"], _DIGEST_RE)
    media_type = artifact["media_type"]
    if type(media_type) is not str or not media_type or len(media_type) > 255:
        raise PanelClientError("Runtime artifact event is invalid.")
    size_bytes = artifact["size_bytes"]
    if type(size_bytes) is not int or size_bytes < 0:
        raise PanelClientError("Runtime artifact event is invalid.")
    if artifact["schema_version"] != 1:
        raise PanelClientError("Runtime artifact event is invalid.")
    state = artifact.get("artifact_state", "available")
    if type(state) is not str or state not in _ARTIFACT_LIFECYCLE_STATES:
        raise PanelClientError("Runtime artifact event is invalid.")
    framing = payload["framing"]
    if type(framing) is not dict or set(framing) != _FRAMING_FIELDS:
        raise PanelClientError("Runtime artifact event is invalid.")
    adjustments = framing["adjustments_used"]
    if type(adjustments) is not int or not 0 <= adjustments <= 2:
        raise PanelClientError("Runtime artifact event is invalid.")
    for field in (
        "margin_left",
        "margin_right",
        "margin_bottom",
        "margin_top",
        "longest_axis_ratio",
        "center_offset",
    ):
        _finite_number(framing[field])
    return MappingProxyType(
        {
            "kind": "captured",
            "state": state,
            "viewable": state == "available",
            "artifact_id": artifact["artifact_id"],
            "relative_path": relative_path,
            "sha256": artifact["sha256"],
            "media_type": media_type,
            "size_bytes": size_bytes,
            "seq": seq,
        }
    )


def append_artifact_summary(
    items: tuple[Mapping[str, object], ...],
    summary: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Prepend one parsed artifact summary, bounded to the newest 50.

    A lifecycle event for an already-listed captured artifact updates
    that row's state in place instead of adding a duplicate row; every
    other summary prepends a new row.
    """
    if summary["kind"] == "lifecycle":
        artifact_id = summary["artifact_id"]
        for index, item in enumerate(items):
            if item["kind"] == "captured" and item.get("artifact_id") == artifact_id:
                merged = dict(item)
                merged["state"] = summary["state"]
                merged["viewable"] = summary["viewable"]
                merged["seq"] = summary["seq"]
                updated = list(items)
                updated[index] = MappingProxyType(merged)
                return tuple(updated)[:_MAX_ARTIFACTS]
    return (summary, *items)[:_MAX_ARTIFACTS]


def vision_refresh_required(message: Mapping[str, object]) -> bool:
    return (
        message.get("kind") == "event"
        and message.get("type") in _VISION_REFRESH_EVENTS
    )


def _bounded_vision_text(value: object, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise PanelClientError("Runtime vision event is invalid.")
    return value


def _bounded_vision_text_list(
    value: object, *, maximum_items: int, maximum_chars: int
) -> None:
    if type(value) is not list or len(value) > maximum_items:
        raise PanelClientError("Runtime vision event is invalid.")
    for item in value:
        _bounded_vision_text(item, maximum_chars)


def _vision_artifact_ref(value: object) -> None:
    if type(value) is not dict or set(value) != _ARTIFACT_FIELDS:
        raise PanelClientError("Runtime vision event is invalid.")
    _matching(value["artifact_id"], _ARTIFACT_ID_RE)
    relative_path = value["relative_path"]
    if (
        type(relative_path) is not str
        or not relative_path
        or len(relative_path) > 512
        or relative_path.startswith("/")
        or "\\" in relative_path
        or ".." in relative_path.split("/")
    ):
        raise PanelClientError("Runtime vision event is invalid.")
    _matching(value["sha256"], _DIGEST_RE)
    media_type = value["media_type"]
    if type(media_type) is not str or not media_type or len(media_type) > 255:
        raise PanelClientError("Runtime vision event is invalid.")
    size_bytes = value["size_bytes"]
    if type(size_bytes) is not int or not 1 <= size_bytes <= 16_777_216:
        raise PanelClientError("Runtime vision event is invalid.")
    if value["schema_version"] != 1:
        raise PanelClientError("Runtime vision event is invalid.")


def parse_vision_event(message: Mapping[str, object]) -> Mapping[str, object]:
    """Validate one durable vision evaluation into a bounded panel summary.

    The summary carries normalized status/decision metadata only; report
    observations stay as counts so unbounded provider text is never
    rendered without an explicit bounded selection.
    """
    if not vision_refresh_required(message):
        raise PanelClientError("Runtime vision event is invalid.")
    seq = message.get("seq")
    if type(seq) is not int or seq <= 0:
        raise PanelClientError("Runtime vision event is invalid.")
    payload = message.get("payload")
    if type(payload) is not dict or set(payload) != _VISION_EVALUATION_FIELDS:
        raise PanelClientError("Runtime vision event is invalid.")
    _bounded_vision_text(payload["brief"], 4096)
    _bounded_vision_text(payload["spec"], 4096)
    _matching(payload["changeset_digest"], _DIGEST_RE)
    _bounded_vision_text(payload["approval"], 512)
    _bounded_vision_text(payload["receipt"], 512)
    _bounded_vision_text_list(
        payload["validation_report"], maximum_items=64, maximum_chars=512
    )
    artifact_refs = payload["artifact_refs"]
    if type(artifact_refs) is not list or len(artifact_refs) > 16:
        raise PanelClientError("Runtime vision event is invalid.")
    for ref in artifact_refs:
        _vision_artifact_ref(ref)
    artifact_status = payload["artifact_status"]
    if (
        type(artifact_status) is not list
        or len(artifact_status) != len(artifact_refs)
    ):
        raise PanelClientError("Runtime vision event is invalid.")
    for state in artifact_status:
        if type(state) is not str or state not in _ARTIFACT_LIFECYCLE_STATES:
            raise PanelClientError("Runtime vision event is invalid.")
    manifest = payload["knowledge_manifest_sha256"]
    if manifest is not None:
        _matching(manifest, _DIGEST_RE)
    status = payload["vision_status"]
    if type(status) is not str or status not in _VISION_STATUSES:
        raise PanelClientError("Runtime vision event is invalid.")
    reason_code = payload["vision_reason_code"]
    if reason_code is not None:
        _bounded_vision_text(reason_code, 64)
    report = payload["vision_report"]
    if report is not None:
        if type(report) is not dict or set(report) != _VISION_REPORT_FIELDS:
            raise PanelClientError("Runtime vision event is invalid.")
        _bounded_vision_text(report["summary"], 2048)
        _bounded_vision_text_list(
            report["observations"], maximum_items=32, maximum_chars=512
        )
        confidence = report["confidence"]
        if (
            type(confidence) is bool
            or type(confidence) not in (int, float)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise PanelClientError("Runtime vision event is invalid.")
        if type(report["advisory_passed"]) is not bool:
            raise PanelClientError("Runtime vision event is invalid.")
    decision = payload["final_decision"]
    if type(decision) is not dict or set(decision) != _VISION_DECISION_FIELDS:
        raise PanelClientError("Runtime vision event is invalid.")
    decision_status = decision["status"]
    if (
        type(decision_status) is not str
        or decision_status not in _VISION_STATUSES
        or decision_status != status
    ):
        raise PanelClientError("Runtime vision event is invalid.")
    if (
        type(decision["accepted"]) is not bool
        or type(decision["deterministic_valid"]) is not bool
    ):
        raise PanelClientError("Runtime vision event is invalid.")
    _bounded_vision_text(decision["summary"], 1024)
    _bounded_vision_text_list(
        payload["recovery_evidence"], maximum_items=32, maximum_chars=512
    )
    if status == "completed":
        if report is None:
            raise PanelClientError("Runtime vision event is invalid.")
        expected = decision["deterministic_valid"] and report["advisory_passed"]
        if decision["accepted"] is not expected:
            raise PanelClientError("Runtime vision event is invalid.")
    elif report is not None:
        raise PanelClientError("Runtime vision event is invalid.")
    if not decision["deterministic_valid"] and decision["accepted"]:
        raise PanelClientError("Runtime vision event is invalid.")
    if status == "failed" and decision["accepted"]:
        raise PanelClientError("Runtime vision event is invalid.")
    return MappingProxyType(
        {
            "kind": "vision",
            "status": status,
            "accepted": decision["accepted"],
            "deterministic_valid": decision["deterministic_valid"],
            "advisory_passed": None if report is None else report["advisory_passed"],
            "summary": decision["summary"],
            "reason_code": reason_code,
            "report_summary": None if report is None else report["summary"],
            "observation_count": 0 if report is None else len(report["observations"]),
            "artifact_count": len(artifact_refs),
            "changeset_digest": payload["changeset_digest"],
            "seq": seq,
        }
    )


def append_vision_summary(
    items: tuple[Mapping[str, object], ...],
    summary: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    """Prepend one parsed vision summary, bounded to the newest 50."""
    return (summary, *items)[:_MAX_VISION]


def approval_is_actionable(
    summary: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> bool:
    approval = summary.get("approval")
    if summary.get("state") != "AwaitingApproval" or type(approval) is not dict:
        return False
    if approval.get("decision") != "Pending":
        return False
    try:
        expires = datetime.fromisoformat(approval["expires_at"])
    except (TypeError, ValueError):
        return False
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return moment.astimezone(timezone.utc) <= expires.astimezone(timezone.utc)


__all__ = [
    "RuntimePanelState",
    "append_artifact_summary",
    "append_vision_summary",
    "approval_is_actionable",
    "artifact_refresh_required",
    "changeset_refresh_required",
    "parse_artifact_event",
    "parse_changeset_list",
    "parse_session_snapshot",
    "parse_vision_event",
    "vision_refresh_required",
]
