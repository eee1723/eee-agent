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
    kind: str        # user|proposal|approval|validation|vision|artifact|notice|error
    title: str
    body: str
    tone: str        # one of TONES
    mono: bool = False


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


def approval_result_card(approved: bool, *, expired: bool = False) -> MessageItem:
    if expired:
        return MessageItem("approval", "Approval expired",
                           "The gate timed out without a decision.", "warn")
    if approved:
        return MessageItem("approval", "Approved",
                           "ChangeSet approved and queued for apply.", "ok")
    return MessageItem("approval", "Rejected",
                       "ChangeSet rejected by the user.", "error")


def validation_card(report: Mapping[str, object]) -> MessageItem:
    accepted = report.get("accepted") is True
    summary = _bounded(report.get("summary") or report.get("report_summary"),
                       MAX_BODY_CHARS)
    return MessageItem(
        kind="validation",
        title="Validation passed" if accepted else "Validation failed",
        body=summary or "No summary reported.",
        tone="ok" if accepted else "error",
    )


def vision_card(summary: Mapping[str, object]) -> MessageItem:
    # Keys follow parse_vision_event: status / accepted / report_summary.
    status = _bounded(summary.get("status"), 40) or "unknown"
    accepted = summary.get("accepted") is True
    decision = "accepted" if accepted else "rejected"
    text = _bounded(summary.get("report_summary"), MAX_BODY_CHARS)
    tone = "ok" if status == "completed" and accepted else "warn"
    body = f"Status: {status}\nDecision: {decision}"
    if text:
        body += f"\n{text}"
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
