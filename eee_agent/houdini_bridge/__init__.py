"""Secure read-only HoudiniBridge — DTO and error contracts.

This package defines the strict, frozen, JSON-canonical data transfer objects
for the loopback-only read-only HoudiniBridge. It is the foundation for a
future authenticated transport (15-B), a main-thread queue (15-C), and a real
Houdini-side adapter (15-D).

The package imports **neither** ``hou`` **nor** ``rpyc``. Runtime consumes only
plain JSON DTOs from this surface; no live HOM object ever crosses the boundary.

The additive Task 16 ``changeset.v1`` capability and the typed preflight
(Task 16-C) / apply / receipt (Task 16-D) DTOs live in
:mod:`eee_agent.houdini_bridge.changesets`. They are exposed here through a
lazy ``__getattr__`` (PEP 562) so importing this package never eagerly pulls in
the ChangeSet contracts — that would form an import cycle, because the Task 16-A
ChangeSet contracts import :class:`SceneBinding` from this package.
"""

from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    BridgeOperation,
    BridgeRequest,
    BridgeResponse,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
    parse_request,
    parse_response,
)

# Additive changeset symbols exposed lazily (see ``__getattr__`` below).
_LAZY_PREFLIGHT = {
    "APPLY_OPERATION",
    "ApplyRequest",
    "ApplyResponse",
    "CHANGESET_V1",
    "OPERATION",
    "PreflightNodeFact",
    "PreflightParmFact",
    "PreflightRequest",
    "PreflightResponse",
    "PreflightResult",
    "PreflightWireFact",
    "RECEIPT_OPERATION",
    "ReceiptRequest",
    "ReceiptResponse",
    "decode_change_receipt",
    "parse_apply_request",
    "parse_apply_response",
    "parse_preflight_request",
    "parse_preflight_response",
    "parse_receipt_request",
    "parse_receipt_response",
    "validate_capabilities",
}

_LAZY_WORKSPACES = {
    "WORKSPACE_INSPECT_OPERATION",
    "WORKSPACE_V1",
    "WorkspaceInspectRequest",
    "WorkspaceInspectError",
    "WorkspaceInspectResponse",
    "WorkspaceInspectResult",
    "WorkspaceInspectionConflict",
    "WorkspaceInspectionUnavailable",
    "WorkspaceNodeObservation",
    "parse_workspace_inspect_request",
    "parse_workspace_inspect_response",
}

__all__ = [
    "BridgeChangeSetProvider",
    "APPLY_OPERATION",
    "ApplyRequest",
    "ApplyResponse",
    "MAX_MESSAGE_BYTES",
    "OPERATION",
    "PROTOCOL",
    "BridgeError",
    "BridgeOperation",
    "BridgeRequest",
    "BridgeResponse",
    "CHANGESET_V1",
    "PreflightNodeFact",
    "PreflightParmFact",
    "PreflightRequest",
    "PreflightResponse",
    "PreflightResult",
    "PreflightWireFact",
    "RECEIPT_OPERATION",
    "ReceiptRequest",
    "ReceiptResponse",
    "SceneBinding",
    "SceneQueryResult",
    "SelectedNode",
    "decode_change_receipt",
    "parse_apply_request",
    "parse_apply_response",
    "parse_preflight_request",
    "parse_preflight_response",
    "parse_receipt_request",
    "parse_receipt_response",
    "parse_request",
    "parse_response",
    "validate_capabilities",
    "WORKSPACE_INSPECT_OPERATION",
    "WORKSPACE_V1",
    "WorkspaceInspectRequest",
    "WorkspaceInspectError",
    "WorkspaceInspectResponse",
    "WorkspaceInspectResult",
    "WorkspaceInspectionConflict",
    "WorkspaceInspectionUnavailable",
    "WorkspaceNodeObservation",
    "parse_workspace_inspect_request",
    "parse_workspace_inspect_response",
]


def __getattr__(name: str) -> object:
    """Lazily resolve additive changeset symbols to avoid an import cycle."""
    if name == "BridgeChangeSetProvider":
        from eee_agent.houdini_bridge.changeset_provider import (
            BridgeChangeSetProvider,
        )

        globals()[name] = BridgeChangeSetProvider
        return BridgeChangeSetProvider
    if name in _LAZY_PREFLIGHT:
        from eee_agent.houdini_bridge import changesets as _mod

        value = getattr(_mod, name)
        globals()[name] = value
        return value
    if name in _LAZY_WORKSPACES:
        from eee_agent.houdini_bridge import workspaces as _mod

        value = getattr(_mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
