"""Production typed ChangeSet provider over discovered Bridge clients."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from eee_agent.changesets.contracts import ChangeReceipt, ChangeSet, WorkspaceManifest
from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    BridgeIdentityError,
    BridgeTokenError,
)
from eee_agent.houdini_bridge.changesets import (
    ApplyRequest,
    PreflightRequest,
    PreflightResult,
    ReceiptRequest,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    BridgeOperation,
    BridgeRequest,
    SceneBinding,
    SceneQueryResult,
)
from eee_agent.houdini_bridge.sensitivity import (
    SensitivitySampleRequest,
    SensitivitySampleResult,
    SensitivitySampleTarget,
)
from eee_agent.houdini_bridge.workspaces import WorkspaceInspectRequest


def _error_category(category: str) -> ErrorCategory:
    if category == "stale_scene":
        return ErrorCategory.STALE_SCENE
    if category in ("auth", "permission"):
        return ErrorCategory.PERMISSION
    if category == "protocol":
        return ErrorCategory.PROTOCOL
    return ErrorCategory.HOUDINI_BRIDGE


def _bridge_error(exc: BridgeClientError, *, may_have_changed: bool) -> AgentException:
    return AgentException(
        AgentError(
            code=exc.code,
            category=_error_category(exc.category),
            message_for_user=exc.message_for_user,
            technical_detail_ref=exc.technical_detail_ref,
            retryable=exc.retryable,
            scene_may_have_changed=may_have_changed,
        )
    )


def _not_available(*, scene_may_have_changed: bool = False) -> AgentException:
    return AgentException(
        AgentError(
            code="bridge.not_available",
            category=ErrorCategory.HOUDINI_BRIDGE,
            message_for_user="The Houdini Bridge is not currently available.",
            retryable=True,
            scene_may_have_changed=scene_may_have_changed,
        )
    )


def _auth_failed() -> AgentException:
    return AgentException(
        AgentError(
            code="bridge.auth_failed",
            category=ErrorCategory.PERMISSION,
            message_for_user="The Houdini Bridge identity could not be verified.",
        )
    )


class BridgeChangeSetProvider:
    """Open one authenticated short-lived Bridge connection per typed call."""

    def __init__(self, state_dir: Path | str, *, deadline_ms: int = 5000) -> None:
        self._state_dir = Path(state_dir)
        if type(deadline_ms) is not int:
            raise TypeError("deadline_ms must be an exact integer")
        if deadline_ms < 1 or deadline_ms > 30_000:
            raise ValueError("deadline_ms must be in 1..30000")
        self._deadline_ms = deadline_ms

    async def current_binding(self) -> SceneBinding:
        request = WorkspaceInspectRequest(
            request_id=self._request_id("binding"),
            deadline_ms=self._deadline_ms,
            scene_epoch=None,
            mode="selection",
            manifest=None,
        )
        result = await self._call("inspect_workspace", request, may_have_changed=False)
        return result.binding

    async def inspect_geometry(self, changeset: ChangeSet) -> SceneQueryResult:
        """Read bounded Cook/geometry facts for exact compiled node paths."""
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        request = BridgeRequest(
            request_id=self._request_id("geometry"),
            operation=BridgeOperation.SCENE_QUERY,
            deadline_ms=self._deadline_ms,
            scene_epoch=changeset.scene_binding.scene_epoch,
            payload={
                "include_selection": False,
                "node_paths": [node.path for node in changeset.affected_nodes],
                "include_geometry_stats": True,
            },
        )
        return await self._call("request", request, may_have_changed=False)

    async def preflight(
        self, changeset: ChangeSet, workspace: WorkspaceManifest | None
    ) -> PreflightResult:
        self._validate_inputs(changeset, workspace)
        request = PreflightRequest.build(
            request_id=self._request_id("preflight"),
            deadline_ms=self._deadline_ms,
            scene_epoch=changeset.scene_binding.scene_epoch,
            changeset=changeset,
            workspace=workspace,
        )
        return await self._call("preflight", request, may_have_changed=False)

    async def apply(
        self, changeset: ChangeSet, workspace: WorkspaceManifest | None
    ) -> ChangeReceipt:
        self._validate_inputs(changeset, workspace)
        request = ApplyRequest.build(
            request_id=self._request_id("apply"),
            deadline_ms=self._deadline_ms,
            scene_epoch=changeset.scene_binding.scene_epoch,
            changeset=changeset,
            workspace=workspace,
        )
        return await self._call("apply", request, may_have_changed=True)

    async def sample_sensitivity(
        self, changeset: ChangeSet, samples: tuple[SensitivitySampleTarget, ...]
    ) -> SensitivitySampleResult:
        """Run the typed sample-and-restore cycle for exact compiled paths.

        The write phase is bounded to the catalog-derived sample targets; the
        bridge restores every written parameter exactly before returning. Any
        uncertainty raises with ``scene_may_have_changed=True``.
        """
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        request = SensitivitySampleRequest.build(
            request_id=self._request_id("sample"),
            deadline_ms=self._deadline_ms,
            scene_epoch=changeset.scene_binding.scene_epoch,
            node_paths=[node.path for node in changeset.affected_nodes],
            samples=samples,
        )
        return await self._call("sample_sensitivity", request, may_have_changed=True)

    async def receipt(self, changeset: ChangeSet) -> ChangeReceipt:
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        request = ReceiptRequest.build(
            request_id=self._request_id("receipt"),
            deadline_ms=self._deadline_ms,
            scene_epoch=changeset.scene_binding.scene_epoch,
            change_id=changeset.change_id,
            changeset_digest=changeset.digest,
        )
        return await self._call("receipt", request, may_have_changed=False)

    async def _call(self, method: str, request: object, *, may_have_changed: bool):
        if not self._handoff_exists():
            raise _not_available()
        try:
            client = BridgeClient.from_state_dir(self._state_dir)
        except (BridgeTokenError, BridgeIdentityError) as exc:
            if not self._handoff_exists():
                raise _not_available() from exc
            raise _auth_failed() from exc

        try:
            async with asyncio.timeout(self._deadline_ms / 1000.0):
                async with client:
                    operation = getattr(client, method)
                    return await operation(request)
        except BridgeClientError as exc:
            raise _bridge_error(exc, may_have_changed=may_have_changed) from exc
        except TimeoutError as exc:
            raise AgentException(
                AgentError(
                    code="bridge.deadline_exceeded",
                    category=ErrorCategory.HOUDINI_BRIDGE,
                    message_for_user="The Houdini Bridge request exceeded its deadline.",
                    retryable=True,
                    scene_may_have_changed=may_have_changed,
                )
            ) from exc
        except OSError as exc:
            raise _not_available(
                scene_may_have_changed=may_have_changed
            ) from exc

    def _handoff_exists(self) -> bool:
        return (
            self._state_dir.joinpath(BRIDGE_DISCOVERY_FILENAME).is_file()
            and self._state_dir.joinpath(BRIDGE_TOKEN_FILENAME).is_file()
        )

    @staticmethod
    def _validate_inputs(
        changeset: ChangeSet, workspace: WorkspaceManifest | None
    ) -> None:
        if type(changeset) is not ChangeSet:
            raise TypeError("changeset must be an exact ChangeSet")
        if workspace is not None and type(workspace) is not WorkspaceManifest:
            raise TypeError("workspace must be an exact WorkspaceManifest or None")

    @staticmethod
    def _request_id(operation: str) -> str:
        return f"req_{operation}_{uuid.uuid4().hex}"


__all__ = ["BridgeChangeSetProvider"]
