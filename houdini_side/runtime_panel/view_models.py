"""Qt-free view models for the three-pane Runtime panel.

Every rendering decision (bounding, tone mapping, digest shortening) lives
here so it is unit-testable without PySide6. Inputs are plain mappings as
emitted by eee_agent.panel.runtime_state parsers; this module is defensive
(.get with defaults) and never raises on partial data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

MAX_MESSAGES = 200
MAX_BODY_CHARS = 2000
MAX_TITLE_CHARS = 120

TONES = frozenset({"normal", "ok", "warn", "error", "gate"})


@dataclass(frozen=True, slots=True)
class MessageItem:
    kind: str        # user|assistant|assistant_streaming|proposal|approval|vision|artifact|notice|error
    title: str
    body: str
    tone: str        # one of TONES
    mono: bool = False
    # Live reasoning text shown in a collapsible block under assistant cards.
    # Empty for non-streaming / non-thinking messages.
    thinking: str = ""


@dataclass(frozen=True, slots=True)
class ContextStatus:
    hip: str
    session: str
    workspace: str
    runtime: str
    bridge: str
    run_state: str


def _bounded(value: object, limit: int) -> str:
    text = value if type(value) is str else ""
    return text[:limit]


def _digest_prefix(value: object) -> str:
    text = value if type(value) is str else ""
    return text[:16]


def user_message(text: str) -> MessageItem:
    return MessageItem(
        kind="user", title="You",
        body=_bounded(text, MAX_BODY_CHARS), tone="normal",
    )


def assistant_message(text: str) -> MessageItem:
    # Renders the run's final output (RuntimePanelState.snapshot()["output"]).
    # mono=False so long prose wraps like the user's message instead of
    # scrolling sideways.
    return MessageItem(
        kind="assistant", title="Assistant",
        body=_bounded(text, MAX_BODY_CHARS), tone="normal",
    )


def streaming_assistant(text: str, *, thinking: str = "") -> MessageItem:
    # A live in-flight assistant reply: the body grows token-by-token from
    # model.text_delta and thinking grows from model.reasoning_delta. The
    # conversation view updates this card in place rather than appending, and
    # swaps it for a final assistant_message once the run terminates. Empty
    # body is allowed (the run just started; only a cursor shows).
    return MessageItem(
        kind="assistant_streaming", title="Assistant",
        body=_bounded(text, MAX_BODY_CHARS), tone="normal",
        thinking=_bounded(thinking, MAX_BODY_CHARS),
    )


def proposal_card(payload: Mapping[str, object]) -> MessageItem:
    count = payload.get("operation_count")
    count_text = str(count) if type(count) is int else "?"
    mode = _bounded(payload.get("permission_mode"), 40) or "unknown mode"
    digest = _digest_prefix(payload.get("changeset_digest"))
    body = f"Permission: {mode}"
    if digest:
        body += f"\nDigest: {digest}"
    return MessageItem(
        kind="proposal",
        title=_bounded(f"Proposal · {count_text} operations", MAX_TITLE_CHARS),
        body=body, tone="gate", mono=True,
    )


def approval_result_card(
    approved: bool,
    *,
    expired: bool = False,
    blocked_recovery: bool = False,
    blocker_ids: tuple[str, ...] = (),
) -> MessageItem:
    if expired:
        return MessageItem("approval", "Approval expired",
                           "The gate timed out without a decision.", "warn")
    if approved and blocked_recovery:
        blockers = ", ".join(blocker_ids[:4])
        detail = (
            "ChangeSet approved, but Apply is blocked until recovery is "
            "completed."
        )
        if blockers:
            detail += f"\nBlocking ChangeSet(s): {blockers}"
        return MessageItem(
            "approval",
            "Approved — recovery required",
            detail,
            "warn",
        )
    if approved:
        return MessageItem("approval", "Approved",
                           "ChangeSet approved and queued for apply.", "ok")
    return MessageItem("approval", "Rejected",
                       "ChangeSet rejected by the user.", "error")


def recover_result_card(
    change_id: str, recovered: bool, *, pending: bool = False
) -> MessageItem:
    """Card summarizing the outcome of a manual changeset.recover."""
    short = change_id[:16]
    if pending:
        return MessageItem(
            "approval",
            "Recovery pending",
            f"The Bridge was unavailable; {short}… remains blocked. Retry once "
            "the Houdini Bridge is reachable.",
            "warn",
        )
    if recovered:
        return MessageItem(
            "approval",
            "Recovered",
            f"CriticalRecovery {short}… resolved. The write barrier is lifted "
            "and further Applies are allowed.",
            "ok",
        )
    return MessageItem(
        "approval",
        "Recovery refused",
        f"The current scene still matches {short}… but its postconditions do "
        "not all hold. Verify the scene in Houdini before retrying.",
        "warn",
    )


def vision_card(summary: Mapping[str, object]) -> MessageItem:
    # Keys follow parse_vision_event: status / accepted / report_summary.
    status = _bounded(summary.get("status"), 40) or "unknown"
    accepted = summary.get("accepted") is True
    decision = "accepted" if accepted else "rejected"
    if status == "completed" and accepted:
        tone = "ok"
    elif status == "failed":
        tone = "error"
    else:
        tone = "warn"
    # Hard cap: the status/decision prefix is structural and must stay whole,
    # so report_summary takes whatever of MAX_BODY_CHARS remains after it.
    prefix = f"Status: {status}\nDecision: {decision}"
    remaining = max(0, MAX_BODY_CHARS - len(prefix) - 1)
    text = _bounded(summary.get("report_summary"), remaining)
    body = prefix + f"\n{text}" if text else prefix
    return MessageItem(kind="vision", title="Vision evaluation",
                       body=body, tone=tone)


def artifact_card(summary: Mapping[str, object]) -> MessageItem:
    path = _bounded(summary.get("relative_path"), MAX_TITLE_CHARS) or "artifact"
    state = _bounded(summary.get("state"), 40) or "unknown"
    size = summary.get("size_bytes")
    size_text = f"{size} bytes" if type(size) is int else "size unknown"
    tone = "ok" if state == "available" else (
        "error" if state in {"failed", "missing"} else "normal")
    return MessageItem(
        kind="artifact", title=_bounded(path, MAX_TITLE_CHARS),
        body=f"State: {state}\n{size_text}", tone=tone, mono=True,
    )


def notice_card(text: str, *, tone: str = "warn") -> MessageItem:
    assert tone in TONES
    return MessageItem(kind="notice", title="Notice",
                       body=_bounded(text, MAX_BODY_CHARS), tone=tone)


def append_bounded(items: list[MessageItem], item: MessageItem) -> None:
    items.append(item)
    del items[: max(0, len(items) - MAX_MESSAGES)]


def context_status(
    *,
    hip: str | None,
    session_title: str | None,
    workspace_id: str | None,
    connection: str,
    bridge: str,
    run_state: str,
) -> ContextStatus:
    return ContextStatus(
        hip=hip or "no hip",
        session=session_title or "no session",
        workspace=(
            "ws " + workspace_id[3:11] if workspace_id else "no workspace"
        ),
        runtime=connection,
        bridge=bridge,
        run_state=run_state,
    )


# --------------------------------------------------------------------------
# Stage A: structured Run/Workspace/Artifacts view models (Qt-free)
# --------------------------------------------------------------------------
# These dataclasses carry everything the new inspector widgets need to render
# without re-parsing the raw snapshot. The inspector.py Qt layer just reads
# these fields; all bounding/normalization lives here so it is unit-testable.

MAX_ACTIVITY_STEPS = 50
MAX_DEPENDENCIES = 128
MAX_PARM_VALUE_LEN = 64

_RUN_STATUS_TONES = {
    "Completed": "ok",
    "Cancelled": "warn",
    "Failed": "error",
    "StopRequested": "warn",
    "Stopping": "warn",
    "Retrying": "warn",
    "Created": "normal",
    "PreparingContext": "normal",
    "Planning": "normal",
    "Finalizing": "normal",
}

_RECEIPT_TONES = {
    "Applied": "ok",
    "AlreadyApplied": "ok",
    "RolledBack": "error",
    "Partial": "warn",
    "CriticalRecovery": "warn",
}


@dataclass(frozen=True, slots=True)
class ActivityStep:
    """One tool step in the activity list."""
    kind: str   # "tool.started" | "tool.completed"
    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class ApplyOutcomeView:
    """A single receipt outcome rendered in the Run tab."""
    change_id: str
    receipt_status: str
    tone: str
    error_code: str
    error_message: str
    applied_op_count: int


# --------------------------------------------------------------------------
# Stage A / Task 2: bounded Run failure projection (Qt-free)
# --------------------------------------------------------------------------
# failure_json is already durable and validated by the panel state. Only the
# safe fields (code, message_for_user, retryable) may enter the view model;
# technical_detail_ref, category internals, suggested_actions, tracebacks,
# provider responses, and unknown nested fields are deliberately dropped so
# they can never reach a conversation card, the Run inspector, or a repr.

_FAILURE_FALLBACK_CODE = "runtime.failed"
_FAILURE_FALLBACK_MESSAGE = "The run failed before producing a response."


@dataclass(frozen=True, slots=True)
class FailureView:
    """Bounded, user-safe projection of a Run failure.

    Only code / message / retryable ever live here. The tone is fixed to
    "error" for a failed Run.
    """
    code: str
    message: str
    retryable: bool
    tone: str = "error"


def failure_view(payload: object) -> FailureView:
    """Normalize a run.failed payload into a bounded FailureView.

    Defensive: a non-dict payload, or any field that is not an exact
    non-empty str, falls back to a safe generic message. The original
    mapping is never retained; no category / technical_detail_ref /
    unknown field is read or stored.
    """
    if type(payload) is not dict:
        return FailureView(_FAILURE_FALLBACK_CODE, _FAILURE_FALLBACK_MESSAGE, False)
    code = payload.get("code")
    message = payload.get("message_for_user")
    retryable = payload.get("retryable")
    code_text = code if type(code) is str and code else _FAILURE_FALLBACK_CODE
    message_text = (
        message if type(message) is str and message else _FAILURE_FALLBACK_MESSAGE
    )
    return FailureView(
        code=_bounded(code_text, MAX_TITLE_CHARS),
        message=_bounded(message_text, MAX_BODY_CHARS),
        retryable=retryable if type(retryable) is bool else False,
    )


@dataclass(frozen=True, slots=True)
class EnvironmentView:
    """The runtime environment block (extracted from model_snapshot_json)."""
    eee_agent: str
    python: str
    platform: str
    houdini_build: str
    kb_schema_version: str
    knowledge_status: str


@dataclass(frozen=True, slots=True)
class RunView:
    """Structured Run-tab data; replaces the old key:value text dump."""
    run_id: str
    run_id_short: str
    status: str
    status_tone: str
    started_at: str
    finished_at: str
    duration_seconds: float | None
    environment: EnvironmentView | None
    dependencies: tuple[tuple[str, str], ...]   # (package, version) pairs
    activity: tuple[ActivityStep, ...]
    apply_outcome: ApplyOutcomeView | None
    # Stage A: bounded failure evidence. Only present for status == "Failed".
    failure: FailureView | None
    todos: tuple["TodoItemView", ...]


@dataclass(frozen=True, slots=True)
class TodoItemView:
    """One item in the deepagents TodoList plan."""
    content: str
    status: str  # "pending" | "in_progress" | "completed"
    tone: str    # "normal" | "warn" | "ok"


_TODO_STATUS_TONES = {
    "pending": "normal",
    "in_progress": "warn",
    "completed": "ok",
}


def todo_items(todos: object) -> tuple[TodoItemView, ...]:
    """Normalize a deepagents todos list (list of {content, status}) into
    bounded TodoItemView records. Returns () for any non-list input."""
    if not isinstance(todos, (list, tuple)):
        return ()
    items: list[TodoItemView] = []
    for raw in todos:
        if type(raw) is not dict:
            continue
        content = raw.get("content")
        status = raw.get("status")
        if type(content) is not str or not content:
            continue
        if type(status) is not str or status not in _TODO_STATUS_TONES:
            continue
        items.append(TodoItemView(
            content=_bounded(content, MAX_BODY_CHARS),
            status=status,
            tone=_TODO_STATUS_TONES[status],
        ))
        if len(items) >= MAX_ACTIVITY_STEPS:
            break
    return tuple(items)


@dataclass(frozen=True, slots=True)
class WorkspaceRow:
    key: str
    value: str
    tone: str


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    state: str
    tone: str
    relative_path: str
    size_text: str


@dataclass(frozen=True, slots=True)
class VisionRow:
    status: str
    accepted: bool
    tone: str
    report_summary: str


def _short_run_id(run_id: str) -> str:
    # run_0123456789abcdef... -> run_01234567
    if len(run_id) >= 12:
        return run_id[:12]
    return run_id


def _parse_iso_duration(started: str, finished: str) -> float | None:
    """Return the run duration in seconds, or None if not computable."""
    from datetime import datetime
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished)
    except (ValueError, TypeError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    delta = (end - start).total_seconds()
    return delta if delta >= 0 else None


def _environment_from_snapshot(model_snapshot: object) -> EnvironmentView | None:
    """Extract a bounded environment view from the run's model_snapshot_json."""
    if type(model_snapshot) is not dict:
        return None

    def _str_field(key: str) -> str:
        value = model_snapshot.get(key)
        return str(value) if value is not None else "-"

    return EnvironmentView(
        eee_agent=_str_field("eee_agent"),
        python=_str_field("python"),
        platform=_str_field("platform"),
        houdini_build=_str_field("houdini_build"),
        kb_schema_version=_str_field("kb_schema_version"),
        knowledge_status=_str_field("knowledge_status"),
    )


