"""Trusted scratch sandbox orchestration and bounded tool adapter.

This module is the agent-facing half of the sandbox+verify+commit workflow
(Phase 1 of the Pi-model pivot). It owns NO Houdini access and NO persistence.
The caller injects one narrow async capability: a :class:`ScratchProvider`
that runs a structured batch of operations against the isolated
``/obj/eee_scratch_<id>`` geo container on the Houdini side and returns
bounded diagnostics.

The design mirrors :mod:`eee_agent.modeling.proposal`:
- a frozen, validated context dataclass (:class:`ScratchSessionContext`);
- a coordinator (:class:`ScratchCoordinator`) that translates plain tool
  arguments into typed DTOs and calls the injected provider; and
- a bounded ``@tool`` (:func:`scratch_build`) that the Runtime graph exposes
  to the agent.

Phase 1 supports structured single-op mode (create_node / set_parm / connect).
Raw-Python ``exec`` (network_mode) is deferred: structured ops are sub-second
and fit the existing 30s synchronous bridge, so an async job protocol is not
needed yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from langchain.tools import ToolRuntime, tool

from eee_agent.core.errors import AgentException

from eee_agent.houdini_bridge.scratch import (
    ScratchCommitResult,
    ScratchDestroyResult,
    ScratchGeometry,
    ScratchOp,
    ScratchResult,
)
from eee_agent.runtime.agent_context import RuntimeToolContext

# Bounded input limit enforced at the tool seam before the bridge wire.
# Per-field length bounds (node name/type/parm name, sandbox id) are enforced
# by the DTO layer in scratch.py (ScratchOp.from_dict / ScratchRequest), so
# they are not duplicated here.
_MAX_OPS_PER_CALL = 64


class ScratchError(ValueError):
    """Bounded failure from the scratch seam."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@runtime_checkable
class ScratchProvider(Protocol):
    """The single injected capability: run structured ops in a sandbox.

    Implemented by :class:`eee_agent.houdini_bridge.changeset_provider.
    BridgeChangeSetProvider` (its ``scratch_exec`` / ``scratch_commit``
    methods). Kept as a Protocol here so this module stays free of
    Houdini/bridge imports — the agent-facing layer depends only on the DTO
    result types.
    """

    async def scratch_exec(
        self,
        *,
        sandbox_id: str,
        operations: tuple[ScratchOp, ...],
        preserve_on_failure: bool = True,
    ) -> ScratchResult: ...

    async def scratch_commit(
        self,
        *,
        sandbox_id: str,
        target_parent_path: str,
        target_name: str,
        orientation_checks: tuple = (),
        skip_structure_check: bool = False,
    ) -> ScratchCommitResult: ...

    async def scratch_destroy(
        self,
        *,
        sandbox_id: str,
    ) -> ScratchDestroyResult: ...


@dataclass(frozen=True, slots=True)
class ScratchSessionContext:
    """Per-run trusted scratch context consumed by :func:`scratch_build`.

    Carries the injected provider and the run-scoped sandbox id prefix so the
    agent's sandbox containers are namespaced to this run and cannot collide
    across concurrent runs.
    """

    provider: ScratchProvider
    sandbox_id: str

    def __post_init__(self) -> None:
        if self.provider is None or not isinstance(self.provider, ScratchProvider):
            raise TypeError(
                "ScratchSessionContext.provider must implement ScratchProvider"
            )
        if type(self.sandbox_id) is not str or not self.sandbox_id:
            raise TypeError("ScratchSessionContext.sandbox_id must be a non-empty string")


