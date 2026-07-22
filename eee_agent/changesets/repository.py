"""Application-database persistence for typed ChangeSet records (Task 16-B1).

A focused, transaction-safe repository over the schema-v2 tables
(``workspaces``, ``session_workspace_state``, ``changesets``, ``approvals``,
``change_receipts``). It
round-trips the frozen Task 16-A DTOs without changing their canonical JSON or
digest, enforces foreign keys and unique identities, and exposes
compare-and-set / single-use-consumption primitives for the later approval
service.

Strictness rules:

* every mutation runs inside :meth:`RuntimeDatabase.write_transaction` and a
  live SQLite connection is never returned to the caller;
* canonical DTO JSON and its SHA-256 digest are stored together, and the digest
  is recomputed from the payload text and checked on every read — a database
  digest is never trusted blindly;
* private strict decoders reconstruct Task 16-A DTOs from canonical JSON and
  reject unknown fields, wrong tags/enums, duplicate keys, non-UTC timestamps,
  digest mismatches, and malformed nested DTOs.

This module imports neither ``hou`` nor the legacy ``eee_agent.bridge``. It
depends only on the accepted read-only :class:`SceneBinding` contract, the
shared Runtime canonical-JSON helpers, and :class:`RuntimeDatabase`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    ConnectInput,
    CreateNode,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    OwnedNodeRef,
    ParmSnapshot,
    ParmValueEquals,
    PermissionMode,
    ReceiptStatus,
    RiskSummary,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WireRef,
    WireSnapshot,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
)
from eee_agent.changesets import codec
from eee_agent.core import AgentError, AgentException, ErrorCategory, IdKind, require_id
from eee_agent.core.events import JsonValue
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.models import EventRecord, RetentionClass, canonical_json_dumps

if TYPE_CHECKING:
    # EventStore lives in the runtime package; importing it eagerly here would
    # create a runtime/changesets import cycle. The dependency is type-only.
    from eee_agent.runtime.events import EventStore

# --------------------------------------------------------------------------
# ChangeSet state machine (design section 5)
# --------------------------------------------------------------------------


class ChangeSetState(StrEnum):
    """Persisted lifecycle state of a ChangeSet."""

    PROPOSED = "Proposed"
    AWAITING_APPROVAL = "AwaitingApproval"
    APPROVED = "Approved"
    APPLYING = "Applying"
    APPLIED = "Applied"
    ROLLED_BACK = "RolledBack"
    CRITICAL_RECOVERY = "CriticalRecovery"
    STALE = "Stale"
    REJECTED = "Rejected"
    EXPIRED = "Expired"


_TRANSITIONS: dict[ChangeSetState, frozenset[ChangeSetState]] = {
    ChangeSetState.PROPOSED: frozenset({ChangeSetState.AWAITING_APPROVAL}),
    ChangeSetState.AWAITING_APPROVAL: frozenset(
        {ChangeSetState.APPROVED, ChangeSetState.REJECTED, ChangeSetState.EXPIRED}
    ),
    ChangeSetState.APPROVED: frozenset({ChangeSetState.APPLYING, ChangeSetState.STALE}),
    ChangeSetState.APPLYING: frozenset(
        {
            ChangeSetState.APPLIED,
            ChangeSetState.ROLLED_BACK,
            ChangeSetState.CRITICAL_RECOVERY,
            # Restart recovery may return an interrupted Applying change back to
            # Approved without replaying a write (design section 5).
            ChangeSetState.APPROVED,
        }
    ),
    ChangeSetState.APPLIED: frozenset(),
    ChangeSetState.ROLLED_BACK: frozenset(),
    ChangeSetState.CRITICAL_RECOVERY: frozenset(),
    ChangeSetState.STALE: frozenset(),
    ChangeSetState.REJECTED: frozenset(),
    ChangeSetState.EXPIRED: frozenset(),
}

TERMINAL_CHANGESET_STATES: frozenset[ChangeSetState] = frozenset(
    s for s, targets in _TRANSITIONS.items() if not targets
)
NONTERMINAL_CHANGESET_STATES: frozenset[ChangeSetState] = frozenset(
    s for s in ChangeSetState if s not in TERMINAL_CHANGESET_STATES
)

# Approval decision transitions reachable through update_approval. The
# Approved -> Consumed transition is performed atomically by consume_approval.
_APPROVAL_TRANSITIONS: dict[ApprovalDecision, frozenset[ApprovalDecision]] = {
    ApprovalDecision.PENDING: frozenset(
        {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED, ApprovalDecision.EXPIRED}
    ),
    ApprovalDecision.APPROVED: frozenset({ApprovalDecision.EXPIRED}),
    ApprovalDecision.CONSUMED: frozenset(),
    ApprovalDecision.REJECTED: frozenset(),
    ApprovalDecision.EXPIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StoredChangeSet:
    """A persisted ChangeSet paired with its lifecycle state."""

    changeset: ChangeSet
    state: ChangeSetState


@dataclass(frozen=True, slots=True)
class ChangeSetView:
    """One consistent persisted ChangeSet/approval/receipt read."""

    stored: StoredChangeSet
    approval: ApprovalRecord | None
    receipt: ChangeReceipt | None

    def __post_init__(self) -> None:
        if type(self.stored) is not StoredChangeSet:
            raise TypeError("stored must be an exact StoredChangeSet")
        if self.approval is not None and type(self.approval) is not ApprovalRecord:
            raise TypeError("approval must be an exact ApprovalRecord or None")
        if self.receipt is not None and type(self.receipt) is not ChangeReceipt:
            raise TypeError("receipt must be an exact ChangeReceipt or None")


@dataclass(frozen=True, slots=True)
class ProposalResult:
    """The committed outcome of a trusted proposal.

    ``events`` is the tuple of durable events appended in the same transaction
    (``changeset.proposed`` then ``approval.requested``). It is empty for an
    idempotent re-proposal of an identical ChangeSet, so the service notifies
    subscribers only for genuinely new state.
    """

    changeset: ChangeSet
    state: ChangeSetState
    approval: ApprovalRecord
    events: tuple[EventRecord, ...]


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """The committed outcome of an approve/reject decision.

    ``outcome`` is the resulting :class:`ApprovalDecision` (``Approved``,
    ``Rejected``, or ``Expired``). The ``Expired`` outcome carries the
    committed expiry transition and its events; the service reports it as the
    ``approval.expired`` error only after subscribers have been notified.
    """

    outcome: ApprovalDecision
    approval: ApprovalRecord
    changeset_state: ChangeSetState
    events: tuple[EventRecord, ...]


@dataclass(frozen=True, slots=True)
class ApplyStartResult:
    """Committed transition across the Runtime write boundary."""

    changeset: ChangeSet
    approval: ApprovalRecord
    events: tuple[EventRecord, ...]
    reused_consumed_approval: bool


@dataclass(frozen=True, slots=True)
class ApplyCompletionResult:
    """Committed receipt, terminal state, and matching durable events."""

    changeset: ChangeSet
    receipt: ChangeReceipt
    state: ChangeSetState
    events: tuple[EventRecord, ...]


@dataclass(frozen=True, slots=True)
class ApplyRecoveryResult:
    """Committed no-write recovery back to explicit-retry eligibility."""

    changeset: ChangeSet
    approval: ApprovalRecord
    state: ChangeSetState
    events: tuple[EventRecord, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceStateRecord:
    """Persisted active-Workspace pointer for one Runtime Session."""

    session_id: str
    active_workspace_id: str
    state_revision: int
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_id_value(self.session_id, IdKind.SESSION)
        _require_id_value(self.active_workspace_id, IdKind.WORKSPACE)
        if type(self.state_revision) is not int:
            raise TypeError("state_revision must be an exact integer")
        if self.state_revision < 1:
            raise ValueError("state_revision must be >= 1")
        object.__setattr__(
            self,
            "updated_at",
            _require_utc_datetime(self.updated_at, "updated_at"),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMutationResult:
    """One committed Workspace lifecycle mutation and its durable events."""

    manifest: WorkspaceManifest
    state: WorkspaceStateRecord | None
    events: tuple[EventRecord, ...]
    changed: bool

    def __post_init__(self) -> None:
        if type(self.manifest) is not WorkspaceManifest:
            raise TypeError("manifest must be an exact WorkspaceManifest")
        if self.state is not None and type(self.state) is not WorkspaceStateRecord:
            raise TypeError("state must be an exact WorkspaceStateRecord or None")
        if type(self.events) is not tuple or any(
            type(event) is not EventRecord for event in self.events
        ):
            raise TypeError("events must be a tuple of exact EventRecord values")
        if type(self.changed) is not bool:
            raise TypeError("changed must be an exact bool")


# --------------------------------------------------------------------------
# strict JSON + storage digest helpers
# --------------------------------------------------------------------------


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError("duplicate object key")
        seen.add(key)
    return dict(pairs)


def _loads_canonical(text: str) -> object:
    """Parse canonical JSON text, rejecting duplicate keys at any depth."""
    if type(text) is not str:
        raise TypeError("canonical JSON text must be an exact string")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateKeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid canonical JSON text: {exc}") from exc


def _storage_digest(dto: object) -> str:
    """SHA-256 of the DTO's canonical JSON — the storage integrity digest."""
    return hashlib.sha256(
        canonical_json_dumps(dto.to_dict()).encode("utf-8")  # type: ignore[attr-defined]
    ).hexdigest()


# --------------------------------------------------------------------------
# strict DTO decoders (reject unknown fields / wrong tags / bad types)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# DTO field sets + strict decoders: delegated to the shared contract codec.
# The codec owns the single typed decode path (exact types, exact field sets,
# tuple collection fields); the names below are kept as thin private wrappers
# plus re-exports so existing internal call sites and tests are unchanged.
# Storage-only boundaries (canonical JSON loading, digest verification) stay
# in this module and never enter the codec.
# --------------------------------------------------------------------------

_OWNED_FIELDS = codec._OWNED_FIELDS
_NODEREF_FIELDS = codec._NODEREF_FIELDS
_WIREREF_FIELDS = codec._WIREREF_FIELDS
_CREATE_FIELDS = codec._CREATE_FIELDS
_SETPARM_FIELDS = codec._SETPARM_FIELDS
_CONNECT_FIELDS = codec._CONNECT_FIELDS
_RISK_FIELDS = codec._RISK_FIELDS
_PARM_SNAPSHOT_FIELDS = codec._PARM_SNAPSHOT_FIELDS
_WIRE_SNAPSHOT_FIELDS = codec._WIRE_SNAPSHOT_FIELDS
_CHECKPOINT_FIELDS = codec._CHECKPOINT_FIELDS
# Historical repository spelling for the ConditionResult field set.
_RESULT_FIELDS = codec._CONDITION_RESULT_FIELDS
_MANIFEST_FIELDS = codec._MANIFEST_FIELDS
_CHANGESET_FIELDS = codec._CHANGESET_FIELDS
_APPROVAL_FIELDS = codec._APPROVAL_FIELDS
_RECEIPT_FIELDS = codec._RECEIPT_FIELDS


