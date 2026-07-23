"""Trusted ChangeSet approval, Apply, and recovery service (Tasks 16-B2a/E).

Coordinates the typed ChangeSet proposal and approval lifecycle above the
strict :class:`ChangeSetRepository`. It owns no SQL and no transport: it
validates the already-computed :class:`PolicyDecision`, mints the
``Pending`` :class:`ApprovalRecord`, and delegates the coupled
state/approval/event transaction to the repository. Notifications are left to
the Runtime service layer, which calls :meth:`RuntimeService._notify` only
after the repository transaction has committed.

The service owns no Bridge transport and never imports ``hou`` or ``rpyc``.
Scene binding, typed preflight, Apply, and receipt queries use an injected
provider seam; production wires the authenticated loopback Bridge and offline
tests inject deterministic providers. The clock is injected so expiry and
recovery receipts are deterministic in tests.

This module imports neither ``hou`` nor the legacy ``eee_agent.bridge``. Task
16-E adds a narrow injected async provider for typed preflight/apply/receipt;
the service still owns no transport or unrestricted execution surface.
"""

from __future__ import annotations

import hashlib
import inspect
from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
    ChangeSet,
    ConditionResult,
    NodeIdentityEquals,
    ParmValueEquals,
    PolicyDecision,
    ReceiptStatus,
    WireInputEquals,
    WorkspaceManifest,
)
from eee_agent.changesets.repository import (
    ApplyCompletionResult,
    ApplyRecoveryResult,
    ChangeSetRepository,
    ChangeSetState,
    ChangeSetView,
    DecisionResult,
    ProposalResult,
)
from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.core.ids import IdKind, new_id
from eee_agent.houdini_bridge.changesets import PreflightResult
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.runtime.models import EventRecord, canonical_json_dumps

# Conservative pending-approval lifetime. Every allowed decision remains
# per-ChangeSet and explicit; a short window bounds how long a stale approval
# can be acted on before the clock expires it.
DEFAULT_APPROVAL_TTL_SECONDS = 300.0
_PANEL_SUMMARY_ITEM_LIMIT = 12

_TRANSIENT_RECOVERY_CODES = frozenset(
    {
        "bridge.not_available",
        "bridge.deadline_exceeded",
        "bridge.cancelled",
        "bridge.capability_unavailable",
    }
)
_MAX_RECOVERY_BLOCKER_IDS = 16


class ChangeSetBridgeProvider(Protocol):
    """Narrow typed Bridge seam used by trusted Runtime orchestration."""

    async def current_binding(self) -> SceneBinding: ...

    async def preflight(
        self, changeset: ChangeSet, workspace: WorkspaceManifest | None
    ) -> PreflightResult: ...

    async def apply(
        self, changeset: ChangeSet, workspace: WorkspaceManifest | None
    ) -> ChangeReceipt: ...

    async def receipt(self, changeset: ChangeSet) -> ChangeReceipt: ...


BootstrapManifestFactory = Callable[
    [ChangeSet, ChangeReceipt], WorkspaceManifest | None
]


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


def _bridge_unavailable() -> AgentException:
    return AgentException(
        AgentError(
            code="bridge.capability_unavailable",
            category=ErrorCategory.HOUDINI_BRIDGE,
            message_for_user="The typed Houdini ChangeSet Bridge is unavailable.",
            retryable=True,
        )
    )