class ScratchCoordinator:
    """Validate tool input, call the provider, return a bounded plain result.

    Owns no state. Translates the agent's plain-dict operation list into typed
    :class:`ScratchOp` instances, delegates to the injected provider, and maps
    every failure into a bounded ``{"ok": False, ...}`` dict so the tool never
    raises into the graph.
    """

    def __init__(self, context: ScratchSessionContext) -> None:
        if type(context) is not ScratchSessionContext:
            raise TypeError("context must be an exact ScratchSessionContext")
        self._context = context

    async def build(
        self,
        *,
        operations: list[Mapping[str, object]],
        preserve_on_failure: bool = True,
    ) -> dict[str, object]:
        sandbox_id = self._context.sandbox_id
        try:
            typed_ops = self._parse_operations(operations)
        except ScratchError as exc:
            return {"ok": False, "code": exc.code, "message": str(exc)}
        try:
            result = await self._context.provider.scratch_exec(
                sandbox_id=sandbox_id,
                operations=typed_ops,
                preserve_on_failure=preserve_on_failure,
            )
        except Exception as exc:  # noqa: BLE001 - bounded at this seam
            return _bridge_or_op_failure(exc, default="scratch.op_failed")
        return self._summarize(result)

    async def commit(
        self,
        *,
        target_parent_path: str,
        target_name: str,
        orientation_checks: list[Mapping[str, object]] | None = None,
        skip_structure_check: bool = False,
    ) -> dict[str, object]:
        """Commit a verified sandbox into the real scene through hard gates.

        Returns a bounded verdict: ok=True (committed) or ok=True with
        refused=True (a hard gate failed — the sandbox is preserved so the
        agent can fix and retry). On a transport/bridge failure returns a
        bounded ``{ok: False, ...}``.
        """
        sandbox_id = self._context.sandbox_id
        # Validate target path/name up front so invalid input fails before the
        # bridge call (matching scratch_build's defensive style).
        if type(target_parent_path) is not str or not target_parent_path.startswith("/"):
            return {
                "ok": False,
                "code": "scratch.input_invalid",
                "message": "target_parent_path must be an absolute node path.",
            }
        if type(target_name) is not str or not target_name:
            return {
                "ok": False,
                "code": "scratch.input_invalid",
                "message": "target_name must be a non-empty node name.",
            }
        try:
            checks = self._parse_orientation_checks(orientation_checks)
        except ScratchError as exc:
            return {"ok": False, "code": exc.code, "message": str(exc)}
        try:
            result = await self._context.provider.scratch_commit(
                sandbox_id=sandbox_id,
                target_parent_path=target_parent_path,
                target_name=target_name,
                orientation_checks=checks,
                skip_structure_check=skip_structure_check,
            )
        except Exception as exc:  # noqa: BLE001 - bounded at this seam
            return _bridge_or_op_failure(exc, default="scratch.op_failed")
        return self._summarize_commit(result)

    def _parse_orientation_checks(
        self, checks: object
    ) -> tuple[dict[str, object], ...]:
        if checks is None:
            return ()
        if not isinstance(checks, (list, tuple)):
            raise ScratchError(
                "scratch.input_invalid",
                "orientation_checks must be a list.",
            )
        if len(checks) > _MAX_OPS_PER_CALL:
            raise ScratchError(
                "scratch.input_invalid",
                f"orientation_checks may contain at most {_MAX_OPS_PER_CALL} checks.",
            )
        out: list[dict[str, object]] = []
        for item in checks:
            if not isinstance(item, Mapping):
                raise ScratchError(
                    "scratch.input_invalid",
                    "each orientation check must be an object.",
                )
            clean: dict[str, object] = {}
            for key in ("component_id", "kind", "expected_axis",
                        "tolerance_deg", "signed", "construction_axis"):
                if key in item:
                    clean[key] = item[key]
            out.append(clean)
        return tuple(out)

    @staticmethod
    def _summarize_commit(result: ScratchCommitResult) -> dict[str, object]:
        return {
            "ok": True,
            "committed": result.committed,
            "refused": result.refused,
            "final_path": result.final_path,
            "reason": result.reason,
            "gates": [dict(g) for g in result.gates],
            "receipt": dict(result.receipt),
        }

    def _parse_operations(
        self, operations: object
    ) -> tuple[ScratchOp, ...]:
        if type(operations) is not list or not operations:
            raise ScratchError(
                "scratch.input_invalid",
                "operations must be a non-empty list.",
            )
        if len(operations) > _MAX_OPS_PER_CALL:
            raise ScratchError(
                "scratch.input_invalid",
                f"operations may contain at most {_MAX_OPS_PER_CALL} ops; "
                "iterate by calling scratch_build again.",
            )
        typed: list[ScratchOp] = []
        for item in operations:
            if not isinstance(item, Mapping):
                raise ScratchError(
                    "scratch.input_invalid",
                    "each operation must be an object.",
                )
            try:
                typed.append(ScratchOp.from_dict(item))
            except (TypeError, ValueError) as exc:
                raise ScratchError(
                    "scratch.input_invalid",
                    f"an operation is invalid: {exc}",
                ) from exc
        return tuple(typed)

    @staticmethod
    def _summarize(result: ScratchResult) -> dict[str, object]:
        # M2: a partial build (some ops failed to apply) is NOT a clean success.
        # Report ok=False when any per-op error was recorded so the model does
        # not proceed on a half-applied graph. The errors list still carries the
        # per-op detail for self-correction; transport/exception failures stay
        # on the separate ok=False path in _bridge_or_op_failure.
        summary: dict[str, object] = {
            "ok": not bool(result.errors),
            "sandbox_root": result.sandbox_root,
            "applied_ops": result.applied_ops,
            "output_node": result.output_node,
            "errors": list(result.errors),
        }
        if result.geometry is not None:
            summary["geometry"] = _geometry_summary(result.geometry)
        else:
            summary["geometry"] = None
        return summary