def _dependencies_from_snapshot(model_snapshot: object) -> tuple[tuple[str, str], ...]:
    """Extract a bounded (package, version) list from model_snapshot_json."""
    if type(model_snapshot) is not dict:
        return ()
    deps = model_snapshot.get("dependencies")
    if type(deps) is not dict:
        return ()
    pairs: list[tuple[str, str]] = []
    for name in sorted(deps):
        version = deps[name]
        if type(version) is not str:
            continue
        pairs.append((str(name), version))
        if len(pairs) >= MAX_DEPENDENCIES:
            break
    return tuple(pairs)


def _activity_steps(activity: object) -> tuple[ActivityStep, ...]:
    """Normalize a panel-state activity list into ActivityStep records."""
    if not isinstance(activity, (list, tuple)):
        return ()
    steps: list[ActivityStep] = []
    for item in activity:
        if type(item) is not dict:
            continue
        kind = item.get("type")
        name = item.get("name")
        detail = item.get("detail") or ""
        if type(kind) is not str or type(name) is not str:
            continue
        steps.append(ActivityStep(
            kind=kind,
            name=_bounded(name, MAX_TITLE_CHARS),
            detail=_bounded(detail, MAX_BODY_CHARS),
        ))
        if len(steps) >= MAX_ACTIVITY_STEPS:
            break
    return tuple(steps)