def _require_dict(value: object, label: str) -> dict[str, object]:
    return codec._require_exact_dict(value, label)


def _require_keys(
    value: dict[str, object], allowed: frozenset[str], label: str
) -> None:
    codec._require_exact_keys(value, allowed, label)


def _decode_dt(value: object, label: str) -> datetime:
    return codec._decode_datetime(value, label)


def _decode_owned(data: object) -> OwnedNodeRef:
    return codec.decode_owned_node_ref(data)


def _decode_noderef(data: object) -> NodeRef:
    return codec.decode_node_ref(data)


def _decode_wiref(data: object) -> WireRef:
    return codec.decode_wire_ref(data)


def _decode_operation(data: object) -> CreateNode | SetParm | ConnectInput:
    return codec.decode_operation(data)


def _decode_condition(
    data: object,
) -> (
    SceneBindingEquals
    | WorkspaceRevisionEquals
    | NodeIdentityEquals
    | ParmValueEquals
    | WireInputEquals
    | NodeAbsent
):
    return codec.decode_condition(data)


def _decode_risk(data: object) -> RiskSummary:
    return codec.decode_risk_summary(data)


def _decode_parm_snapshot(data: object) -> ParmSnapshot:
    return codec.decode_parm_snapshot(data)


def _decode_wire_snapshot(data: object) -> WireSnapshot:
    return codec.decode_wire_snapshot(data)


def _decode_checkpoint(data: object) -> CheckpointPlan:
    return codec.decode_checkpoint_plan(data)


def _decode_manifest(data: object) -> WorkspaceManifest:
    return codec.decode_workspace_manifest(data)


def _decode_result(data: object) -> ConditionResult:
    return codec.decode_condition_result(data)


def _decode_changeset(data: object) -> ChangeSet:
    return codec.decode_changeset(data)


def _decode_approval(data: object) -> ApprovalRecord:
    return codec.decode_approval_record(data)


def _decode_receipt(data: object) -> ChangeReceipt:
    return codec.decode_change_receipt(data)


# --------------------------------------------------------------------------
# typed errors
# --------------------------------------------------------------------------


def _err(code: str, message: str, *, category: ErrorCategory = ErrorCategory.VALIDATION) -> AgentException:
    return AgentException(
        AgentError(code=code, category=category, message_for_user=message)
    )


def _session_not_found() -> AgentException:
    return _err("runtime.session_not_found", "The Runtime session does not exist.")


def _run_not_found() -> AgentException:
    return _err("runtime.run_not_found", "The Runtime run does not exist.")


def _workspace_not_found() -> AgentException:
    return _err("runtime.workspace_not_found", "The workspace does not exist.")


def _changeset_not_found() -> AgentException:
    return _err("runtime.changeset_not_found", "The ChangeSet does not exist.")


def _approval_not_found() -> AgentException:
    return _err("runtime.approval_not_found", "The approval does not exist.")


def _receipt_not_found() -> AgentException:
    return _err("runtime.receipt_not_found", "The change receipt does not exist.")


def _workspace_exists() -> AgentException:
    return _err("runtime.duplicate_workspace", "The workspace already exists.")


def _workspace_identity_conflict() -> AgentException:
    return _err(
        "workspace.identity_conflict",
        "The workspace identity conflicts with persisted state.",
    )


def _workspace_session_mismatch() -> AgentException:
    return _err(
        "workspace.session_mismatch",
        "The workspace or creating run belongs to another Session.",
    )


def _workspace_revision_conflict() -> AgentException:
    return _err(
        "workspace.revision_conflict",
        "The workspace manifest changed before this update could be applied.",
    )


def _workspace_active_conflict() -> AgentException:
    return _err(
        "workspace.active_conflict",
        "The active workspace changed before this update could be applied.",
    )


def _changeset_exists() -> AgentException:
    return _err("runtime.duplicate_changeset", "The ChangeSet already exists.")


def _approval_exists() -> AgentException:
    return _err("runtime.duplicate_approval", "An approval already exists for this ChangeSet.")


def _receipt_conflict() -> AgentException:
    return _err(
        "runtime.receipt_conflict",
        "A different receipt already exists for this ChangeSet.",
    )


def _record_corrupt() -> AgentException:
    return _err(
        "runtime.record_corrupt",
        "A persisted record failed its integrity check.",
        category=ErrorCategory.INTERNAL_INVARIANT,
    )


def _approval_identity_mismatch() -> AgentException:
    return _err(
        "approval.identity_mismatch",
        "The approval does not match the persisted approval identity.",
    )


def _approval_digest_mismatch() -> AgentException:
    return _err(
        "approval.digest_mismatch",
        "The approval does not match the canonical ChangeSet digest.",
    )


def _approval_expired() -> AgentException:
    return _err("approval.expired", "The approval has expired.")


def _approval_already_consumed() -> AgentException:
    return _err("approval.already_consumed", "The approval is not available for consumption.")


def _approval_required() -> AgentException:
    return _err(
        "approval.required",
        "The ChangeSet has not been proposed for approval.",
    )


def _changeset_proposal_conflict() -> AgentException:
    return _err(
        "changeset.proposal_conflict",
        "The ChangeSet conflicts with an existing proposal.",
    )


def _approval_binding_unavailable() -> AgentException:
    return _err(
        "bridge.capability_unavailable",
        "The current scene binding is not available.",
    )


def _approval_binding_mismatch() -> AgentException:
    return _err(
        "changeset.stale",
        "The approved scene binding no longer matches the ChangeSet.",
        category=ErrorCategory.STALE_SCENE,
    )


def _cas_conflict() -> AgentException:
    return _err(
        "runtime.state_conflict",
        "The record changed before this update could be applied.",
    )


def _invalid_changeset_transition(current: ChangeSetState, target: ChangeSetState) -> AgentException:
    return _err(
        "runtime.invalid_changeset_transition",
        f"ChangeSet cannot transition {current.value} -> {target.value}.",
    )


def _invalid_approval_transition(
    current: ApprovalDecision, target: ApprovalDecision
) -> AgentException:
    return _err(
        "approval.invalid_transition",
        f"Approval cannot transition {current.value} -> {target.value}.",
    )


def _require_id_value(value: object, kind: IdKind) -> str:
    if type(value) is not str:
        raise TypeError(f"{kind.value}_ id must be a string")
    return require_id(value, kind)


def _require_utc_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# SQL column lists
# --------------------------------------------------------------------------

_WORKSPACE_COLUMNS = (
    "workspace_id, session_id, instance_id, scene_epoch, revision, "
    "created_by_run, updated_at, digest, payload_json, schema_version"
)
_CHANGESET_COLUMNS = (
    "change_id, session_id, run_id, workspace_id, digest, state, created_at, "
    "payload_json, schema_version"
)
_APPROVAL_COLUMNS = (
    "approval_id, change_id, changeset_digest, decision, decided_by, "
    "requested_at, decided_at, expires_at, approved_instance_id, "
    "approved_scene_epoch, updated_at, digest, payload_json, schema_version"
)
_RECEIPT_COLUMNS = (
    "change_id, status, instance_id, scene_epoch, digest, payload_json, "
    "completed_at, schema_version"
)


def _approval_updated_at(approval: ApprovalRecord) -> str:
    moment = approval.decided_at if approval.decided_at is not None else approval.requested_at
    return moment.isoformat()


def _verify_payload(payload_json: str, stored_digest: str) -> None:
    actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if actual != stored_digest:
        raise _record_corrupt()


def _manifest_from_row(row) -> WorkspaceManifest:
    _verify_payload(row["payload_json"], row["digest"])
    manifest = _decode_manifest(_loads_canonical(row["payload_json"]))
    if row["payload_json"] != canonical_json_dumps(manifest.to_dict()):
        raise _record_corrupt()
    if (
        row["workspace_id"] != manifest.workspace_id
        or row["session_id"] != manifest.session_id
        or row["instance_id"] != manifest.instance_id
        or row["scene_epoch"] != manifest.scene_epoch
        or row["revision"] != manifest.revision
        or row["created_by_run"] != manifest.created_by_run
        or row["updated_at"] != manifest.updated_at.isoformat()
        or row["schema_version"] != manifest.schema_version
    ):
        raise _record_corrupt()
    return manifest


def _workspace_state_from_row(row) -> WorkspaceStateRecord:
    return WorkspaceStateRecord(
        session_id=row["session_id"],
        active_workspace_id=row["active_workspace_id"],
        state_revision=row["state_revision"],
        updated_at=_decode_dt(row["updated_at"], "WorkspaceStateRecord.updated_at"),
    )