def _geometry_summary(geo: ScratchGeometry) -> dict[str, object]:
    return {
        "point_count": geo.point_count,
        "prim_count": geo.prim_count,
        "vertex_count": geo.vertex_count,
        "bbox_min": list(geo.bbox_min),
        "bbox_max": list(geo.bbox_max),
    }


def _bridge_or_op_failure(exc: BaseException, *, default: str) -> dict[str, object]:
    """Map a scratch provider exception to a bounded ``{ok: False, ...}`` dict.

    Distinguishes a transport-level bridge failure (the sandbox was never
    reached) from an operation-level failure (the bridge ran an op that raised
    — the sandbox may carry partial work the agent can inspect). An
    ``AgentException`` carrying a ``bridge.not_available`` / ``bridge.auth_failed``
    code is reported as transport-level; anything else (including an executor
    ``bridge.scratch_failed`` or a generic provider error) is reported as an
    operation failure and its message is forwarded verbatim so the agent can
    see how far the build got.
    """
    if isinstance(exc, AgentException):
        code = getattr(exc.error, "code", "")
        if code in ("bridge.not_available", "bridge.auth_failed"):
            return {
                "ok": False,
                "code": "scratch.bridge_unavailable",
                "message": (
                    "The sandbox build could not reach the Houdini bridge; the "
                    "sandbox was not modified."
                ),
            }
        msg = getattr(exc.error, "message_for_user", "") or str(exc)
        return {"ok": False, "code": default, "message": msg}
    msg = str(exc) or "The sandbox operation failed."
    return {"ok": False, "code": default, "message": msg}



@dataclass(frozen=True, slots=True)
class ScratchToolContext:
    """Non-model context injected by the Runtime graph for scratch runs."""

    coordinator: ScratchCoordinator


