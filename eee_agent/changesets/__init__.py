"""Immutable typed ChangeSet contracts and pure policy engine (Task 16-A).

Stable public surface only: the frozen DTOs/enums, the unions of typed
operations/conditions, and :func:`evaluate_policy`. Internal validators stay
private in :mod:`eee_agent.changesets.contracts`.

This package imports neither ``hou`` nor the legacy ``eee_agent.bridge``; it
depends only on the accepted read-only :class:`SceneBinding` contract and the
shared Runtime canonical-JSON helpers.
"""

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    ConnectInput,
    CreateNode,
    Effect,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    OwnedNodeRef,
    ParmSnapshot,
    ParmValueEquals,
    PermissionMode,
    PolicyDecision,
    Postcondition,
    Precondition,
    ReceiptStatus,
    RiskSummary,
    SceneBindingEquals,
    SetParm,
    TypedOperation,
    WireInputEquals,
    WireRef,
    WireSnapshot,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
)
from eee_agent.changesets.policy import evaluate_policy

__all__ = [
    "ApprovalDecision",
    "ApprovalRecord",
    "ChangeReceipt",
    "ChangeSet",
    "CheckpointPlan",
    "ConditionResult",
    "ConnectInput",
    "CreateNode",
    "Effect",
    "NodeAbsent",
    "NodeIdentityEquals",
    "NodeRef",
    "OwnedNodeRef",
    "ParmSnapshot",
    "ParmValueEquals",
    "PermissionMode",
    "PolicyDecision",
    "Postcondition",
    "Precondition",
    "ReceiptStatus",
    "RiskSummary",
    "SceneBindingEquals",
    "SetParm",
    "TypedOperation",
    "WireInputEquals",
    "WireRef",
    "WireSnapshot",
    "WorkspaceManifest",
    "WorkspaceRevisionEquals",
    "evaluate_policy",
]
