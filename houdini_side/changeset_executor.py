"""ChangeSet preflight adapter + transactional executor for the secure bridge.

Two classes share this module:

* :class:`ChangeSetPreflightAdapter` (Task 16-C) derives bounded, typed scene
  facts needed to independently verify a
  :class:`~eee_agent.houdini_bridge.changesets.PreflightRequest`, and evaluates
  the supplied preconditions against those facts. It performs **no mutation**:
  it never creates/destroys nodes, sets parms, connects inputs, writes user
  data, touches undo, loads/saves/clears the HIP, installs HDAs or source,
  runs a shell, or evaluates arbitrary Python/VEX.
* :class:`ChangeSetExecutor` is the mutating counterpart: transactional
  ChangeSet apply, sensitivity sampling, capture rendering, and the
  ``scratch.exec/commit/destroy`` sandbox operations — all serialized through
  the same main-thread FIFO with journaled rollback on failure.

The adapter never imports ``hou`` at module import time. ``hou`` is reached
through the injected :class:`~houdini_side.secure_bridge.HoudiniSceneAdapter`
(same package), which holds the ``hou`` module bound inside the Houdini process.
Offline tests inject a ``hou``-free fake scene instead. All HOM access happens
inside the synchronous :meth:`preflight` callable, which the shared
:class:`~eee_agent.houdini_bridge.queue.MainThreadReadQueue` pumps on the
Houdini main thread — there is no second queue, worker, task, or concurrent HOM
path.

Identity resolution follows the design (sections 7–8): a stable owned node id
(reported in the mirrored ``eee.node_id`` user-data key) is resolved BEFORE the
path, and any ambiguity (one stable id mirrored at two distinct paths) fails
closed. A path is never identity by itself. The bounded scene identity resolver
performs a single read-only pass over the scene root's sub-tree, indexing every
node that mirrors an ``eee.node_id``; an owned reference resolves to the unique
current node with that id (even at a moved path), and more than one such node
is ``policy.ownership_ambiguous``. For owned manifest references the mirrored
``eee.schema_version`` and ``eee.created_by_run`` are validated as identity
facts (schema must be the supported schema-v1 value; the run of origin must
agree with the supplied manifest); these raw values are never exposed in a DTO.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from eee_agent.changesets.contracts import (
    ChangeReceipt,
    ChangeSet,
    ConditionResult,
    ConnectInput,
    CreateNode,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    ParmSnapshot,
    ParmValueEquals,
    ReceiptStatus,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WireRef,
    WireSnapshot,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
    _derive_create_path,
    _parm_value_json,
    _validate_parm_value,
)
from eee_agent.changesets.policy import evaluate_policy
from eee_agent.houdini_bridge.capture import (
    PNG_MEDIA_TYPE,
    _MAX_PNG_BYTES,
    CaptureFramingReport,
    CaptureRequest,
    CaptureResult,
)
from eee_agent.houdini_bridge.scratch import (
    ScratchCommitRequest,
    ScratchCommitResult,
    ScratchDeleteRequest,
    ScratchDeleteResult,
    ScratchDestroyRequest,
    ScratchDestroyResult,
    ScratchExpr,
    ScratchGeometry,
    ScratchOp,
    ScratchRequest,
    ScratchResult,
    ScratchTopologyRequest,
    ScratchTopologyResult,
)
from eee_agent.houdini_bridge.changesets import (
    ApplyRequest,
    PreflightNodeFact,
    PreflightParmFact,
    PreflightRequest,
    PreflightResult,
    PreflightWireFact,
)
from eee_agent.houdini_bridge.sensitivity import (
    SensitivitySampleRequest,
    SensitivitySampleResult,
)
from eee_agent.modeling.framing import (
    BoundingBox,
    FramingError,
    FramingTolerance,
    camera_basis,
    compute_framing,
)
from eee_agent.runtime.models import canonical_json_dumps
from houdini_side.secure_bridge import HoudiniAdapterError, HoudiniSceneAdapter


# Mirrored ownership keys (design section 4.1): path is a locator, never identity.
_WS_KEY = "eee.workspace_id"
_NODE_ID_KEY = "eee.node_id"
_CAP_KEY = "eee.capability"
_ROLE_KEY = "eee.role"
_SCHEMA_KEY = "eee.schema_version"
_RUN_KEY = "eee.created_by_run"
# The only supported WorkspaceManifest schema version is 1 (the contracts reject
# every other value). The mirror carries it as the user-data string "1".
_SUPPORTED_SCHEMA_VERSION = "1"


def _identity(ref: NodeRef) -> str:
    """Stable identity of a node reference: id when present, else path."""
    return ref.node_id if ref.node_id is not None else ref.path


def _stale(message: str) -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="changeset.stale",
        category="stale",
        message_for_user=message,
        retryable=True,
    )


def _scratch_failed(message: str) -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="bridge.scratch_failed",
        category="runtime",
        message_for_user=message,
        retryable=True,
    )


def _gate_failure_reason(gate_report: dict) -> str:
    """Summarize the hard gate failures into one bounded reason string."""
    failures = gate_report.get("hard_failures", [])
    if not failures:
        return ""
    parts = []
    for g in failures[:4]:
        name = g.get("gate", "?")
        reason = g.get("reason", "") or "failed"
        parts.append(f"{name}: {reason[:200]}")
    return "; ".join(parts)[:_MAX_ERROR_CHARS]


def _build_commit_receipt(
    gate_report: dict, parameters: tuple[object, ...] = ()
) -> dict:
    """Build the tamper-evident verification receipt for a commit verdict.

    The agent's report must reference fields from this receipt rather than
    re-counting geometry — the receipt is a bounded object returned by the
    tool, so the agent cannot rewrite its numbers.
    """
    gates = gate_report.get("gates", [])
    health_gate = next((g for g in gates if g.get("gate") == "health"), {})
    health_detail = health_gate.get("detail", {}) if isinstance(health_gate, dict) else {}
    orientation_gate = next((g for g in gates if g.get("gate") == "orientation"), {})
    orientation_detail = orientation_gate.get("detail", {}) if isinstance(orientation_gate, dict) else {}
    receipt = {
        "passed": gate_report.get("passed", False),
        "orientation": {
            "passed": orientation_detail.get("passed", 0) if isinstance(orientation_detail, dict) else 0,
            "failed": orientation_detail.get("failed", 0) if isinstance(orientation_detail, dict) else 0,
            "total": orientation_detail.get("total", 0) if isinstance(orientation_detail, dict) else 0,
        },
        "health": {
            "hard_errors_count": health_detail.get("hard_errors_count", 0) if isinstance(health_detail, dict) else 0,
            "soft_warnings_count": health_detail.get("soft_warnings_count", 0) if isinstance(health_detail, dict) else 0,
        },
    }
    if parameters:
        tabs: dict[str, list[str]] = {}
        ranges: dict[str, dict[str, object]] = {}
        for item in parameters:
            tabs.setdefault(item.tab, []).append(item.name)
            if item.classification == "design_intent":
                ranges[item.name] = {
                    "min": item.min, "default": item.default, "max": item.max
                }
        receipt["parameter_tabs"] = tabs
        receipt["parameter_ranges"] = ranges
    return receipt


# Bounds mirrored from the scratch DTO module for error-text truncation.
_MAX_ERROR_CHARS = 1000
_MAX_ERRORS = 32

_SCRATCH_EXPR_BINOPS = {"add": "+", "sub": "-", "mul": "*", "div": "/"}


def _resolve_scratch_ref(
    hou: object, container_path: str, target_path: str, ref: str
) -> str:
    """Resolve a sandbox parm ref to an absolute ``node/parm`` path.

    Relative refs resolve against the TARGET node's path (``..`` walks up).
    The resolved node must live inside the sandbox container, must exist, and
    must carry the named parm; anything else fails closed with a bounded
    message that identifies the offending ref.
    """
    if ref.startswith("/"):
        resolved: list[str] = []
        segments = ref[1:].split("/")
    else:
        resolved = [s for s in target_path.split("/") if s]
        segments = ref.split("/")
    for segment in segments:
        if segment == "..":
            if not resolved:
                raise _scratch_failed(
                    f"expression ref escapes above root: {ref}"
                )
            resolved.pop()
        else:
            resolved.append(segment)
    if len(resolved) < 2:
        raise _scratch_failed(f"expression ref is not a parm path: {ref}")
    node_path = "/" + "/".join(resolved[:-1])
    parm_name = resolved[-1]
    if node_path != container_path and not node_path.startswith(container_path + "/"):
        raise _scratch_failed(
            f"expression ref escapes the sandbox: {ref}"
        )
    ref_node = hou.node(node_path)  # type: ignore[attr-defined]
    if ref_node is None:
        raise _scratch_failed(f"expression ref node not found: {ref}")
    if ref_node.parm(parm_name) is None:
        raise _scratch_failed(f"expression ref parm not found: {ref}")
    return f"{node_path}/{parm_name}"


def _render_scratch_expr(
    hou: object, container_path: str, target_path: str, expr: ScratchExpr
) -> str:
    """Render a validated ScratchExpr to a bounded Hscript expression string."""
    if expr.kind == "num":
        return repr(expr.value)
    if expr.kind == "ref":
        abs_parm = _resolve_scratch_ref(hou, container_path, target_path, expr.path)
        return f'ch("{abs_parm}")'
    if expr.kind == "op":
        if expr.name == "neg":
            inner = _render_scratch_expr(hou, container_path, target_path, expr.args[0])
            return f"(-{inner})"
        symbol = _SCRATCH_EXPR_BINOPS[expr.name]
        left = _render_scratch_expr(hou, container_path, target_path, expr.args[0])
        right = _render_scratch_expr(hou, container_path, target_path, expr.args[1])
        return f"({left} {symbol} {right})"
    rendered_args = ", ".join(
        _render_scratch_expr(hou, container_path, target_path, arg)
        for arg in expr.args
    )
    return f"{expr.name}({rendered_args})"


def _apply_scratch_expr(
    hou: object, container_path: str, node: object, parm: object, op: ScratchOp
) -> None:
    """Set a typed expression on a numeric scratch parm (fail-closed)."""
    template = parm.parmTemplate()  # type: ignore[attr-defined]
    template_type = template.type() if template is not None else None
    numeric = (hou.parmTemplateType.Float, hou.parmTemplateType.Int)  # type: ignore[attr-defined]
    if template_type not in numeric:
        raise _scratch_failed(
            f"expression target parm is not numeric: {op.node_name}/{op.parm}"
        )
    rendered = _render_scratch_expr(hou, container_path, node.path(), op.expr)  # type: ignore[attr-defined,arg-type]
    try:
        parm.setExpression(rendered, hou.exprLanguage.Hscript)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — a rejected expression is an op failure
        raise _scratch_failed(
            f"expression rejected on {op.node_name}/{op.parm}: {exc}"[:_MAX_ERROR_CHARS]
        ) from exc


def _ambiguous() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="policy.ownership_ambiguous",
        category="ownership",
        message_for_user=(
            "A stable node id resolves to more than one scene path; the ChangeSet "
            "cannot be verified unambiguously."
        ),
        retryable=True,
    )


def _invalid(message: str) -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="changeset.invalid",
        category="invalid",
        message_for_user=message,
    )


class ChangeSetPreflightAdapter:
    """Read-only preflight fact gatherer bound to a scene adapter.

    Reuses the scene adapter's tracked epoch/instance via :meth:`binding`, and
    reads node/parameter/wire facts through the same ``hou`` module. Same-package
    access to ``scene_adapter._hou`` is intentional and documented.
    """

    def __init__(self, scene_adapter: HoudiniSceneAdapter) -> None:
        if not isinstance(scene_adapter, HoudiniSceneAdapter):
            raise TypeError("scene_adapter must be a HoudiniSceneAdapter")
        self._scene = scene_adapter

    @property
    def _hou(self) -> object:
        return self._scene._hou  # type: ignore[attr-defined]

    def binding(self):  # type: ignore[no-untyped-def]
        """Delegate the current scene binding (tracked epoch + instance)."""
        return self._scene.binding()

    # --------------------------------------------------------------- preflight

    def preflight(self, request: PreflightRequest) -> PreflightResult:
        """Gather bounded read-only facts and evaluate the request's preconditions."""
        hou = self._hou
        binding = self.binding()
        # Scene-epoch drift is checked first (consistent with scene.query).
        if binding.scene_epoch != request.scene_epoch:
            raise HoudiniAdapterError(
                code="bridge.stale_scene",
                category="stale_scene",
                message_for_user="The Houdini scene changed; refresh before continuing.",
                retryable=True,
            )
        workspace = request.workspace
        if workspace is not None and (
            workspace.instance_id != binding.instance_id
            or workspace.scene_epoch != binding.scene_epoch
        ):
            raise _stale("The workspace manifest no longer matches the current scene.")

        changeset = request.changeset
        owned_by_id = (
            {node.node_id: node for node in workspace.nodes}
            if workspace is not None
            else {}
        )
        create_targets = self._create_target_identities(changeset)
        refs = self._collect_node_refs(changeset)

        # Single bounded read-only pass: index every scene node that mirrors a
        # stable owned id. An owned reference resolves through this index BEFORE
        # its (locator) path; more than one scene node mirroring the same id is
        # full identity ambiguity and fails closed (design sections 7-8).
        node_id_index = self._index_scene_by_node_id(hou)

        # Pass 1: resolve every reference. Owned refs resolve by stable id
        # (raising on ambiguity); external path-only refs stay path-based. The
        # mirrored schema/run identity facts travel out-of-band, never in a DTO.
        facts: dict[str, PreflightNodeFact] = {}
        extras: dict[str, tuple[str | None, str | None]] = {}
        for ref in refs:
            fact, schema_version, created_by_run = self._resolve_node_fact(hou, ref, node_id_index)
            identity = _identity(ref)
            facts[identity] = fact
            extras[identity] = (schema_version, created_by_run)

        # Pass 2: verify references against current facts. Existing-required refs
        # must match; a create target must instead be ABSENT — no existing node may
        # reuse its stable id and no node may already occupy its derived create
        # path. A genuinely absent create target is accepted.
        for ref in refs:
            identity = _identity(ref)
            if identity in create_targets:
                self._verify_create_target(hou, ref, facts[identity])
            else:
                self._verify_existing(ref, facts[identity], extras[identity], workspace, owned_by_id)

        parm_facts = self._gather_parm_facts(hou, changeset, facts)
        wire_facts = self._gather_wire_facts(hou, changeset, facts)
        condition_results = self._evaluate_conditions(
            changeset, binding, workspace, facts, parm_facts, wire_facts
        )
        all_hold = all(result.passed for result in condition_results)

        node_facts = tuple(sorted(facts.values(), key=lambda f: _identity(f.requested)))
        parm_facts = tuple(sorted(parm_facts, key=lambda f: (_identity(f.target), f.parm_name)))
        wire_facts = tuple(sorted(wire_facts, key=lambda f: (_identity(f.target), f.input_index)))
        try:
            return PreflightResult(
                binding=binding,
                workspace_id=workspace.workspace_id if workspace is not None else None,
                workspace_revision=workspace.revision if workspace is not None else None,
                node_facts=node_facts,
                parm_facts=parm_facts,
                wire_facts=wire_facts,
                condition_results=tuple(condition_results),
                all_preconditions_hold=all_hold,
                scene_may_have_changed=False,
            )
        except ValueError as exc:
            # An oversized or otherwise unrepresentable fact set must not leak.
            raise _invalid("The preflight facts could not be represented within bounds.") from exc

    # --------------------------------------------------------------- references

    def _create_target_identities(self, changeset) -> set[str]:  # type: ignore[no-untyped-def]
        identities: set[str] = set()
        for op in changeset.operations:
            if isinstance(op, CreateNode):
                identities.add(op.node_id)
        return identities

    def _collect_node_refs(self, changeset) -> list[NodeRef]:  # type: ignore[no-untyped-def]
        ordered: list[NodeRef] = []
        seen: set[tuple[str | None, str]] = set()

        def add(ref: NodeRef) -> None:
            # Dedup by (node_id, path): the same node referenced twice collapses,
            # but the SAME stable id at two DISTINCT paths is preserved so the
            # ambiguity check can fail closed on it.
            key = (ref.node_id, ref.path)
            if key not in seen:
                seen.add(key)
                ordered.append(ref)

        for ref in changeset.affected_nodes:
            add(ref)
        for ref in changeset.read_dependencies:
            add(ref)
        for op in changeset.operations:
            if isinstance(op, CreateNode):
                add(op.parent)
                add(
                    NodeRef(
                        node_id=op.node_id,
                        path=_derive_create_path(op.parent.path, op.node_name),
                        expected_type=op.node_type,
                        expected_workspace_id=op.workspace_id,
                    )
                )
            elif isinstance(op, SetParm):
                add(op.target)
            elif isinstance(op, ConnectInput):
                add(op.target)
                add(op.source)
        for cond in changeset.preconditions:
            ref = _condition_node_ref(cond)
            if ref is not None:
                add(ref)
        return ordered

    # --------------------------------------------------------------- resolution

    def _index_scene_by_node_id(self, hou: object) -> dict[str, list[object]]:
        """Single bounded read-only pass: map mirrored node id -> scene nodes.

        Enumerates the scene root's sub-tree once and groups every node that
        mirrors an ``eee.node_id`` user-data key. Nodes without a mirrored id
        (external/unowned) are ignored. This index is the only scene enumeration
        in preflight; it is read-only, finite, and confined to this queue
        callable. An id mapped to more than one node is ambiguity.
        """
        root = hou.node("/")  # type: ignore[union-attr]
        nodes = () if root is None else tuple(root.allSubChildren())  # type: ignore[union-attr]
        index: dict[str, list[object]] = {}
        for node in nodes:
            node_id = _read_user_data(node, _NODE_ID_KEY)
            if node_id is None:
                continue
            index.setdefault(node_id, []).append(node)
        return index

    def _resolve_node_fact(
        self, hou: object, ref: NodeRef, node_id_index: dict[str, list[object]]
    ) -> tuple[PreflightNodeFact, str | None, str | None]:
        """Resolve a reference to a node fact plus out-of-band identity extras.

        Owned references (``node_id is not None``) resolve by mirrored stable id
        BEFORE the locator path: the unique current node mirroring that id is the
        node even if its current path differs from ``ref.path``. Two or more
        scene nodes mirroring the same id is ``policy.ownership_ambiguous``; zero
        is a provably-absent owned node (fail closed). External path-only
        references (``node_id is None``) remain path-based. Returns the fact and
        the mirrored ``schema_version`` / ``created_by_run`` (``None`` when the
        node is absent or external); these never enter the result DTO.
        """
        if ref.node_id is None:
            # External scoped node: path is the only locator, no ownership claim.
            return self._fact_from_node(hou.node(ref.path), ref)  # type: ignore[union-attr]
        mirrors = node_id_index.get(ref.node_id, ())
        if len(mirrors) > 1:
            raise _ambiguous()
        node = mirrors[0] if mirrors else None
        return self._fact_from_node(node, ref)

    def _fact_from_node(
        self, node: object | None, ref: NodeRef
    ) -> tuple[PreflightNodeFact, str | None, str | None]:
        """Build a node fact from a resolved HOM node, plus identity extras.

        ``node is None`` yields an absent fact. The mirrored schema/run values
        are returned alongside (for internal validation) but are not carried on
        the DTO.
        """
        if node is None:
            return (
                PreflightNodeFact(
                    requested=ref,
                    exists=False,
                    actual_path=None,
                    actual_type=None,
                    parent_path=None,
                    workspace_id=None,
                    node_id=None,
                    capability=None,
                    role=None,
                    is_locked=False,
                ),
                None,
                None,
            )
        return (
            PreflightNodeFact(
                requested=ref,
                exists=True,
                actual_path=str(node.path()),  # type: ignore[union-attr]
                actual_type=str(node.type().name()),  # type: ignore[union-attr]
                parent_path=str(node.parent().path()),  # type: ignore[union-attr]
                workspace_id=_read_user_data(node, _WS_KEY),
                node_id=_read_user_data(node, _NODE_ID_KEY),
                capability=_read_user_data(node, _CAP_KEY),
                role=_read_user_data(node, _ROLE_KEY),
                is_locked=_read_is_locked(node),
            ),
            _read_user_data(node, _SCHEMA_KEY),
            _read_user_data(node, _RUN_KEY),
        )

    def _verify_existing(
        self,
        ref: NodeRef,
        fact: PreflightNodeFact,
        identity_extras: tuple[str | None, str | None],
        workspace: WorkspaceManifest | None,
        owned_by_id: dict[str, object],
    ) -> None:
        if not fact.exists:
            raise _stale("The referenced node was not found in the scene.")
        if ref.expected_type != fact.actual_type:
            raise _stale(
                f"The node at {fact.actual_path} has type {fact.actual_type!r}, not {ref.expected_type!r}."
            )
        if ref.node_id is None:
            return  # external scoped node: identity is path-only, no ownership claim
        # Owned node: mirrored stable id must agree with the requested id.
        if fact.node_id is None or fact.node_id != ref.node_id:
            raise _stale(
                "The resolved node does not mirror the expected stable node id."
            )
        # Workspace ownership is an identity fact, not an optional hint. When a
        # WorkspaceManifest is supplied, the resolved node must belong to that
        # workspace even when the NodeRef omits expected_workspace_id — otherwise
        # an external node that happens to mirror the same stable id could reuse
        # a manifest id/path and pass preflight. The request's explicit
        # expected_workspace_id still governs when present (it may name a
        # workspace other than the manifest's).
        expected_workspace_id = (
            ref.expected_workspace_id
            if ref.expected_workspace_id is not None
            else (workspace.workspace_id if workspace is not None else None)
        )
        if expected_workspace_id is not None and (
            fact.workspace_id is None or fact.workspace_id != expected_workspace_id
        ):
            raise _stale(
                "The resolved node does not belong to the expected workspace."
            )
        # Mirrored identity facts (design section 4.1): the schema version must
        # be the supported schema-v1 value, and the run of origin must agree with
        # the supplied workspace manifest. Missing/mismatched values fail closed.
        schema_version, created_by_run = identity_extras
        if schema_version != _SUPPORTED_SCHEMA_VERSION:
            raise _stale(
                "The resolved node's mirrored schema version is missing or unsupported."
            )
        if workspace is not None and (
            created_by_run is None or created_by_run != workspace.created_by_run
        ):
            raise _stale(
                "The resolved node's mirrored run of origin does not match the workspace."
            )
        if workspace is not None:
            owned = owned_by_id.get(ref.node_id)
            if owned is None:
                raise _stale(
                    f"The node id {ref.node_id!r} is not present in the workspace manifest."
                )
            # Manifest facts are compared against the CURRENT scene facts (the
            # resolved actual path/type/parent), not the request's locator path.
            if (
                owned.path != fact.actual_path  # type: ignore[attr-defined]
                or owned.node_type != fact.actual_type  # type: ignore[attr-defined]
                or owned.parent_path != fact.parent_path  # type: ignore[attr-defined]
                or owned.capability != fact.capability  # type: ignore[attr-defined]
                or owned.role != fact.role  # type: ignore[attr-defined]
            ):
                raise _stale(
                    "The resolved node no longer matches the workspace manifest facts."
                )

    def _verify_create_target(
        self, hou: object, ref: NodeRef, fact: PreflightNodeFact
    ) -> None:
        """Fail closed unless a created node targets a genuinely absent slot.

        A CreateNode must not reuse a stable node id already mirrored anywhere
        in the scene (``fact.exists`` — the create ref resolves by node id) and
        must not collide with a node already occupying the derived create path
        (``ref.path``). Both checks hold even without a workspace manifest
        (ProjectChange): a duplicate stable id and an occupied create path are
        invalid regardless of ownership. A genuinely absent target is accepted.
        Reads are confined to this queue callable; nothing is mutated.
        """
        if fact.exists:
            raise _stale(
                "The node to be created already exists with the given stable id."
            )
        if hou.node(ref.path) is not None:  # type: ignore[union-attr]
            raise _stale(
                "The node to be created already exists at the target path."
            )

    # --------------------------------------------------------------- parm facts

    def _gather_parm_facts(
        self, hou: object, changeset, node_facts: dict[str, PreflightNodeFact]
    ) -> list[PreflightParmFact]:  # type: ignore[no-untyped-def]
        targets: list[tuple[NodeRef, str]] = []
        seen: set[tuple[str, str]] = set()
        # F3: exclude transaction-created targets (they don't exist at preflight).
        created_ids = {op.node_id for op in changeset.operations if isinstance(op, CreateNode)}

        def add(target: NodeRef, name: str) -> None:
            if target.node_id in created_ids:
                return  # created node — no parm fact at preflight
            key = (_identity(target), name)
            if key not in seen:
                seen.add(key)
                targets.append((target, name))

        for op in changeset.operations:
            if isinstance(op, SetParm):
                add(op.target, op.parm_name)
        for cond in changeset.preconditions:
            if isinstance(cond, ParmValueEquals):
                add(cond.target, cond.parm_name)

        facts: list[PreflightParmFact] = []
        for target, name in targets:
            # Resolve through the identity-resolved fact so a moved node's parm
            # is read from its current path, not a stale locator.
            node = self._resolved_node(hou, target, node_facts)
            exists = False
            value: object | None = None
            if node is not None:
                value, exists = self._read_parm_value(node, name)
            facts.append(PreflightParmFact(target=target, parm_name=name, exists=exists, value=value))
        return facts

    def _resolved_node(
        self, hou: object, target: NodeRef, node_facts: dict[str, PreflightNodeFact]
    ) -> object | None:
        """Look up a referenced node through its resolved fact's current path."""
        fact = node_facts.get(_identity(target))
        if fact is None or not fact.exists or fact.actual_path is None:
            return None
        return hou.node(fact.actual_path)  # type: ignore[union-attr]

    def _read_parm_value(self, node: object, name: str) -> tuple[object | None, bool]:
        parm = node.parm(name)  # type: ignore[union-attr]
        if parm is not None:
            raw = parm.eval()  # type: ignore[union-attr]
            return self._bounded_parm(raw), True
        pt = node.parmTuple(name)  # type: ignore[union-attr]
        if pt is not None and pt.size() > 1:  # type: ignore[union-attr]
            raw = tuple(pt.eval())  # type: ignore[union-attr]
            return self._bounded_parm(raw), True
        return None, False

    def _bounded_parm(self, raw: object) -> object:
        try:
            return _validate_parm_value(raw, "parm value")
        except (TypeError, ValueError) as exc:
            raise _invalid("A referenced parameter has an unsupported or unbounded value.") from exc

    # --------------------------------------------------------------- wire facts

    def _gather_wire_facts(
        self, hou: object, changeset, node_facts: dict[str, PreflightNodeFact]
    ) -> list[PreflightWireFact]:  # type: ignore[no-untyped-def]
        targets: list[tuple[NodeRef, int]] = []
        seen: set[tuple[str, int]] = set()
        # F3: exclude transaction-created targets (they don't exist at preflight).
        created_ids = {op.node_id for op in changeset.operations if isinstance(op, CreateNode)}

        def add(target: NodeRef, index: int) -> None:
            if target.node_id in created_ids:
                return  # created node — no wire fact at preflight
            key = (_identity(target), index)
            if key not in seen:
                seen.add(key)
                targets.append((target, index))

        for op in changeset.operations:
            if isinstance(op, ConnectInput):
                add(op.target, op.input_index)
        for cond in changeset.preconditions:
            if isinstance(cond, WireInputEquals):
                add(cond.target, cond.input_index)

        facts: list[PreflightWireFact] = []
        for target, index in targets:
            # Resolve through the identity-resolved fact (current path), not the
            # locator, so a moved node's input is read from where it now is.
            node = self._resolved_node(hou, target, node_facts)
            source: WireRef | None = None
            if node is not None:
                source = self._read_wire_source(node, index)
            facts.append(PreflightWireFact(target=target, input_index=index, source=source))
        return facts

    def _read_wire_source(self, node: object, index: int) -> WireRef | None:
        """Read the typed source wired into ``node`` input ``index``.

        The source node is read via ``node.inputs()[index]`` (reliable across
        Houdini node kinds in 21.0.440); ``inputConnections().outputNode()`` can
        return the node itself for some kinds. The output index is read from
        ``inputConnections()``.
        """
        try:
            all_inputs = node.inputs()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — a node with no inputs reports nothing
            all_inputs = ()
        src_node: object | None = None
        if isinstance(all_inputs, (tuple, list)) and 0 <= index < len(all_inputs):
            src_node = all_inputs[index]
        if src_node is None:
            return None
        output_index = 0
        try:
            for conn in node.inputConnections():  # type: ignore[union-attr]
                if int(conn.inputIndex()) == index:
                    output_index = int(conn.outputIndex())  # type: ignore[union-attr]
                    break
        except Exception:  # noqa: BLE001
            pass
        source = NodeRef(
            node_id=_read_user_data(src_node, _NODE_ID_KEY),
            path=str(src_node.path()),  # type: ignore[union-attr]
            expected_type=str(src_node.type().name()),  # type: ignore[union-attr]
            expected_workspace_id=_read_user_data(src_node, _WS_KEY),
        )
        return WireRef(source=source, source_output_index=output_index)

    # --------------------------------------------------------------- conditions

    def _evaluate_conditions(
        self,
        changeset,
        binding,  # SceneBinding
        workspace: WorkspaceManifest | None,
        node_facts: dict[str, PreflightNodeFact],
        parm_facts: list[PreflightParmFact],
        wire_facts: list[PreflightWireFact],
    ) -> list[ConditionResult]:
        parm_by_key = {(_identity(f.target), f.parm_name): f for f in parm_facts}
        wire_by_key = {(_identity(f.target), f.input_index): f for f in wire_facts}
        results: list[ConditionResult] = []
        for cond in changeset.preconditions:
            kind = cond.kind
            if isinstance(cond, SceneBindingEquals):
                passed = (
                    binding.instance_id == cond.instance_id
                    and binding.scene_epoch == cond.scene_epoch
                )
            elif isinstance(cond, WorkspaceRevisionEquals):
                passed = (
                    workspace is not None
                    and workspace.workspace_id == cond.workspace_id
                    and workspace.revision == cond.revision
                )
            elif isinstance(cond, NodeIdentityEquals):
                passed = self._node_identity_holds(node_facts.get(_identity(cond.node)), cond.node)
            elif isinstance(cond, ParmValueEquals):
                fact = parm_by_key.get((_identity(cond.target), cond.parm_name))
                passed = fact is not None and fact.exists and _parm_equal(fact.value, cond.value)
            elif isinstance(cond, WireInputEquals):
                fact = wire_by_key.get((_identity(cond.target), cond.input_index))
                actual = fact.source if fact is not None else None
                passed = _wire_equal(actual, cond.source)
            elif isinstance(cond, NodeAbsent):
                passed = self._hou.node(cond.path) is None  # type: ignore[union-attr]
            else:  # pragma: no cover - the union is closed
                raise _invalid(f"Unsupported precondition kind: {kind!r}")
            results.append(ConditionResult(kind=kind, passed=passed, detail=None))
        return results

    def _node_identity_holds(self, fact: PreflightNodeFact | None, ref: NodeRef) -> bool:
        if fact is None or not fact.exists:
            return False
        if fact.actual_path != ref.path:
            return False
        if fact.actual_type != ref.expected_type:
            return False
        if ref.node_id is not None and fact.node_id != ref.node_id:
            return False
        if ref.expected_workspace_id is not None and (
            fact.workspace_id is None or fact.workspace_id != ref.expected_workspace_id
        ):
            return False
        return True


