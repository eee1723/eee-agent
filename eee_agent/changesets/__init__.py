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

_WORKSPACE_SERVICE_EXPORTS = frozenset(
    {
        "WorkspaceFactProvider",
        "WorkspaceHealth",
        "WorkspaceInspectionSummary",
        "WorkspaceLifecycleSummary",
        "WorkspaceService",
        "WorkspaceSummary",
    }
)


def __getattr__(name: str):
    # Lazy to preserve the accepted changesets.contracts -> houdini_bridge
    # import boundary: the Bridge workspace DTOs themselves import the manifest
    # decoder, so eager service exports would create a package initialization
    # cycle.
    if name in _WORKSPACE_SERVICE_EXPORTS:
        from eee_agent.changesets import workspace_service

        return getattr(workspace_service, name)
    raise AttributeError(name)

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
    "WorkspaceFactProvider",
    "WorkspaceHealth",
    "WorkspaceInspectionSummary",
    "WorkspaceLifecycleSummary",
    "WorkspaceService",
    "WorkspaceSummary",
    "evaluate_policy",
]
