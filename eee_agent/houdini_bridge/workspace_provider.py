"""Production async workspace-fact provider over discovered Bridge clients."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from eee_agent.changesets.contracts import WorkspaceManifest
from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    BridgeIdentityError,
    BridgeTokenError,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectRequest,
    WorkspaceInspectResult,
    WorkspaceInspectionConflict,
    WorkspaceInspectionUnavailable,
)

_UNAVAILABLE_CODES = frozenset(
    {
        "bridge.not_available",
        "bridge.deadline_exceeded",
        "bridge.cancelled",
        "bridge.capability_unavailable",
    }
)


class BridgeWorkspaceFactProvider:
    """Opens one short-lived discovered Bridge client for each live read."""

    def __init__(self, state_dir: Path | str, *, deadline_ms: int = 5000) -> None:
        self._state_dir = Path(state_dir)
        if type(deadline_ms) is not int:
            raise TypeError("deadline_ms must be an exact integer")
        if deadline_ms < 1 or deadline_ms > 30_000:
            raise ValueError("deadline_ms must be in 1..30000")
        self._deadline_ms = deadline_ms

    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        request = WorkspaceInspectRequest(
            request_id=self._new_request_id(),
            deadline_ms=self._deadline_ms,
            scene_epoch=expected_scene_epoch,
            mode="selection",
            manifest=None,
        )
        return await self._inspect(request)

    async def inspect_manifest(
        self,
        manifest: WorkspaceManifest,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectResult:
        if type(manifest) is not WorkspaceManifest:
            raise TypeError("manifest must be an exact WorkspaceManifest")
        request = WorkspaceInspectRequest(
            request_id=self._new_request_id(),
            deadline_ms=self._deadline_ms,
            scene_epoch=expected_scene_epoch,
            mode="manifest",
            manifest=manifest,
        )
        return await self._inspect(request)

    async def _inspect(
        self, request: WorkspaceInspectRequest
    ) -> WorkspaceInspectResult:
        if not self._handoff_exists():
            raise WorkspaceInspectionUnavailable(
                "The Houdini Bridge is not currently available."
            )
        try:
            client = BridgeClient.from_state_dir(self._state_dir)
        except BridgeTokenError as exc:
            # A handoff disappearing between the existence check and the read is
            # an ordinary shutdown race. Existing but malformed files remain a
            # hard authentication/protocol failure and are not hidden offline.
            if not self._handoff_exists():
                raise WorkspaceInspectionUnavailable(
                    "The Houdini Bridge is not currently available."
                ) from exc
            raise
        except BridgeIdentityError:
            raise

        try:
            async with asyncio.timeout(self._deadline_ms / 1000.0):
                async with client:
                    return await client.inspect_workspace(request)
        except BridgeClientError as exc:
            if exc.code in _UNAVAILABLE_CODES:
                raise WorkspaceInspectionUnavailable(
                    "The Houdini Bridge is not currently available."
                ) from exc
            if exc.code == "workspace.identity_conflict":
                raise WorkspaceInspectionConflict(
                    "The live workspace identity is ambiguous."
                ) from exc
            if exc.code == "bridge.stale_scene":
                raise AgentException(
                    AgentError(
                        code=exc.code,
                        category=ErrorCategory.STALE_SCENE,
                        message_for_user=exc.message_for_user,
                        technical_detail_ref=exc.technical_detail_ref,
                        retryable=exc.retryable,
                    )
                ) from exc
            raise
        except (OSError, TimeoutError) as exc:
            raise WorkspaceInspectionUnavailable(
                "The Houdini Bridge is not currently available."
            ) from exc

    def _handoff_exists(self) -> bool:
        return (
            self._state_dir.joinpath(BRIDGE_DISCOVERY_FILENAME).is_file()
            and self._state_dir.joinpath(BRIDGE_TOKEN_FILENAME).is_file()
        )

    @staticmethod
    def _new_request_id() -> str:
        return f"req_workspace_{uuid.uuid4().hex}"


__all__ = ["BridgeWorkspaceFactProvider"]