def _require_revision(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be an exact string")
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _same_bind_identity(old: WorkspaceManifest, new: WorkspaceManifest) -> bool:
    if (
        old.workspace_id != new.workspace_id
        or old.session_id != new.session_id
        or old.created_by_run != new.created_by_run
    ):
        return False
    old_nodes = {node.node_id: node for node in old.nodes}
    new_nodes = {node.node_id: node for node in new.nodes}
    if set(old_nodes) != set(new_nodes):
        return False
    if {root.node_id for root in old.roots} != {root.node_id for root in new.roots}:
        return False
    for node_id, before in old_nodes.items():
        after = new_nodes[node_id]
        if (
            before.node_type != after.node_type
            or before.capability != after.capability
            or before.role != after.role
        ):
            return False
    return True


async def _fetch_workspace_row(conn, workspace_id: str):
    cursor = await conn.execute(
        f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces WHERE workspace_id = ?",
        (workspace_id,),
    )
    return await cursor.fetchone()


async def _fetch_workspace_state_row(conn, session_id: str):
    cursor = await conn.execute(
        "SELECT session_id, active_workspace_id, state_revision, updated_at "
        "FROM session_workspace_state WHERE session_id = ?",
        (session_id,),
    )
    return await cursor.fetchone()


def _check_approval_row_identity(row, approval: ApprovalRecord) -> None:
    """Verify the denormalized approval columns match the decoded payload DTO.

    The ``approvals`` row denormalizes four identity fields — ``approval_id``,
    ``change_id``, ``changeset_digest``, ``decision`` — that also live inside the
    canonical ``payload_json``. The storage digest alone cannot detect a direct
    ``UPDATE approvals SET approval_id = ...`` that swaps one of these columns
    for another valid value while leaving ``payload_json``/``digest`` untouched:
    the payload still decodes and its digest still verifies, yet the row and the
    decoded DTO disagree about whose approval this is. Requiring the four
    columns to match the decoded DTO closes that gap and fails closed as
    ``runtime.record_corrupt``.
    """
    if (
        row["approval_id"] != approval.approval_id
        or row["change_id"] != approval.change_id
        or row["changeset_digest"] != approval.changeset_digest
        or row["decision"] != approval.decision.value
    ):
        raise _record_corrupt()


def _coerce_states(states: object) -> list[ChangeSetState]:
    if isinstance(states, ChangeSetState):
        raise TypeError("states must be an iterable of ChangeSetState, not a single value")
    if isinstance(states, str):
        raise TypeError("states must be an iterable of ChangeSetState, not a string")
    if not isinstance(states, Iterable):
        raise TypeError("states must be an iterable of ChangeSetState")
    out: list[ChangeSetState] = []
    for item in states:
        if type(item) is not ChangeSetState:
            raise TypeError("states must contain only ChangeSetState values")
        out.append(item)
    if not out:
        raise ValueError("states must contain at least one ChangeSetState")
    return out


# --------------------------------------------------------------------------
# repository
# --------------------------------------------------------------------------


class ChangeSetRepository:
    """Strict, transaction-safe persistence for typed ChangeSet records.

    Plain persistence methods never emit events. The focused combined Workspace
    lifecycle and ChangeSet approval primitives use the injected EventStore's
    caller-owned append helper so coupled state and durable events commit in the
    same transaction. Every mutation performs its checks and writes inside one
    :meth:`RuntimeDatabase.write_transaction`, so concurrent callers serialize
    and a failed transaction leaves all affected rows unchanged.
    """

    def __init__(
        self,
        database: RuntimeDatabase,
        *,
        events: "EventStore | None" = None,
    ) -> None:
        self._database = database
        # Optional EventStore used by the combined Workspace lifecycle and
        # proposal/decision primitives so matching durable events commit in the
        # SAME transaction as their state mutation. Plain insert/get/update/
        # consume primitives never touch the events table.
        self._events = events

    # --- workspace manifests ---------------------------------------------

    async def insert_workspace(self, manifest: WorkspaceManifest) -> WorkspaceManifest:
        if type(manifest) is not WorkspaceManifest:
            raise TypeError("manifest must be an exact WorkspaceManifest")
        payload = canonical_json_dumps(manifest.to_dict())
        digest = _storage_digest(manifest)
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, manifest.session_id)
            await self._require_run(conn, manifest.created_by_run)
            existing = await conn.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?",
                (manifest.workspace_id,),
            )
            if await existing.fetchone() is not None:
                raise _workspace_exists()
            await conn.execute(
                f"INSERT INTO workspaces({_WORKSPACE_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.workspace_id,
                    manifest.session_id,
                    manifest.instance_id,
                    manifest.scene_epoch,
                    manifest.revision,
                    manifest.created_by_run,
                    manifest.updated_at.isoformat(),
                    digest,
                    payload,
                    manifest.schema_version,
                ),
            )
        return manifest

    async def get_workspace(self, workspace_id: str) -> WorkspaceManifest:
        wid = _require_id_value(workspace_id, IdKind.WORKSPACE)
        row = await self._database.fetchone(
            f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces WHERE workspace_id = ?", (wid,)
        )
        if row is None:
            raise _workspace_not_found()
        return _manifest_from_row(row)

    async def list_workspaces(self, session_id: str) -> tuple[WorkspaceManifest, ...]:
        sid = _require_id_value(session_id, IdKind.SESSION)
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, sid)
            cursor = await conn.execute(
                f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces "
                "WHERE session_id = ? ORDER BY workspace_id",
                (sid,),
            )
            rows = list(await cursor.fetchall())
        return tuple(_manifest_from_row(row) for row in rows)

    async def create_workspace_lifecycle(
        self, manifest: WorkspaceManifest
    ) -> WorkspaceMutationResult:
        """Atomically create, initially activate, and event a Workspace."""
        if type(manifest) is not WorkspaceManifest:
            raise TypeError("manifest must be an exact WorkspaceManifest")
        payload = canonical_json_dumps(manifest.to_dict())
        digest = _storage_digest(manifest)
        events_store = self._events
        if events_store is None:
            raise TypeError(
                "ChangeSetRepository.create_workspace_lifecycle requires an EventStore"
            )
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, manifest.session_id)
            run_cursor = await conn.execute(
                "SELECT session_id FROM runs WHERE run_id = ?",
                (manifest.created_by_run,),
            )
            run_row = await run_cursor.fetchone()
            if run_row is None:
                raise _run_not_found()
            if run_row["session_id"] != manifest.session_id:
                raise _workspace_session_mismatch()

            existing_row = await _fetch_workspace_row(conn, manifest.workspace_id)
            state_row = await _fetch_workspace_state_row(conn, manifest.session_id)
            if existing_row is not None:
                existing = _manifest_from_row(existing_row)
                if existing.session_id != manifest.session_id:
                    raise _workspace_session_mismatch()
                if existing.revision != manifest.revision:
                    raise _workspace_identity_conflict()
                if state_row is None:
                    raise _workspace_active_conflict()
                state = _workspace_state_from_row(state_row)
                if state.active_workspace_id != manifest.workspace_id:
                    raise _workspace_active_conflict()
                return WorkspaceMutationResult(existing, state, (), False)
            if state_row is not None:
                raise _workspace_active_conflict()

            await conn.execute(
                f"INSERT INTO workspaces({_WORKSPACE_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.workspace_id,
                    manifest.session_id,
                    manifest.instance_id,
                    manifest.scene_epoch,
                    manifest.revision,
                    manifest.created_by_run,
                    manifest.updated_at.isoformat(),
                    digest,
                    payload,
                    manifest.schema_version,
                ),
            )
            await conn.execute(
                "INSERT INTO session_workspace_state(session_id, "
                "active_workspace_id, state_revision, updated_at) VALUES (?, ?, 1, ?)",
                (
                    manifest.session_id,
                    manifest.workspace_id,
                    manifest.updated_at.isoformat(),
                ),
            )
            state = WorkspaceStateRecord(
                manifest.session_id,
                manifest.workspace_id,
                1,
                manifest.updated_at,
            )
            event = await events_store._append_conn(
                conn,
                session_id=manifest.session_id,
                run_id=manifest.created_by_run,
                event_type="workspace.created",
                payload=_workspace_event_payload(manifest, state, active=True),
                retention_class=RetentionClass.DURABLE,
            )
            return WorkspaceMutationResult(manifest, state, (event,), True)

    async def bind_workspace(
        self,
        manifest: WorkspaceManifest,
        *,
        expected_manifest_revision: str,
    ) -> WorkspaceMutationResult:
        """Atomically compare/update a manifest and append ``workspace.bound``."""
        if type(manifest) is not WorkspaceManifest:
            raise TypeError("manifest must be an exact WorkspaceManifest")
        expected = _require_revision(
            expected_manifest_revision, "expected_manifest_revision"
        )
        payload = canonical_json_dumps(manifest.to_dict())
        digest = _storage_digest(manifest)
        events_store = self._events
        if events_store is None:
            raise TypeError("ChangeSetRepository.bind_workspace requires an EventStore")
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, manifest.session_id)
            row = await _fetch_workspace_row(conn, manifest.workspace_id)
            if row is None:
                raise _workspace_not_found()
            stored = _manifest_from_row(row)
            if stored.session_id != manifest.session_id:
                raise _workspace_session_mismatch()
            run_cursor = await conn.execute(
                "SELECT session_id FROM runs WHERE run_id = ?",
                (stored.created_by_run,),
            )
            run_row = await run_cursor.fetchone()
            if run_row is None:
                raise _run_not_found()
            if run_row["session_id"] != stored.session_id:
                raise _workspace_session_mismatch()
            if stored.revision != expected:
                raise _workspace_revision_conflict()
            if not _same_bind_identity(stored, manifest):
                raise _workspace_identity_conflict()
            state_row = await _fetch_workspace_state_row(conn, manifest.session_id)
            state = (
                None if state_row is None else _workspace_state_from_row(state_row)
            )
            if stored.revision == manifest.revision:
                return WorkspaceMutationResult(stored, state, (), False)
            await conn.execute(
                "UPDATE workspaces SET instance_id = ?, scene_epoch = ?, revision = ?, "
                "updated_at = ?, digest = ?, payload_json = ?, schema_version = ? "
                "WHERE workspace_id = ? AND revision = ?",
                (
                    manifest.instance_id,
                    manifest.scene_epoch,
                    manifest.revision,
                    manifest.updated_at.isoformat(),
                    digest,
                    payload,
                    manifest.schema_version,
                    manifest.workspace_id,
                    expected,
                ),
            )
            active = state is not None and state.active_workspace_id == manifest.workspace_id
            event = await events_store._append_conn(
                conn,
                session_id=manifest.session_id,
                run_id=manifest.created_by_run,
                event_type="workspace.bound",
                payload=_workspace_event_payload(
                    manifest,
                    state,
                    active=active,
                    old_revision=stored.revision,
                ),
                retention_class=RetentionClass.DURABLE,
            )
            return WorkspaceMutationResult(manifest, state, (event,), True)

    async def switch_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_active_workspace_id: str | None,
        expected_manifest_revision: str,
        updated_at: datetime,
    ) -> WorkspaceMutationResult:
        """Atomically compare/switch the Session active Workspace pointer."""
        sid = _require_id_value(session_id, IdKind.SESSION)
        wid = _require_id_value(workspace_id, IdKind.WORKSPACE)
        if expected_active_workspace_id is None:
            expected_active = None
        else:
            expected_active = _require_id_value(
                expected_active_workspace_id, IdKind.WORKSPACE
            )
        expected_revision = _require_revision(
            expected_manifest_revision, "expected_manifest_revision"
        )
        moment = _require_utc_datetime(updated_at, "updated_at")
        events_store = self._events
        if events_store is None:
            raise TypeError("ChangeSetRepository.switch_workspace requires an EventStore")
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, sid)
            row = await _fetch_workspace_row(conn, wid)
            if row is None:
                raise _workspace_not_found()
            manifest = _manifest_from_row(row)
            if manifest.session_id != sid:
                raise _workspace_session_mismatch()
            if manifest.revision != expected_revision:
                raise _workspace_revision_conflict()
            run_cursor = await conn.execute(
                "SELECT session_id FROM runs WHERE run_id = ?",
                (manifest.created_by_run,),
            )
            run_row = await run_cursor.fetchone()
            if run_row is None:
                raise _run_not_found()
            if run_row["session_id"] != sid:
                raise _workspace_session_mismatch()
            state_row = await _fetch_workspace_state_row(conn, sid)
            if state_row is None:
                if expected_active is not None:
                    raise _workspace_active_conflict()
                state = WorkspaceStateRecord(sid, wid, 1, moment)
                await conn.execute(
                    "INSERT INTO session_workspace_state(session_id, "
                    "active_workspace_id, state_revision, updated_at) "
                    "VALUES (?, ?, 1, ?)",
                    (sid, wid, moment.isoformat()),
                )
                previous = None
            else:
                before = _workspace_state_from_row(state_row)
                if before.active_workspace_id != expected_active:
                    raise _workspace_active_conflict()
                if before.active_workspace_id == wid:
                    return WorkspaceMutationResult(manifest, before, (), False)
                previous = before.active_workspace_id
                state = WorkspaceStateRecord(
                    sid, wid, before.state_revision + 1, moment
                )
                await conn.execute(
                    "UPDATE session_workspace_state SET active_workspace_id = ?, "
                    "state_revision = ?, updated_at = ? WHERE session_id = ?",
                    (wid, state.state_revision, moment.isoformat(), sid),
                )
            event = await events_store._append_conn(
                conn,
                session_id=sid,
                run_id=None,
                event_type="workspace.updated",
                payload=_workspace_event_payload(
                    manifest, state, active=True, previous_workspace_id=previous
                ),
                retention_class=RetentionClass.DURABLE,
            )
            return WorkspaceMutationResult(manifest, state, (event,), True)

    async def get_active_workspace(
        self, session_id: str
    ) -> WorkspaceStateRecord | None:
        sid = _require_id_value(session_id, IdKind.SESSION)
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, sid)
            row = await _fetch_workspace_state_row(conn, sid)
        return None if row is None else _workspace_state_from_row(row)

    async def list_workspace_state(
        self, session_id: str
    ) -> tuple[tuple[WorkspaceManifest, ...], WorkspaceStateRecord | None]:
        sid = _require_id_value(session_id, IdKind.SESSION)
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, sid)
            cursor = await conn.execute(
                f"SELECT {_WORKSPACE_COLUMNS} FROM workspaces "
                "WHERE session_id = ? ORDER BY workspace_id",
                (sid,),
            )
            manifests = tuple(_manifest_from_row(row) for row in await cursor.fetchall())
            state_row = await _fetch_workspace_state_row(conn, sid)
            state = (
                None if state_row is None else _workspace_state_from_row(state_row)
            )
            if state is not None and state.active_workspace_id not in {
                manifest.workspace_id for manifest in manifests
            }:
                raise _record_corrupt()
            return manifests, state

    # --- changesets ------------------------------------------------------

    async def insert_changeset(
        self,
        changeset: ChangeSet,
        *,
        state: ChangeSetState = ChangeSetState.PROPOSED,
    ) -> StoredChangeSet:
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        if type(state) is not ChangeSetState:
            raise TypeError("state must be an exact ChangeSetState")
        payload = canonical_json_dumps(changeset.to_dict())
        digest = _storage_digest(changeset)
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, changeset.session_id)
            await self._require_run(conn, changeset.run_id)
            existing = await conn.execute(
                "SELECT 1 FROM changesets WHERE change_id = ?", (changeset.change_id,)
            )
            if await existing.fetchone() is not None:
                raise _changeset_exists()
            await conn.execute(
                f"INSERT INTO changesets({_CHANGESET_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    changeset.change_id,
                    changeset.session_id,
                    changeset.run_id,
                    changeset.workspace_id,
                    digest,
                    state.value,
                    changeset.created_at.isoformat(),
                    payload,
                    changeset.schema_version,
                ),
            )
        return StoredChangeSet(changeset, state)

    async def get_changeset(self, change_id: str) -> StoredChangeSet:
        cid = _require_id_value(change_id, IdKind.CHANGE)
        row = await self._database.fetchone(
            f"SELECT {_CHANGESET_COLUMNS} FROM changesets WHERE change_id = ?", (cid,)
        )
        if row is None:
            raise _changeset_not_found()
        _verify_payload(row["payload_json"], row["digest"])
        return StoredChangeSet(
            _decode_changeset(_loads_canonical(row["payload_json"])),
            ChangeSetState(row["state"]),
        )

    async def list_changesets(self, session_id: str) -> tuple[StoredChangeSet, ...]:
        sid = _require_id_value(session_id, IdKind.SESSION)
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, sid)
            cursor = await conn.execute(
                f"SELECT {_CHANGESET_COLUMNS} FROM changesets "
                "WHERE session_id = ? ORDER BY change_id",
                (sid,),
            )
            rows = list(await cursor.fetchall())
        return tuple(_stored_from_row(row) for row in rows)

    async def list_changeset_views(
        self, session_id: str, *, limit: int
    ) -> tuple[ChangeSetView, ...]:
        """Return newest bounded ChangeSet views from one consistent read."""
        sid = _require_id_value(session_id, IdKind.SESSION)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit must be an exact integer in 1..50")
        async with self._database.write_transaction() as conn:
            await self._require_session(conn, sid)
            cursor = await conn.execute(
                f"SELECT {_CHANGESET_COLUMNS} FROM changesets "
                "WHERE session_id = ? "
                "ORDER BY created_at DESC, change_id DESC LIMIT ?",
                (sid, limit),
            )
            rows = list(await cursor.fetchall())
            views: list[ChangeSetView] = []
            for row in rows:
                stored = _stored_from_row(row)
                change_id = stored.changeset.change_id
                if (
                    change_id != row["change_id"]
                    or stored.changeset.session_id != sid
                ):
                    raise _record_corrupt()
                approval = await _fetch_approval_record(conn, change_id)
                receipt = await _fetch_receipt_record(conn, change_id)
                if receipt is not None and receipt.change_id != change_id:
                    raise _record_corrupt()
                views.append(
                    ChangeSetView(
                        stored=stored,
                        approval=approval,
                        receipt=receipt,
                    )
                )
        return tuple(views)

    async def transition_changeset(
        self,
        change_id: str,
        *,
        from_state: ChangeSetState,
        to_state: ChangeSetState,
    ) -> StoredChangeSet:
        if type(from_state) is not ChangeSetState:
            raise TypeError("from_state must be an exact ChangeSetState")
        if type(to_state) is not ChangeSetState:
            raise TypeError("to_state must be an exact ChangeSetState")
        cid = _require_id_value(change_id, IdKind.CHANGE)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT payload_json, digest, state FROM changesets WHERE change_id = ?",
                (cid,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise _changeset_not_found()
            current = ChangeSetState(row["state"])
            if current is not from_state:
                raise _cas_conflict()
            if to_state not in _TRANSITIONS[current]:
                raise _invalid_changeset_transition(current, to_state)
            await conn.execute(
                "UPDATE changesets SET state = ? WHERE change_id = ?",
                (to_state.value, cid),
            )
            _verify_payload(row["payload_json"], row["digest"])
            changeset = _decode_changeset(_loads_canonical(row["payload_json"]))
        return StoredChangeSet(changeset, to_state)

    async def nonterminal_changesets(self) -> tuple[StoredChangeSet, ...]:
        return await self.changesets_in_states(NONTERMINAL_CHANGESET_STATES)

    async def changesets_in_states(
        self, states: Iterable[ChangeSetState]
    ) -> tuple[StoredChangeSet, ...]:
        state_list = _coerce_states(states)
        placeholders = ",".join("?" for _ in state_list)
        rows = await self._database.fetchall(
            f"SELECT {_CHANGESET_COLUMNS} FROM changesets "
            f"WHERE state IN ({placeholders}) ORDER BY change_id",
            tuple(s.value for s in state_list),
        )
        return tuple(_stored_from_row(row) for row in rows)

    # --- approvals -------------------------------------------------------

    async def insert_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        if type(approval) is not ApprovalRecord:
            raise TypeError("approval must be an exact ApprovalRecord")
        payload = canonical_json_dumps(approval.to_dict())
        digest = _storage_digest(approval)
        async with self._database.write_transaction() as conn:
            cs_cursor = await conn.execute(
                "SELECT digest FROM changesets WHERE change_id = ?",
                (approval.change_id,),
            )
            cs_row = await cs_cursor.fetchone()
            if cs_row is None:
                raise _changeset_not_found()
            if approval.changeset_digest != cs_row["digest"]:
                raise _approval_digest_mismatch()
            existing = await conn.execute(
                "SELECT 1 FROM approvals WHERE change_id = ?", (approval.change_id,)
            )
            if await existing.fetchone() is not None:
                raise _approval_exists()
            await conn.execute(
                f"INSERT INTO approvals({_APPROVAL_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.approval_id,
                    approval.change_id,
                    approval.changeset_digest,
                    approval.decision.value,
                    approval.decided_by,
                    approval.requested_at.isoformat(),
                    approval.decided_at.isoformat() if approval.decided_at is not None else None,
                    approval.expires_at.isoformat(),
                    approval.approved_instance_id,
                    approval.approved_scene_epoch,
                    _approval_updated_at(approval),
                    digest,
                    payload,
                    approval.schema_version,
                ),
            )
        return approval

    async def get_approval(self, change_id: str) -> ApprovalRecord:
        cid = _require_id_value(change_id, IdKind.CHANGE)
        row = await self._database.fetchone(
            f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE change_id = ?", (cid,)
        )
        if row is None:
            raise _approval_not_found()
        _verify_payload(row["payload_json"], row["digest"])
        approval = _decode_approval(_loads_canonical(row["payload_json"]))
        _check_approval_row_identity(row, approval)
        return approval

    async def update_approval(
        self,
        approval: ApprovalRecord,
        *,
        expected_decision: ApprovalDecision,
        expected_changeset_digest: str,
    ) -> ApprovalRecord:
        if type(approval) is not ApprovalRecord:
            raise TypeError("approval must be an exact ApprovalRecord")
        if type(expected_decision) is not ApprovalDecision:
            raise TypeError("expected_decision must be an exact ApprovalDecision")
        if type(expected_changeset_digest) is not str:
            raise TypeError("expected_changeset_digest must be a string")
        payload = canonical_json_dumps(approval.to_dict())
        digest = _storage_digest(approval)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT approval_id, decision FROM approvals WHERE change_id = ?",
                (approval.change_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise _approval_not_found()
            # approval_id is the immutable row identity: an update that would
            # replace it is rejected outright, before any state mutation, so the
            # column and payload_json can never disagree about whose approval
            # this is.
            if approval.approval_id != row["approval_id"]:
                raise _approval_identity_mismatch()
            current_decision = ApprovalDecision(row["decision"])
            if current_decision is not expected_decision:
                raise _cas_conflict()
            if approval.decision not in _APPROVAL_TRANSITIONS[current_decision]:
                raise _invalid_approval_transition(current_decision, approval.decision)
            cs_cursor = await conn.execute(
                "SELECT digest FROM changesets WHERE change_id = ?",
                (approval.change_id,),
            )
            cs_row = await cs_cursor.fetchone()
            if cs_row is None:
                raise _changeset_not_found()
            canonical_digest = cs_row["digest"]
            if expected_changeset_digest != canonical_digest:
                raise _approval_digest_mismatch()
            if approval.changeset_digest != canonical_digest:
                raise _approval_digest_mismatch()
            await conn.execute(
                "UPDATE approvals SET changeset_digest = ?, decision = ?, decided_by = ?, "
                "requested_at = ?, decided_at = ?, expires_at = ?, "
                "approved_instance_id = ?, approved_scene_epoch = ?, updated_at = ?, "
                "digest = ?, payload_json = ? WHERE change_id = ?",
                (
                    approval.changeset_digest,
                    approval.decision.value,
                    approval.decided_by,
                    approval.requested_at.isoformat(),
                    approval.decided_at.isoformat() if approval.decided_at is not None else None,
                    approval.expires_at.isoformat(),
                    approval.approved_instance_id,
                    approval.approved_scene_epoch,
                    _approval_updated_at(approval),
                    digest,
                    payload,
                    approval.change_id,
                ),
            )
        return approval

    async def consume_approval(
        self, change_id: str, *, now: datetime
    ) -> ApprovalRecord:
        cid = _require_id_value(change_id, IdKind.CHANGE)
        now_utc = _require_utc_datetime(now, "now")
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT approval_id, change_id, changeset_digest, decision, "
                "expires_at, payload_json, digest "
                "FROM approvals WHERE change_id = ?",
                (cid,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise _approval_not_found()
            # Integrity-first, like get_approval/_fetch_approval_record: verify
            # the payload, decode the canonical record, and confirm the
            # denormalized identity columns (approval_id/change_id/
            # changeset_digest/decision) agree with it BEFORE any expiry/digest/
            # state handling. consume_approval is the single-use Apply boundary,
            # so a tampered identity column with an otherwise intact payload/
            # digest must fail closed as runtime.record_corrupt and leave the row
            # untouched rather than being consumed.
            _verify_payload(row["payload_json"], row["digest"])
            original = _decode_approval(_loads_canonical(row["payload_json"]))
            _check_approval_row_identity(row, original)
            current_decision = ApprovalDecision(row["decision"])
            if current_decision is not ApprovalDecision.APPROVED:
                raise _approval_already_consumed()
            expires_at = datetime.fromisoformat(row["expires_at"])
            if now_utc > expires_at:
                raise _approval_expired()
            cs_cursor = await conn.execute(
                "SELECT digest FROM changesets WHERE change_id = ?", (cid,)
            )
            cs_row = await cs_cursor.fetchone()
            if cs_row is None:
                raise _changeset_not_found()
            if row["changeset_digest"] != cs_row["digest"]:
                raise _approval_digest_mismatch()
            consumed = ApprovalRecord(
                schema_version=original.schema_version,
                approval_id=original.approval_id,
                change_id=original.change_id,
                changeset_digest=original.changeset_digest,
                decision=ApprovalDecision.CONSUMED,
                decided_by="local_user",
                requested_at=original.requested_at,
                decided_at=now_utc,
                expires_at=original.expires_at,
                approved_instance_id=original.approved_instance_id,
                approved_scene_epoch=original.approved_scene_epoch,
            )
            consumed_payload = canonical_json_dumps(consumed.to_dict())
            consumed_digest = _storage_digest(consumed)
            await conn.execute(
                "UPDATE approvals SET decision = ?, decided_by = ?, decided_at = ?, "
                "updated_at = ?, digest = ?, payload_json = ? WHERE change_id = ?",
                (
                    ApprovalDecision.CONSUMED.value,
                    "local_user",
                    now_utc.isoformat(),
                    now_utc.isoformat(),
                    consumed_digest,
                    consumed_payload,
                    cid,
                ),
            )
        return consumed

    # --- receipts --------------------------------------------------------

    async def insert_receipt(self, receipt: ChangeReceipt) -> ChangeReceipt:
        if type(receipt) is not ChangeReceipt:
            raise TypeError("receipt must be an exact ChangeReceipt")
        payload = canonical_json_dumps(receipt.to_dict())
        digest = _storage_digest(receipt)
        async with self._database.write_transaction() as conn:
            cs_cursor = await conn.execute(
                "SELECT 1 FROM changesets WHERE change_id = ?", (receipt.change_id,)
            )
            if await cs_cursor.fetchone() is None:
                raise _changeset_not_found()
            existing = await conn.execute(
                "SELECT digest FROM change_receipts WHERE change_id = ?",
                (receipt.change_id,),
            )
            existing_row = await existing.fetchone()
            if existing_row is not None:
                if existing_row["digest"] == digest:
                    return receipt  # idempotent: identical receipt already persisted
                raise _receipt_conflict()
            await conn.execute(
                f"INSERT INTO change_receipts({_RECEIPT_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.change_id,
                    receipt.status.value,
                    receipt.instance_id,
                    receipt.scene_epoch,
                    digest,
                    payload,
                    receipt.completed_at.isoformat(),
                    receipt.schema_version,
                ),
            )
        return receipt

    async def get_receipt(self, change_id: str) -> ChangeReceipt:
        cid = _require_id_value(change_id, IdKind.CHANGE)
        row = await self._database.fetchone(
            f"SELECT {_RECEIPT_COLUMNS} FROM change_receipts WHERE change_id = ?", (cid,)
        )
        if row is None:
            raise _receipt_not_found()
        _verify_payload(row["payload_json"], row["digest"])
        return _decode_receipt(_loads_canonical(row["payload_json"]))

    async def list_receipts(self) -> tuple[ChangeReceipt, ...]:
        rows = await self._database.fetchall(
            f"SELECT {_RECEIPT_COLUMNS} FROM change_receipts ORDER BY change_id"
        )
        return tuple(
            _decode_row(row, "payload_json", "digest", _decode_receipt) for row in rows
        )

    # --- combined transactional proposal/decision primitives -------------
    #
    # These mutate the ChangeSet/Approval rows AND append the matching durable
    # events inside ONE write_transaction (sharing the connection-scoped
    # EventStore append helper), so a coupled state change and its event either
    # commit together or roll back together. They never notify subscribers;
    # that is the service layer's post-commit responsibility.

    async def propose(
        self,
        changeset: ChangeSet,
        approval: ApprovalRecord,
    ) -> ProposalResult:
        """Atomically persist a proposed ChangeSet and request its approval.

        One transaction inserts the ChangeSet in ``Proposed``, the exact-digest
        ``Pending`` ApprovalRecord, transitions the ChangeSet to
        ``AwaitingApproval``, and appends the bounded ``changeset.proposed`` and
        ``approval.requested`` events. Re-proposing the same ``change_id`` with
        an identical digest is idempotent (no new rows or events); the same
        ``change_id`` with a different digest is rejected as a proposal
        conflict.
        """
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        if type(approval) is not ApprovalRecord:
            raise TypeError("approval must be an exact ApprovalRecord")
        if approval.decision is not ApprovalDecision.PENDING:
            raise ValueError("proposal approval must be Pending")
        if approval.change_id != changeset.change_id:
            raise ValueError("approval must reference the proposed ChangeSet")
        events_store = self._events
        if events_store is None:
            raise TypeError("ChangeSetRepository.propose requires an EventStore")
        digest = _storage_digest(changeset)
        if approval.changeset_digest != digest:
            raise _approval_digest_mismatch()
        payload = canonical_json_dumps(changeset.to_dict())
        approval_payload = canonical_json_dumps(approval.to_dict())
        approval_digest = _storage_digest(approval)
        cid = changeset.change_id
        async with self._database.write_transaction() as conn:
            existing = await _fetch_stored_changeset(conn, cid)
            if existing is not None:
                # Idempotent re-proposal: identical digest is a no-op; a
                # different digest for the same change_id is a conflict.
                if existing.changeset.digest != digest:
                    raise _changeset_proposal_conflict()
                existing_approval = await _fetch_approval_record(conn, cid)
                if existing_approval is None:
                    raise _approval_required()
                return ProposalResult(
                    existing.changeset, existing.state, existing_approval, ()
                )
            await self._require_session(conn, changeset.session_id)
            await self._require_run(conn, changeset.run_id)
            await conn.execute(
                f"INSERT INTO changesets({_CHANGESET_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    changeset.session_id,
                    changeset.run_id,
                    changeset.workspace_id,
                    digest,
                    ChangeSetState.PROPOSED.value,
                    changeset.created_at.isoformat(),
                    payload,
                    changeset.schema_version,
                ),
            )
            duplicate_approval = await (
                await conn.execute(
                    "SELECT 1 FROM approvals WHERE change_id = ?", (cid,)
                )
            ).fetchone()
            if duplicate_approval is not None:
                raise _approval_exists()
            await conn.execute(
                f"INSERT INTO approvals({_APPROVAL_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.approval_id,
                    cid,
                    approval.changeset_digest,
                    approval.decision.value,
                    approval.decided_by,
                    approval.requested_at.isoformat(),
                    approval.decided_at.isoformat()
                    if approval.decided_at is not None
                    else None,
                    approval.expires_at.isoformat(),
                    approval.approved_instance_id,
                    approval.approved_scene_epoch,
                    _approval_updated_at(approval),
                    approval_digest,
                    approval_payload,
                    approval.schema_version,
                ),
            )
            await self._transition_state_conn(
                conn, cid, ChangeSetState.PROPOSED, ChangeSetState.AWAITING_APPROVAL
            )
            sid = changeset.session_id
            rid = changeset.run_id
            proposed_event = await events_store._append_conn(
                conn,
                session_id=sid,
                run_id=rid,
                event_type="changeset.proposed",
                payload=_proposed_payload(changeset, ChangeSetState.AWAITING_APPROVAL),
                retention_class=RetentionClass.DURABLE,
            )
            requested_event = await events_store._append_conn(
                conn,
                session_id=sid,
                run_id=rid,
                event_type="approval.requested",
                payload=_requested_payload(approval),
                retention_class=RetentionClass.DURABLE,
            )
            return ProposalResult(
                changeset,
                ChangeSetState.AWAITING_APPROVAL,
                approval,
                (proposed_event, requested_event),
            )

    async def decide(
        self,
        change_id: str,
        *,
        changeset_digest: str,
        decision: ApprovalDecision,
        now: datetime,
        approved_binding: SceneBinding | None = None,
    ) -> DecisionResult:
        """Atomically approve, reject, or expire a pending approval.

        Requires the persisted ChangeSet to be ``AwaitingApproval`` with a
        ``Pending`` approval and the exact canonical digest, then in one
        transaction transitions the approval and the ChangeSet together and
        appends the matching durable events. An expired pending approval
        transitions to ``Expired`` exactly once and is returned as the
        ``Expired`` outcome (the service surfaces it as ``approval.expired``
        after notifying subscribers). A consumed, rejected, or expired record
        cannot be decided again.
        """
        if type(changeset_digest) is not str:
            raise TypeError("changeset_digest must be a string")
        if type(decision) is not ApprovalDecision:
            raise TypeError("decision must be an exact ApprovalDecision")
        if decision not in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED):
            raise ValueError("decision must be Approved or Rejected")
        # APPROVED requires an exact SceneBinding; capture the binding scalars
        # as precise locals here so the later ApprovalRecord construction (in a
        # separate control-flow branch) does not re-touch the union type. REJECTED
        # leaves them unset and never reads them.
        approved_instance_id: str | None = None
        approved_scene_epoch: int | None = None
        if decision is ApprovalDecision.APPROVED:
            if type(approved_binding) is not SceneBinding:
                raise _approval_binding_unavailable()
            approved_instance_id = approved_binding.instance_id
            approved_scene_epoch = approved_binding.scene_epoch
        events_store = self._events
        if events_store is None:
            raise TypeError("ChangeSetRepository.decide requires an EventStore")
        cid = _require_id_value(change_id, IdKind.CHANGE)
        now_utc = _require_utc_datetime(now, "now")

        async with self._database.write_transaction() as conn:
            stored = await _fetch_stored_changeset(conn, cid)
            if stored is None:
                raise _changeset_not_found()
            approval = await _fetch_approval_record(conn, cid)
            if approval is None:
                raise _approval_required()
            canonical_digest = stored.changeset.digest
            if changeset_digest != canonical_digest:
                raise _approval_digest_mismatch()
            if approval.changeset_digest != canonical_digest:
                raise _approval_digest_mismatch()

            current_decision = approval.decision
            if current_decision is not ApprovalDecision.PENDING:
                if current_decision is ApprovalDecision.EXPIRED:
                    raise _approval_expired()
                raise _approval_already_consumed()

            sid = stored.changeset.session_id
            rid = stored.changeset.run_id
            expires_at = approval.expires_at

            if now_utc > expires_at:
                expired_approval = ApprovalRecord(
                    schema_version=approval.schema_version,
                    approval_id=approval.approval_id,
                    change_id=approval.change_id,
                    changeset_digest=approval.changeset_digest,
                    decision=ApprovalDecision.EXPIRED,
                    decided_by=None,
                    requested_at=approval.requested_at,
                    decided_at=now_utc,
                    expires_at=approval.expires_at,
                    approved_instance_id=None,
                    approved_scene_epoch=None,
                )
                await self._write_approval_conn(
                    conn, expired_approval, expected_decision=ApprovalDecision.PENDING
                )
                await self._transition_state_conn(
                    conn,
                    cid,
                    ChangeSetState.AWAITING_APPROVAL,
                    ChangeSetState.EXPIRED,
                )
                events = (
                    await events_store._append_conn(
                        conn,
                        session_id=sid,
                        run_id=rid,
                        event_type="approval.expired",
                        payload=_decided_payload(expired_approval),
                        retention_class=RetentionClass.DURABLE,
                    ),
                    await events_store._append_conn(
                        conn,
                        session_id=sid,
                        run_id=rid,
                        event_type="changeset.state_changed",
                        payload=_state_changed_payload(
                            cid,
                            ChangeSetState.AWAITING_APPROVAL,
                            ChangeSetState.EXPIRED,
                        ),
                        retention_class=RetentionClass.DURABLE,
                    ),
                )
                return DecisionResult(
                    ApprovalDecision.EXPIRED,
                    expired_approval,
                    ChangeSetState.EXPIRED,
                    events,
                )

            if decision is ApprovalDecision.APPROVED:
                decided_approval = ApprovalRecord(
                    schema_version=approval.schema_version,
                    approval_id=approval.approval_id,
                    change_id=approval.change_id,
                    changeset_digest=approval.changeset_digest,
                    decision=ApprovalDecision.APPROVED,
                    decided_by="local_user",
                    requested_at=approval.requested_at,
                    decided_at=now_utc,
                    expires_at=approval.expires_at,
                    approved_instance_id=approved_instance_id,
                    approved_scene_epoch=approved_scene_epoch,
                )
                await self._write_approval_conn(
                    conn, decided_approval, expected_decision=ApprovalDecision.PENDING
                )
                await self._transition_state_conn(
                    conn,
                    cid,
                    ChangeSetState.AWAITING_APPROVAL,
                    ChangeSetState.APPROVED,
                )
                events = (
                    await events_store._append_conn(
                        conn,
                        session_id=sid,
                        run_id=rid,
                        event_type="approval.approved",
                        payload=_approved_payload(decided_approval),
                        retention_class=RetentionClass.DURABLE,
                    ),
                    await events_store._append_conn(
                        conn,
                        session_id=sid,
                        run_id=rid,
                        event_type="changeset.state_changed",
                        payload=_state_changed_payload(
                            cid,
                            ChangeSetState.AWAITING_APPROVAL,
                            ChangeSetState.APPROVED,
                        ),
                        retention_class=RetentionClass.DURABLE,
                    ),
                )
                return DecisionResult(
                    ApprovalDecision.APPROVED,
                    decided_approval,
                    ChangeSetState.APPROVED,
                    events,
                )

            # decision is REJECTED
            decided_approval = ApprovalRecord(
                schema_version=approval.schema_version,
                approval_id=approval.approval_id,
                change_id=approval.change_id,
                changeset_digest=approval.changeset_digest,
                decision=ApprovalDecision.REJECTED,
                decided_by="local_user",
                requested_at=approval.requested_at,
                decided_at=now_utc,
                expires_at=approval.expires_at,
                approved_instance_id=None,
                approved_scene_epoch=None,
            )
            await self._write_approval_conn(
                conn, decided_approval, expected_decision=ApprovalDecision.PENDING
            )
            await self._transition_state_conn(
                conn,
                cid,
                ChangeSetState.AWAITING_APPROVAL,
                ChangeSetState.REJECTED,
            )
            events = (
                await events_store._append_conn(
                    conn,
                    session_id=sid,
                    run_id=rid,
                    event_type="approval.rejected",
                    payload=_decided_payload(decided_approval),
                    retention_class=RetentionClass.DURABLE,
                ),
                await events_store._append_conn(
                    conn,
                    session_id=sid,
                    run_id=rid,
                    event_type="changeset.state_changed",
                    payload=_state_changed_payload(
                        cid,
                        ChangeSetState.AWAITING_APPROVAL,
                        ChangeSetState.REJECTED,
                    ),
                    retention_class=RetentionClass.DURABLE,
                ),
            )
            return DecisionResult(
                ApprovalDecision.REJECTED,
                decided_approval,
                ChangeSetState.REJECTED,
                events,
            )

    # --- combined transactional apply/recovery primitives ---------------

    async def begin_apply(
        self,
        change_id: str,
        *,
        now: datetime,
    ) -> ApplyStartResult:
        """Consume approval and persist ``Applying`` before Bridge I/O.

        A ChangeSet recovered to ``Approved`` retains its already-consumed
        approval. An explicit trusted retry may cross the boundary again, but
        the approval is never consumed or rewritten a second time.
        """
        events_store = self._events
        if events_store is None:
            raise TypeError("ChangeSetRepository.begin_apply requires an EventStore")
        cid = _require_id_value(change_id, IdKind.CHANGE)
        now_utc = _require_utc_datetime(now, "now")
        async with self._database.write_transaction() as conn:
            stored = await _fetch_stored_changeset(conn, cid)
            if stored is None:
                raise _changeset_not_found()
            if stored.state is not ChangeSetState.APPROVED:
                raise _cas_conflict()
            approval = await _fetch_approval_record(conn, cid)
            if approval is None:
                raise _approval_required()
            changeset = stored.changeset
            if approval.changeset_digest != changeset.digest:
                raise _approval_digest_mismatch()
            binding = changeset.scene_binding
            if (
                approval.approved_instance_id != binding.instance_id
                or approval.approved_scene_epoch != binding.scene_epoch
            ):
                raise _approval_binding_mismatch()

            reused = approval.decision is ApprovalDecision.CONSUMED
            if approval.decision is ApprovalDecision.APPROVED:
                if now_utc > approval.expires_at:
                    raise _approval_expired()
                approval = await self._consume_approval_conn(conn, approval, now_utc)
            elif not reused:
                raise _approval_already_consumed()

            await self._transition_state_conn(
                conn, cid, ChangeSetState.APPROVED, ChangeSetState.APPLYING
            )
            event = await events_store._append_conn(
                conn,
                session_id=changeset.session_id,
                run_id=changeset.run_id,
                event_type="changeset.state_changed",
                payload=_state_changed_payload(
                    cid, ChangeSetState.APPROVED, ChangeSetState.APPLYING
                ),
                retention_class=RetentionClass.DURABLE,
            )
            return ApplyStartResult(changeset, approval, (event,), reused)

    async def complete_apply(
        self,
        receipt: ChangeReceipt,
    ) -> ApplyCompletionResult:
        """Atomically persist a reconciled receipt, state, and events."""
        if type(receipt) is not ChangeReceipt:
            raise TypeError("receipt must be an exact ChangeReceipt")
        events_store = self._events
        if events_store is None:
            raise TypeError("ChangeSetRepository.complete_apply requires an EventStore")
        cid = receipt.change_id
        target = _state_for_receipt(receipt.status)
        async with self._database.write_transaction() as conn:
            stored = await _fetch_stored_changeset(conn, cid)
            if stored is None:
                raise _changeset_not_found()
            changeset = stored.changeset
            if receipt.status in (
                ReceiptStatus.APPLIED,
                ReceiptStatus.ALREADY_APPLIED,
                ReceiptStatus.ROLLED_BACK,
            ) and (
                receipt.instance_id != changeset.scene_binding.instance_id
                or receipt.scene_epoch != changeset.scene_binding.scene_epoch
            ):
                raise _approval_binding_mismatch()

            existing = await _fetch_receipt_record(conn, cid)
            if existing is not None:
                if _storage_digest(existing) != _storage_digest(receipt):
                    raise _receipt_conflict()
                if stored.state is not target:
                    raise _record_corrupt()
                return ApplyCompletionResult(changeset, existing, target, ())
            if stored.state is not ChangeSetState.APPLYING:
                raise _cas_conflict()

            await self._insert_receipt_conn(conn, receipt)
            await self._transition_state_conn(
                conn, cid, ChangeSetState.APPLYING, target
            )
            state_event = await events_store._append_conn(
                conn,
                session_id=changeset.session_id,
                run_id=changeset.run_id,
                event_type="changeset.state_changed",
                payload=_state_changed_payload(cid, ChangeSetState.APPLYING, target),
                retention_class=RetentionClass.DURABLE,
            )
            outcome_event = await events_store._append_conn(
                conn,
                session_id=changeset.session_id,
                run_id=changeset.run_id,
                event_type=_event_for_receipt(receipt.status),
                payload=_receipt_event_payload(changeset, receipt, target),
                retention_class=RetentionClass.DURABLE,
            )
            return ApplyCompletionResult(
                changeset, receipt, target, (state_event, outcome_event)
            )

    async def complete_bootstrap_apply(
        self,
        receipt: ChangeReceipt,
        workspace: WorkspaceManifest,
    ) -> ApplyCompletionResult:
        """Atomically complete a successful first Apply and activate ownership."""
        if type(receipt) is not ChangeReceipt:
            raise TypeError("receipt must be an exact ChangeReceipt")
        if type(workspace) is not WorkspaceManifest:
            raise TypeError("workspace must be an exact WorkspaceManifest")
        if not receipt.is_success:
            raise ValueError("bootstrap receipt must be applied")
        events_store = self._events
        if events_store is None:
            raise TypeError(
                "ChangeSetRepository.complete_bootstrap_apply requires an EventStore"
            )
        cid = receipt.change_id
        target = ChangeSetState.APPLIED
        async with self._database.write_transaction() as conn:
            stored = await _fetch_stored_changeset(conn, cid)
            if stored is None:
                raise _changeset_not_found()
            changeset = stored.changeset
            if (
                changeset.workspace_id is not None
                or changeset.required_permission is not PermissionMode.PROJECT_CHANGE
                or workspace.session_id != changeset.session_id
                or workspace.created_by_run != changeset.run_id
                or workspace.instance_id != receipt.instance_id
                or workspace.scene_epoch != receipt.scene_epoch
                or receipt.instance_id != changeset.scene_binding.instance_id
                or receipt.scene_epoch != changeset.scene_binding.scene_epoch
            ):
                raise _approval_binding_mismatch()
            created_workspace_ids = {
                operation.workspace_id
                for operation in changeset.operations
                if isinstance(operation, CreateNode)
            }
            if created_workspace_ids != {workspace.workspace_id}:
                raise _workspace_identity_conflict()

            existing_receipt = await _fetch_receipt_record(conn, cid)
            existing_workspace_row = await _fetch_workspace_row(
                conn, workspace.workspace_id
            )
            state_row = await _fetch_workspace_state_row(
                conn, workspace.session_id
            )
            if existing_receipt is not None:
                if (
                    _storage_digest(existing_receipt) != _storage_digest(receipt)
                    or stored.state is not target
                    or existing_workspace_row is None
                    or state_row is None
                ):
                    raise _record_corrupt()
                existing_workspace = _manifest_from_row(existing_workspace_row)
                state = _workspace_state_from_row(state_row)
                if (
                    existing_workspace.revision != workspace.revision
                    or state.active_workspace_id != workspace.workspace_id
                ):
                    raise _record_corrupt()
                return ApplyCompletionResult(
                    changeset, existing_receipt, target, ()
                )
            if stored.state is not ChangeSetState.APPLYING:
                raise _cas_conflict()
            if existing_workspace_row is not None or state_row is not None:
                raise _workspace_active_conflict()

            await self._insert_receipt_conn(conn, receipt)
            await self._transition_state_conn(
                conn, cid, ChangeSetState.APPLYING, target
            )
            state_event = await events_store._append_conn(
                conn,
                session_id=changeset.session_id,
                run_id=changeset.run_id,
                event_type="changeset.state_changed",
                payload=_state_changed_payload(
                    cid, ChangeSetState.APPLYING, target
                ),
                retention_class=RetentionClass.DURABLE,
            )
            outcome_event = await events_store._append_conn(
                conn,
                session_id=changeset.session_id,
                run_id=changeset.run_id,
                event_type=_event_for_receipt(receipt.status),
                payload=_receipt_event_payload(changeset, receipt, target),
                retention_class=RetentionClass.DURABLE,
            )

            payload = canonical_json_dumps(workspace.to_dict())
            digest = _storage_digest(workspace)
            await conn.execute(
                f"INSERT INTO workspaces({_WORKSPACE_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace.workspace_id,
                    workspace.session_id,
                    workspace.instance_id,
                    workspace.scene_epoch,
                    workspace.revision,
                    workspace.created_by_run,
                    workspace.updated_at.isoformat(),
                    digest,
                    payload,
                    workspace.schema_version,
                ),
            )
            await conn.execute(
                "INSERT INTO session_workspace_state(session_id, "
                "active_workspace_id, state_revision, updated_at) "
                "VALUES (?, ?, 1, ?)",
                (
                    workspace.session_id,
                    workspace.workspace_id,
                    workspace.updated_at.isoformat(),
                ),
            )
            state = WorkspaceStateRecord(
                workspace.session_id,
                workspace.workspace_id,
                1,
                workspace.updated_at,
            )
            workspace_event = await events_store._append_conn(
                conn,
                session_id=workspace.session_id,
                run_id=workspace.created_by_run,
                event_type="workspace.created",
                payload=_workspace_event_payload(workspace, state, active=True),
                retention_class=RetentionClass.DURABLE,
            )
            return ApplyCompletionResult(
                changeset,
                receipt,
                target,
                (state_event, outcome_event, workspace_event),
            )

    async def recover_to_approved(self, change_id: str) -> ApplyRecoveryResult:
        """Persist a proven no-write recovery without replaying the ChangeSet."""
        events_store = self._events
        if events_store is None:
            raise TypeError(
                "ChangeSetRepository.recover_to_approved requires an EventStore"
            )
        cid = _require_id_value(change_id, IdKind.CHANGE)
        async with self._database.write_transaction() as conn:
            stored = await _fetch_stored_changeset(conn, cid)
            if stored is None:
                raise _changeset_not_found()
            if stored.state is not ChangeSetState.APPLYING:
                raise _cas_conflict()
            approval = await _fetch_approval_record(conn, cid)
            if approval is None or approval.decision is not ApprovalDecision.CONSUMED:
                raise _record_corrupt()
            await self._transition_state_conn(
                conn, cid, ChangeSetState.APPLYING, ChangeSetState.APPROVED
            )
            event = await events_store._append_conn(
                conn,
                session_id=stored.changeset.session_id,
                run_id=stored.changeset.run_id,
                event_type="changeset.state_changed",
                payload={
                    **_state_changed_payload(
                        cid, ChangeSetState.APPLYING, ChangeSetState.APPROVED
                    ),
                    "reason": "before_state_recovered",
                },
                retention_class=RetentionClass.DURABLE,
            )
            return ApplyRecoveryResult(
                stored.changeset,
                approval,
                ChangeSetState.APPROVED,
                (event,),
            )

    # --- shared internal helpers -----------------------------------------

    @staticmethod
    async def _require_session(conn, session_id: str) -> None:
        cursor = await conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        )
        if await cursor.fetchone() is None:
            raise _session_not_found()

    @staticmethod
    async def _require_run(conn, run_id: str) -> None:
        cursor = await conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,))
        if await cursor.fetchone() is None:
            raise _run_not_found()

    @staticmethod
    async def _transition_state_conn(
        conn,
        change_id: str,
        from_state: ChangeSetState,
        to_state: ChangeSetState,
    ) -> None:
        """Connection-scoped CAS ChangeSet state transition (no event)."""
        cursor = await conn.execute(
            "SELECT state FROM changesets WHERE change_id = ?", (change_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise _changeset_not_found()
        current = ChangeSetState(row["state"])
        if current is not from_state:
            raise _cas_conflict()
        if to_state not in _TRANSITIONS[current]:
            raise _invalid_changeset_transition(current, to_state)
        await conn.execute(
            "UPDATE changesets SET state = ? WHERE change_id = ?",
            (to_state.value, change_id),
        )

    @staticmethod
    async def _write_approval_conn(
        conn,
        approval: ApprovalRecord,
        *,
        expected_decision: ApprovalDecision,
    ) -> None:
        """Connection-scoped CAS approval update (identity/digest/transition safe)."""
        cursor = await conn.execute(
            "SELECT approval_id, decision FROM approvals WHERE change_id = ?",
            (approval.change_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise _approval_not_found()
        # approval_id is the immutable row identity: refuse to swap it.
        if approval.approval_id != row["approval_id"]:
            raise _approval_identity_mismatch()
        current_decision = ApprovalDecision(row["decision"])
        if current_decision is not expected_decision:
            raise _cas_conflict()
        if approval.decision not in _APPROVAL_TRANSITIONS[current_decision]:
            raise _invalid_approval_transition(current_decision, approval.decision)
        payload = canonical_json_dumps(approval.to_dict())
        digest = _storage_digest(approval)
        await conn.execute(
            "UPDATE approvals SET changeset_digest = ?, decision = ?, decided_by = ?, "
            "requested_at = ?, decided_at = ?, expires_at = ?, "
            "approved_instance_id = ?, approved_scene_epoch = ?, updated_at = ?, "
            "digest = ?, payload_json = ? WHERE change_id = ?",
            (
                approval.changeset_digest,
                approval.decision.value,
                approval.decided_by,
                approval.requested_at.isoformat(),
                approval.decided_at.isoformat()
                if approval.decided_at is not None
                else None,
                approval.expires_at.isoformat(),
                approval.approved_instance_id,
                approval.approved_scene_epoch,
                _approval_updated_at(approval),
                digest,
                payload,
                approval.change_id,
            ),
        )

    @staticmethod
    async def _consume_approval_conn(
        conn,
        approval: ApprovalRecord,
        now: datetime,
    ) -> ApprovalRecord:
        """Connection-scoped exact Approved -> Consumed transition."""
        consumed = ApprovalRecord(
            schema_version=approval.schema_version,
            approval_id=approval.approval_id,
            change_id=approval.change_id,
            changeset_digest=approval.changeset_digest,
            decision=ApprovalDecision.CONSUMED,
            decided_by="local_user",
            requested_at=approval.requested_at,
            decided_at=now,
            expires_at=approval.expires_at,
            approved_instance_id=approval.approved_instance_id,
            approved_scene_epoch=approval.approved_scene_epoch,
        )
        payload = canonical_json_dumps(consumed.to_dict())
        digest = _storage_digest(consumed)
        cursor = await conn.execute(
            "UPDATE approvals SET decision = ?, decided_by = ?, decided_at = ?, "
            "updated_at = ?, digest = ?, payload_json = ? "
            "WHERE change_id = ? AND approval_id = ? AND decision = ?",
            (
                ApprovalDecision.CONSUMED.value,
                "local_user",
                now.isoformat(),
                now.isoformat(),
                digest,
                payload,
                approval.change_id,
                approval.approval_id,
                ApprovalDecision.APPROVED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise _cas_conflict()
        return consumed

    @staticmethod
    async def _insert_receipt_conn(conn, receipt: ChangeReceipt) -> None:
        payload = canonical_json_dumps(receipt.to_dict())
        digest = _storage_digest(receipt)
        await conn.execute(
            f"INSERT INTO change_receipts({_RECEIPT_COLUMNS}) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.change_id,
                receipt.status.value,
                receipt.instance_id,
                receipt.scene_epoch,
                digest,
                payload,
                receipt.completed_at.isoformat(),
                receipt.schema_version,
            ),
        )


def _stored_from_row(row) -> StoredChangeSet:
    _verify_payload(row["payload_json"], row["digest"])
    return StoredChangeSet(
        _decode_changeset(_loads_canonical(row["payload_json"])),
        ChangeSetState(row["state"]),
    )


class _StringKeyedRow(Protocol):
    """Minimal structural type for a row that supports string-keyed indexing.

    A ``sqlite3.Row`` is not a ``dict`` and does not satisfy ``Mapping`` (it
    lacks ``keys``/``items``/``__iter__`` in the mapping sense), but it does
    support ``row["col"]``. This Protocol declares only that single capability,
    so the decoder depends on the narrowest useful shape instead of masquerading
    a Row as a ``dict`` via ``cast``.
    """

    def __getitem__(self, key: str, /) -> object:
        ...


def _decode_row(
    row: _StringKeyedRow,
    payload_key: str,
    digest_key: str,
    decoder: Callable[[object], ChangeReceipt],
) -> ChangeReceipt:
    # ``row`` is a duck-typed sqlite row supporting string-keyed indexing. The
    # payload/digest cells are stored canonical JSON text and a SHA-256 digest;
    # narrow them to exact strings before handing them to the storage boundary.
    payload_json = codec._require_str(row[payload_key], payload_key)
    stored_digest = codec._require_str(row[digest_key], digest_key)
    _verify_payload(payload_json, stored_digest)
    return decoder(_loads_canonical(payload_json))


# --------------------------------------------------------------------------
# connection-scoped reads + bounded event payloads (combined primitives)
# --------------------------------------------------------------------------


async def _fetch_stored_changeset(conn, change_id: str) -> StoredChangeSet | None:
    cursor = await conn.execute(
        f"SELECT {_CHANGESET_COLUMNS} FROM changesets WHERE change_id = ?",
        (change_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _stored_from_row(row)


async def _fetch_approval_record(conn, change_id: str) -> ApprovalRecord | None:
    cursor = await conn.execute(
        f"SELECT {_APPROVAL_COLUMNS} FROM approvals WHERE change_id = ?",
        (change_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    _verify_payload(row["payload_json"], row["digest"])
    approval = _decode_approval(_loads_canonical(row["payload_json"]))
    _check_approval_row_identity(row, approval)
    return approval


async def _fetch_receipt_record(conn, change_id: str) -> ChangeReceipt | None:
    cursor = await conn.execute(
        f"SELECT {_RECEIPT_COLUMNS} FROM change_receipts WHERE change_id = ?",
        (change_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _decode_row(row, "payload_json", "digest", _decode_receipt)


def _state_for_receipt(status: ReceiptStatus) -> ChangeSetState:
    if status in (ReceiptStatus.APPLIED, ReceiptStatus.ALREADY_APPLIED):
        return ChangeSetState.APPLIED
    if status is ReceiptStatus.ROLLED_BACK:
        return ChangeSetState.ROLLED_BACK
    if status in (ReceiptStatus.PARTIAL, ReceiptStatus.CRITICAL_RECOVERY):
        return ChangeSetState.CRITICAL_RECOVERY
    raise TypeError("unsupported receipt status")


def _event_for_receipt(status: ReceiptStatus) -> str:
    if status in (ReceiptStatus.APPLIED, ReceiptStatus.ALREADY_APPLIED):
        return "changeset.applied"
    if status is ReceiptStatus.ROLLED_BACK:
        return "changeset.rolled_back"
    return "recovery.critical"


def _state_changed_payload(
    change_id: str, from_state: ChangeSetState, to_state: ChangeSetState
) -> dict[str, JsonValue]:
    return {
        "change_id": change_id,
        "from": from_state.value,
        "to": to_state.value,
    }


def _receipt_event_payload(
    changeset: ChangeSet,
    receipt: ChangeReceipt,
    state: ChangeSetState,
) -> dict[str, JsonValue]:
    # applied_op_ids is a tuple[str, ...]; build a fresh list[JsonValue] so the
    # event payload carries a JSON list in the original order without widening
    # the projection to dict[str, object] or casting.
    applied_op_ids: list[JsonValue] = []
    applied_op_ids.extend(receipt.applied_op_ids)
    payload: dict[str, JsonValue] = {
        "change_id": changeset.change_id,
        "changeset_digest": changeset.digest,
        "state": state.value,
        "receipt_status": receipt.status.value,
        "instance_id": receipt.instance_id,
        "scene_epoch": receipt.scene_epoch,
        "before_revision": receipt.before_revision,
        "after_revision": receipt.after_revision,
        "applied_op_ids": applied_op_ids,
        "scene_may_have_changed": receipt.scene_may_have_changed,
    }
    # B-2: surface the structured apply cause so the LLM and UI can branch on
    # error_code instead of guessing from applied_op_ids alone. Omitted when
    # the receipt has no cause (APPLIED, or a RolledBack that classified
    # cleanly without a captured exception).
    if receipt.error_code is not None:
        payload["error_code"] = receipt.error_code
    if receipt.error_message is not None:
        payload["error_message"] = receipt.error_message
    return payload


def _workspace_event_payload(
    manifest: WorkspaceManifest,
    state: WorkspaceStateRecord | None,
    *,
    active: bool,
    old_revision: str | None = None,
    previous_workspace_id: str | None = None,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "session_id": manifest.session_id,
        "workspace_id": manifest.workspace_id,
        "revision": manifest.revision,
        "node_count": len(manifest.nodes),
        "instance_id": manifest.instance_id,
        "scene_epoch": manifest.scene_epoch,
        "active": active,
        "state_revision": None if state is None else state.state_revision,
    }
    if old_revision is not None:
        payload["old_revision"] = old_revision
        payload["new_revision"] = manifest.revision
    if previous_workspace_id is not None:
        payload["previous_workspace_id"] = previous_workspace_id
    return payload


def _risk_summary_projection(risk: RiskSummary) -> dict[str, JsonValue]:
    # The ChangeSet event payload carries a finite, auditable projection of the
    # risk summary — not the full DTO. RiskSummary.to_dict() is statically
    # dict[str, object], so the projection is reconstructed here as a precise
    # dict[str, JsonValue] with the same fields and values, building the two
    # string sequences as list[JsonValue] to avoid widening or casting.
    effect_names: list[JsonValue] = []
    effect_names.extend(risk.effect_names)
    affected_paths: list[JsonValue] = []
    affected_paths.extend(risk.affected_paths)
    return {
        "touches_external_nodes": risk.touches_external_nodes,
        "changes_wiring": risk.changes_wiring,
        "requires_backup": risk.requires_backup,
        "operation_count": risk.operation_count,
        "effect_names": effect_names,
        "affected_paths": affected_paths,
    }


def _proposed_payload(
    changeset: ChangeSet, state: ChangeSetState
) -> dict[str, JsonValue]:
    # Bounded proposal facts: IDs, the canonical digest, the resulting state,
    # the permission mode, and the already-bounded risk summary. No full DTO,
    # operations, or parameter values are emitted in the event.
    return {
        "change_id": changeset.change_id,
        "session_id": changeset.session_id,
        "run_id": changeset.run_id,
        "changeset_digest": changeset.digest,
        "state": state.value,
        "required_permission": changeset.required_permission.value,
        "risk_summary": _risk_summary_projection(changeset.risk_summary),
    }


def _requested_payload(approval: ApprovalRecord) -> dict[str, JsonValue]:
    return {
        "approval_id": approval.approval_id,
        "change_id": approval.change_id,
        "changeset_digest": approval.changeset_digest,
        "expires_at": approval.expires_at.isoformat(),
    }


def _approved_payload(approval: ApprovalRecord) -> dict[str, JsonValue]:
    # A decided approval is contractually required to carry decided_at
    # (ApprovalRecord.__post_init__ enforces this for APPROVED/CONSUMED). The
    # static type is datetime | None, so narrow to datetime locally and fail
    # closed via the existing record-corrupt error rather than leaking payload.
    decided_at = approval.decided_at
    if decided_at is None:
        raise _record_corrupt()
    return {
        "approval_id": approval.approval_id,
        "change_id": approval.change_id,
        "changeset_digest": approval.changeset_digest,
        "approved_instance_id": approval.approved_instance_id,
        "approved_scene_epoch": approval.approved_scene_epoch,
        "decided_at": decided_at.isoformat(),
    }


def _decided_payload(approval: ApprovalRecord) -> dict[str, JsonValue]:
    # Rejection and expiry carry the same bounded decision facts (no binding).
    # See _approved_payload: decided_at is contractually present for REJECTED
    # and EXPIRED, narrowed here from datetime | None.
    decided_at = approval.decided_at
    if decided_at is None:
        raise _record_corrupt()
    return {
        "approval_id": approval.approval_id,
        "change_id": approval.change_id,
        "changeset_digest": approval.changeset_digest,
        "decided_at": decided_at.isoformat(),
    }