# --------------------------------------------------------------------------
# module-level read helpers (hou is always injected; never imported here)
# --------------------------------------------------------------------------


def _read_user_data(node: object, key: str) -> str | None:
    try:
        value = node.userData(key)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — a node without the key reports nothing
        return None
    if value is None:
        return None
    return str(value)


def _read_is_locked(node: object) -> bool:
    try:
        return bool(node.isHardLocked() or node.isSoftLocked())  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — non-SOP nodes are simply not locked
        return False


def _condition_node_ref(cond: object) -> NodeRef | None:
    if isinstance(cond, NodeIdentityEquals):
        return cond.node
    if isinstance(cond, ParmValueEquals):
        return cond.target
    if isinstance(cond, WireInputEquals):
        return cond.target
    return None


def _parm_equal(actual: object | None, expected: object) -> bool:
    if actual is None:
        return False
    return canonical_json_dumps(_parm_value_json(actual)) == canonical_json_dumps(
        _parm_value_json(expected)
    )


def _wire_equal(actual: WireRef | None, expected: WireRef | None) -> bool:
    """Compare wire sources. path/type/output are always required; node_id and
    workspace match only when the expected ref DECLARES them (non-None)."""
    if expected is None:
        return actual is None
    if actual is None:
        return False
    if actual.source.path != expected.source.path:
        return False
    if actual.source.expected_type != expected.source.expected_type:
        return False
    if actual.source_output_index != expected.source_output_index:
        return False
    if expected.source.node_id is not None and actual.source.node_id != expected.source.node_id:
        return False
    if (
        expected.source.expected_workspace_id is not None
        and actual.source.expected_workspace_id != expected.source.expected_workspace_id
    ):
        return False
    return True