@tool
async def scratch_build(
    operations: list[dict[str, object]],
    runtime: ToolRuntime,
    preserve_on_failure: bool = True,
) -> dict[str, object]:
    """Build or refine nodes in an isolated sandbox and observe cooked results.

    This tool writes ONLY to a reserved sandbox container
    (``/obj/eee_scratch_<run>``) — never to the real scene. It runs structured
    operations, then returns bounded diagnostics (cooked geometry stats + cook
    errors) so you can iterate: build → observe → adjust → rebuild. Nothing is
    committed to the real scene by this tool; committing happens later through
    a separate hard-gated step.

    Submit operations in FUNCTIONAL UNITS, not one node at a time. One
    scratch_build call should build a complete, independently verifiable piece
    of the asset — e.g. (create a template primitive + set all its parms) or
    (create a scatter source + set its parms + create a copy-to-points + wire
    both inputs). Batching a functional unit into one call keeps the agent loop
    short (fewer turns, far fewer repeated-input tokens) while still letting you
    observe and correct each unit before moving on. The hard cap is 64
    operations per call; stay well under it and group by what you can verify at
    once. Avoid the opposite extreme too — don't dump an entire complex asset's
    whole node tree in one call, since a single bad parm then fails the whole
    batch and is harder to localize.

    operations — a non-empty list (<=64) of operation objects. Each has:
      kind: "create_node" | "set_parm" | "connect"
      For create_node:
        node_name: identifier (^[A-Za-z_][A-Za-z0-9_]*$, <=64 chars)
        node_type: a catalog node type (e.g. "box", "grid", "xform",
          "polyextrude2", "copytopoints2", "sweep2", "merge", "null",
          "fuse2", "subdivide", "resample", "boolean2", "line", "geo")
        parent: (optional) sandbox-relative name of a geo node to parent under;
          omit or "" to create directly under the sandbox container
      For set_parm:
        node_name: the node to modify (must exist in the sandbox)
        parm: the parameter name (^[A-Za-z_][A-Za-z0-9_]*$)
        value: a number, integer, boolean, string, or a homogeneous list of
          <=16 numbers; literal values only — no expressions, no file paths
      For connect:
        node_name: the node whose input to wire
        input_index: int >= 0
        source: the upstream node name inside the sandbox
        source_output_index: int >= 0

    preserve_on_failure: (default true) keep the sandbox container on failure
      so you can inspect and retry. Pass false only to discard a failed sandbox.

    Returns a bounded object:
      ok: bool
      sandbox_root: the container path (e.g. "/obj/eee_scratch_...")
      applied_ops: how many operations were applied
      output_node: the output node path (the last created node, or the container)
      errors: list of cook errors (empty when healthy)
      geometry: null or {point_count, prim_count, vertex_count, bbox_min, bbox_max}

    After a successful scratch_build, use geometry_stats / query_scene to read
    the output_node path for deeper inspection. Repeat scratch_build until the
    cooked geometry matches your intent, then proceed to verify + commit.

    Example — a full functional unit in one call (template + scatter source +
    copy-to-points, all wired), verifiable as one piece:
      operations = [
        {"kind": "create_node", "node_name": "template", "node_type": "tube"},
        {"kind": "set_parm", "node_name": "template", "parm": "rad1", "value": 0.5},
        {"kind": "set_parm", "node_name": "template", "parm": "rad2", "value": 0.0},
        {"kind": "set_parm", "node_name": "template", "parm": "height", "value": 2.0},
        {"kind": "create_node", "node_name": "pts", "node_type": "grid"},
        {"kind": "set_parm", "node_name": "pts", "parm": "size", "value": [10.0, 10.0]},
        {"kind": "create_node", "node_name": "scatter", "node_type": "scatter"},
        {"kind": "set_parm", "node_name": "scatter", "parm": "npts", "value": 100},
        {"kind": "connect", "node_name": "scatter", "input_index": 0, "source": "pts", "source_output_index": 0},
        {"kind": "create_node", "node_name": "copy", "node_type": "copytopoints2"},
        {"kind": "connect", "node_name": "copy", "input_index": 0, "source": "template", "source_output_index": 0},
        {"kind": "connect", "node_name": "copy", "input_index": 1, "source": "scatter", "source_output_index": 0},
      ]
    """
    context = getattr(runtime, "context", None)
    if type(context) is not RuntimeToolContext:
        return {
            "ok": False,
            "code": "scratch.context_invalid",
            "message": "A trusted scratch context is unavailable.",
        }
    scratch_context = getattr(context, "scratch", None)
    if type(scratch_context) is not ScratchToolContext:
        return {
            "ok": False,
            "code": "scratch.context_invalid",
            "message": "A trusted scratch context is unavailable.",
        }
    if type(preserve_on_failure) is not bool:
        return {
            "ok": False,
            "code": "scratch.input_invalid",
            "message": "preserve_on_failure must be a boolean.",
        }
    return await scratch_context.coordinator.build(
        operations=operations,
        preserve_on_failure=preserve_on_failure,
    )