def _apply_outcome_view(outcomes: object) -> ApplyOutcomeView | None:
    """Pick the latest apply outcome (if any) and render it as a view."""
    if not isinstance(outcomes, (list, tuple)):
        return None
    if not outcomes:
        return None
    latest = outcomes[-1]
    if type(latest) is not dict:
        return None
    change_id = latest.get("change_id")
    if type(change_id) is not str or not change_id:
        return None
    receipt_status = latest.get("receipt_status")
    receipt_text = receipt_status if type(receipt_status) is str else "?"
    error_code = latest.get("error_code")
    error_message = latest.get("error_message")
    applied = latest.get("applied_op_ids")
    applied_count = len(applied) if isinstance(applied, (list, tuple)) else 0
    return ApplyOutcomeView(
        change_id=change_id,
        receipt_status=receipt_text,
        tone=_RECEIPT_TONES.get(receipt_text, "warn"),
        error_code=_bounded(error_code, MAX_TITLE_CHARS) if type(error_code) is str else "",
        error_message=_bounded(error_message, MAX_BODY_CHARS) if type(error_message) is str else "",
        applied_op_count=applied_count,
    )


def run_view(
    snapshot: object,
    activity: object = (),
    apply_outcomes: object = (),
) -> RunView | None:
    """Build a structured RunView from a run snapshot dict.

    Returns None when the snapshot is missing/invalid so the inspector can
    show its empty state. Never raises: defensive .get with type checks so a
    malformed snapshot cannot crash the right pane.
    """
    if type(snapshot) is not dict:
        return None
    run_id = snapshot.get("run_id")
    if type(run_id) is not str or not run_id:
        return None
    status = snapshot.get("status")
    status_text = status if type(status) is str else "-"
    model_snapshot = snapshot.get("model_snapshot_json")
    started = snapshot.get("started_at")
    finished = snapshot.get("finished_at")
    started_text = started if type(started) is str else "-"
    finished_text = finished if type(finished) is str else "-"
    duration = None
    if type(started) is str and type(finished) is str:
        duration = _parse_iso_duration(started, finished)
    return RunView(
        run_id=run_id,
        run_id_short=_short_run_id(run_id),
        status=status_text,
        status_tone=_RUN_STATUS_TONES.get(status_text, "normal"),
        started_at=started_text,
        finished_at=finished_text,
        duration_seconds=duration,
        environment=_environment_from_snapshot(model_snapshot),
        dependencies=_dependencies_from_snapshot(model_snapshot),
        activity=_activity_steps(activity),
        apply_outcome=_apply_outcome_view(apply_outcomes),
        # Stage A: only a Failed Run reads failure_json; a Completed /
        # Cancelled Run ignores any stale failure data instead of presenting
        # a contradictory state.
        failure=(
            failure_view(snapshot.get("failure_json"))
            if status_text == "Failed"
            else None
        ),
        # D-3: render the deepagents plan from the run snapshot. The panel
        # state keeps this fresh from both the D-1 todos.updated event and
        # the D-2 persisted RunRecord.todos.
        todos=todo_items(snapshot.get("todos")),
    )


