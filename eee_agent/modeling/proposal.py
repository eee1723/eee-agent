"""Trusted modeling proposal orchestration and bounded tool adapter.

This module does not own Runtime persistence or Houdini access. The caller
injects those as one narrow async callback after the pure compiler has produced
an allowed typed ChangeSet.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain.tools import ToolRuntime, tool

from eee_agent.changesets.contracts import (
    ChangeSet,
    PolicyDecision,
    WorkspaceManifest,
)
from eee_agent.changesets.policy import evaluate_policy
from eee_agent.core.ids import IdKind, new_id, require_id
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.modeling.compiler import (
    ModelingCompileError,
    NodeCatalog,
    WorkspaceBootstrapContext,
    compile_bootstrap_procedural_spec,
    compile_procedural_spec,
)
from eee_agent.modeling.contracts import (
    ModelingBrief,
    ProceduralSpec,
    QualityProfile,
)
from eee_agent.modeling.validation import validate_compilation
from eee_agent.runtime.agent_context import RuntimeToolContext

_MAX_SUMMARY_BYTES = 16 * 1024


class ModelingProposalError(ValueError):
    """Bounded failure from the proposal seam."""

    def __init__(self, code: str, message: str, *, cause_code: str | None = None):
        self.code = code
        self.cause_code = cause_code
        super().__init__(message)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ModelingProposalSummary:
    change_id: str
    changeset_digest: str
    workspace_id: str
    state: str
    approval_required: bool
    operation_count: int
    effect_names: tuple[str, ...]
    affected_path_count: int

    def __post_init__(self) -> None:
        require_id(self.change_id, IdKind.CHANGE)
        if type(self.changeset_digest) is not str or len(self.changeset_digest) != 64:
            raise ValueError("ModelingProposalSummary.changeset_digest is invalid")
        require_id(self.workspace_id, IdKind.WORKSPACE)
        if self.state != "AwaitingApproval":
            raise ValueError(
                "ModelingProposalSummary.state must be AwaitingApproval"
            )
        if type(self.approval_required) is not bool or not self.approval_required:
            raise ValueError(
                "ModelingProposalSummary.approval_required must be True"
            )
        if type(self.operation_count) is not int or not 1 <= self.operation_count <= 256:
            raise ValueError("ModelingProposalSummary.operation_count is out of bounds")
        if type(self.affected_path_count) is not int or not 1 <= self.affected_path_count <= 4096:
            raise ValueError(
                "ModelingProposalSummary.affected_path_count is out of bounds"
            )
        if isinstance(self.effect_names, str):
            raise TypeError("ModelingProposalSummary.effect_names must be a sequence")
        names = tuple(self.effect_names)
        if any(type(name) is not str for name in names):
            raise TypeError("ModelingProposalSummary.effect_names must contain strings")
        object.__setattr__(self, "effect_names", names)
        if len(self.to_dict()["effect_names"]) > 16:
            raise ValueError("ModelingProposalSummary has too many effect names")

    def to_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "changeset_digest": self.changeset_digest,
            "workspace_id": self.workspace_id,
            "state": self.state,
            "approval_required": self.approval_required,
            "operation_count": self.operation_count,
            "effect_names": list(self.effect_names),
            "affected_path_count": self.affected_path_count,
        }


ProposalCallback = Callable[
    [ChangeSet, PolicyDecision], Awaitable[object] | object
]


@dataclass(frozen=True, slots=True)
class ModelingProposalContext:
    session_id: str
    run_id: str
    workspace: WorkspaceManifest | None
    scene_binding: SceneBinding
    catalog: NodeCatalog
    quality_profile: QualityProfile
    propose_callback: ProposalCallback
    bootstrap: WorkspaceBootstrapContext | None = None
    clock: Callable[[], datetime] = _now_utc
    change_id_factory: Callable[[], str] = lambda: new_id(IdKind.CHANGE)

    def __post_init__(self) -> None:
        require_id(self.session_id, IdKind.SESSION)
        require_id(self.run_id, IdKind.RUN)
        if self.workspace is not None and type(self.workspace) is not WorkspaceManifest:
            raise TypeError(
                "ModelingProposalContext.workspace must be a WorkspaceManifest or None"
            )
        if self.bootstrap is not None and type(self.bootstrap) is not WorkspaceBootstrapContext:
            raise TypeError(
                "ModelingProposalContext.bootstrap must be a WorkspaceBootstrapContext or None"
            )
        if (self.workspace is None) == (self.bootstrap is None):
            raise ValueError(
                "ModelingProposalContext requires exactly one Workspace mode"
            )
        if type(self.scene_binding) is not SceneBinding:
            raise TypeError("ModelingProposalContext.scene_binding must be a SceneBinding")
        if type(self.catalog) is not NodeCatalog:
            raise TypeError("ModelingProposalContext.catalog must be a NodeCatalog")
        if type(self.quality_profile) is not QualityProfile:
            raise TypeError(
                "ModelingProposalContext.quality_profile must be a QualityProfile"
            )
        if not callable(self.propose_callback):
            raise TypeError("ModelingProposalContext.propose_callback must be callable")
        if not callable(self.clock) or not callable(self.change_id_factory):
            raise TypeError("ModelingProposalContext factories must be callable")


class ModelingProposalCoordinator:
    """Compile and persist one exact proposal through injected trust seams."""

    def __init__(self, context: ModelingProposalContext) -> None:
        if type(context) is not ModelingProposalContext:
            raise TypeError("context must be an exact ModelingProposalContext")
        self._context = context

    async def propose(
        self,
        *,
        brief_data: Mapping[str, object],
        spec_data: Mapping[str, object],
    ) -> ModelingProposalSummary:
        try:
            brief = ModelingBrief.from_dict(brief_data)
            trusted_spec = dict(spec_data)
            trusted_spec["brief_digest"] = brief.digest
            trusted_spec["quality_profile_id"] = (
                self._context.quality_profile.profile_id
            )
            trusted_spec["workspace_root_node_id"] = (
                self._context.workspace.roots[0].node_id
                if self._context.workspace is not None
                else "bootstrap_root"
            )
            spec = ProceduralSpec.from_dict(trusted_spec)
        except (TypeError, ValueError, KeyError) as exc:
            raise ModelingProposalError(
                "modeling.proposal_input_invalid",
                "The modeling Brief or Spec payload is invalid.",
            ) from exc

        change_id = self._context.change_id_factory()
        try:
            require_id(change_id, IdKind.CHANGE)
            created_at = self._context.clock()
            if self._context.workspace is not None:
                result = compile_procedural_spec(
                    brief=brief,
                    spec=spec,
                    quality_profile=self._context.quality_profile,
                    catalog=self._context.catalog,
                    workspace=self._context.workspace,
                    scene_binding=self._context.scene_binding,
                    session_id=self._context.session_id,
                    run_id=self._context.run_id,
                    change_id=change_id,
                    created_at=created_at,
                )
            else:
                assert self._context.bootstrap is not None
                result = compile_bootstrap_procedural_spec(
                    brief=brief,
                    spec=spec,
                    quality_profile=self._context.quality_profile,
                    catalog=self._context.catalog,
                    bootstrap=self._context.bootstrap,
                    scene_binding=self._context.scene_binding,
                    session_id=self._context.session_id,
                    run_id=self._context.run_id,
                    change_id=change_id,
                    created_at=created_at,
                )
            validation = validate_compilation(
                brief=brief,
                spec=spec,
                quality_profile=self._context.quality_profile,
                catalog=self._context.catalog,
                compilation=result,
            )
            if validation.hard_failures:
                first = validation.hard_failures[0]
                raise ModelingCompileError(first.code, first.message)
            decision = evaluate_policy(
                result.changeset, workspace=self._context.workspace
            )
        except (TypeError, ValueError, ModelingCompileError) as exc:
            cause_code = exc.code if isinstance(exc, ModelingCompileError) else None
            raise ModelingProposalError(
                "modeling.proposal_compile_failed",
                "The modeling intent could not be compiled into an allowed proposal.",
                cause_code=cause_code,
            ) from exc

        try:
            callback_result = self._context.propose_callback(
                result.changeset, decision
            )
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception as exc:  # noqa: BLE001 - bounded at this seam
            raise ModelingProposalError(
                "modeling.proposal_persist_failed",
                "The trusted proposal could not be persisted.",
            ) from exc

        summary = ModelingProposalSummary(
            change_id=result.changeset.change_id,
            changeset_digest=result.changeset.digest,
            workspace_id=(
                result.changeset.workspace_id
                or (
                    self._context.workspace.workspace_id
                    if self._context.workspace is not None
                    else self._context.bootstrap.workspace_id  # type: ignore[union-attr]
                )
            ),
            state="AwaitingApproval",
            approval_required=decision.approval_required,
            operation_count=len(result.changeset.operations),
            effect_names=result.changeset.risk_summary.effect_names,
            affected_path_count=len(result.changeset.risk_summary.affected_paths),
        )
        if len(str(summary.to_dict()).encode("utf-8")) > _MAX_SUMMARY_BYTES:
            raise ModelingProposalError(
                "modeling.proposal_persist_failed",
                "The bounded proposal summary exceeds its size limit.",
            )
        return summary


@dataclass(frozen=True, slots=True)
class ModelingToolContext:
    """Non-model context injected by a future Runtime graph integration."""

    coordinator: ModelingProposalCoordinator


@tool
async def propose_modeling(
    brief: dict[str, object],
    spec: dict[str, object],
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Propose a bounded procedural model for explicit user approval.

    This tool creates no scene effect. It compiles strict modeling intent and
    returns only a ChangeSet digest/risk summary; the Runtime must provide the
    trusted context. In ``spec``, Runtime supplies the brief digest, quality
    profile ID, and Workspace root binding; the model should supply the strict
    component/node/parameter/input graph rather than inventing scene IDs.
    """
    context = getattr(runtime, "context", None)
    if type(context) is not RuntimeToolContext:
        return {
            "ok": False,
            "code": "modeling.proposal_context_invalid",
            "message": "A trusted modeling context is unavailable.",
        }
    modeling_context = context.modeling
    if type(modeling_context) is not ModelingToolContext:
        return {
            "ok": False,
            "code": "modeling.proposal_context_invalid",
            "message": "A trusted modeling context is unavailable.",
        }
    try:
        summary = await modeling_context.coordinator.propose(
            brief_data=brief,
            spec_data=spec,
        )
    except ModelingProposalError as exc:
        result: dict[str, object] = {
            "ok": False,
            "code": exc.code,
            "message": str(exc),
        }
        if exc.cause_code is not None:
            result["cause_code"] = exc.cause_code
        return result
    return {"ok": True, "proposal": summary.to_dict()}


__all__ = [
    "ModelingProposalContext",
    "ModelingProposalCoordinator",
    "ModelingProposalError",
    "ModelingProposalSummary",
    "ModelingToolContext",
    "propose_modeling",
]
