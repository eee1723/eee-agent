"""Secure read-only HoudiniBridge — DTO and error contracts.

This package defines the strict, frozen, JSON-canonical data transfer objects
for the loopback-only read-only HoudiniBridge. It is the foundation for a
future authenticated transport (15-B), a main-thread queue (15-C), and a real
Houdini-side adapter (15-D).

The package imports **neither** ``hou`` **nor** ``rpyc``. Runtime consumes only
plain JSON DTOs from this surface; no live HOM object ever crosses the boundary.
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

__all__ = [
    "MAX_MESSAGE_BYTES",
    "PROTOCOL",
    "BridgeError",
    "BridgeOperation",
    "BridgeRequest",
    "BridgeResponse",
    "SceneBinding",
    "SceneQueryResult",
    "SelectedNode",
    "parse_request",
    "parse_response",
]