# ==========================================================================
# Task 16-D: transactional ChangeSet executor (apply / rollback / receipt)
#
# The executor reuses the accepted read-only preflight adapter to re-validate
# every binding/identity/precondition/create-target-absence fact from the
# CURRENT scene immediately before the first write (zero writes on any
# pre-transaction failure). It then captures a bounded before snapshot, derives
# inverse actions internally (no inverse/delete is ever accepted from the wire),
# executes the ordered create/set/connect effects inside exactly one
# ``hou.undos.group``, mirrors the ownership keys on created nodes, re-reads the
# expected postconditions, and returns an :class:`Applied` receipt only after
# reconciliation succeeds. On execution or reconciliation failure it runs the
# derived inverses in strict reverse order and classifies the result as
# ``RolledBack``/``Partial``/``CriticalRecovery``; ``Partial``/``CriticalRecovery``
# freeze further writes until an explicit process restart.
#
# All HOM access happens inside the single synchronous ``apply`` callable that
# the shared :class:`~eee_agent.houdini_bridge.queue.MainThreadReadQueue` pumps
# on the Houdini main thread. There is no second queue, worker, or task, and no
# HOM access on the socket loop.
# ==========================================================================


_RECEIPT_CACHE_MAX = 256
_UNDO_LABEL_PREFIX = "EEE Agent - "

_LAYOUT_COLUMN_WIDTH = 3.0
_LAYOUT_ROW_HEIGHT = 2.0


def _layered_layout(
    node_paths: Sequence[str],
    edges: Mapping[str, Sequence[str]],
    *,
    anchor: tuple[float, float],
) -> dict[str, tuple[float, float]]:
    """Return a deterministic layered layout for a bounded node set.

    The input order is the stable tie-break. Edges outside the set are ignored
    and cycles are broken at depth zero, so malformed topology can never make
    finalization recurse forever.
    """
    members = set(node_paths)
    depths: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(path: str) -> int:
        if path in depths:
            return depths[path]
        if path in visiting:
            return 0
        visiting.add(path)
        upstream = [u for u in edges.get(path, ()) if u in members]
        value = 0 if not upstream else 1 + max(depth(u) for u in upstream)
        visiting.discard(path)
        depths[path] = value
        return value

    columns: dict[int, list[str]] = {}
    for path in node_paths:
        columns.setdefault(depth(path), []).append(path)
    positions: dict[str, tuple[float, float]] = {}
    for column, paths in columns.items():
        for row, path in enumerate(paths):
            positions[path] = (
                anchor[0] + column * _LAYOUT_COLUMN_WIDTH,
                anchor[1] - row * _LAYOUT_ROW_HEIGHT,
            )
    return positions


def _frozen() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="bridge.write_frozen",
        category="write_frozen",
        message_for_user=(
            "The bridge is frozen for writes after an uncertain recovery; "
            "restart the bridge before applying further changes."
        ),
        retryable=False,
    )


def _conflict() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="changeset.receipt_conflict",
        category="conflict",
        message_for_user=(
            "A different ChangeSet digest was already applied for this change id."
        ),
        retryable=False,
    )


def _precondition_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="changeset.stale",
        category="stale",
        message_for_user="A ChangeSet precondition does not hold against the current scene.",
        retryable=True,
    )


def _backup_unavailable() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="bridge.backup_unavailable",
        category="backup",
        message_for_user=(
            "The ChangeSet requires a backup, but no backup capability is available."
        ),
        retryable=False,
    )


def _apply_failed(message: str) -> HoudiniAdapterError:
    # Raised only when a write cannot even start; transaction-internal write
    # failures are classified into receipts, not raised.
    return HoudiniAdapterError(
        code="changeset.apply_failed",
        category="apply_failed",
        message_for_user=message,
    )


def _policy_denied(denial_codes: tuple[str, ...]) -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="policy.denied",
        category="policy",
        message_for_user=(
            "The ChangeSet is denied by policy against the current scene facts."
        ),
        retryable=False,
        technical_detail_ref=",".join(denial_codes) if denial_codes else None,
    )


def _mandatory_omitted(message: str) -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="changeset.stale",
        category="stale",
        message_for_user=message,
        retryable=True,
    )


def _stale_scene() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="bridge.stale_scene",
        category="stale_scene",
        message_for_user="The Houdini scene changed; refresh before continuing.",
        retryable=True,
    )


def _sample_invalid(message: str) -> HoudiniAdapterError:
    # Raised only before the first sample write: an unresolvable target or a
    # missing parameter means the request does not match the current scene.
    return HoudiniAdapterError(
        code="sensitivity.invalid",
        category="invalid",
        message_for_user=message,
        retryable=False,
    )


def _cook_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="sensitivity.cook_failed",
        category="cook",
        message_for_user="A sampled parameter did not cook cleanly.",
        retryable=True,
    )


def _sample_restore_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="sensitivity.restore_failed",
        category="restore",
        message_for_user=(
            "A sampled parameter could not be restored exactly; the bridge is "
            "frozen for writes until restart."
        ),
        retryable=False,
    )


def _sample_aborted() -> HoudiniAdapterError:
    # Any non-structured failure during the write/capture phase, raised only
    # after the scene was verifiably restored: classified, never guessed.
    return HoudiniAdapterError(
        code="sensitivity.sample_failed",
        category="sample",
        message_for_user="The sensitivity sample could not be completed.",
        retryable=True,
    )


def _capture_invalid(message: str) -> HoudiniAdapterError:
    # Raised only before the temp scope is created: an unresolvable evidence
    # node means the request does not match the current scene (zero changes).
    return HoudiniAdapterError(
        code="capture.invalid",
        category="invalid",
        message_for_user=message,
        retryable=False,
    )


def _capture_target_unavailable() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.target_unavailable",
        category="filesystem",
        message_for_user="The Runtime-owned artifacts target directory is unavailable.",
        retryable=False,
    )


def _capture_cook_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.cook_failed",
        category="cook",
        message_for_user="A capture evidence node did not cook cleanly.",
        retryable=True,
    )


def _capture_no_geometry() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.no_geometry",
        category="geometry",
        message_for_user="The capture evidence nodes contain no frameable geometry.",
        retryable=False,
    )


def _capture_framing_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.framing_failed",
        category="framing",
        message_for_user=(
            "No acceptable capture framing was reachable within two adjustments."
        ),
        retryable=False,
    )


def _capture_render_unavailable() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.render_unavailable",
        category="render",
        message_for_user="The deterministic capture render surface is unavailable.",
        retryable=False,
    )


def _capture_render_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.render_failed",
        category="render",
        message_for_user="The capture render did not produce a verified PNG.",
        retryable=True,
    )


def _capture_cleanup_failed() -> HoudiniAdapterError:
    return HoudiniAdapterError(
        code="capture.cleanup_failed",
        category="cleanup",
        message_for_user=(
            "The capture temp scope could not be removed; the scene may retain "
            "temporary nodes and the capture was discarded."
        ),
        retryable=False,
    )


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_HASH_CHUNK_BYTES = 1024 * 1024