def _critical_required(*, scene_may_have_changed: bool = True) -> AgentException:
    return AgentException(
        AgentError(
            code="recovery.critical_required",
            category=ErrorCategory.CRITICAL_RECOVERY,
            message_for_user=(
                "A ChangeSet outcome requires recovery before further writes."
            ),
            requires_user_action=True,
            scene_may_have_changed=scene_may_have_changed,
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


@dataclass(frozen=True, slots=True)
class PanelChangeSetSummary:
    """Bounded JSON-only ChangeSet state for the docked approval surface."""

    payload: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return deepcopy(self.payload)


def _approval_panel_summary(approval: ApprovalRecord | None) -> dict | None:
    if approval is None:
        return None
    return {
        "approval_id": approval.approval_id,
        "decision": approval.decision.value,
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": (
            approval.decided_at.isoformat()
            if approval.decided_at is not None
            else None
        ),
        "approved_instance_id": approval.approved_instance_id,
        "approved_scene_epoch": approval.approved_scene_epoch,
    }


def _receipt_panel_summary(receipt: ChangeReceipt | None) -> dict | None:
    if receipt is None:
        return None
    return {
        "status": receipt.status.value,
        "instance_id": receipt.instance_id,
        "scene_epoch": receipt.scene_epoch,
        "before_revision": receipt.before_revision,
        "after_revision": receipt.after_revision,
        "applied_operation_count": len(receipt.applied_op_ids),
        "scene_may_have_changed": receipt.scene_may_have_changed,
        "completed_at": receipt.completed_at.isoformat(),
    }


def _panel_summary(view: ChangeSetView) -> PanelChangeSetSummary:
    changeset = view.stored.changeset
    risk = changeset.risk_summary
    effects = risk.effect_names[:_PANEL_SUMMARY_ITEM_LIMIT]
    paths = risk.affected_paths[:_PANEL_SUMMARY_ITEM_LIMIT]
    return PanelChangeSetSummary(
        {
            "change_id": changeset.change_id,
            "run_id": changeset.run_id,
            "state": view.stored.state.value,
            "changeset_digest": changeset.digest,
            "created_at": changeset.created_at.isoformat(),
            "required_permission": changeset.required_permission.value,
            "risk": {
                "operation_count": risk.operation_count,
                "touches_external_nodes": risk.touches_external_nodes,
                "changes_wiring": risk.changes_wiring,
                "requires_backup": risk.requires_backup,
                "effect_count": len(risk.effect_names),
                "effect_names": list(effects),
                "affected_path_count": len(risk.affected_paths),
                "affected_paths": list(paths),
                "affected_paths_truncated": (
                    len(risk.affected_paths) > len(paths)
                ),
            },
            "approval": _approval_panel_summary(view.approval),
            "receipt": _receipt_panel_summary(view.receipt),
        }
    )


@dataclass(frozen=True, slots=True)
class ChangeSetRecoveryResult:
    """One restart/apply reconciliation outcome and its committed events."""

    change_id: str
    state: ChangeSetState
    receipt: ChangeReceipt | None
    events: tuple[EventRecord, ...]
    pending: bool = False


class ApplyOrchestrationError(Exception):
    """A Bridge failure after durable Apply/recovery events committed."""

    def __init__(self, cause: Exception, events: tuple[EventRecord, ...]) -> None:
        self.cause = cause
        self.events = events
        super().__init__("typed ChangeSet apply did not reach a terminal receipt")


class ChangeSetService:
    """Trusted proposal + approval lifecycle coordinator.

    Construction is cheap and side-effect free; all work happens in the async
    methods. ``clock`` and ``binding_provider`` are injection seams with
    defaults that keep the service usable in production (real clock) while
    failing closed when no typed Bridge provider or scene binding is available.
    """

    def __init__(
        self,
        repository: ChangeSetRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        binding_provider: Callable[[], SceneBinding] | None = None,
        bridge_provider: ChangeSetBridgeProvider | None = None,
        bootstrap_manifest_factory: BootstrapManifestFactory | None = None,
        approval_ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> None:
        if type(repository) is not ChangeSetRepository:
            raise TypeError("repository must be an exact ChangeSetRepository")
        self._repository = repository
        self._clock = clock if clock is not None else _now_utc
        self._binding_provider = binding_provider
        self._bridge_provider = bridge_provider
        if bootstrap_manifest_factory is not None and not callable(
            bootstrap_manifest_factory
        ):
            raise TypeError("bootstrap_manifest_factory must be callable or None")
        self._bootstrap_manifest_factory = bootstrap_manifest_factory
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

    async def list_panel_summaries(
        self, session_id: str, *, limit: int
    ) -> tuple[PanelChangeSetSummary, ...]:
        """Return newest bounded approval/receipt summaries for one Session."""
        views = await self._repository.list_changeset_views(
            session_id, limit=limit
        )
        return tuple(_panel_summary(view) for view in views)

    async def critical_recovery_blockers(self) -> tuple[str, ...]:
        """Return bounded IDs of terminal outcomes that freeze further writes."""
        blockers = await self._repository.changesets_in_states(
            (ChangeSetState.CRITICAL_RECOVERY,)
        )
        return tuple(
            item.changeset.change_id
            for item in blockers[:_MAX_RECOVERY_BLOCKER_IDS]
        )

    async def approve(
        self,
        change_id: str,
        changeset_digest: str,
    ) -> DecisionResult:
        """Approve a pending ChangeSet, binding the current scene facts."""
        binding = await self._current_binding()
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

    async def apply(self, change_id: str) -> ApplyCompletionResult:
        """Apply one approved ChangeSet through the trusted typed Bridge seam."""
        stored = await self._repository.get_changeset(change_id)
        if stored.state in (
            ChangeSetState.APPLIED,
            ChangeSetState.ROLLED_BACK,
            ChangeSetState.CRITICAL_RECOVERY,
        ):
            receipt = await self._repository.get_receipt(change_id)
            return ApplyCompletionResult(stored.changeset, receipt, stored.state, ())

        if stored.state is ChangeSetState.APPLYING:
            recovered = await self.recover_one(change_id)
            if recovered.pending:
                raise _bridge_unavailable()
            if recovered.receipt is not None:
                return ApplyCompletionResult(
                    stored.changeset,
                    recovered.receipt,
                    recovered.state,
                    recovered.events,
                )
            stored = await self._repository.get_changeset(change_id)

        await self._ensure_write_available(change_id)
        bridge = self._require_bridge()
        workspace = await self._workspace_for(stored.changeset)
        started = await self._repository.begin_apply(
            change_id, now=_require_utc(self._clock())
        )
        try:
            receipt = await bridge.apply(started.changeset, workspace)
            self._validate_receipt(started.changeset, receipt)
        except Exception as exc:
            recovered = await self.recover_one(change_id)
            if recovered.receipt is not None:
                return ApplyCompletionResult(
                    started.changeset,
                    recovered.receipt,
                    recovered.state,
                    started.events + recovered.events,
                )
            raise ApplyOrchestrationError(
                exc, started.events + recovered.events
            ) from exc
        completed = await self._complete_receipt(started.changeset, receipt)
        return ApplyCompletionResult(
            completed.changeset,
            completed.receipt,
            completed.state,
            started.events + completed.events,
        )

    async def recover_applying(self) -> tuple[ChangeSetRecoveryResult, ...]:
        """Reconcile every interrupted Apply without replaying a write."""
        applying = await self._repository.changesets_in_states(
            (ChangeSetState.APPLYING,)
        )
        results: list[ChangeSetRecoveryResult] = []
        for stored in applying:
            results.append(await self.recover_one(stored.changeset.change_id))
        return tuple(results)

    async def recover_one(self, change_id: str) -> ChangeSetRecoveryResult:
        """Query receipt then facts for one durable ``Applying`` record."""
        stored = await self._repository.get_changeset(change_id)
        if stored.state is not ChangeSetState.APPLYING:
            receipt = None
            if stored.state in (
                ChangeSetState.APPLIED,
                ChangeSetState.ROLLED_BACK,
                ChangeSetState.CRITICAL_RECOVERY,
            ):
                receipt = await self._repository.get_receipt(change_id)
            return ChangeSetRecoveryResult(
                change_id, stored.state, receipt, (), pending=False
            )

        try:
            bridge = self._require_bridge()
        except AgentException as exc:
            if exc.error.code in _TRANSIENT_RECOVERY_CODES:
                return ChangeSetRecoveryResult(
                    change_id, ChangeSetState.APPLYING, None, (), pending=True
                )
            raise
        changeset = stored.changeset
        workspace = await self._workspace_for(changeset)
        try:
            receipt = await bridge.receipt(changeset)
        except AgentException as exc:
            if exc.error.code in _TRANSIENT_RECOVERY_CODES:
                return ChangeSetRecoveryResult(
                    change_id, ChangeSetState.APPLYING, None, (), pending=True
                )
            if exc.error.code != "changeset.receipt_unavailable":
                return await self._commit_critical(changeset, None, exc.error.code)
        else:
            try:
                self._validate_receipt(changeset, receipt)
            except AgentException:
                return await self._commit_critical(
                    changeset, None, "recovery.receipt_invalid"
                )
            committed = await self._complete_receipt(changeset, receipt)
            return _recovery_from_completion(committed)

        try:
            facts = await bridge.preflight(changeset, workspace)
        except AgentException as exc:
            if exc.error.code in _TRANSIENT_RECOVERY_CODES:
                return ChangeSetRecoveryResult(
                    change_id, ChangeSetState.APPLYING, None, (), pending=True
                )
            return await self._commit_critical(changeset, None, exc.error.code)

        binding = changeset.scene_binding
        if not _recovery_facts_match(changeset, workspace, facts):
            return await self._commit_critical(
                changeset, facts, "recovery.facts_mismatch"
            )
        if facts.all_preconditions_hold:
            recovered = await self._repository.recover_to_approved(change_id)
            return _recovery_from_approved(recovered)

        post_results = _evaluate_postconditions(changeset, facts)
        if post_results and all(result.passed for result in post_results):
            receipt = ChangeReceipt(
                change_id=change_id,
                status=ReceiptStatus.ALREADY_APPLIED,
                instance_id=binding.instance_id,
                scene_epoch=binding.scene_epoch,
                before_revision=changeset.base_revision,
                after_revision=_evidence_digest(facts.to_dict()),
                applied_op_ids=tuple(op.op_id for op in changeset.operations),
                postcondition_results=post_results,
                rollback_results=(),
                scene_may_have_changed=False,
                completed_at=_require_utc(self._clock()),
            )
            committed = await self._complete_receipt(changeset, receipt)
            return _recovery_from_completion(committed)
        return await self._commit_critical(
            changeset, facts, "recovery.state_ambiguous"
        )

    async def _commit_critical(
        self,
        changeset: ChangeSet,
        facts: PreflightResult | None,
        reason: str,
    ) -> ChangeSetRecoveryResult:
        evidence: dict[str, object] = {
            "change_id": changeset.change_id,
            "changeset_digest": changeset.digest,
            "reason": reason,
            "facts": None if facts is None else facts.to_dict(),
        }
        post_results = (
            _failed_postconditions(changeset)
            if facts is None
            else _evaluate_postconditions(changeset, facts)
        )
        receipt = ChangeReceipt(
            change_id=changeset.change_id,
            status=ReceiptStatus.CRITICAL_RECOVERY,
            instance_id=(
                changeset.scene_binding.instance_id
                if facts is None
                else facts.binding.instance_id
            ),
            scene_epoch=(
                changeset.scene_binding.scene_epoch
                if facts is None
                else facts.binding.scene_epoch
            ),
            before_revision=changeset.base_revision,
            after_revision=_evidence_digest(evidence),
            applied_op_ids=(),
            postcondition_results=post_results,
            rollback_results=(),
            scene_may_have_changed=True,
            completed_at=_require_utc(self._clock()),
        )
        committed = await self._complete_receipt(changeset, receipt)
        return _recovery_from_completion(committed)

    async def _complete_receipt(
        self,
        changeset: ChangeSet,
        receipt: ChangeReceipt,
    ) -> ApplyCompletionResult:
        factory = self._bootstrap_manifest_factory
        workspace = None if factory is None else factory(changeset, receipt)
        if workspace is not None and type(workspace) is not WorkspaceManifest:
            raise TypeError(
                "bootstrap_manifest_factory must return WorkspaceManifest or None"
            )
        if workspace is not None:
            return await self._repository.complete_bootstrap_apply(
                receipt, workspace
            )
        return await self._repository.complete_apply(receipt)

    async def _ensure_write_available(self, change_id: str) -> None:
        blockers = await self._repository.changesets_in_states(
            (ChangeSetState.APPLYING, ChangeSetState.CRITICAL_RECOVERY)
        )
        if any(item.changeset.change_id != change_id for item in blockers):
            raise _critical_required(
                scene_may_have_changed=any(
                    item.state is ChangeSetState.CRITICAL_RECOVERY
                    for item in blockers
                )
            )

    async def _workspace_for(
        self, changeset: ChangeSet
    ) -> WorkspaceManifest | None:
        if changeset.workspace_id is None:
            return None
        workspace = await self._repository.get_workspace(changeset.workspace_id)
        if workspace.session_id != changeset.session_id:
            raise _critical_required(scene_may_have_changed=False)
        return workspace

    def _require_bridge(self) -> ChangeSetBridgeProvider:
        if self._bridge_provider is None:
            raise _bridge_unavailable()
        return self._bridge_provider

    async def _current_binding(self) -> SceneBinding:
        if self._bridge_provider is not None:
            binding = await self._bridge_provider.current_binding()
        else:
            provider = self._binding_provider
            if provider is None:
                raise _binding_unavailable()
            binding = provider()
            if inspect.isawaitable(binding):
                binding = await binding
        if type(binding) is not SceneBinding:
            raise _binding_unavailable()
        return binding

    async def current_binding(self) -> SceneBinding:
        """Return the trusted current scene binding for capability context."""
        return await self._current_binding()

    @staticmethod
    def _validate_receipt(changeset: ChangeSet, receipt: ChangeReceipt) -> None:
        if type(receipt) is not ChangeReceipt or receipt.change_id != changeset.change_id:
            raise _critical_required()
        if receipt.status in (
            ReceiptStatus.APPLIED,
            ReceiptStatus.ALREADY_APPLIED,
            ReceiptStatus.ROLLED_BACK,
        ) and (
            receipt.instance_id != changeset.scene_binding.instance_id
            or receipt.scene_epoch != changeset.scene_binding.scene_epoch
        ):
            raise _critical_required()


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


def _identity(ref) -> str:  # type: ignore[no-untyped-def]
    return ref.node_id if ref.node_id is not None else ref.path


def _json_equal(left: object, right: object) -> bool:
    return canonical_json_dumps(left) == canonical_json_dumps(right)


def _recovery_facts_match(
    changeset: ChangeSet,
    workspace: WorkspaceManifest | None,
    facts: PreflightResult,
) -> bool:
    binding = changeset.scene_binding
    if (
        facts.binding.instance_id != binding.instance_id
        or facts.binding.scene_epoch != binding.scene_epoch
    ):
        return False
    if workspace is None:
        if facts.workspace_id is not None or facts.workspace_revision is not None:
            return False
    elif (
        facts.workspace_id != workspace.workspace_id
        or facts.workspace_revision != workspace.revision
    ):
        return False

    expected_kinds = tuple(cond.kind for cond in changeset.preconditions)
    actual_kinds = tuple(result.kind for result in facts.condition_results)
    if actual_kinds != expected_kinds:
        return False
    if facts.all_preconditions_hold != all(
        result.passed for result in facts.condition_results
    ):
        return False

    node_keys = tuple(_identity(item.requested) for item in facts.node_facts)
    parm_keys = tuple(
        (_identity(item.target), item.parm_name) for item in facts.parm_facts
    )
    wire_keys = tuple(
        (_identity(item.target), item.input_index) for item in facts.wire_facts
    )
    return (
        len(node_keys) == len(set(node_keys))
        and len(parm_keys) == len(set(parm_keys))
        and len(wire_keys) == len(set(wire_keys))
    )


def _evaluate_postconditions(
    changeset: ChangeSet, facts: PreflightResult
) -> tuple[ConditionResult, ...]:
    node_facts = {_identity(item.requested): item for item in facts.node_facts}
    parm_facts = {
        (_identity(item.target), item.parm_name): item for item in facts.parm_facts
    }
    wire_facts = {
        (_identity(item.target), item.input_index): item for item in facts.wire_facts
    }
    results: list[ConditionResult] = []
    for cond in changeset.expected_postconditions:
        passed = False
        if isinstance(cond, NodeIdentityEquals):
            node_fact = node_facts.get(_identity(cond.node))
            if node_fact is not None and node_fact.exists:
                passed = (
                    node_fact.actual_path == cond.node.path
                    and node_fact.actual_type == cond.node.expected_type
                    and (
                        cond.node.node_id is None
                        or node_fact.node_id == cond.node.node_id
                    )
                    and (
                        cond.node.expected_workspace_id is None
                        or node_fact.workspace_id
                        == cond.node.expected_workspace_id
                    )
                )
        elif isinstance(cond, ParmValueEquals):
            parm_fact = parm_facts.get(
                (_identity(cond.target), cond.parm_name)
            )
            passed = (
                parm_fact is not None
                and parm_fact.exists
                and _json_equal(parm_fact.value, cond.value)
            )
        elif isinstance(cond, WireInputEquals):
            wire_fact = wire_facts.get(
                (_identity(cond.target), cond.input_index)
            )
            passed = wire_fact is not None and wire_fact.source == cond.source
        results.append(ConditionResult(kind=cond.kind, passed=passed, detail=None))
    return tuple(results)


def _failed_postconditions(changeset: ChangeSet) -> tuple[ConditionResult, ...]:
    return tuple(
        ConditionResult(kind=cond.kind, passed=False, detail=None)
        for cond in changeset.expected_postconditions
    )


def _evidence_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_dumps(value).encode("utf-8")).hexdigest()


def _recovery_from_completion(
    result: ApplyCompletionResult,
) -> ChangeSetRecoveryResult:
    return ChangeSetRecoveryResult(
        result.changeset.change_id,
        result.state,
        result.receipt,
        result.events,
        pending=False,
    )


def _recovery_from_approved(
    result: ApplyRecoveryResult,
) -> ChangeSetRecoveryResult:
    return ChangeSetRecoveryResult(
        result.changeset.change_id,
        result.state,
        None,
        result.events,
        pending=False,
    )