@tool
async def scratch_commit(
    target_parent_path: str,
    target_name: str,
    runtime: ToolRuntime,
    orientation_checks: list[dict[str, object]] | None = None,
    skip_structure_check: bool = False,
) -> dict[str, object]:
    """Promote the verified sandbox into the real scene through hard gates.

    This is the COMMIT step of the sandbox→verify→commit workflow. It runs four
    hard quality gates on your sandbox output (bake / structure / orientation /
    health). If ALL hard gates pass, the sandbox is promoted into the real scene
    at ``target_parent_path/target_name``. If ANY hard gate fails, the commit is
    REFUSED and the sandbox is preserved untouched so you can fix the problem
    and re-commit.

    IMPORTANT: only call scratch_commit AFTER you have iterated with
    scratch_build until the cooked geometry matches your intent. Do NOT call it
    speculatively. A refused commit is not an error to retry blindly — read the
    ``reason`` and ``gates`` fields, fix the named defect in the sandbox, then
    re-commit.

    target_parent_path: absolute path of the parent to promote into (e.g. "/obj"
      for a top-level asset, or a workspace container path).
    target_name: the node name for the promoted asset (must be a valid Houdini
      node name). A node must NOT already exist at the combined path — if it
      does, the commit is refused.
    orientation_checks: (optional) list of orientation assertions for assets
      with construction axes. Each check:
      {component_id, kind ("radial"|"elongated"|"planar"), expected_axis
      ("X"|"Y"|"Z"|"-X"|"-Y"|"-Z"), tolerance_deg (default 15), signed (bool),
      construction_axis (optional override)}. Omit for assets with no
      meaningful construction axes.
    skip_structure_check: (default false) pass true ONLY for genuinely simple
      single-piece assets. Never use it to bypass a monolithic-structure
      failure.

    Returns a bounded object:
      ok: bool (true if the request reached the bridge; a refused commit is
        still ok=true with refused=true)
      committed: bool (true if the sandbox was promoted to the real scene)
      refused: bool (true if a hard gate failed; exactly one of committed/refused)
      final_path: the promoted node path, or the sandbox path if refused
      reason: empty when committed, or a summary of the failed gates when refused
      gates: list of per-gate results {gate, passed, hard, reason, detail}
      receipt: tamper-evident verification summary {passed, orientation,
        health} — reference its fields in your report, do NOT re-count geometry

    After a committed result, the sandbox container no longer exists (it was
    renamed). Report the final_path and the receipt to the user.
    """
    context = getattr(runtime, "context", None)
    if type(context) is not RuntimeToolContext:
        return {
            "ok": False,
            "code": "scratch.context_invalid",
            "message": "A trusted scratch context is unavailable.",
        }
    scratch_context = getattr(context, "scratch", None)
    if type(scratch_context) is not ScratchToolContext:
        return {
            "ok": False,
            "code": "scratch.context_invalid",
            "message": "A trusted scratch context is unavailable.",
        }
    if type(skip_structure_check) is not bool:
        return {
            "ok": False,
            "code": "scratch.input_invalid",
            "message": "skip_structure_check must be a boolean.",
        }
    return await scratch_context.coordinator.commit(
        target_parent_path=target_parent_path,
        target_name=target_name,
        orientation_checks=orientation_checks,
        skip_structure_check=skip_structure_check,
    )


__all__ = [
    "ScratchCoordinator",
    "ScratchError",
    "ScratchProvider",
    "ScratchSessionContext",
    "ScratchToolContext",
    "scratch_build",
    "scratch_commit",
]