def failure_card(failure: FailureView) -> MessageItem:
    """Render a normalized FailureView as an error-tone conversation card.

    Accepts only an exact FailureView (never a raw mapping). When retryable,
    a fixed, descriptive English hint is appended. The hint is computed as a
    structural suffix: the body is bounded to leave room for it, so the whole
    body never exceeds MAX_BODY_CHARS and the hint stays intact. No automatic
    retry behavior, button, or command is introduced.
    """
    if type(failure) is not FailureView:
        raise TypeError("failure must be an exact FailureView")
    title = _bounded(f"Run failed · {failure.code}", MAX_TITLE_CHARS)
    hint = "Retry may succeed." if failure.retryable else ""
    if hint:
        # Reserve space for the two-line join (message + "\n" + hint) before
        # bounding so the hint itself is never truncated past MAX_BODY_CHARS.
        remaining = max(0, MAX_BODY_CHARS - len(hint) - 1)
        body = _bounded(failure.message, remaining) + "\n" + hint
        body = body[:MAX_BODY_CHARS]
    else:
        body = _bounded(failure.message, MAX_BODY_CHARS)
    return MessageItem(kind="error", title=title, body=body, tone="error")


def terminal_result_items(
    run: object,
    output: object,
) -> tuple[MessageItem, ...]:
    """Pure terminal-result selector shared by history replay and live settling.

    All Completed / Cancelled / Failed rendering decisions live here. It never
    mutates run, never returns an empty assistant card, and never raises on an
    unknown status or malformed run. Non-terminal or non-dict runs yield ().
    output counts only when it is an exact str; otherwise it is treated as
    empty. A Failed Run with partial output yields (assistant, error) in that
    fixed order.
    """
    if type(run) is not dict:
        return ()
    status = run.get("status")
    text = output if type(output) is str else ""
    if status == "Completed":
        if text:
            return (assistant_message(text),)
        return (notice_card("The run completed without a response.", tone="normal"),)
    if status == "Cancelled":
        if text:
            return (assistant_message(text),)
        return (notice_card("The run was cancelled.", tone="warn"),)
    if status == "Failed":
        error = failure_card(failure_view(run.get("failure_json")))
        if text:
            return (assistant_message(text), error)
        return (error,)
    # Non-terminal (Planning / Finalizing / StopRequested / ...) and unknown
    # statuses settle without terminal cards.
    return ()


