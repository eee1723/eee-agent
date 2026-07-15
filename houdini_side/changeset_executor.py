"""Read-only ChangeSet preflight adapter for the secure HoudiniBridge (Task 16-C).

:class:`ChangeSetPreflightAdapter` derives bounded, typed scene facts needed to
independently verify a :class:`~eee_agent.houdini_bridge.changesets.PreflightRequest`,
and evaluates the supplied preconditions against those facts. It performs **no
mutation**: it never creates/destroys nodes, sets parms, connects inputs, writes
user data, touches undo, loads/saves/clears the HIP, installs HDAs or source,
runs a shell, or evaluates arbitrary Python/VEX.

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

from eee_agent.changesets.contracts import (
    ConditionResult,
    ConnectInput,
    CreateNode,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    ParmValueEquals,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WireRef,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
    _derive_create_path,
    _parm_value_json,
    _validate_parm_value,
)
from eee_agent.houdini_bridge.changesets import (
    PreflightNodeFact,
    PreflightParmFact,
    PreflightRequest,
    PreflightResult,
    PreflightWireFact,
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

        def add(target: NodeRef, name: str) -> None:
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

        def add(target: NodeRef, index: int) -> None:
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
        try:
            connections = node.inputConnections()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — a node with no connections reports nothing
            return None
        for conn in connections:
            if int(conn.inputIndex()) != index:
                continue
            src_node = conn.outputNode()  # type: ignore[union-attr]
            source = NodeRef(
                node_id=_read_user_data(src_node, _NODE_ID_KEY),
                path=str(src_node.path()),
                expected_type=str(src_node.type().name()),
                expected_workspace_id=_read_user_data(src_node, _WS_KEY),
            )
            return WireRef(source=source, source_output_index=int(conn.outputIndex()))  # type: ignore[union-attr]
        return None

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
    if expected is None:
        return actual is None
    if actual is None:
        return False
    return (
        actual.source.path == expected.source.path
        and actual.source.expected_type == expected.source.expected_type
        and actual.source_output_index == expected.source_output_index
    )
