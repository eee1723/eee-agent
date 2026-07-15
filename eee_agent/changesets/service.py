"""Trusted ChangeSet approval service (Task 16-B2a).

Coordinates the typed ChangeSet proposal and approval lifecycle above the
strict :class:`ChangeSetRepository`. It owns no SQL and no transport: it
validates the already-computed :class:`PolicyDecision`, mints the
``Pending`` :class:`ApprovalRecord`, and delegates the coupled
state/approval/event transaction to the repository. Notifications are left to
the Runtime service layer, which calls :meth:`RuntimeService._notify` only
after the repository transaction has committed.

The service never calls Bridge, ``hou``, ``rpyc``, the network, shell, or
filesystem. The current :class:`SceneBinding` used to bind an approval is
fetched from an injected provider seam (a plain callable); in production it is
wired to the read-only Bridge query, and offline tests inject a controllable
binding. Likewise the clock is an injected callable so expiry is deterministic
in tests.

This module imports neither ``hou`` nor the legacy ``eee_agent.bridge``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeSet,
    PolicyDecision,
)
from eee_agent.changesets.repository import (
    ChangeSetRepository,
    DecisionResult,
    ProposalResult,
)
from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.core.ids import IdKind, new_id
from eee_agent.houdini_bridge.contracts import SceneBinding

# Conservative pending-approval lifetime. Every allowed decision remains
# per-ChangeSet and explicit; a short window bounds how long a stale approval
# can be acted on before the clock expires it.
DEFAULT_APPROVAL_TTL_SECONDS = 300.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _policy_denied(decision: PolicyDecision) -> AgentException:
    return AgentException(
        AgentError(
            code="policy.denied",
            category=ErrorCategory.PERMISSION,
            message_for_user="The ChangeSet was denied by policy.",
        )
    )


def _changeset_policy_digest_mismatch() -> AgentException:
    return AgentException(
        AgentError(
            code="changeset.invalid",
            category=ErrorCategory.VALIDATION,
            message_for_user="The policy decision does not match the ChangeSet.",
        )
    )


def _binding_unavailable() -> AgentException:
    return AgentException(
        AgentError(
            code="bridge.capability_unavailable",
            category=ErrorCategory.VALIDATION,
            message_for_user="The current scene binding is not available.",
        )
    )


def _approval_expired() -> AgentException:
    return AgentException(
        AgentError(
            code="approval.expired",
            category=ErrorCategory.VALIDATION,
            message_for_user="The approval has expired.",
        )
    )


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    """A bounded, JSON-only view of a decided approval for protocol responses.

    Carries only stable identifiers, the canonical digest, the resulting
    decision, the ChangeSet state, and (for approvals) the bound scene facts.
    It never includes the token, the full DTO, operations, parameter values, or
    any exception detail.
    """

    change_id: str
    approval_id: str
    changeset_digest: str
    decision: ApprovalDecision
    changeset_state: str
    approved_instance_id: str | None
    approved_scene_epoch: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "approval_id": self.approval_id,
            "changeset_digest": self.changeset_digest,
            "decision": self.decision.value,
            "state": self.changeset_state,
            "approved_instance_id": self.approved_instance_id,
            "approved_scene_epoch": self.approved_scene_epoch,
        }


class ChangeSetService:
    """Trusted proposal + approval lifecycle coordinator.

    Construction is cheap and side-effect free; all work happens in the async
    methods. ``clock`` and ``binding_provider`` are injection seams with
    defaults that keep the service usable in production (real clock) while
    failing closed when no scene binding is available (until the Bridge seam
    is wired in a later task).
    """

    def __init__(
        self,
        repository: ChangeSetRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        binding_provider: Callable[[], SceneBinding] | None = None,
        approval_ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> None:
        if type(repository) is not ChangeSetRepository:
            raise TypeError("repository must be an exact ChangeSetRepository")
        self._repository = repository
        self._clock = clock if clock is not None else _now_utc
        self._binding_provider = binding_provider
        if type(approval_ttl_seconds) not in (int, float) or approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be a positive number")
        self._approval_ttl = timedelta(seconds=float(approval_ttl_seconds))

    async def propose(
        self,
        changeset: ChangeSet,
        policy_decision: PolicyDecision,
    ) -> ProposalResult:
        """Propose a ChangeSet and request its approval (trusted caller only).

        The ChangeSet must already carry a computed, *allowing* policy decision
        whose canonical digest matches the ChangeSet. The service mints the
        ``Pending`` approval with an expiry drawn from the injected clock and
        delegates the atomic insert/transition/event transaction to the
        repository.
        """
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        if type(policy_decision) is not PolicyDecision:
            raise TypeError("policy_decision must be an exact PolicyDecision")
        if not policy_decision.allowed:
            raise _policy_denied(policy_decision)
        if policy_decision.changeset_digest != changeset.digest:
            raise _changeset_policy_digest_mismatch()
        now = _require_utc(self._clock())
        approval = ApprovalRecord(
            approval_id=new_id(IdKind.APPROVAL),
            change_id=changeset.change_id,
            changeset_digest=changeset.digest,
            decision=ApprovalDecision.PENDING,
            decided_by=None,
            requested_at=now,
            decided_at=None,
            expires_at=now + self._approval_ttl,
            approved_instance_id=None,
            approved_scene_epoch=None,
        )
        return await self._repository.propose(changeset, approval)

    async def approve(
        self,
        change_id: str,
        changeset_digest: str,
    ) -> DecisionResult:
        """Approve a pending ChangeSet, binding the current scene facts."""
        binding = self._current_binding()
        now = _require_utc(self._clock())
        return await self._repository.decide(
            change_id,
            changeset_digest=changeset_digest,
            decision=ApprovalDecision.APPROVED,
            now=now,
            approved_binding=binding,
        )

    async def reject(
        self,
        change_id: str,
        changeset_digest: str,
    ) -> DecisionResult:
        """Reject a pending ChangeSet."""
        now = _require_utc(self._clock())
        return await self._repository.decide(
            change_id,
            changeset_digest=changeset_digest,
            decision=ApprovalDecision.REJECTED,
            now=now,
            approved_binding=None,
        )

    def _current_binding(self) -> SceneBinding:
        provider = self._binding_provider
        if provider is None:
            raise _binding_unavailable()
        binding = provider()
        if type(binding) is not SceneBinding:
            raise _binding_unavailable()
        return binding


def _require_utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def summary_from_decision(result: DecisionResult) -> ApprovalSummary:
    """Build the bounded protocol summary for a committed decision result."""
    approved_instance_id = result.approval.approved_instance_id
    approved_scene_epoch = result.approval.approved_scene_epoch
    return ApprovalSummary(
        change_id=result.approval.change_id,
        approval_id=result.approval.approval_id,
        changeset_digest=result.approval.changeset_digest,
        decision=result.approval.decision,
        changeset_state=result.changeset_state.value,
        approved_instance_id=approved_instance_id,
        approved_scene_epoch=approved_scene_epoch,
    )


def expired_error() -> AgentException:
    """The ``approval.expired`` error raised post-notification on expiry."""
    return _approval_expired()