def workspace_rows(facts: object) -> tuple[WorkspaceRow, ...]:
    """Render a workspace facts dict as bounded (key, value, tone) rows."""
    if type(facts) is not dict:
        return ()
    rows: list[WorkspaceRow] = []
    for key in sorted(facts):
        value = facts[key]
        value_text = str(value) if value is not None else "-"
        tone = "normal"
        if type(value) is str:
            value_lower = value.lower()
            if value_lower in {"healthy", "ready", "available", "applied"}:
                tone = "ok"
            elif value_lower in {"stale", "expired", "missing"}:
                tone = "warn"
            elif value_lower in {"failed", "error", "unavailable"}:
                tone = "error"
        rows.append(WorkspaceRow(
            key=_bounded(key, MAX_TITLE_CHARS),
            value=_bounded(value_text, MAX_BODY_CHARS),
            tone=tone,
        ))
    return tuple(rows)


def artifact_rows(artifacts: object) -> tuple[ArtifactRow, ...]:
    """Render an artifact summaries tuple as bounded ArtifactRow records."""
    if not isinstance(artifacts, (list, tuple)):
        return ()
    rows: list[ArtifactRow] = []
    for summary in artifacts:
        if type(summary) is not dict:
            continue
        state = summary.get("state")
        state_text = state if type(state) is str else "unknown"
        size = summary.get("size_bytes")
        size_text = f"{size} bytes" if type(size) is int else "size unknown"
        path = summary.get("relative_path")
        path_text = path if type(path) is str else "artifact"
        tone = "ok" if state_text == "available" else (
            "error" if state_text in {"failed", "missing"} else "normal")
        rows.append(ArtifactRow(
            state=state_text,
            tone=tone,
            relative_path=_bounded(path_text, MAX_TITLE_CHARS),
            size_text=size_text,
        ))
    return tuple(rows)


def vision_rows(visions: object) -> tuple[VisionRow, ...]:
    """Render vision evaluation summaries as bounded VisionRow records."""
    if not isinstance(visions, (list, tuple)):
        return ()
    rows: list[VisionRow] = []
    for summary in visions:
        if type(summary) is not dict:
            continue
        status = summary.get("status")
        status_text = status if type(status) is str else "unknown"
        accepted = summary.get("accepted") is True
        tone = "ok" if status_text == "completed" and accepted else "warn"
        report = summary.get("report_summary")
        report_text = report if type(report) is str else ""
        rows.append(VisionRow(
            status=status_text,
            accepted=accepted,
            tone=tone,
            report_summary=_bounded(report_text, MAX_BODY_CHARS),
        ))
    return tuple(rows)