def _verify_capture_file(path: Path) -> tuple[str, int]:
    """Verify the rendered PNG magic and stream its SHA-256 + exact size."""
    try:
        with path.open("rb") as handle:
            magic = handle.read(len(_PNG_MAGIC))
            if magic != _PNG_MAGIC:
                raise _capture_render_failed()
            digest = hashlib.sha256(magic)
            size = len(magic)
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_PNG_BYTES:
                    # Fail before the atomic rename: an oversized render must
                    # never land at the final path and surface later as an
                    # opaque agent-side ``CaptureResult`` validation error.
                    raise _capture_render_failed()
                digest.update(chunk)
    except HoudiniAdapterError:
        raise
    except OSError as exc:
        raise _capture_render_failed() from exc
    if size <= len(_PNG_MAGIC):
        raise _capture_render_failed()
    return digest.hexdigest(), size


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _rmdir_quietly(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _transform_bbox_world(
    values: tuple[float, float, float, float, float, float],
    m: tuple[float, ...],
) -> "BoundingBox":
    """World-space AABB of a local bbox under a row-major 4x4 matrix.

    The matrix follows ``hou.Matrix4.asTuple()`` ordering (translation at
    indices 12..14); points multiply as row vectors. All eight corners are
    transformed and re-bounded, which is exact for affine transforms.
    """
    mn_x, mn_y, mn_z, mx_x, mx_y, mx_z = values
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for cx in (mn_x, mx_x):
        for cy in (mn_y, mx_y):
            for cz in (mn_z, mx_z):
                w = cx * m[3] + cy * m[7] + cz * m[11] + m[15]
                if w == 0.0:
                    continue
                xs.append((cx * m[0] + cy * m[4] + cz * m[8] + m[12]) / w)
                ys.append((cx * m[1] + cy * m[5] + cz * m[9] + m[13]) / w)
                zs.append((cx * m[2] + cy * m[6] + cz * m[10] + m[14]) / w)
    if not xs:
        return BoundingBox(*values)
    return BoundingBox(min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _inverse_condition_kind(tag: str) -> str:
    """The ConditionResult kind that matches an inverse tag (F10)."""
    if tag == "parm":
        return "parm.value_equals"
    if tag == "wire":
        return "wire.input_equals"
    return "node.absent"


# --------------------------------------------------------------------------
# mandatory derived preconditions / postconditions / checkpoint coverage
# (design sections 4.3 and 8). The executor derives these from every operation
# and rejects a ChangeSet that omits or contradicts them before any write.
# --------------------------------------------------------------------------


def _created_ref(op: CreateNode) -> NodeRef:
    return NodeRef(
        node_id=op.node_id,
        path=_derive_create_path(op.parent.path, op.node_name),
        expected_type=op.node_type,
        expected_workspace_id=op.workspace_id,
    )


def derive_mandatory_preconditions(
    operations, binding, workspace: WorkspaceManifest | None
) -> list[object]:
    """Mandatory preconditions the executor derives from the operations.

    A ChangeSet must include every one of these (and may add more). Omission or
    contradiction (a supplied condition with the same identity but different
    facts) fails closed before writes, because the exact mandatory fact is then
    absent from the supplied set.
    """
    pre: list[object] = [
        SceneBindingEquals(
            instance_id=binding.instance_id,
            scene_epoch=binding.scene_epoch,
        )
    ]
    if workspace is not None:
        pre.append(
            WorkspaceRevisionEquals(
                workspace_id=workspace.workspace_id, revision=workspace.revision
            )
        )
    # Nodes created earlier in this transaction do not exist at preflight time, so
    # they get no node.identity_equals precondition (they are proven absent and are
    # brought into being by the create); only pre-existing references do.
    created_ids = {op.node_id for op in operations if isinstance(op, CreateNode)}
    # Track previously mutated slots in operation order: only the FIRST write to a
    # pre-existing slot gets a preflight old-value precondition; later expected-old
    # state is JIT-only (F2).
    seen_parm_slots: set[tuple[str, str]] = set()
    seen_wire_slots: set[tuple[str, int]] = set()
    for op in operations:
        if isinstance(op, CreateNode):
            pre.append(NodeAbsent(path=_derive_create_path(op.parent.path, op.node_name), node_id=op.node_id))
            if op.parent.node_id not in created_ids:
                pre.append(NodeIdentityEquals(node=op.parent))
        elif isinstance(op, SetParm):
            target_created = op.target.node_id in created_ids
            parm_key = (_identity(op.target), op.parm_name)
            first_write = parm_key not in seen_parm_slots
            seen_parm_slots.add(parm_key)
            if not target_created and first_write:
                pre.append(NodeIdentityEquals(node=op.target))
                pre.append(ParmValueEquals(target=op.target, parm_name=op.parm_name, value=op.expected_old_value))
            # Later writes or created targets: identity/old-value are JIT-checked.
        elif isinstance(op, ConnectInput):
            target_created = op.target.node_id in created_ids
            wire_key = (_identity(op.target), op.input_index)
            first_write = wire_key not in seen_wire_slots
            seen_wire_slots.add(wire_key)
            if not target_created and first_write:
                pre.append(NodeIdentityEquals(node=op.target))
                pre.append(WireInputEquals(target=op.target, input_index=op.input_index, source=op.expected_old_source))
            if op.source.node_id not in created_ids:
                pre.append(NodeIdentityEquals(node=op.source))
            # Later writes or created targets: wire old-value is JIT-checked.
    return pre


def derive_mandatory_postconditions(operations) -> list[object]:
    """Mandatory post-write postconditions derived from the operations.

    F2: for repeated writes to the same slot, only the LAST value is emitted
    (intermediate postconditions would be contradictory at construction).
    """
    post: list[object] = []
    # Track the last postcondition per slot; overwrite on repeat.
    parm_posts: dict[tuple[str, str], int] = {}  # key -> index in post
    wire_posts: dict[tuple[str, int], int] = {}
    for op in operations:
        if isinstance(op, CreateNode):
            post.append(NodeIdentityEquals(node=_created_ref(op)))
        elif isinstance(op, SetParm):
            key = (_identity(op.target), op.parm_name)
            cond = ParmValueEquals(target=op.target, parm_name=op.parm_name, value=op.value)
            if key in parm_posts:
                post[parm_posts[key]] = cond
            else:
                parm_posts[key] = len(post)
                post.append(cond)
        elif isinstance(op, ConnectInput):
            key = (_identity(op.target), op.input_index)
            cond = WireInputEquals(
                target=op.target,
                input_index=op.input_index,
                source=WireRef(source=op.source, source_output_index=op.source_output_index),
            )
            if key in wire_posts:
                post[wire_posts[key]] = cond
            else:
                wire_posts[key] = len(post)
                post.append(cond)
    return post


def derive_mandatory_checkpoint(operations) -> tuple[list[NodeRef], list[ParmSnapshot], list[WireSnapshot]]:
    """Exact before-snapshot coverage derived from the operations (design 4.4).

    D1: excludes nonexistent created-node state (it cannot be snapshotted before
    the transaction) while retaining before-state coverage for pre-existing nodes.
    """
    created_ids = {op.node_id for op in operations if isinstance(op, CreateNode)}
    nodes: list[NodeRef] = []
    parms: list[ParmSnapshot] = []
    wires: list[WireSnapshot] = []
    for op in operations:
        if isinstance(op, CreateNode):
            if op.parent.node_id not in created_ids:
                nodes.append(op.parent)
        elif isinstance(op, SetParm):
            if op.target.node_id not in created_ids:
                nodes.append(op.target)
                parms.append(ParmSnapshot(target=op.target, parm_name=op.parm_name))
        elif isinstance(op, ConnectInput):
            if op.target.node_id not in created_ids:
                nodes.append(op.target)
                wires.append(WireSnapshot(target=op.target, input_index=op.input_index))
            if op.source.node_id not in created_ids:
                nodes.append(op.source)
    return nodes, parms, wires


def _canonical(item: object) -> str:
    return canonical_json_dumps(item.to_dict())  # type: ignore[attr-defined]


def _dedup(items: list[object]) -> list[object]:
    """Dedup by canonical form, preserving first occurrence.

    Multiple operations referencing the same node yield the same derived fact
    (e.g. two ``NodeIdentityEquals(child)``); the ChangeSet constructor rejects
    duplicate identities, so the derived set must be deduped.
    """
    seen: set[str] = set()
    out: list[object] = []
    for item in items:
        key = _canonical(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _verify_superset(supplied: list[object], mandatory: list[object], label: str) -> None:
    supplied_keys = {_canonical(item) for item in supplied}
    for required in mandatory:
        if _canonical(required) not in supplied_keys:
            raise _mandatory_omitted(
                f"The ChangeSet {label} omits a mandatory derived fact."
            )


def verify_mandatory_coverage(changeset: ChangeSet, workspace: WorkspaceManifest | None) -> None:
    """Reject (zero writes) unless the ChangeSet carries every derived fact."""
    _verify_superset(
        list(changeset.preconditions),
        _dedup(derive_mandatory_preconditions(changeset.operations, changeset.scene_binding, workspace)),
        "preconditions",
    )
    _verify_superset(
        list(changeset.expected_postconditions),
        _dedup(derive_mandatory_postconditions(changeset.operations)),
        "expected_postconditions",
    )
    m_nodes, m_parms, m_wires = derive_mandatory_checkpoint(changeset.operations)
    plan = changeset.checkpoint_plan
    _verify_superset(list(plan.nodes), _dedup(list(m_nodes)), "checkpoint_plan.nodes")
    _verify_superset(list(plan.parameters), _dedup(list(m_parms)), "checkpoint_plan.parameters")
    _verify_superset(list(plan.wires), _dedup(list(m_wires)), "checkpoint_plan.wires")


class _Snapshot:
    """Bounded before/after fact set: node existence + parm values + wire sources.

    Values are stored in JSON-able form (parm scalars/lists, wire dicts) so the
    deterministic revision hash and postcondition evaluation share one read.
    """

    __slots__ = ("nodes", "parms", "wires")

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, object]] = {}
        self.parms: dict[tuple[str, str], dict[str, object]] = {}
        self.wires: dict[tuple[str, int], dict[str, object] | None] = {}


class ChangeSetExecutor:
    """Transactional ChangeSet executor bound to a scene adapter.

    Reuses :class:`ChangeSetPreflightAdapter` for zero-write re-validation and
    stable-id-first reference collection, then performs the short write
    transaction. Same-package access to ``scene_adapter._hou`` is intentional.
    """

    def __init__(
        self,
        scene_adapter: HoudiniSceneAdapter,
        *,
        receipt_cache_max: int = _RECEIPT_CACHE_MAX,
    ) -> None:
        if not isinstance(scene_adapter, HoudiniSceneAdapter):
            raise TypeError("scene_adapter must be a HoudiniSceneAdapter")
        if type(receipt_cache_max) is not int or receipt_cache_max <= 0:
            raise ValueError("receipt_cache_max must be a positive integer")
        self._scene = scene_adapter
        self._preflight = ChangeSetPreflightAdapter(scene_adapter)
        self._receipts: "OrderedDict[tuple[str, str], ChangeReceipt]" = OrderedDict()
        self._receipt_cache_max = receipt_cache_max
        self._write_frozen = False
        self._current_run_id: str = ""
        self._created_identity: dict[str, tuple[str, str]] = {}

    @property
    def _hou(self) -> object:
        return self._scene._hou  # type: ignore[attr-defined]

    @property
    def write_frozen(self) -> bool:
        """True once a Partial/CriticalRecovery receipt froze writes."""
        return self._write_frozen

    @property
    def _tracked_epoch(self) -> int:
        # In-memory scene epoch (no HOM access) — safe to read from a receipt query.
        return self._scene._scene_epoch  # type: ignore[attr-defined]

    def binding(self):  # type: ignore[no-untyped-def]
        """Delegate the current scene binding (tracked epoch + instance)."""
        return self._scene.binding()

    # --------------------------------------------------------------- receipt query

    def receipt(
        self,
        change_id: str,
        changeset_digest: str,
        *,
        scene_epoch: int | None = None,
    ) -> ChangeReceipt | None:
        """Return the cached terminal receipt for ``(change_id, digest)`` or None.

        Never touches or mutates the scene. A supplied ``scene_epoch`` is checked
        against the in-memory tracked epoch BEFORE any cache access (a stale
        receipt request fails closed). A cached terminal result for the same
        change id under a DIFFERENT digest is a ``changeset.receipt_conflict``.
        Cache bounds and deterministic eviction are tested; the cache is
        process-local and not persisted.
        """
        if scene_epoch is not None and scene_epoch != self._tracked_epoch:
            raise _stale_scene()
        for (cached_id, cached_digest), _r in self._receipts.items():
            if cached_id == change_id and cached_digest != changeset_digest:
                raise _conflict()
        return self._receipts.get((change_id, changeset_digest))

    # --------------------------------------------------------------- apply

    def apply(self, request: ApplyRequest) -> ChangeReceipt:
        """Execute one admitted ChangeSet transactionally; return its receipt.

        Pre-transaction failures (write freeze, digest conflict, backup
        required, stale scene/manifest/identity, locked target, a precondition
        that does not hold, missing/contradictory derived facts, or any policy
        denial) raise :class:`HoudiniAdapterError` and perform zero writes.
        Execution or reconciliation failures return a ``RolledBack``/``Partial``/
        ``CriticalRecovery`` receipt and never claim success; the transaction
        phase never raises out of this method.
        """
        if self._write_frozen:
            raise _frozen()

        hou = self._hou
        changeset = request.changeset
        digest = request.changeset_digest
        change_id = changeset.change_id
        key = (change_id, digest)

        # Idempotency: a prior SUCCESS for the exact (change_id, digest) replays
        # as AlreadyApplied (zero writes). A prior non-success is returned verbatim
        # (do not re-run the transaction). Same id + different digest is a conflict.
        cached = self._receipts.get(key)
        if cached is not None:
            if cached.status in (ReceiptStatus.APPLIED, ReceiptStatus.ALREADY_APPLIED):
                return self._already_applied(cached)
            return cached
        self._reject_conflicting_digest(change_id, digest)

        if changeset.risk_summary.requires_backup:
            raise _backup_unavailable()

        # ---- Zero-write re-validation ----------------------------------------
        # Preflight re-derives binding/identity/create-absence and evaluates the
        # supplied preconditions from the current scene (raises on stale/identity).
        preflight_result = self._preflight.preflight(request)
        if not preflight_result.all_preconditions_hold:
            raise _precondition_failed()
        self._reject_locked_changes(preflight_result, changeset)
        # F2: the ChangeSet must carry every derived pre/postcondition and the
        # exact before-snapshot coverage; omission/contradiction fails closed.
        verify_mandatory_coverage(changeset, request.workspace)
        # F1: re-run the accepted pure policy engine against the current workspace
        # and current lock facts; any denial fails closed before writes.
        decision = evaluate_policy(
            changeset,
            workspace=request.workspace,
            locked_node_paths=self._locked_paths(preflight_result),
        )
        if not decision.allowed:
            raise _policy_denied(decision.denial_codes)

        binding = self.binding()
        self._current_run_id = changeset.run_id
        # D1: map created node_id -> (capability, role) for JIT identity verification
        self._created_identity = {
            op.node_id: (op.capability, op.role)
            for op in changeset.operations if isinstance(op, CreateNode)
        }
        index = self._index_scene_by_node_id(hou)
        before = self._snapshot(hou, changeset, index)
        before_revision = self._revision(before)

        # ---- TRANSACTION: from the first write on, never raise; always receipt --
        applied_op_ids: list[str] = []
        inverses: list[tuple] = []
        write_error: BaseException | None = None
        try:
            with hou.undos.group(_UNDO_LABEL_PREFIX + change_id):  # type: ignore[union-attr]
                for op in changeset.operations:
                    self._execute_op(hou, op, index, inverses, applied_op_ids)
        except Exception as exc:  # noqa: BLE001 — any execution failure -> rollback
            write_error = exc

        post_results: tuple[ConditionResult, ...] = ()
        reconciled = False
        reconciliation_error: BaseException | None = None
        if write_error is None:
            try:
                after_index = self._index_scene_by_node_id(hou)
                after = self._snapshot(hou, changeset, after_index)
                post_results = self._evaluate_postconditions(changeset, after, binding)
                reconciled = all(result.passed for result in post_results)
            except Exception as exc:  # noqa: BLE001 — reconciliation crash -> uncertain
                reconciliation_error = exc
                reconciled = False
            if reconciled:
                after_revision = self._revision(after)
                receipt = ChangeReceipt(
                    change_id=change_id,
                    status=ReceiptStatus.APPLIED,
                    instance_id=binding.instance_id,
                    scene_epoch=binding.scene_epoch,
                    before_revision=before_revision,
                    after_revision=after_revision,
                    applied_op_ids=tuple(applied_op_ids),
                    postcondition_results=post_results,
                    rollback_results=(),
                    scene_may_have_changed=False,
                    completed_at=_now(),
                )
                self._cache(key, receipt)
                return receipt

        # Failure (write error, postcondition failure, or reconciliation crash):
        # attempt rollback, classify truthfully, and freeze on uncertainty.
        rollback_results, rollback_ok = self._safe_rollback(hou, inverses)
        after_revision = self._safe_after_revision(hou, changeset, before_revision)
        status, scene_may_have_changed = self._classify_failure(
            rollback_results, rollback_ok
        )
        if status != ReceiptStatus.ROLLED_BACK:
            # An uncertain recovery freezes further writes until process restart.
            self._write_frozen = True
        error_code, error_message = self._classify_apply_failure(
            write_error=write_error,
            reconciliation_error=reconciliation_error,
            post_results=post_results,
            reconciled=reconciled,
            applied_op_ids=applied_op_ids,
            operations=changeset.operations,
        )
        receipt = ChangeReceipt(
            change_id=change_id,
            status=status,
            instance_id=binding.instance_id,
            scene_epoch=binding.scene_epoch,
            before_revision=before_revision,
            after_revision=after_revision,
            applied_op_ids=tuple(applied_op_ids),
            postcondition_results=post_results,
            rollback_results=rollback_results,
            scene_may_have_changed=scene_may_have_changed,
            completed_at=_now(),
            error_code=error_code,
            error_message=error_message,
        )
        self._cache(key, receipt)
        return receipt

    # --------------------------------------------------------------- idempotency

    def _already_applied(self, cached: ChangeReceipt) -> ChangeReceipt:
        return ChangeReceipt(
            change_id=cached.change_id,
            status=ReceiptStatus.ALREADY_APPLIED,
            instance_id=cached.instance_id,
            scene_epoch=cached.scene_epoch,
            before_revision=cached.before_revision,
            after_revision=cached.after_revision,
            applied_op_ids=(),
            postcondition_results=cached.postcondition_results,
            rollback_results=(),
            scene_may_have_changed=False,
            completed_at=_now(),
        )

    def _reject_conflicting_digest(self, change_id: str, digest: str) -> None:
        for (cached_id, cached_digest), _receipt in self._receipts.items():
            if cached_id == change_id and cached_digest != digest:
                raise _conflict()

    def _reject_locked_changes(self, preflight_result: PreflightResult, changeset) -> None:
        """Reject a ChangeSet that writes to a locked node (zero writes).

        The accepted 16-C preflight reports ``is_locked`` as a fact but does not
        gate on it (preflight is read-only). The transaction must not mutate a
        locked target, so this re-derivation fails closed before any write.
        """
        locked = self._locked_paths(preflight_result)
        if not locked:
            return
        for ref in self._changed_targets(changeset):
            if ref.path in locked:
                raise _stale("A changed target is locked in the current scene.")

    @staticmethod
    def _locked_paths(preflight_result: PreflightResult) -> frozenset[str]:
        return frozenset(
            fact.requested.path for fact in preflight_result.node_facts if fact.is_locked
        )

    def _changed_targets(self, changeset) -> list[NodeRef]:  # type: ignore[no-untyped-def]
        targets: list[NodeRef] = []
        for op in changeset.operations:
            if isinstance(op, SetParm):
                targets.append(op.target)
            elif isinstance(op, ConnectInput):
                targets.append(op.target)
                targets.append(op.source)
        return targets

    def _cache(self, key: tuple[str, str], receipt: ChangeReceipt) -> None:
        self._receipts[key] = receipt
        while len(self._receipts) > self._receipt_cache_max:
            self._receipts.popitem(last=False)

    # --------------------------------------------------------------- sensitivity sampling

    def sample_sensitivity(
        self, request: SensitivitySampleRequest
    ) -> SensitivitySampleResult:
        """Run one bounded sample-and-restore cycle; return typed evidence.

        Pre-write failures (write freeze, stale scene epoch, an unresolvable
        sample target, or a missing parameter) raise
        :class:`HoudiniAdapterError` and perform zero writes. From the first
        write on, this method never raises out of the write phase: every
        written parameter is restored to its exact original value with
        read-back verification (mirroring the apply rollback discipline), a
        cook failure or aborted capture is classified into a structured error,
        and an unverifiable restore freezes writes and fails closed. A result
        is returned only after restoration is proven; it is never a guessed
        success. All HOM access happens inside the single synchronous callable
        the shared main-thread FIFO pumps, exactly like :meth:`apply`.
        """
        if self._write_frozen:
            raise _frozen()

        hou = self._hou
        binding = self.binding()
        # Scene-epoch drift is checked first (consistent with scene.query/apply).
        if binding.scene_epoch != request.scene_epoch:
            raise _stale_scene()

        # ---- Zero-write resolution -----------------------------------------
        # Every target must resolve (stable id first, then path) and expose the
        # exact parameter BEFORE the first write.
        index = self._index_scene_by_node_id(hou)
        resolved: list[tuple[object, str, object, object]] = []
        for target in request.samples:
            node = self._resolve_sample_node(hou, target, index)
            if node is None:
                raise _sample_invalid(
                    "A sensitivity sample target was not found in the scene."
                )
            original, existed = self._read_parm_value(node, target.parm_name)
            if not existed:
                raise _sample_invalid(
                    "A sensitivity sample parameter was not found on its node."
                )
            resolved.append((node, target.parm_name, target.value, original))

        # Bounded read-only baseline (may raise a structured stale/read error).
        baseline = self._capture_sample_scene(request)

        # ---- WRITE PHASE: from the first write on, never raise; restore and
        # classify (mirrors the apply transaction discipline).
        samples: list = []
        written: list[tuple[object, str, object]] = []
        failure: BaseException | None = None
        try:
            with hou.undos.group(_UNDO_LABEL_PREFIX + "sensitivity.sample"):  # type: ignore[union-attr]
                for node, parm_name, sample_value, original in resolved:
                    self._write_parm_value(node, parm_name, sample_value)
                    written.append((node, parm_name, original))
                    self._cook_sample_node(node)
                    samples.append(self._capture_sample_scene(request))
        except Exception as exc:  # noqa: BLE001 — any write/capture failure -> restore
            failure = exc

        # Exact restore in reverse write order, each write-back verified by a
        # read-back comparison (the _rollback_parm primitive, inlined so one
        # bad restore marks the whole cycle unrestored).
        restore_ok = True
        for node, parm_name, original in reversed(written):
            try:
                self._write_parm_value(node, parm_name, original)
                current, existed = self._read_parm_value(node, parm_name)
            except Exception:  # noqa: BLE001 — restore failure => not restored
                restore_ok = False
                break
            if not existed or not _parm_equal(current, original):
                restore_ok = False
                break
        if not restore_ok:
            # The scene may be left mutated: freeze writes until process restart
            # (same uncertainty rule as a Partial/CriticalRecovery receipt).
            self._write_frozen = True
            raise _sample_restore_failed()
        if failure is not None:
            if isinstance(failure, HoudiniAdapterError):
                raise failure
            raise _sample_aborted() from failure

        restored = self._capture_sample_scene(request)
        return SensitivitySampleResult(
            baseline=baseline,
            samples=tuple(samples),
            restored=restored,
        )

    def _resolve_sample_node(
        self, hou: object, target: object, index: dict[str, list[object]]
    ) -> object | None:
        """Resolve a sample target stable-id-first (mirrors _resolve_existing)."""
        node_id = target.node_id  # type: ignore[attr-defined]
        if node_id is None:
            return hou.node(target.path)  # type: ignore[union-attr,attr-defined]
        mirrors = index.get(node_id, ())
        if len(mirrors) > 1:
            raise _ambiguous()
        if mirrors:
            return mirrors[-1]
        return hou.node(target.path)  # type: ignore[union-attr,attr-defined]

    def _capture_sample_scene(self, request: SensitivitySampleRequest):  # type: ignore[no-untyped-def]
        """Bounded read-only evidence capture over the exact requested paths."""
        return self._scene.scene_query(
            include_selection=False,
            node_paths=request.node_paths,
            include_geometry_stats=True,
            expected_scene_epoch=request.scene_epoch,
        )

    @staticmethod
    def _cook_sample_node(node: object) -> None:
        """Force-cook one sampled node and fail closed on any cook error."""
        try:
            node.cook(force=True)  # type: ignore[attr-defined]
            errors = node.errors()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — a failed cook is not a sample
            raise _cook_failed() from exc
        if errors:
            raise _cook_failed()

    # --------------------------------------------------------------- artifact capture

    def capture(self, request: CaptureRequest) -> CaptureResult:
        """Run one deterministic screenshot capture; return its typed reference.

        Pre-scope failures (stale scene epoch, an unavailable target directory,
        an unresolvable evidence node, a cook failure, empty geometry, or an
        unreachable framing) raise :class:`HoudiniAdapterError` and perform
        zero scene changes. From the temp camera/ROP scope creation on, the
        op never raises out of the scope phase: the scope is destroyed first
        (always, including on render failure), partial files are removed, and
        every failure is classified into a structured error — a cleanup
        failure is explicit and discards the capture, never a guessed visual
        success. A result is returned only after the PNG was verified,
        hashed, and atomically renamed and the owned temp scope is gone; the
        result carries content-addressed reference fields only, never image
        bytes. All HOM access happens inside the single synchronous callable
        the shared main-thread FIFO pumps, exactly like :meth:`apply`.
        """
        hou = self._hou
        binding = self.binding()
        # Scene-epoch drift is checked first (consistent with scene.query/apply).
        if binding.scene_epoch != request.scene_epoch:
            raise _stale_scene()

        # ---- Zero-scene-change prechecks -----------------------------------
        target_dir = Path(request.target_dir)
        if not target_dir.is_dir():
            raise _capture_target_unavailable()
        # The in-progress render lives in a sibling temp directory so the
        # picture path keeps its .png extension: the flipbook picks the image
        # format from the extension and an unrecognized one silently renders
        # a non-PNG file. Renaming the verified file into place stays atomic.
        tmp_dir = target_dir / (".tmp_" + request.artifact_id)
        try:
            tmp_dir.mkdir(exist_ok=True)
        except OSError:
            raise _capture_target_unavailable() from None
        nodes: list[object] = []
        for path in request.node_paths:
            node = hou.node(path)  # type: ignore[union-attr]
            if node is None:
                raise _capture_invalid(
                    "A capture evidence node was not found in the scene."
                )
            nodes.append(node)
        for node in nodes:
            self._cook_capture_node(node)
        union = self._union_capture_bbox(nodes)
        if union.bounding_radius <= 0.0:
            raise _capture_no_geometry()
        settings = request.settings
        try:
            plan = compute_framing(
                union,
                frame_width=settings.preflight_width,
                frame_height=settings.preflight_height,
                tolerance=FramingTolerance(
                    margin_min=settings.margin_min,
                    longest_axis_min=settings.longest_axis_min,
                    longest_axis_max=settings.longest_axis_max,
                    center_offset_max=settings.center_offset_max,
                ),
                max_adjustments=settings.max_adjustments,
            )
        except FramingError:
            raise _capture_framing_failed() from None

        # ---- Temp scope + render: from the first node creation on, classify
        # and always clean up (mirrors the apply transaction discipline).
        file_name = request.file_name
        tmp_path = tmp_dir / file_name
        final_path = target_dir / file_name
        scope: list[object] = []
        failure: BaseException | None = None
        try:
            with hou.undos.group(_UNDO_LABEL_PREFIX + "capture.capture"):  # type: ignore[union-attr]
                cam = self._create_capture_camera(hou, request, plan, scope)
                rop = self._create_capture_rop(hou, request, cam, tmp_path, scope)
                self._render_capture(rop)
        except Exception as exc:  # noqa: BLE001 — any scope/render failure -> cleanup
            failure = exc
        cleanup_failure = self._destroy_capture_scope(scope)
        if cleanup_failure is not None:
            # The scene may retain temp nodes: the capture is discarded and
            # the failure is explicit, never a masqueraded success.
            _unlink_quietly(tmp_path)
            _rmdir_quietly(tmp_dir)
            _unlink_quietly(final_path)
            raise _capture_cleanup_failed() from cleanup_failure
        if failure is not None:
            _unlink_quietly(tmp_path)
            _rmdir_quietly(tmp_dir)
            _unlink_quietly(final_path)
            if isinstance(failure, HoudiniAdapterError):
                raise failure
            raise _capture_render_failed() from failure

        # Bytes verified only after the temp scope is gone; the Houdini side
        # hashes the exact delivered file once and renames it atomically.
        try:
            sha256, size = _verify_capture_file(tmp_path)
            os.replace(tmp_path, final_path)
        except HoudiniAdapterError:
            _unlink_quietly(tmp_path)
            _rmdir_quietly(tmp_dir)
            raise
        except OSError as exc:
            _unlink_quietly(tmp_path)
            _rmdir_quietly(tmp_dir)
            raise _capture_render_failed() from exc
        _rmdir_quietly(tmp_dir)
        evaluation = plan.evaluation
        return CaptureResult(
            artifact_id=request.artifact_id,
            relative_path=file_name,
            sha256=sha256,
            media_type=PNG_MEDIA_TYPE,
            size_bytes=size,
            framing=CaptureFramingReport(
                adjustments_used=plan.adjustments_used,
                margin_left=evaluation.margin_left,
                margin_right=evaluation.margin_right,
                margin_bottom=evaluation.margin_bottom,
                margin_top=evaluation.margin_top,
                longest_axis_ratio=evaluation.longest_axis_ratio,
                center_offset=evaluation.center_offset,
            ),
        )

    @staticmethod
    def _cook_capture_node(node: object) -> None:
        """Force-cook one evidence node and fail closed on any cook error."""
        try:
            node.cook(force=True)  # type: ignore[attr-defined]
            errors = node.errors()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — a failed cook is not a capture
            raise _capture_cook_failed() from exc
        if errors:
            raise _capture_cook_failed()

    @staticmethod
    def _union_capture_bbox(nodes: list[object]) -> BoundingBox:
        """Union the cooked world-space bounding boxes of the evidence nodes.

        SOP nodes expose ``geometry()`` directly; object-level nodes do not
        (``ObjNode`` has no ``geometry()``), so their display SOP geometry is
        read instead and lifted to world space by the object world transform.
        A SOP bbox is likewise lifted by its parent object transform when one
        is readable. Nodes with no readable geometry contribute nothing.
        """
        boxes: list[BoundingBox] = []
        for node in nodes:
            box = ChangeSetExecutor._world_capture_bbox(node)
            if box is not None:
                boxes.append(box)
        if not boxes:
            raise _capture_no_geometry()
        return BoundingBox.union(boxes)

    @staticmethod
    def _world_capture_bbox(node: object) -> BoundingBox | None:
        """World-space bbox of one evidence node; None when nothing is readable."""
        try:
            geometry = node.geometry()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — object-level nodes have no geometry()
            geometry = None
        if geometry is not None:
            # SOP node: its geometry is local; lift it by the parent object
            # world transform when one is readable.
            try:
                parent = node.parent()  # type: ignore[attr-defined]
                xform = parent.worldTransform() if parent is not None else None
            except Exception:  # noqa: BLE001 — no transform contributes local space
                xform = None
        else:
            # Object-level node: frame its display SOP geometry transformed
            # by the object's own world transform.
            try:
                display = node.displayNode()  # type: ignore[attr-defined]
                if display is None:
                    return None
                geometry = display.geometry()
                xform = node.worldTransform()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — no readable geometry contributes nothing
                return None
        try:
            bbox = geometry.boundingBox()
            mn = bbox.minvec()
            mx = bbox.maxvec()
        except Exception:  # noqa: BLE001 — no readable geometry contributes nothing
            return None
        values = (mn.x(), mn.y(), mn.z(), mx.x(), mx.y(), mx.z())
        matrix: tuple[float, ...] | None = None
        if xform is not None:
            try:
                candidate = tuple(xform.asTuple())
            except Exception:  # noqa: BLE001 — untransformed is better than nothing
                candidate = ()
            if len(candidate) == 16:
                matrix = candidate
        if matrix is None:
            return BoundingBox(*values)
        return _transform_bbox_world(values, matrix)

    def _create_capture_camera(
        self, hou: object, request: CaptureRequest, plan: object, scope: list[object]
    ) -> object:
        """Create the owned temp camera at the deterministic plan placement."""
        obj = hou.node("/obj")  # type: ignore[union-attr]
        if obj is None:
            raise _capture_render_unavailable()
        camera = plan.camera  # type: ignore[attr-defined]
        right, up, forward = camera_basis(camera.position, camera.look_at)
        # Houdini cameras look down -Z with +Y up: the world-from-camera rows
        # are (right, up, -forward, position); setParmTransform applies t/r.
        rows = (
            (right[0], up[0], -forward[0], camera.position[0]),
            (right[1], up[1], -forward[1], camera.position[1]),
            (right[2], up[2], -forward[2], camera.position[2]),
            (0.0, 0.0, 0.0, 1.0),
        )
        try:
            cam = obj.createNode("cam", "eee_capture_" + request.artifact_id[4:])  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — no camera, no capture
            raise _capture_render_unavailable() from exc
        # Journal the created node immediately: it now exists, so any failure
        # in the mirror steps must clean it up (mirrors _execute_create).
        scope.append(cam)
        try:
            cam.setParmTransform(hou.Matrix4(rows))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — an unusable camera is unavailable
            raise _capture_render_unavailable() from exc
        self._set_capture_parm(cam, "focal", camera.focal_length_mm)
        self._set_capture_parm(cam, "aperture", camera.sensor_width_mm)
        return cam

    def _create_capture_rop(
        self,
        hou: object,
        request: CaptureRequest,
        cam: object,
        tmp_path: Path,
        scope: list[object],
    ) -> object:
        """Create the owned temp Vulkan Flipbook ROP with neutral settings."""
        out = hou.node("/out")  # type: ignore[union-attr]
        if out is None:
            raise _capture_render_unavailable()
        try:
            rop = out.createNode("flipbook", "eee_capture_" + request.artifact_id[4:])  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — no flipbook ROP, no capture
            raise _capture_render_unavailable() from exc
        # Journal the created node immediately: it now exists, so any failure
        # in the settings steps must clean it up (mirrors _execute_create).
        scope.append(rop)
        settings = request.settings
        self._set_capture_parm(rop, "trange", "off")
        self._set_capture_parm(rop, "camera", cam.path())  # type: ignore[attr-defined]
        self._set_capture_parm(rop, "picture", tmp_path.as_posix())
        self._set_capture_parm(rop, "tres", 1)
        resolution = rop.parmTuple("res")  # type: ignore[attr-defined]
        if resolution is None:
            raise _capture_render_unavailable()
        resolution.set((settings.width, settings.height))
        # Deterministic neutral look: 8x AA, smooth shaded, fixed headlight,
        # no materials/textures/transparency/motion blur.
        self._set_capture_parm(rop, "aamode", "aa8")
        self._set_capture_parm(rop, "shadingmode", "smooth")
        self._set_capture_parm(rop, "lighting", "headlight")
        self._set_capture_parm(rop, "worklighttype", "headlight")
        self._set_capture_parm(rop, "usematerials", 0)
        self._set_capture_parm(rop, "usetextures", 0)
        self._set_capture_parm(rop, "transparency", "off")
        self._set_capture_parm(rop, "motionblur", 0)
        return rop

    @staticmethod
    def _set_capture_parm(node: object, name: str, value: object) -> None:
        """Set one render parameter; a missing surface fails closed."""
        parm = node.parm(name)  # type: ignore[attr-defined]
        if parm is None:
            raise _capture_render_unavailable()
        try:
            parm.set(value)
        except Exception as exc:  # noqa: BLE001 — an unsettable parm is unavailable
            raise _capture_render_unavailable() from exc

    @staticmethod
    def _render_capture(rop: object) -> None:
        """Render the single current frame and fail closed on any error."""
        try:
            rop.render()  # type: ignore[attr-defined]
            errors = rop.errors()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — a failed render is not a capture
            raise _capture_render_failed() from exc
        if errors:
            raise _capture_render_failed()

    @staticmethod
    def _destroy_capture_scope(scope: list[object]) -> BaseException | None:
        """Destroy the owned temp nodes in reverse order; return first failure."""
        failure: BaseException | None = None
        for node in reversed(scope):
            try:
                node.destroy()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — keep destroying the rest
                if failure is None:
                    failure = exc
        return failure

    # --------------------------------------------------------------- scratch sandbox

    def _resolve_scratch_node(
        self, container_path: str, node_index: dict[str, object], ref: str,
    ) -> object | None:
        """Resolve a scratch-relative ref without silently falling back to root."""
        if ref == "":
            return self._hou.node(container_path)
        node = node_index.get(ref)
        if node is not None:
            return node
        if ref.startswith("/") or "\\" in ref or any(
            segment in ("", ".", "..") for segment in ref.split("/")
        ):
            return None
        return self._hou.node(f"{container_path}/{ref}")

    def scratch_exec(self, request: ScratchRequest) -> ScratchResult:
        """Build/extend a reserved scratch container and return diagnostics.

        The sandbox lives at ``/obj/eee_scratch_<sandbox_id>`` (a plain geo
        container, created on first call, reused across calls so the agent can
        iterate). Structured ops (create_node / set_parm / connect) are applied
        in one undo group WITHOUT ownership mirrors — scratch nodes never carry
        ``eee.node_id`` / ``eee.workspace_id``, so they cannot pollute a real
        workspace's node-id index. On failure the container is preserved by
        default (``preserve_on_failure``) so the agent can inspect and retry;
        only when the flag is False is the whole container destroyed.

        Returns bounded diagnostics: the container path, the count of ops
        applied, the output node (last created node), cook errors, and the
        cooked geometry stats of the output node.
        """
        binding = self.binding()
        if binding.scene_epoch != request.scene_epoch:
            raise _stale(
                "The scene changed before the scratch operation could run."
            )
        hou = self._hou
        container_path = request.container_path
        obj_network = hou.node("/obj")
        if obj_network is None:
            raise _scratch_failed(
                "The /obj network is unavailable in this Houdini session."
            )

        container = hou.node(container_path)
        created_this_call: list[object] = []
        applied = 0
        output_node_path = container_path
        errors: list[str] = []
        failure: BaseException | None = None
        node_index: dict[str, object] = {}

        try:
            with hou.undos.group(_UNDO_LABEL_PREFIX + "scratch.exec"):
                # Create or reuse the sandbox container.
                if container is None:
                    container = obj_network.createNode(
                        "geo", request.container_name
                    )
                    created_this_call.append(container)
                node_index[""] = container  # parent "" => container root

                for op in request.operations:
                    if op.kind == "create_node":
                        parent = self._resolve_scratch_node(
                            container_path, node_index, op.parent
                        )
                        if parent is None:
                            raise _scratch_failed(
                                f"create_node parent not found: {op.parent}"
                            )
                        node = parent.createNode(op.node_type, op.node_name)
                        created_this_call.append(node)
                        key = f"{op.parent}/{op.node_name}" if op.parent else op.node_name
                        node_index[key] = node
                        output_node_path = node.path()
                        applied += 1
                    elif op.kind == "declare_parm":
                        node = self._resolve_scratch_node(
                            container_path, node_index, op.node_name
                        )
                        if node is None:
                            raise _scratch_failed(
                                f"declare_parm target node not found: {op.node_name}"
                            )
                        if node.type().name() not in {"subnet", "geo"}:
                            raise _scratch_failed(
                                "declare_parm target must be a subnet or geo container"
                            )
                        existing = node.parm(op.parm)
                        if existing is not None:
                            if not getattr(existing, "isSpare", lambda: False)():
                                raise _scratch_failed(
                                    f"declare_parm cannot replace native parm: "
                                    f"{op.node_name}/{op.parm}"
                                )
                            template = existing.parmTemplate()
                            if template.type().name() != "Float":
                                raise _scratch_failed(
                                    f"declare_parm requires a float spare parm: "
                                    f"{op.node_name}/{op.parm}"
                                )
                            existing.set(float(op.value))
                        else:
                            template = hou.FloatParmTemplate(
                                op.parm,
                                op.label,
                                1,
                                default_value=(float(op.value),),
                                min=float(op.minimum),
                                max=float(op.maximum),
                            )
                            template.setTags({"unit": op.unit})
                            node.addSpareParmTuple(template)
                        applied += 1
                    elif op.kind == "set_parm":
                        node = self._resolve_scratch_node(
                            container_path, node_index, op.node_name
                        )
                        if node is None:
                            raise _scratch_failed(
                                f"set_parm target node not found: {op.node_name}"
                            )
                        parm = node.parm(op.parm)
                        if parm is None:
                            raise _scratch_failed(
                                f"parm not found on {op.node_name}: {op.parm}"
                            )
                        if op.expr is not None:
                            _apply_scratch_expr(
                                hou, container_path, node, parm, op
                            )
                        else:
                            parm.set(op.value)
                        applied += 1
                    elif op.kind == "connect":
                        node = self._resolve_scratch_node(
                            container_path, node_index, op.node_name
                        )
                        source = self._resolve_scratch_node(
                            container_path, node_index, op.source
                        )
                        if node is None or source is None:
                            raise _scratch_failed(
                                f"connect endpoints not found: "
                                f"{op.node_name} or {op.source}"
                            )
                        node.setInput(op.input_index, source, op.source_output_index)
                        applied += 1
                    elif op.kind == "delete_node":
                        node = self._resolve_scratch_node(
                            container_path, node_index, op.node_name
                        )
                        if node is None:
                            raise _scratch_failed(
                                f"delete_node target node not found: {op.node_name}"
                            )
                        node.destroy()
                        for key, value in list(node_index.items()):
                            if value is node:
                                node_index.pop(key, None)
                        if output_node_path == node.path():
                            output_node_path = container_path
                        applied += 1
        except HoudiniAdapterError:
            failure = sys.exc_info()[1]  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001 — classify any cook/HOM failure
            failure = exc

        # Collect diagnostics from the output node (best-effort, never raises).
        geometry: ScratchGeometry | None = None
        try:
            output_node = hou.node(output_node_path)
            if output_node is not None:
                output_node.cook(force=True)
                node_errors = list(output_node.errors() or [])
                errors = [str(e)[:_MAX_ERROR_CHARS] for e in node_errors[:_MAX_ERRORS]]
                geometry = self._scratch_geometry(output_node)
        except Exception:  # noqa: BLE001 — diagnostics are best-effort
            pass

        # Cleanup: if requested AND this call created the container, destroy
        # everything created this call (the whole sandbox if we made it, else
        # just the nodes created this call that followed a failure). When
        # preserve_on_failure is True (default), leave the sandbox intact so
        # the agent can inspect and retry. A successful call never destroys.
        if failure is not None and not request.preserve_on_failure:
            self._destroy_capture_scope(created_this_call)

        if failure is not None:
            # Any op failure must be surfaced honestly. When
            # ``preserve_on_failure`` is True (default) the partially-applied
            # sandbox is left intact for inspection; the applied_ops count and
            # output_node in the raised diagnostics tell the agent how far it
            # got. We never return a ScratchResult that looks like success when
            # an operation failed — that would let a half-applied build read as
            # a partial success to the agent.
            if isinstance(failure, HoudiniAdapterError):
                raise failure
            raise _scratch_failed(
                f"scratch operation failed after {applied} op(s) at "
                f"{output_node_path}: {failure}"[:_MAX_ERROR_CHARS]
            ) from failure

        return ScratchResult(
            sandbox_root=container_path,
            applied_ops=applied,
            output_node=output_node_path,
            errors=tuple(errors),
            geometry=geometry,
        )

    def scratch_destroy(self, request: ScratchDestroyRequest) -> ScratchDestroyResult:
        """Best-effort destroy of one run-scoped sandbox container.

        Run-end/cancel/restart hooks call this to avoid leaking
        ``/obj/eee_scratch_<sandbox_id>`` containers. If the container does not
        exist (already committed or cleaned up), returns ``missing=True`` — a
        normal, non-error outcome. Any destroy failure is swallowed and the
        surviving path is simply not reported as destroyed.
        """
        hou = self._hou
        container_path = request.container_path
        destroyed: list[str] = []
        try:
            container = hou.node(container_path)
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            return ScratchDestroyResult(destroyed_paths=(), missing=True)
        if container is None:
            return ScratchDestroyResult(destroyed_paths=(), missing=True)
        try:
            # Cleanup is terminal: destroy must NOT be a user-undoable chunk.
            # If it were grouped, a later hou.undos.undo() (by the user or any
            # code path) could resurrect /obj/eee_scratch_<sandbox_id> after the
            # run is already terminal, leaving an orphan that collides with the
            # next run of the same id. Run it with undo recording disabled.
            with hou.undos.disabler():  # type: ignore[union-attr]
                container.destroy()
                destroyed.append(container_path)
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            # Leave destroyed as-is (empty if the destroy threw before completing).
            pass
        return ScratchDestroyResult(
            destroyed_paths=tuple(destroyed),
            missing=False,
        )

    def delete_nodes(self, request: ScratchDeleteRequest) -> ScratchDeleteResult:
        """Delete allowlisted nodes, refusing nodes with external consumers."""
        binding = self.binding()
        if binding.scene_epoch != request.scene_epoch:
            raise _stale("The scene changed before the delete operation could run.")
        hou = self._hou
        allowed = set(request.allowed_paths)
        delete_set = set(request.paths)
        deleted: list[str] = []
        skipped: list[dict[str, object]] = []
        with hou.undos.group(_UNDO_LABEL_PREFIX + "cleanup"):
            for path in request.paths:
                if path not in allowed:
                    skipped.append({"path": path, "reason": "not in the allowlist"})
                    continue
                node = hou.node(path)
                if node is None:
                    skipped.append({"path": path, "reason": "node does not exist"})
                    continue
                try:
                    consumers = [output.path() for output in (node.outputs() or [])]
                except Exception as exc:  # noqa: BLE001
                    skipped.append(
                        {
                            "path": path,
                            "reason": f"topology read failed: {exc}"[:_MAX_ERROR_CHARS],
                        }
                    )
                    continue
                external = [consumer for consumer in consumers if consumer not in delete_set]
                if external:
                    skipped.append(
                        {
                            "path": path,
                            "reason": f"still referenced by {external[0]}",
                        }
                    )
                    continue
                try:
                    node.destroy()
                    deleted.append(path)
                except Exception as exc:  # noqa: BLE001
                    skipped.append(
                        {
                            "path": path,
                            "reason": f"destroy failed: {exc}"[:_MAX_ERROR_CHARS],
                        }
                    )
        return ScratchDeleteResult(
            deleted_paths=tuple(deleted), skipped=tuple(skipped)
        )

    def scratch_topology(
        self, request: ScratchTopologyRequest
    ) -> ScratchTopologyResult:
        """Read-only bounded wiring facts for cleanup analysis."""
        binding = self.binding()
        if binding.scene_epoch != request.scene_epoch:
            raise _stale("The scene changed before the topology query could run.")
        hou = self._hou
        nodes: list[dict[str, object]] = []
        for path in request.paths:
            node = hou.node(path)
            if node is None:
                nodes.append(
                    {
                        "path": path,
                        "exists": False,
                        "inputs": [],
                        "outputs": [],
                        "display_flag": False,
                    }
                )
                continue
            inputs = [
                src.path()
                for src in (node.inputs() or ())  # type: ignore[attr-defined]
                if src is not None
            ][:_MAX_ERRORS]
            outputs = [output.path() for output in (node.outputs() or [])][:_MAX_ERRORS]
            nodes.append(
                {
                    "path": path,
                    "exists": True,
                    "inputs": inputs,
                    "outputs": outputs,
                    "display_flag": bool(node.isDisplayFlagSet()),
                }
            )
        return ScratchTopologyResult(nodes=tuple(nodes))

    def scratch_commit(self, request: ScratchCommitRequest) -> ScratchCommitResult:
        """Promote a verified sandbox into the real scene through hard gates.

        The sandbox at ``/obj/eee_scratch_<sandbox_id>`` is cooked, then the
        four verify gates (bake / structure / orientation / health) run against
        its output node. On PASS, the sandbox container is renamed into the
        real scene at ``target_parent_path/target_name`` inside a single
        ``hou.undos.group``. Note: ``hou.undos.group`` groups operations into
        one user-undoable chunk — it is NOT a transaction and does NOT roll
        back automatically if a HOM call raises mid-group. The promotion
        therefore journals the pre-rename name explicitly and restores it on
        any failure, so the sandbox is never left half-promoted (renamed but
        not moved). On REFUSE, the sandbox is preserved untouched so the agent
        can fix and re-commit.

        Returns a bounded verdict: committed/refused, the final path, the
        per-gate results, and a tamper-evident verification receipt.
        """
        binding = self.binding()
        if binding.scene_epoch != request.scene_epoch:
            raise _stale(
                "The scene changed before the scratch commit could run."
            )
        hou = self._hou
        container_path = request.container_path
        container = hou.node(container_path)
        if container is None:
            raise _scratch_failed(
                f"The sandbox container does not exist: {container_path}"
            )

        # Resolve the output node (the display/render node of the container).
        output_node = self._scratch_output_node(container)
        final_path = f"{request.target_parent_path}/{request.target_name}"

        # Run the verify gates (hou-free module; reads cooked geometry).
        gate_report = self._run_scratch_gates(
            container, output_node, request.orientation_checks,
            request.skip_structure_check,
        )

        if not gate_report["passed"]:
            # Refused: preserve the sandbox. Build a receipt + verdict.
            receipt = _build_commit_receipt(gate_report, request.parameters)
            return ScratchCommitResult(
                committed=False,
                refused=True,
                final_path=container_path,
                reason=_gate_failure_reason(gate_report),
                gates=tuple(gate_report["gates"]),
                receipt=receipt,
            )

        # Gates passed: promote the sandbox into the real scene. The rename
        # runs inside one undo group (so the user sees a single undo entry),
        # but hou.undos.group is NOT a transaction — it does not roll back
        # applied HOM calls if a later call raises. We therefore journal the
        # original name and restore it on any failure so the sandbox is never
        # left half-promoted (renamed but not moved).
        original_name = container.name()
        moved = False
        finalize_warnings: list[str] = []
        promote_failure: BaseException | None = None
        try:
            with hou.undos.group(_UNDO_LABEL_PREFIX + "scratch.commit"):
                target_parent = hou.node(request.target_parent_path)
                if target_parent is None:
                    raise _scratch_failed(
                        f"The target parent does not exist: "
                        f"{request.target_parent_path}"
                    )
                # If a node already exists at the target path, refuse rather
                # than silently clobber it (the agent must rename or remove it).
                existing = hou.node(final_path)
                if existing is not None and existing.path() != container.path():
                    raise _scratch_failed(
                        f"A node already exists at the target path: {final_path}"
                    )
                container.setName(request.target_name)
                # setName keeps the node under its current parent (/obj);
                # move it under the target parent if that differs.
                if container.parent().path() != request.target_parent_path:
                    container.move(target_parent)
                    moved = True
                finalize_warnings = self._finalize_commit(
                    container, output_node, final_path, request.annotations,
                    request.parameters,
                )
        except HoudiniAdapterError:
            promote_failure = sys.exc_info()[1]  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001 — classify any HOM failure
            promote_failure = exc

        if promote_failure is not None:
            # hou.undos.group did NOT auto-rollback. Undo the partial promotion
            # manually so the sandbox is intact (not renamed-but-not-moved).
            # Promotion is two steps: setName then move. If move failed after a
            # successful setName, move back to /obj (the sandbox's original
            # parent) then restore the original name. Best-effort — if cleanup
            # itself fails, that secondary error is folded into the message.
            rollback_error: BaseException | None = None
            try:
                if moved:
                    obj_net = hou.node("/obj")
                    if obj_net is not None and container.parent().path() != "/obj":
                        container.move(obj_net)
                if container.name() != original_name:
                    container.setName(original_name)
            except Exception as rb_exc:  # noqa: BLE001 — best-effort restore
                rollback_error = rb_exc
            if rollback_error is None:
                if isinstance(promote_failure, HoudiniAdapterError):
                    raise promote_failure
                raise _scratch_failed(
                    f"scratch commit promotion failed: {promote_failure}"[:_MAX_ERROR_CHARS]
                ) from promote_failure
            raise _scratch_failed(
                f"scratch commit promotion failed and rollback also failed: "
                f"{promote_failure}; rollback: {rollback_error}"[:_MAX_ERROR_CHARS]
            ) from promote_failure

        # Houdini can recompute network flags when a container is renamed or
        # moved. Re-assert cosmetic state after the promotion group closes;
        # this remains best-effort and never changes the hard-gate verdict.
        # Resolve fresh HOM handles after rename/move; retained handles can be
        # stale for network-flag writes in Houdini even though their path reads.
        fresh_container = hou.node(final_path) or container
        output_name = output_node.name()
        fresh_output = hou.node(f"{final_path}/{output_name}") or output_node
        finalize_warnings.extend(
            self._finalize_commit(
                fresh_container, fresh_output, final_path, request.annotations,
                request.parameters,
            )
        )
        # Idempotent safety net: re-assert the flags on freshly resolved
        # handles after the container rename/move, so a stale pre-promotion
        # handle can never silently drop the write.
        try:
            with hou.undos.disabler():
                fresh_output.setDisplayFlag(True)
                if fresh_output.type().category().name() == "Sop":
                    fresh_output.setRenderFlag(True)
                # A second fresh lookup flushes Houdini's network-item cache
                # after a parent rename/move.
                refreshed_output = hou.node(f"{final_path}/{output_name}")
                if refreshed_output is not None:
                    refreshed_output.setDisplayFlag(True)
                    if refreshed_output.type().category().name() == "Sop":
                        refreshed_output.setRenderFlag(True)
            if not fresh_output.isDisplayFlagSet():  # type: ignore[attr-defined]
                finalize_warnings.append("display flag did not persist")
            elif fresh_output.type().category().name() == "Sop" and not fresh_output.isRenderFlagSet():  # type: ignore[attr-defined]
                finalize_warnings.append("render flag did not persist")
        except Exception as exc:  # noqa: BLE001
            finalize_warnings.append(f"display flag failed: {exc}"[:_MAX_ERROR_CHARS])
        receipt = _build_commit_receipt(gate_report, request.parameters)
        return ScratchCommitResult(
            committed=True,
            refused=False,
            final_path=final_path,
            reason="",
            gates=tuple(gate_report["gates"]),
            receipt=receipt,
            warnings=tuple(finalize_warnings),
        )

    def _scratch_output_node(self, container: object) -> object:
        """Return the output (terminal) node of the sandbox container.

        Prefers the unique sink — the child with no downstream connections.
        The sandbox display flag is only an accidental creation default
        (``scratch_exec`` never sets it), so it can point at a mid-chain
        node; committing the flag holder instead of the chain end would
        verify and publish the wrong geometry. Ambiguous networks (zero or
        multiple sinks) fall back to the display-flag holder, then the last
        child, then the container itself.
        """
        try:
            children = list(container.children())  # type: ignore[attr-defined]
        except Exception:
            return container
        if not children:
            return container
        sinks: list[object] = []
        for child in children:
            try:
                if not (child.outputs() or []):  # type: ignore[attr-defined]
                    sinks.append(child)
            except Exception:  # noqa: BLE001 — unreadable node is not a sink
                pass
        if len(sinks) == 1:
            return sinks[0]
        if sinks:
            # Multiple sinks: an assembled asset's output is downstream of
            # the parts (a merge/output null), while a stray disconnected
            # node (e.g. a leftover default box) has no inputs. Prefer the
            # sink with the most wired inputs so junk can never hijack the
            # commit output and display flag; ties keep children order.
            def _in_degree(node: object) -> int:
                try:
                    return sum(
                        1
                        for src in (node.inputs() or ())  # type: ignore[attr-defined]
                        if src is not None
                    )
                except Exception:  # noqa: BLE001
                    return 0

            connected = [sink for sink in sinks if _in_degree(sink) > 0]
            if connected:
                return max(connected, key=_in_degree)
        for child in children:
            if getattr(child, "isDisplayFlagSet", lambda: False)():
                return child
        return children[-1]

    def _finalize_commit(
        self,
        container: object,
        output_node: object,
        final_path: str,
        annotations: tuple[tuple[str, str], ...],
        parameters: tuple[object, ...] = (),
    ) -> list[str]:
        """Best-effort cosmetic finalization inside the commit undo group."""
        warnings: list[str] = []
        hou = self._hou
        # Prefer a fresh path lookup: a HOM handle captured before a network
        # rename/move can silently ignore flag writes after promotion.
        try:
            resolved = hou.node(f"{final_path}/{output_node.name()}")
            if resolved is not None:
                output_node = resolved
        except Exception:  # noqa: BLE001
            pass
        try:
            children = list(container.children())  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            children = []
            warnings.append(f"layout skipped: cannot list committed nodes: {exc}")
        if children:
            try:
                edges: dict[str, tuple[str, ...]] = {}
                xs: list[float] = []
                ys: list[float] = []
                def descendants(parent: object) -> list[object]:
                    found: list[object] = []
                    for child in parent.children() or ():  # type: ignore[attr-defined]
                        found.append(child)
                        found.extend(descendants(child))
                    return found
                all_children = descendants(container)
                for child in all_children:
                    upstream: list[str] = []
                    # node.inputs() is the reliable upstream accessor;
                    # inputConnections().outputNode() returns the node itself
                    # in 21.0.440 (see _read_wire_source).
                    for src in child.inputs() or ():
                        if src is not None:
                            upstream.append(src.path())
                    edges[child.path()] = tuple(upstream)
                    pos = child.position()
                    xs.append(float(pos[0]))
                    ys.append(float(pos[1]))
                positions = _layered_layout(
                    [child.path() for child in all_children],
                    edges,
                    anchor=(min(xs), max(ys)),
                )
                for child in all_children:
                    target = positions.get(child.path())
                    if target is not None:
                        child.setPosition(target)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"layout failed: {exc}")

        try:
            output_node.setDisplayFlag(True)  # type: ignore[attr-defined]
            try:
                category = output_node.type().category().name()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                category = ""
            if category == "Sop":
                output_node.setRenderFlag(True)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"display flag failed: {exc}")

        if annotations:
            try:
                root = container.path().rstrip("/")
                all_nodes: dict[str, object] = {}
                for child in (container.children() or ()):  # type: ignore[attr-defined]
                    stack = [child]
                    while stack:
                        current = stack.pop()
                        rel = current.path()[len(root) + 1:]
                        all_nodes[rel] = current
                        stack.extend(list(current.children() or ()))
                by_name = all_nodes
            except Exception:  # noqa: BLE001
                by_name = {}
            for name, comment in annotations:
                node = by_name.get(name)
                if node is None:
                    warnings.append(f"comment skipped: no committed node at {name}")
                    continue
                try:
                    node.setComment(comment)
                    node.setGenericFlag(hou.nodeFlag.DisplayComment, True)  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"comment failed on {name}: {exc}")
        if parameters:
            try:
                manifest_json = canonical_json_dumps(
                    [item.to_dict() for item in parameters]
                )
                container.setComment(manifest_json[:8192])
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"parameter manifest comment failed: {exc}")
        return [warning[:_MAX_ERROR_CHARS] for warning in warnings[:_MAX_ERRORS]]

    def _run_scratch_gates(
        self,
        container: object,
        output_node: object,
        orientation_checks: tuple[dict[str, object], ...],
        skip_structure_check: bool,
    ) -> dict:
        """Run the verify gates and return the orchestrator report.

        Imported lazily so the executor module stays importable without the
        verify module (and its orientation_math transitive import) at module
        load. The verify module itself imports no ``hou`` at module level.
        """
        from houdini_side.scratch_verify import (
            check_modular_structure,
            inspect_geometry_health,
            verify_orientation,
            verify_world_axes_baked,
        )

        # Cook the output node so the gates read fresh geometry. A cook failure
        # is a single root cause that would otherwise make every geometry-reading
        # gate report its own "could not read geometry"; surface it once as a
        # dedicated hard pre-gate failure instead.
        cook_error: str | None = None
        try:
            output_node.cook(force=True)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — cook failure -> pre-gate
            cook_error = str(exc)[:_MAX_ERROR_CHARS] or "cook failed"

        if cook_error is not None:
            cook_gate = {
                "gate": "cook", "passed": False, "hard": True,
                "reason": f"The sandbox output did not cook: {cook_error}",
                "detail": {"cook_error": cook_error},
            }
            return {
                "passed": False,
                "gates": [cook_gate],
                "hard_failures": [cook_gate],
            }

        bake = verify_world_axes_baked(
            output_node,
            require_component_ids=bool(orientation_checks),
        )
        if skip_structure_check:
            structure = {
                "gate": "structure", "passed": True, "hard": True,
                "reason": "", "detail": {"skipped": "skip_structure_check"},
            }
        else:
            structure = check_modular_structure(container)
        orientation = verify_orientation(
            output_node, [dict(c) for c in orientation_checks]
        )
        health = inspect_geometry_health(output_node)

        gates = [bake, structure, orientation, health]
        hard_failures = [g for g in gates if g["hard"] and not g["passed"]]
        return {
            "passed": len(hard_failures) == 0,
            "gates": gates,
            "hard_failures": hard_failures,
        }

    def _scratch_geometry(self, node: object) -> ScratchGeometry | None:
        """Read cooked geometry stats of a sandbox node (best-effort)."""
        try:
            geometry = node.geometry()  # type: ignore[attr-defined]
            points = int(geometry.pointCount())
            prims = int(geometry.primCount())
            vertices = int(geometry.intrinsicValue("vertexcount"))
            bbox = geometry.boundingBox()
            mn = bbox.minvec()
            mx = bbox.maxvec()
        except Exception:  # noqa: BLE001 — node has no cookable geometry
            return None
        return ScratchGeometry(
            point_count=points,
            prim_count=prims,
            vertex_count=vertices,
            bbox_min=(mn.x(), mn.y(), mn.z()),
            bbox_max=(mx.x(), mx.y(), mx.z()),
        )

    # --------------------------------------------------------------- writes

    def _execute_op(
        self,
        hou: object,
        op: object,
        index: dict[str, list[object]],
        inverses: list[tuple],
        applied_op_ids: list[str],
    ) -> None:
        """Execute one typed effect, mirroring ownership and journaling inverses.

        Each inverse is journaled as soon as its restoration facts/object exist
        and BEFORE the (next) mutating HOM call, so a mirror/mutate failure can
        never strand an applied effect without a rollback entry. ``index`` is
        mutated in place when a node is created so later ops can resolve it.
        """
        if isinstance(op, CreateNode):
            self._execute_create(hou, op, index, inverses, applied_op_ids)
        elif isinstance(op, SetParm):
            self._execute_set_parm(hou, op, index, inverses, applied_op_ids)
        elif isinstance(op, ConnectInput):
            self._execute_connect(hou, op, index, inverses, applied_op_ids)
        else:
            raise _apply_failed("Unsupported operation kind.")  # pragma: no cover - closed union

    def _execute_create(
        self,
        hou: object,
        op: CreateNode,
        index: dict[str, list[object]],
        inverses: list[tuple],
        applied_op_ids: list[str],
    ) -> None:
        parent = self._resolve_existing(hou, op.parent, index)
        if parent is None:
            raise _apply_failed("The create parent was not found in the scene.")
        # D1: if the parent is a created node, verify its full identity before use.
        if op.parent.node_id in self._created_identity:
            self._verify_created_identity(parent, op.parent)
        created = parent.createNode(op.node_type, op.node_name)  # type: ignore[attr-defined]
        derived_path = _derive_create_path(op.parent.path, op.node_name)
        # Journal the create-inverse immediately: the node now exists, so any
        # failure in the mirror steps (or a later op) must roll it back.
        inverses.append(
            ("create", created, derived_path, op.node_id, op.workspace_id,
             op.capability, op.role, self._current_run_id)
        )
        applied_op_ids.append(op.op_id)
        index.setdefault(op.node_id, []).append(created)
        # Mirror the six ownership keys (design 4.1); node_id first so the node
        # is identifiable even if a later mirror step fails. Each step may fail;
        # the create is already journaled for rollback.
        created.setUserData(_NODE_ID_KEY, op.node_id)  # type: ignore[attr-defined]
        created.setUserData(_WS_KEY, op.workspace_id)  # type: ignore[attr-defined]
        created.setUserData(_CAP_KEY, op.capability)  # type: ignore[attr-defined]
        created.setUserData(_ROLE_KEY, op.role)  # type: ignore[attr-defined]
        created.setUserData(_SCHEMA_KEY, _SUPPORTED_SCHEMA_VERSION)  # type: ignore[attr-defined]
        created.setUserData(_RUN_KEY, self._current_run_id)  # type: ignore[attr-defined]

    def _execute_set_parm(
        self,
        hou: object,
        op: SetParm,
        index: dict[str, list[object]],
        inverses: list[tuple],
        applied_op_ids: list[str],
    ) -> None:
        node = self._resolve_existing(hou, op.target, index)
        if node is None:
            raise _apply_failed("The parameter target was not found in the scene.")
        # D1: if target is a created node, verify its full identity before use.
        if op.target.node_id in self._created_identity:
            self._verify_created_identity(node, op.target)
        old_value, _existed = self._read_parm_value(node, op.parm_name)
        # D1 JIT: compare current value to expected_old_value immediately before write.
        if not _parm_equal(old_value, op.expected_old_value):
            raise _stale("The current parameter value does not match expected_old_value.")
        # Journal the inverse (captured before-state) BEFORE the mutating call,
        # so a mutate-then-raise still has an exact restoration entry.
        inverses.append(("parm", op.target, op.parm_name, old_value))
        self._write_parm_value(node, op.parm_name, op.value)
        applied_op_ids.append(op.op_id)

    def _execute_connect(
        self,
        hou: object,
        op: ConnectInput,
        index: dict[str, list[object]],
        inverses: list[tuple],
        applied_op_ids: list[str],
    ) -> None:
        target = self._resolve_existing(hou, op.target, index)
        source = self._resolve_existing(hou, op.source, index)
        if target is None:
            raise _apply_failed("The wire target was not found in the scene.")
        if source is None:
            raise _apply_failed("The wire source was not found in the scene.")
        # D1: verify created endpoint identities before use.
        if op.target.node_id in self._created_identity:
            self._verify_created_identity(target, op.target)
        if op.source.node_id in self._created_identity:
            self._verify_created_identity(source, op.source)
        old_source = self._read_wire_source(target, op.input_index)
        # D1 JIT: compare current source to expected_old_source immediately before write.
        if not _wire_equal(old_source, op.expected_old_source):
            raise _stale("The current wire source does not match expected_old_source.")
        # D1 JIT: if the expected_old_source is a created node, verify its actual
        # six-mirror identity too (path/type/workspace/node_id are checked by
        # _wire_equal, but capability/role/schema/run are not visible there).
        if (
            op.expected_old_source is not None
            and op.expected_old_source.source.node_id in self._created_identity
        ):
            actual_old_node = self._resolve_existing(hou, op.expected_old_source.source, index)
            if actual_old_node is None:
                raise _stale("The expected old created wire source was not found in the scene.")
            self._verify_created_identity(actual_old_node, op.expected_old_source.source)
        inverses.append(("wire", op.target, op.input_index, old_source))
        target.setInput(op.input_index, source, op.source_output_index)  # type: ignore[attr-defined]
        applied_op_ids.append(op.op_id)

    def _verify_created_identity(self, node: object, ref: NodeRef) -> None:
        """D1: verify a created node's current full identity (path, type, and all
        six mirrored ownership keys) before any transactional use.

        A mismatch or unreadable key raises ``changeset.stale`` (zero current
        write). If prior ops wrote, the accepted reverse rollback applies.
        """
        expected_cap, expected_role = self._created_identity.get(ref.node_id, ("", ""))
        expected: dict[str, str] = {
            _NODE_ID_KEY: ref.node_id,
            _WS_KEY: ref.expected_workspace_id if ref.expected_workspace_id else "",
            _CAP_KEY: expected_cap,
            _ROLE_KEY: expected_role,
            _SCHEMA_KEY: _SUPPORTED_SCHEMA_VERSION,
            _RUN_KEY: self._current_run_id,
        }
        for key, intended in expected.items():
            value, ok = self._read_user_data_strict(node, key)
            if not ok or value != intended:
                raise _stale(f"The created node identity mismatch at {key!r}.")
        try:
            if str(node.path()) != ref.path:  # type: ignore[attr-defined]
                raise _stale("The created node path has changed.")
            if str(node.type().name()) != ref.expected_type:  # type: ignore[attr-defined]
                raise _stale("The created node type has changed.")
        except HoudiniAdapterError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _stale("The created node identity could not be verified.") from exc

    # --------------------------------------------------------------- rollback

    def _safe_rollback(
        self, hou: object, inverses: list[tuple]
    ) -> tuple[tuple[ConditionResult, ...], bool]:
        """Run derived inverses in strict reverse order; never raise.

        Returns the per-inverse results and an ``ok`` flag that is False if the
        rollback pass itself could not complete (index rebuild or iteration
        crash). Individual inverse failures are captured as failing results, not
        raised.
        """
        try:
            index = self._index_scene_by_node_id(hou)
        except Exception:  # noqa: BLE001 — cannot even index -> uncertain
            return (), False
        results: list[ConditionResult] = []
        for inverse in reversed(inverses):
            try:
                results.append(self._rollback_one(hou, inverse, index))
            except Exception:  # noqa: BLE001 — a single inverse must not abort the rest
                results.append(
                    ConditionResult(kind=_inverse_condition_kind(inverse[0]), passed=False, detail=None)
                )
        return tuple(results), True

    def _rollback_one(
        self, hou: object, inverse: tuple, index: dict[str, list[object]]
    ) -> ConditionResult:
        kind = inverse[0]
        if kind == "create":
            return self._rollback_create(hou, inverse)
        if kind == "parm":
            _tag, ref, name, old_value = inverse  # type: ignore[misc]
            return self._rollback_parm(hou, ref, name, old_value, index)
        if kind == "wire":
            _tag, ref, idx, old_source = inverse  # type: ignore[misc]
            return self._rollback_wire(hou, ref, idx, old_source, index)
        return ConditionResult(kind="node.absent", passed=False, detail=None)

    def _rollback_create(self, hou: object, inverse: tuple) -> ConditionResult:
        """Destroy a node created by THIS transaction after full identity proof.

        The inverse carries the actual created node object and the exact mirrored
        identity this transaction intended. Rollback destroys it only if the
        object is still at the derived path and every mirrored key can be READ
        (a read exception refuses destroy — uncertain recovery) and is either
        unset (a mirror step failed mid-way) or matches the intended value.
        """
        _tag, created, path, node_id, ws_id, cap, role, run_id = inverse  # type: ignore[misc]
        alive_path = self._safe_path(created)
        if alive_path is None:
            # Already gone: the rollback goal (absence) is met.
            return ConditionResult(kind="node.absent", passed=self._node_gone(hou, path), detail=None)
        if alive_path != path:
            return ConditionResult(kind="node.absent", passed=False, detail=None)
        intended = {
            _NODE_ID_KEY: node_id,
            _WS_KEY: ws_id,
            _CAP_KEY: cap,
            _ROLE_KEY: role,
            _SCHEMA_KEY: _SUPPORTED_SCHEMA_VERSION,
            _RUN_KEY: run_id,
        }
        # F10: read each mirrored key STRICTLY. A read that raises (not merely
        # returns None) must refuse destroy → uncertain recovery + freeze. A key
        # that is unset (None) is allowed (mirror step may have failed mid-way).
        verified = True
        for key, intended_value in intended.items():
            value, ok = self._read_user_data_strict(created, key)
            if not ok:
                verified = False
                break
            if value is not None and value != intended_value:
                verified = False
                break
        if verified:
            try:
                created.destroy()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — destroy failure => not restored
                verified = False
        return ConditionResult(
            kind="node.absent", passed=bool(verified and self._node_gone(hou, path)), detail=None
        )

    def _rollback_parm(
        self,
        hou: object,
        ref: NodeRef,
        name: str,
        old_value: object,
        index: dict[str, list[object]],
    ) -> ConditionResult:
        node = self._resolve_existing(hou, ref, index)
        if node is None:
            return ConditionResult(kind="parm.value_equals", passed=False, detail=None)
        try:
            self._write_parm_value(node, name, old_value)
        except Exception:  # noqa: BLE001
            return ConditionResult(kind="parm.value_equals", passed=False, detail=None)
        current, _existed = self._read_parm_value(node, name)
        return ConditionResult(
            kind="parm.value_equals", passed=_parm_equal(current, old_value), detail=None
        )

    def _rollback_wire(
        self,
        hou: object,
        ref: NodeRef,
        idx: int,
        old_source: WireRef | None,
        index: dict[str, list[object]],
    ) -> ConditionResult:
        node = self._resolve_existing(hou, ref, index)
        if node is None:
            return ConditionResult(kind="wire.input_equals", passed=False, detail=None)
        try:
            if old_source is None:
                node.setInput(idx, None)  # type: ignore[attr-defined]
            else:
                src = self._resolve_existing(hou, old_source.source, index)
                if src is None:
                    return ConditionResult(kind="wire.input_equals", passed=False, detail=None)
                node.setInput(idx, src, old_source.source_output_index)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return ConditionResult(kind="wire.input_equals", passed=False, detail=None)
        current = self._read_wire_source(node, idx)
        return ConditionResult(
            kind="wire.input_equals", passed=_wire_equal(current, old_source), detail=None
        )

    def _classify_failure(
        self, rollback_results: tuple[ConditionResult, ...], rollback_ok: bool
    ) -> tuple[ReceiptStatus, bool]:
        """Classify a failed transaction truthfully (F4).

        ``rollback_ok False`` (rollback itself crashed) is always uncertain ->
        ``CriticalRecovery``. Otherwise the per-inverse results decide: a full
        clean restore is ``RolledBack`` (scene restored); a mixed restore is
        ``Partial``; a total restore failure is ``CriticalRecovery``.
        """
        if not rollback_ok:
            return ReceiptStatus.CRITICAL_RECOVERY, True
        if not rollback_results:
            # Nothing was applied before the failure -> nothing to undo -> clean.
            return ReceiptStatus.ROLLED_BACK, False
        passed = sum(1 for r in rollback_results if r.passed)
        if passed == len(rollback_results):
            return ReceiptStatus.ROLLED_BACK, False
        if passed == 0:
            return ReceiptStatus.CRITICAL_RECOVERY, True
        return ReceiptStatus.PARTIAL, True

    def _classify_apply_failure(
        self,
        *,
        write_error: BaseException | None,
        reconciliation_error: BaseException | None,
        post_results: tuple[ConditionResult, ...],
        reconciled: bool,
        applied_op_ids: list[str],
        operations: tuple,
    ) -> tuple[str | None, str | None]:
        """Translate a failed transaction into a bounded (code, message) pair.

        The classification is intentionally lossy: callers (the LLM, the UI,
        the durable event log) dispatch on the dotted ``code`` vocabulary, not
        on prose. ``message`` carries a bounded human-readable cause so a user
        can read what actually broke instead of seeing the old "applied_op_ids
        is empty, scene unchanged, no further info" dead-end.

        Order matters: an explicit write exception beats a reconciliation
        crash beats a postcondition mismatch beats the generic fallback.
        """
        # 1) Write exception (an op raised before all operations completed).
        if write_error is not None:
            code, message = self._exception_to_code(write_error)
            # Tag the failed op index so the user can see where execution died.
            # The failing op is at index len(applied_op_ids): when no op has
            # been applied yet this is 0 (the first op raised), and so on.
            if operations:
                failed_index = len(applied_op_ids)
                if 0 <= failed_index < len(operations):
                    op = operations[failed_index]
                    op_label = self._describe_op_for_error(op)
                    if op_label:
                        message = f"{message} (failed at op #{failed_index + 1}: {op_label})"
            return code, self._bounded_exception_message_str(message)
        # 2) Reconciliation crash (post-snapshot / postcondition eval raised).
        if reconciliation_error is not None:
            code, message = self._exception_to_code(reconciliation_error)
            return code, self._bounded_exception_message_str(
                f"post-apply reconciliation failed: {message}"
            )
        # 3) Postconditions evaluated but did not all pass.
        if not reconciled:
            failed = [
                r for r in post_results if not r.passed
            ]
            if failed:
                kinds = ", ".join(sorted({r.kind for r in failed}))
                return (
                    "apply.postcondition_failed",
                    self._bounded_exception_message_str(
                        f"{len(failed)} postcondition(s) failed: {kinds}"
                    ),
                )
        # 4) Nothing else to say; leave the receipt descriptive-only so
        # consumers still see RolledBack without a fabricated cause.
        return None, None

    def _exception_to_code(
        self, exc: BaseException
    ) -> tuple[str, str]:
        """Map a raised exception to a bounded (code, message) pair.

        ``hou.OperationFailed`` is the dominant case for "createNode / setParm
        / connect rejected by Houdini" (bad node type name, wrong context,
        parm name not on the node, etc.). ``HoudiniAdapterError`` (imported
        at the top of this module from ``houdini_side.secure_bridge``) is the
        typed failure path the bridge raises for stale/frozen scenes and
        carries its own dotted ``code``.
        """
        message = self._bounded_exception_message(exc)
        # HoudiniAdapterError is already imported at the top of this module.
        if isinstance(exc, HoudiniAdapterError):
            code = getattr(exc, "code", None)
            # message_for_user is the bounded, leak-free cause string.
            cause = getattr(exc, "message_for_user", None)
            if type(cause) is str and cause.strip():
                message = self._bounded_exception_message_str(cause)
            if type(code) is str and code:
                return (code, message)
            return ("houdini.adapter_error", message)
        # hou is only available inside Houdini; guard the import.
        try:
            import hou  # type: ignore
            if isinstance(exc, hou.OperationFailed):  # type: ignore[attr-defined]
                return ("houdini.operation_failed", message)
        except Exception:  # noqa: BLE001 — hou absent in the test venv
            pass
        # Generic exception: prefix the class name so the message stays
        # self-describing, then re-bound (the prefix can push past the limit).
        cls = type(exc).__name__
        return (
            "apply.unexpected_error",
            self._bounded_exception_message_str(f"{cls}: {message}"),
        )

    @staticmethod
    def _bounded_exception_message(exc: BaseException) -> str:
        """Best-effort one-line message from an exception, bounded."""
        text = str(exc).strip()
        if not text:
            text = type(exc).__name__
        return ChangeSetExecutor._bounded_exception_message_str(text)

    @staticmethod
    def _bounded_exception_message_str(text: str) -> str:
        """Bound and collapse whitespace in a pre-extracted message string."""
        # Collapse newlines so the (code, message) contract stays one record.
        text = " ".join(text.split())
        if len(text) > 500:
            text = text[:497] + "..."
        return text

    @staticmethod
    def _describe_op_for_error(op: object) -> str:
        """Short human label for a failed operation (node type + name).

        Only the most common CreateNode/SetParm/ConnectInput shapes are
        described; anything else returns an empty string so the caller skips
        the op-tag suffix instead of inventing a label.
        """
        node_type = getattr(op, "node_type", None)
        node_name = getattr(op, "node_name", None)
        if type(node_type) is str and type(node_name) is str:
            return f"{node_type} '{node_name}'"
        parm_name = getattr(op, "parm_name", None)
        if type(parm_name) is str:
            target = getattr(op, "node_id", None) or getattr(op, "node_name", None)
            target_text = f" on {target}" if target else ""
            return f"set parm '{parm_name}'{target_text}"
        input_index = getattr(op, "input_index", None)
        if type(input_index) is int:
            return f"connect input {input_index}"
        return ""

    def _safe_after_revision(
        self, hou: object, changeset: ChangeSet, before_revision: str
    ) -> str:
        """Best-effort post-failure revision; falls back to before on read error."""
        try:
            post_index = self._index_scene_by_node_id(hou)
            return self._revision(self._snapshot(hou, changeset, post_index))
        except Exception:  # noqa: BLE001 — never let revision computation escape
            return before_revision

    @staticmethod
    def _safe_path(node: object) -> str | None:
        try:
            return str(node.path())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — deleted/inaccessible node
            return None

    @staticmethod
    def _read_user_data_strict(node: object, key: str) -> tuple[str | None, bool]:
        """Read a mirrored key with an explicit success flag (F10).

        Returns ``(value, ok)``. ``ok`` is False on ANY read exception (the
        identity could not be proven → destroy must be refused). A successful
        read returning None means the key is genuinely unset (allowed).
        """
        try:
            value = node.userData(key)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None, False
        if value is None:
            return None, True
        return str(value), True

    @staticmethod
    def _node_gone(hou: object, path: str) -> bool:
        try:
            return hou.node(path) is None  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return False

    # --------------------------------------------------------------- postconditions

    def _evaluate_postconditions(
        self, changeset, after: _Snapshot, binding  # type: ignore[no-untyped-def]
    ) -> tuple[ConditionResult, ...]:
        results: list[ConditionResult] = []
        for cond in changeset.expected_postconditions:
            kind = cond.kind
            if isinstance(cond, NodeIdentityEquals):
                fact = after.nodes.get(_identity(cond.node))
                passed = fact is not None and bool(fact.get("exists")) and self._node_identity_holds(fact, cond.node)
            elif isinstance(cond, ParmValueEquals):
                slot = after.parms.get((_identity(cond.target), cond.parm_name))
                passed = slot is not None and bool(slot.get("exists")) and _parm_equal(slot.get("value"), cond.value)
            elif isinstance(cond, WireInputEquals):
                current = after.wires.get((_identity(cond.target), cond.input_index))
                passed = _wire_equal(current, cond.source)
            else:  # pragma: no cover - postcondition union is closed
                raise _apply_failed(f"Unsupported postcondition kind: {kind!r}")
            results.append(ConditionResult(kind=kind, passed=passed, detail=None))
        return tuple(results)

    @staticmethod
    def _node_identity_holds(fact: dict[str, object], ref: NodeRef) -> bool:
        if not bool(fact.get("exists")):
            return False
        if fact.get("path") != ref.path:
            return False
        if fact.get("type") != ref.expected_type:
            return False
        if ref.node_id is not None and fact.get("node_id") != ref.node_id:
            return False
        if ref.expected_workspace_id is not None and (
            fact.get("workspace_id") is None or fact.get("workspace_id") != ref.expected_workspace_id
        ):
            return False
        return True

    # --------------------------------------------------------------- snapshot

    def _snapshot(
        self, hou: object, changeset, index: dict[str, list[object]]
    ) -> _Snapshot:
        snap = _Snapshot()
        for ref in self._preflight._collect_node_refs(changeset):
            node = self._resolve_existing(hou, ref, index)
            snap.nodes[_identity(ref)] = self._node_fact_dict(node)
        for ref, name in self._parm_targets(changeset):
            node = self._resolve_existing(hou, ref, index)
            value, existed = self._read_parm_value(node, name) if node is not None else (None, False)
            snap.parms[(_identity(ref), name)] = {
                "exists": existed,
                "value": _parm_value_json(value) if value is not None else None,
            }
        for ref, idx in self._wire_targets(changeset):
            node = self._resolve_existing(hou, ref, index)
            source = self._read_wire_source(node, idx) if node is not None else None
            snap.wires[(_identity(ref), idx)] = source
        return snap

    def _parm_targets(self, changeset) -> list[tuple[NodeRef, str]]:  # type: ignore[no-untyped-def]
        targets: list[tuple[NodeRef, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(target: NodeRef, name: str) -> None:
            key = (_identity(target), name)
            if key not in seen:
                seen.add(key)
                targets.append((target, name))

        for op in changeset.operations:
            if isinstance(op, SetParm):
                add(op.target, op.parm_name)
        for cond in changeset.expected_postconditions:
            if isinstance(cond, ParmValueEquals):
                add(cond.target, cond.parm_name)
        return targets

    def _wire_targets(self, changeset) -> list[tuple[NodeRef, int]]:  # type: ignore[no-untyped-def]
        targets: list[tuple[NodeRef, int]] = []
        seen: set[tuple[str, int]] = set()

        def add(target: NodeRef, index: int) -> None:
            key = (_identity(target), index)
            if key not in seen:
                seen.add(key)
                targets.append((target, index))

        for op in changeset.operations:
            if isinstance(op, ConnectInput):
                add(op.target, op.input_index)
        for cond in changeset.expected_postconditions:
            if isinstance(cond, WireInputEquals):
                add(cond.target, cond.input_index)
        return targets

    @staticmethod
    def _node_fact_dict(node: object | None) -> dict[str, object]:
        if node is None:
            return {
                "exists": False,
                "path": None,
                "type": None,
                "parent": None,
                "node_id": None,
                "workspace_id": None,
            }
        parent = node.parent()  # type: ignore[attr-defined]
        return {
            "exists": True,
            "path": str(node.path()),  # type: ignore[attr-defined]
            "type": str(node.type().name()),  # type: ignore[attr-defined]
            "parent": str(parent.path()) if parent is not None else "/",  # type: ignore[attr-defined]
            "node_id": _read_user_data(node, _NODE_ID_KEY),
            "workspace_id": _read_user_data(node, _WS_KEY),
        }

    def _revision(self, snap: _Snapshot) -> str:
        payload = {
            "nodes": [[k, v] for k, v in sorted(snap.nodes.items())],
            "parms": [[list(k), v] for k, v in sorted(snap.parms.items())],
            "wires": [
                [list(k), (v.to_dict() if v is not None else None)]
                for k, v in sorted(snap.wires.items())
            ],
        }
        return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()

    # --------------------------------------------------------------- resolution + reads

    def _index_scene_by_node_id(self, hou: object) -> dict[str, list[object]]:
        root = hou.node("/")  # type: ignore[union-attr]
        nodes = () if root is None else tuple(root.allSubChildren())  # type: ignore[attr-defined]
        index: dict[str, list[object]] = {}
        for node in nodes:
            node_id = _read_user_data(node, _NODE_ID_KEY)
            if node_id is None:
                continue
            index.setdefault(node_id, []).append(node)
        return index

    def _resolve_existing(
        self, hou: object, ref: NodeRef, index: dict[str, list[object]]
    ) -> object | None:
        """Resolve a reference to its current HOM node (stable id first)."""
        if ref.node_id is None:
            return hou.node(ref.path)  # type: ignore[union-attr]
        mirrors = index.get(ref.node_id, ())
        if len(mirrors) > 1:
            raise _ambiguous()
        if mirrors:
            return mirrors[-1]
        return hou.node(ref.path)  # type: ignore[union-attr]

    def _read_parm_value(self, node: object, name: str) -> tuple[object | None, bool]:
        parm = node.parm(name)  # type: ignore[attr-defined]
        if parm is not None:
            return self._bounded_parm(parm.eval()), True  # type: ignore[attr-defined]
        pt = node.parmTuple(name)  # type: ignore[attr-defined]
        if pt is not None and pt.size() > 1:  # type: ignore[attr-defined]
            return self._bounded_parm(tuple(pt.eval())), True  # type: ignore[attr-defined]
        return None, False

    def _write_parm_value(self, node: object, name: str, value: object) -> None:
        if isinstance(value, tuple):
            pt = node.parmTuple(name)  # type: ignore[attr-defined]
            if pt is None:
                raise _apply_failed("The parameter tuple was not found on the node.")
            pt.set(tuple(value))  # type: ignore[attr-defined]
            return
        parm = node.parm(name)  # type: ignore[attr-defined]
        if parm is None:
            raise _apply_failed("The parameter was not found on the node.")
        parm.set(value)  # type: ignore[attr-defined]

    @staticmethod
    def _bounded_parm(raw: object) -> object:
        try:
            return _validate_parm_value(raw, "parm value")
        except (TypeError, ValueError) as exc:
            raise _apply_failed("A parameter has an unsupported or unbounded value.") from exc

    def _read_wire_source(self, node: object, index: int) -> WireRef | None:
        """Read the typed source wired into ``node`` input ``index``.

        The source node is read via ``node.inputs()[index]``, which is reliable
        across Houdini node kinds; ``inputConnections().outputNode()`` is not
        (it can return the node itself for some node kinds in 21.0.440). The
        source output index is read from ``inputConnections()``.
        """
        try:
            all_inputs = node.inputs()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            all_inputs = ()
        src_node: object | None = None
        if isinstance(all_inputs, (tuple, list)) and 0 <= index < len(all_inputs):
            src_node = all_inputs[index]
        if src_node is None:
            return None
        output_index = 0
        try:
            for conn in node.inputConnections():  # type: ignore[attr-defined]
                if int(conn.inputIndex()) == index:  # type: ignore[attr-defined]
                    output_index = int(conn.outputIndex())  # type: ignore[attr-defined]
                    break
        except Exception:  # noqa: BLE001
            pass
        source = NodeRef(
            node_id=_read_user_data(src_node, _NODE_ID_KEY),
            path=str(src_node.path()),  # type: ignore[attr-defined]
            expected_type=str(src_node.type().name()),  # type: ignore[attr-defined]
            expected_workspace_id=_read_user_data(src_node, _WS_KEY),
        )
        return WireRef(source=source, source_output_index=output_index)
