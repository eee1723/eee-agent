"""Secure read-only HoudiniBridge — DTO and error contracts.

This package defines the strict, frozen, JSON-canonical data transfer objects
for the loopback-only read-only HoudiniBridge. It is the foundation for a
future authenticated transport (15-B), a main-thread queue (15-C), and a real
Houdini-side adapter (15-D).

The package imports **neither** ``hou`` **nor** ``rpyc``. Runtime consumes only
plain JSON DTOs from this surface; no live HOM object ever crosses the boundary.

The additive Task 16-C ``changeset.v1`` capability and typed preflight DTOs live
in :mod:`eee_agent.houdini_bridge.changesets`. They are exposed here through a
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

# Additive preflight symbols exposed lazily (see ``__getattr__`` below).
_LAZY_PREFLIGHT = {
    "CHANGESET_V1",
    "PreflightNodeFact",
    "PreflightParmFact",
    "PreflightRequest",
    "PreflightResponse",
    "PreflightResult",
    "PreflightWireFact",
    "parse_preflight_request",
    "parse_preflight_response",
    "validate_capabilities",
}

__all__ = [
    "MAX_MESSAGE_BYTES",
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
    "SceneBinding",
    "SceneQueryResult",
    "SelectedNode",
    "parse_preflight_request",
    "parse_preflight_response",
    "parse_request",
    "parse_response",
    "validate_capabilities",
]


def __getattr__(name: str) -> object:
    """Lazily resolve additive preflight symbols to avoid an import cycle."""
    if name in _LAZY_PREFLIGHT:
        from eee_agent.houdini_bridge import changesets as _mod

        value = getattr(_mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
