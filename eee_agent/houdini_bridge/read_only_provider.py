"""Production bridge-backed ReadOnlyProvider for Runtime agent tools.

The Runtime tool boundary (:mod:`eee_agent.runtime.agent_tools`) talks to a
provider-neutral ``ReadOnlyProvider`` protocol and never sees the Bridge.
This module is the production implementation of that protocol: every method
opens one short-lived authenticated client from the discovered Secure Bridge
handoff files (same discovery pattern as
:class:`~eee_agent.houdini_bridge.workspace_provider.BridgeWorkspaceFactProvider`)
and maps the typed read-only bridge operations — ``workspace.inspect``
(selection mode, nullable epoch) and ``scene.query`` (exact epoch) — onto
bounded plain-JSON results.

Fail-closed rules:

* A missing handoff, transport failure, or deadline produces a bounded
  ``{"ok": False, ...}`` result, never an exception, so the tool boundary can
  forward a truthful unavailable answer.
* Bridge-originated messages (``BridgeClientError.message_for_user``) are
  already bounded and leak-free; every other exception collapses to a generic
  unavailable result so internal detail never reaches the model.
* Node lists are capped (``_MAX_NODES``) so provider output stays well inside
  the tool boundary's item/byte budgets.

No server-side additions are required: the five read methods compose the
existing ``workspace.inspect`` and ``scene.query`` operations only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    BridgeIdentityError,
    BridgeTokenError,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import BridgeOperation, BridgeRequest
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectRequest,
    WorkspaceInspectResult,
)
from eee_agent.runtime.agent_context import PlainData

# Caps keep provider output inside the tool boundary's 64-item / 16 KiB
# budgets with headroom for the envelope fields.
_MAX_NODES = 32
_MAX_NODE_PATHS = 64
_MAX_ID_LENGTH = 128
_STALE_RETRY_CODES = frozenset({"bridge.stale_scene", "bridge.binding_mismatch"})

ClientFactory = Callable[[Path], BridgeClient]


def _unavailable(code: str, message: str) -> dict[str, PlainData]:
    return {"ok": False, "code": code, "message": message}


def _not_available() -> dict[str, PlainData]:
    return _unavailable(
        "bridge.not_available", "The Houdini Bridge is not currently available."
    )


class BridgeReadOnlyProvider:
    """Discovered-Bridge implementation of the Runtime ReadOnlyProvider."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        deadline_ms: int = 5000,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        if type(deadline_ms) is not int:
            raise TypeError("deadline_ms must be an exact integer")
        if deadline_ms < 1 or deadline_ms > 30_000:
            raise ValueError("deadline_ms must be in 1..30000")
        self._deadline_ms = deadline_ms
        self._client_factory = client_factory or BridgeClient.from_state_dir

    # ------------------------------------------------------------------ tools

    async def scene_status(self) -> Mapping[str, PlainData]:
        async def operation(client: BridgeClient) -> dict[str, PlainData]:
            inspection = await self._inspect_selection(client)
            binding = inspection.binding
            return {
                "ok": True,
                "connected": True,
                "instance_id": binding.instance_id,
                "scene_epoch": binding.scene_epoch,
                "hip_path": binding.hip_path,
                "observed_revision": binding.observed_revision,
                "capabilities": list(client.capabilities),
            }

        return await self._run(operation)

    async def query_scene(
        self, node_paths: list[str]
    ) -> Mapping[str, PlainData]:
        if (
            type(node_paths) is not list
            or not node_paths
            or len(node_paths) > _MAX_NODE_PATHS
            or any(type(path) is not str for path in node_paths)
        ):
            return _unavailable(
                "bridge.invalid_request", "node_paths must be a bounded string list."
            )

        async def operation(client: BridgeClient) -> dict[str, PlainData]:
            result = await self._scene_query(client, node_paths)
            return {
                "ok": True,
                "binding": result.binding.to_dict(),
                "node_count": len(result.nodes),
                "nodes": [node.to_dict() for node in result.nodes[:_MAX_NODES]],
            }

        return await self._run_with_stale_retry(operation)

    async def inspect_workspace(
        self, workspace_id: str
    ) -> Mapping[str, PlainData]:
        invalid = self._invalid_workspace_id(workspace_id)
        if invalid is not None:
            return invalid

        async def operation(client: BridgeClient) -> dict[str, PlainData]:
            inspection = await self._inspect_selection(client)
            matched = self._workspace_observations(inspection, workspace_id)
            return {
                "ok": True,
                "workspace_id": workspace_id,
                "scope": "selection",
                "scene_epoch": inspection.binding.scene_epoch,
                "observed_revision": inspection.observed_revision,
                "node_count": len(matched),
                "truncated": len(matched) > _MAX_NODES,
                "nodes": [item.to_dict() for item in matched[:_MAX_NODES]],
            }

        return await self._run(operation)

    async def geometry_stats(self, node_path: str) -> Mapping[str, PlainData]:
        if type(node_path) is not str or not node_path.startswith("/"):
            return _unavailable(
                "bridge.invalid_request", "node_path must be an absolute scene path."
            )

        async def operation(client: BridgeClient) -> dict[str, PlainData]:
            result = await self._scene_query(client, [node_path])
            node = result.nodes[0] if result.nodes else None
            return {
                "ok": True,
                "path": node_path,
                "node_type": node.node_type if node is not None else None,
                "is_locked": node.is_locked if node is not None else None,
                "geometry_stats": (
                    node.geometry_stats if node is not None else None
                ),
            }

        return await self._run_with_stale_retry(operation)

    async def work_status(
        self, workspace_id: str
    ) -> Mapping[str, PlainData]:
        invalid = self._invalid_workspace_id(workspace_id)
        if invalid is not None:
            return invalid

        async def operation(client: BridgeClient) -> dict[str, PlainData]:
            inspection = await self._inspect_selection(client)
            matched = self._workspace_observations(inspection, workspace_id)
            capped = matched[:_MAX_NODES]
            return {
                "ok": True,
                "workspace_id": workspace_id,
                "scope": "selection",
                "scene_epoch": inspection.binding.scene_epoch,
                "selected_node_count": len(inspection.observations),
                "node_count": len(matched),
                "locked_node_count": sum(1 for item in matched if item.is_locked),
                "truncated": len(matched) > _MAX_NODES,
                "paths": [item.path for item in capped],
            }

        return await self._run(operation)

    # -------------------------------------------------------------- internals

    async def _run(
        self, operation: Callable[[BridgeClient], Awaitable[dict[str, PlainData]]]
    ) -> dict[str, PlainData]:
        """Run one read through a short-lived client; never raise."""
        if not self._handoff_exists():
            return _not_available()
        try:
            client = self._client_factory(self._state_dir)
        except (BridgeTokenError, BridgeIdentityError):
            if not self._handoff_exists():
                return _not_available()
            return _unavailable(
                "bridge.auth_failed",
                "The Houdini Bridge identity could not be verified.",
            )
        try:
            async with asyncio.timeout(self._deadline_ms / 1000.0):
                async with client:
                    return await operation(client)
        except BridgeClientError as exc:
            return _unavailable(exc.code, exc.message_for_user)
        except TimeoutError:
            return _unavailable(
                "bridge.deadline_exceeded",
                "The Houdini Bridge request exceeded its deadline.",
            )
        except OSError:
            return _not_available()
        except Exception:  # noqa: BLE001 - detail must never reach the model
            return _unavailable(
                "bridge.unavailable",
                "The trusted read-only provider is unavailable.",
            )

    async def _run_with_stale_retry(
        self, operation: Callable[[BridgeClient], Awaitable[dict[str, PlainData]]]
    ) -> dict[str, PlainData]:
        """Retry the two-read binding cycle once on a stale-scene boundary."""
        result = await self._run(operation)
        if result.get("ok") is False and result.get("code") in _STALE_RETRY_CODES:
            result = await self._run(operation)
        return result

    async def _inspect_selection(self, client: BridgeClient) -> WorkspaceInspectResult:
        return await client.inspect_workspace(
            WorkspaceInspectRequest(
                request_id=self._request_id("inspect"),
                deadline_ms=self._deadline_ms,
                scene_epoch=None,
                mode="selection",
                manifest=None,
            )
        )

    async def _scene_query(self, client: BridgeClient, node_paths: list[str]):
        """Two-read cycle: nullable-epoch binding, then exact-epoch scene query."""
        inspection = await self._inspect_selection(client)
        binding = inspection.binding
        result = await client.request(
            BridgeRequest(
                request_id=self._request_id("query"),
                operation=BridgeOperation.SCENE_QUERY,
                deadline_ms=self._deadline_ms,
                scene_epoch=binding.scene_epoch,
                payload={
                    "include_selection": False,
                    "node_paths": list(node_paths),
                    "include_geometry_stats": True,
                },
            )
        )
        if (
            result.binding.instance_id != binding.instance_id
            or result.binding.scene_epoch != binding.scene_epoch
        ):
            raise BridgeClientError(
                code="bridge.binding_mismatch",
                category="stale_scene",
                message_for_user="The Secure Bridge returned inconsistent scene identity.",
                retryable=True,
            )
        return result

    @staticmethod
    def _workspace_observations(
        inspection: WorkspaceInspectResult, workspace_id: str
    ) -> list:
        return [
            item
            for item in inspection.observations
            if item.workspace_id == workspace_id
        ]

    @staticmethod
    def _invalid_workspace_id(workspace_id: str) -> dict[str, PlainData] | None:
        if (
            type(workspace_id) is not str
            or not workspace_id
            or len(workspace_id) > _MAX_ID_LENGTH
        ):
            return _unavailable(
                "bridge.invalid_request", "workspace_id is invalid."
            )
        return None

    def _handoff_exists(self) -> bool:
        return (
            self._state_dir.joinpath(BRIDGE_DISCOVERY_FILENAME).is_file()
            and self._state_dir.joinpath(BRIDGE_TOKEN_FILENAME).is_file()
        )

    @staticmethod
    def _request_id(label: str) -> str:
        return f"req_readonly_{label}_{uuid.uuid4().hex}"


__all__ = ["BridgeReadOnlyProvider"]
