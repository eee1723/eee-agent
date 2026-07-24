"""Additive ``scratch.v1`` Bridge capability and typed scratch DTOs.

This module adds a sandbox-style incremental-modeling surface on top of the
accepted :mod:`eee_agent.houdini_bridge.changesets` typed operation pattern.
It defines:

* the advertised :data:`SCRATCH_V1` capability;
* frozen, slotted, JSON-canonical DTOs for the ``scratch.exec`` request and
  its bounded diagnostics response; and
* strict parsers (:func:`parse_scratch_request` / :func:`parse_scratch_response`).

It imports **neither** ``hou`` **nor** ``rpyc``. The operation is an internal
trusted Runtime-to-Bridge operation exactly like ``capture.capture``: it runs
only on the single main-thread FIFO, the Houdini side builds nodes inside a
reserved sandbox container (``/obj/eee_scratch_<id>``) and returns only
bounded diagnostics (node paths, cook errors, geometry stats). Sandbox nodes
are NOT stamped with ownership mirrors (``eee.node_id`` / ``eee.workspace_id``),
so they never pollute a real workspace's node-id index. The sandbox is
preserved across calls so the agent can iterate (build → observe → adjust);
the agent or the runtime tears it down explicitly when done.

Phase 1 supports **structured single-op mode**: a bounded list of typed
operations (create_node / set_parm / connect) against the sandbox container.
Network/raw-Python ``exec`` mode is deferred to a later phase (it requires an
async job protocol to avoid the 30s deadline ceiling).

Strictness mirrors the rest of the bridge package: exact primitive types,
exact field sets, deep-frozen canonical JSON, duplicate-key rejection, finite
numbers, and an explicit size limit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from eee_agent.houdini_bridge.changesets import (
    _MAX_DEADLINE_MS,
    _MAX_RESULT_BYTES,
    _MIN_DEADLINE_MS,
    _REQUEST_FIELDS,
    _RESPONSE_REQUIRED_FIELDS,
    _load_strict_dict,
    _require_exact_bool,
    _require_exact_dict,
    _require_exact_int,
    _require_exact_keys,
    _require_request_id,
)
from eee_agent.houdini_bridge.contracts import (
    PROTOCOL,
    BridgeError,
)
from eee_agent.runtime.models import canonical_json_dumps

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

SCRATCH_V1 = "scratch.v1"
SCRATCH_EXEC_OPERATION = "scratch.exec"
SCRATCH_COMMIT_OPERATION = "scratch.commit"
SCRATCH_DESTROY_OPERATION = "scratch.destroy"

# Bounded operation counts: a single scratch.exec call may carry a small
# batch of ops (create a few nodes + set their parms + wire them), but never
# an unbounded graph. The agent iterates by calling scratch.exec repeatedly.
_MAX_OPS = 64
_MAX_NODE_NAME_LEN = 64
_MAX_NODE_TYPE_LEN = 64
_MAX_PARM_NAME_LEN = 64
_MAX_OP_ARGUMENT_CHARS = 4000
_MAX_SANDBOX_NAME_LEN = 128
_MAX_ERRORS = 32
_MAX_ERROR_CHARS = 1000

# exact field sets for envelope + payload validation
_PAYLOAD_FIELDS = frozenset(
    {"sandbox_id", "operations", "preserve_on_failure"}
)
_OP_FIELDS = frozenset({"kind", "node_name", "node_type", "parent", "parm", "value", "input_index", "source", "source_output_index"})
_RESULT_FIELDS = frozenset(
    {"sandbox_root", "applied_ops", "output_node", "errors", "geometry"}
)
_GEOMETRY_FIELDS = frozenset({"point_count", "prim_count", "vertex_count", "bbox_min", "bbox_max"})

_OP_KINDS = frozenset({"create_node", "set_parm", "connect"})
_NODE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NODE_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:]*$")
_PARM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


# --------------------------------------------------------------------------
# shared strictness helpers
# --------------------------------------------------------------------------


def _require_sandbox_id(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > _MAX_SANDBOX_NAME_LEN:
        raise ValueError(f"{label} must be a non-empty string (<=128 chars)")
    if _SANDBOX_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be alphanumeric/underscore/hyphen")


def _require_node_name(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if _NODE_NAME_RE.fullmatch(value) is None or len(value) > _MAX_NODE_NAME_LEN:
        raise ValueError(f"{label} must be a bounded Houdini node name")


def _require_node_type(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if _NODE_TYPE_RE.fullmatch(value) is None or len(value) > _MAX_NODE_TYPE_LEN:
        raise ValueError(f"{label} must be a bounded node type")


def _require_parm_name(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if _PARM_NAME_RE.fullmatch(value) is None or len(value) > _MAX_PARM_NAME_LEN:
        raise ValueError(f"{label} must be a bounded parm name")


def _require_op_value(value: object, label: str) -> None:
    """A parm value is a JSON scalar or a homogeneous list of scalars."""
    if value is None or type(value) in (str, int, float, bool):
        if type(value) is str and len(value) > _MAX_OP_ARGUMENT_CHARS:
            raise ValueError(f"{label} string exceeds the maximum length")
        return
    if type(value) is list:
        if len(value) > 64:
            raise ValueError(f"{label} list exceeds 64 elements")
        for item in value:
            if item is None or type(item) in (str, int, float, bool):
                if type(item) is str and len(item) > _MAX_OP_ARGUMENT_CHARS:
                    raise ValueError(f"{label} list item exceeds the maximum length")
                continue
            raise TypeError(f"{label} list items must be JSON scalars")
        return
    raise TypeError(f"{label} must be a JSON scalar or list of scalars")


def _require_node_ref(value: object, label: str) -> None:
    """A node reference inside the sandbox: a relative name or /obj path."""
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if len(value) > _MAX_NODE_NAME_LEN * 4:
        raise ValueError(f"{label} exceeds the maximum length")


def _require_error_text(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if len(value) > _MAX_ERROR_CHARS:
        raise ValueError(f"{label} exceeds the maximum length")


# --------------------------------------------------------------------------
# operation DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchOp:
    """One structured sandbox operation.

    kinds:
      - create_node: create ``node_type`` named ``node_name`` under ``parent``
        (a sandbox-relative ref or the container root).
      - set_parm: set ``parm`` on ``node_name`` to ``value``.
      - connect: wire input ``input_index`` of ``node_name`` from ``source``
        output ``source_output_index``.
    """

    kind: str
    node_name: str
    node_type: str = ""
    parent: str = ""
    parm: str = ""
    value: object = None
    input_index: int = 0
    source: str = ""
    source_output_index: int = 0

    def __post_init__(self) -> None:
        if self.kind not in _OP_KINDS:
            raise ValueError("ScratchOp.kind must be create_node|set_parm|connect")
        _require_node_name(self.node_name, "ScratchOp.node_name")
        if self.kind == "create_node":
            _require_node_type(self.node_type, "ScratchOp.node_type")
            # parent may be "" (= sandbox root) or a sandbox-relative ref
            if self.parent:
                _require_node_ref(self.parent, "ScratchOp.parent")
        elif self.kind == "set_parm":
            _require_parm_name(self.parm, "ScratchOp.parm")
            _require_op_value(self.value, "ScratchOp.value")
        elif self.kind == "connect":
            _require_exact_int(self.input_index, "ScratchOp.input_index")
            if self.input_index < 0:
                raise ValueError("ScratchOp.input_index must be >= 0")
            _require_node_ref(self.source, "ScratchOp.source")
            _require_exact_int(self.source_output_index, "ScratchOp.source_output_index")
            if self.source_output_index < 0:
                raise ValueError("ScratchOp.source_output_index must be >= 0")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchOp":
        d = _require_exact_dict(data, "ScratchOp")
        # kind + node_name are always required; the rest are kind-specific and
        # optional on the wire (to_dict omits irrelevant fields per kind).
        if "kind" not in d or "node_name" not in d:
            raise ValueError("ScratchOp must have kind and node_name")
        # Reject any unknown field not in the op vocabulary.
        unknown = set(d) - _OP_FIELDS
        if unknown:
            raise ValueError(f"ScratchOp has unknown fields: {sorted(unknown)}")
        return cls(
            kind=d["kind"],  # type: ignore[arg-type]
            node_name=d["node_name"],  # type: ignore[arg-type]
            node_type=d.get("node_type", ""),  # type: ignore[arg-type]
            parent=d.get("parent", ""),  # type: ignore[arg-type]
            parm=d.get("parm", ""),  # type: ignore[arg-type]
            value=d.get("value", None),
            input_index=d.get("input_index", 0),  # type: ignore[arg-type]
            source=d.get("source", ""),  # type: ignore[arg-type]
            source_output_index=d.get("source_output_index", 0),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"kind": self.kind, "node_name": self.node_name}
        if self.kind == "create_node":
            d["node_type"] = self.node_type
            if self.parent:
                d["parent"] = self.parent
        elif self.kind == "set_parm":
            d["parm"] = self.parm
            d["value"] = self.value
        elif self.kind == "connect":
            d["input_index"] = self.input_index
            d["source"] = self.source
            d["source_output_index"] = self.source_output_index
        return d


# --------------------------------------------------------------------------
# geometry diagnostics DTO
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchGeometry:
    """Cooked geometry stats of the sandbox output node (bounded)."""

    point_count: int
    prim_count: int
    vertex_count: int
    bbox_min: tuple[float, ...]
    bbox_max: tuple[float, ...]

    def __post_init__(self) -> None:
        for field in ("point_count", "prim_count", "vertex_count"):
            v = getattr(self, field)
            _require_exact_int(v, f"ScratchGeometry.{field}")
            if v < 0:
                raise ValueError(f"ScratchGeometry.{field} must be >= 0")
        for label, vec in (("bbox_min", self.bbox_min), ("bbox_max", self.bbox_max)):
            if not isinstance(vec, Sequence) or isinstance(vec, str):
                raise TypeError(f"ScratchGeometry.{label} must be a sequence")
            if len(vec) != 3:
                raise ValueError(f"ScratchGeometry.{label} must have 3 elements")
            for comp in vec:
                if type(comp) not in (int, float) or type(comp) is bool:
                    raise TypeError(f"ScratchGeometry.{label} components must be numbers")
            object.__setattr__(self, label, tuple(float(c) for c in vec))

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchGeometry":
        d = _require_exact_dict(data, "ScratchGeometry")
        _require_exact_keys(d, _GEOMETRY_FIELDS, "ScratchGeometry")
        return cls(
            point_count=d["point_count"],  # type: ignore[arg-type]
            prim_count=d["prim_count"],  # type: ignore[arg-type]
            vertex_count=d["vertex_count"],  # type: ignore[arg-type]
            bbox_min=d["bbox_min"],  # type: ignore[arg-type]
            bbox_max=d["bbox_max"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "point_count": self.point_count,
            "prim_count": self.prim_count,
            "vertex_count": self.vertex_count,
            "bbox_min": list(self.bbox_min),
            "bbox_max": list(self.bbox_max),
        }


# --------------------------------------------------------------------------
# request DTO
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchRequest:
    """A parsed, validated ``scratch.exec`` request envelope.

    Carries the sandbox id (names the reserved ``/obj/eee_scratch_<id>``
    container), a bounded list of structured operations, and a flag for
    whether to preserve the sandbox on failure (default True so the agent can
    inspect and retry). The envelope ``scene_epoch`` is re-checked before any
    node is created; a mismatch fails closed with zero scene changes.
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    sandbox_id: str
    operations: tuple[ScratchOp, ...]
    preserve_on_failure: bool = True

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchRequest.scene_epoch must be >= 1")
        _require_sandbox_id(self.sandbox_id, "ScratchRequest.sandbox_id")
        ops = tuple(self.operations)
        if not ops or len(ops) > _MAX_OPS:
            raise ValueError("ScratchRequest.operations must contain 1..64 ops")
        for op in ops:
            if type(op) is not ScratchOp:
                raise TypeError("ScratchRequest.operations must be ScratchOp instances")
        object.__setattr__(self, "operations", ops)
        _require_exact_bool(self.preserve_on_failure, "ScratchRequest.preserve_on_failure")

    @property
    def container_name(self) -> str:
        """The exact Houdini geo container node name for this sandbox."""
        return f"eee_scratch_{self.sandbox_id}"

    @property
    def container_path(self) -> str:
        return f"/obj/{self.container_name}"

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        sandbox_id: str,
        operations: Sequence[ScratchOp],
        preserve_on_failure: bool = True,
    ) -> "ScratchRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            sandbox_id=sandbox_id,
            operations=tuple(operations),
            preserve_on_failure=preserve_on_failure,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_EXEC_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "sandbox_id": self.sandbox_id,
                "operations": [op.to_dict() for op in self.operations],
                "preserve_on_failure": self.preserve_on_failure,
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchRequest":
        envelope = _require_exact_dict(data, "ScratchRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchRequest kind must be request")
        if envelope["operation"] != SCRATCH_EXEC_OPERATION:
            raise ValueError("ScratchRequest operation must be scratch.exec")
        payload = _require_exact_dict(envelope["payload"], "ScratchRequest payload")
        _require_exact_keys(payload, _PAYLOAD_FIELDS, "ScratchRequest payload")
        ops_raw = payload["operations"]
        if type(ops_raw) is not list:
            raise TypeError("ScratchRequest payload operations must be a list")
        ops = tuple(ScratchOp.from_dict(op) for op in ops_raw)
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            sandbox_id=payload["sandbox_id"],  # type: ignore[arg-type]
            operations=ops,
            preserve_on_failure=payload["preserve_on_failure"],  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# result + response DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchResult:
    """The bounded diagnostics of one completed scratch.exec call.

    Returns the sandbox container path, the number of ops applied, the output
    node path (the last created node, or the container if none), any cook
    errors, and the cooked geometry stats of the output node. Never carries a
    Houdini object — only bounded, JSON-serializable evidence.
    """

    sandbox_root: str
    applied_ops: int
    output_node: str
    errors: tuple[str, ...]
    geometry: ScratchGeometry | None

    def __post_init__(self) -> None:
        if type(self.sandbox_root) is not str or not self.sandbox_root.startswith("/obj/"):
            raise ValueError("ScratchResult.sandbox_root must be an /obj/ path")
        _require_exact_int(self.applied_ops, "ScratchResult.applied_ops")
        if self.applied_ops < 0:
            raise ValueError("ScratchResult.applied_ops must be >= 0")
        if type(self.output_node) is not str:
            raise TypeError("ScratchResult.output_node must be a string")
        errs = tuple(self.errors)
        if len(errs) > _MAX_ERRORS:
            raise ValueError("ScratchResult.errors exceeds the maximum count")
        for e in errs:
            _require_error_text(e, "ScratchResult.errors")
        object.__setattr__(self, "errors", errs)
        if self.geometry is not None and type(self.geometry) is not ScratchGeometry:
            raise TypeError("ScratchResult.geometry must be ScratchGeometry or None")
        # Bound the whole result so a runaway response cannot exceed the wire limit.
        if len(canonical_json_dumps(self.to_dict())) > _MAX_RESULT_BYTES:
            raise ValueError("ScratchResult exceeds the maximum result size")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchResult":
        d = _require_exact_dict(data, "ScratchResult")
        _require_exact_keys(d, _RESULT_FIELDS, "ScratchResult")
        errors_raw = d["errors"]
        if type(errors_raw) is not list:
            raise TypeError("ScratchResult errors must be a list")
        geometry_raw = d["geometry"]
        geometry = None if geometry_raw is None else ScratchGeometry.from_dict(geometry_raw)
        return cls(
            sandbox_root=d["sandbox_root"],  # type: ignore[arg-type]
            applied_ops=d["applied_ops"],  # type: ignore[arg-type]
            output_node=d["output_node"],  # type: ignore[arg-type]
            errors=tuple(errors_raw),  # type: ignore[arg-type]
            geometry=geometry,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "sandbox_root": self.sandbox_root,
            "applied_ops": self.applied_ops,
            "output_node": self.output_node,
            "errors": list(self.errors),
            "geometry": self.geometry.to_dict() if self.geometry is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ScratchResponse:
    """The ``scratch.exec`` response envelope (mirrors CaptureResponse)."""

    request_id: str
    result: ScratchResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError("ScratchResponse must carry exactly one of result/error")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            d["result"] = self.result.to_dict()
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchResponse":
        envelope = _require_exact_dict(data, "ScratchResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("ScratchResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("ScratchResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("ScratchResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "ScratchResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError(
                    "ScratchResponse ok=true requires result and no error"
                )
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError(
                "ScratchResponse ok=false requires error and no result"
            )
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# commit DTOs (scratch.commit — promote sandbox to real scene through gates)
# --------------------------------------------------------------------------

_COMMIT_PAYLOAD_FIELDS = frozenset(
    {"sandbox_id", "target_parent_path", "target_name",
     "orientation_checks", "skip_structure_check"}
)
_COMMIT_RESULT_FIELDS = frozenset(
    {"committed", "refused", "final_path", "reason", "gates", "receipt"}
)


def _require_orientation_checks(value: object, label: str) -> tuple[dict[str, object], ...]:
    """Validate the orientation_checks list: bounded list/sequence of bounded dicts."""
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be a list")
    if len(value) > _MAX_OPS:
        raise ValueError(f"{label} exceeds {_MAX_OPS} checks")
    out: list[dict[str, object]] = []
    known = {
        "component_id", "kind", "expected_axis",
        "tolerance_deg", "signed", "construction_axis",
    }
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(f"{label} items must be objects")
        unknown = set(item) - known
        if unknown:
            # Reject rather than silently drop: a typo'd key would otherwise
            # pass validation while the Houdini-side gate runs without the
            # intended check.
            raise ValueError(
                f"{label} items contain unknown keys: {sorted(unknown)}"
            )
        out.append(dict(item))
    return tuple(out)


def _require_node_path(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute node path")
    if len(value) > _MAX_OP_ARGUMENT_CHARS:
        raise ValueError(f"{label} exceeds the maximum length")


@dataclass(frozen=True, slots=True)
class ScratchCommitRequest:
    """A parsed, validated ``scratch.commit`` request envelope.

    Promotes a verified sandbox into the real scene. The Houdini side runs the
    four verify gates (bake/structure/orientation/health) on the sandbox output
    node BEFORE any promotion; a gate failure is a refusal, not a rollback —
    the sandbox is preserved untouched. On pass, the sandbox container is
    renamed into ``target_parent_path`` under ``target_name``; the rename is
    journaled so a mid-promotion failure restores the original name (a
    ``hou.undos.group`` is not a transaction and is not relied on for
    atomicity).
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    sandbox_id: str
    target_parent_path: str
    target_name: str
    orientation_checks: tuple[dict[str, object], ...]
    skip_structure_check: bool = False

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchCommitRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchCommitRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchCommitRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchCommitRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchCommitRequest.scene_epoch must be >= 1")
        _require_sandbox_id(self.sandbox_id, "ScratchCommitRequest.sandbox_id")
        _require_node_path(self.target_parent_path, "ScratchCommitRequest.target_parent_path")
        _require_node_name(self.target_name, "ScratchCommitRequest.target_name")
        ops = _require_orientation_checks(
            self.orientation_checks, "ScratchCommitRequest.orientation_checks"
        )
        object.__setattr__(self, "orientation_checks", ops)
        _require_exact_bool(self.skip_structure_check, "ScratchCommitRequest.skip_structure_check")

    @property
    def container_path(self) -> str:
        return f"/obj/eee_scratch_{self.sandbox_id}"

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        sandbox_id: str,
        target_parent_path: str,
        target_name: str,
        orientation_checks: Sequence[Mapping[str, object]] | None = None,
        skip_structure_check: bool = False,
    ) -> "ScratchCommitRequest":
        checks = [dict(c) for c in (orientation_checks or [])]
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            sandbox_id=sandbox_id,
            target_parent_path=target_parent_path,
            target_name=target_name,
            orientation_checks=checks,  # type: ignore[arg-type]
            skip_structure_check=skip_structure_check,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_COMMIT_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "sandbox_id": self.sandbox_id,
                "target_parent_path": self.target_parent_path,
                "target_name": self.target_name,
                "orientation_checks": [dict(c) for c in self.orientation_checks],
                "skip_structure_check": self.skip_structure_check,
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchCommitRequest":
        envelope = _require_exact_dict(data, "ScratchCommitRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchCommitRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchCommitRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchCommitRequest kind must be request")
        if envelope["operation"] != SCRATCH_COMMIT_OPERATION:
            raise ValueError("ScratchCommitRequest operation must be scratch.commit")
        payload = _require_exact_dict(envelope["payload"], "ScratchCommitRequest payload")
        _require_exact_keys(payload, _COMMIT_PAYLOAD_FIELDS, "ScratchCommitRequest payload")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            sandbox_id=payload["sandbox_id"],  # type: ignore[arg-type]
            target_parent_path=payload["target_parent_path"],  # type: ignore[arg-type]
            target_name=payload["target_name"],  # type: ignore[arg-type]
            orientation_checks=_require_orientation_checks(  # type: ignore[arg-type]
                payload["orientation_checks"], "ScratchCommitRequest.orientation_checks"
            ),
            skip_structure_check=payload["skip_structure_check"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchCommitResult:
    """The bounded verdict + verification receipt of a scratch.commit call.

    ``committed=True`` means the sandbox was promoted into the real scene at
    ``final_path``. ``refused=True`` means a hard gate failed and the sandbox
    was preserved (exactly one of committed/refused is True). ``gates`` carries
    the per-gate results; ``receipt`` is the tamper-evident verification
    summary the agent must reference in its report.
    """

    committed: bool
    refused: bool
    final_path: str
    reason: str
    gates: tuple[dict[str, object], ...]
    receipt: dict[str, object]

    def __post_init__(self) -> None:
        _require_exact_bool(self.committed, "ScratchCommitResult.committed")
        _require_exact_bool(self.refused, "ScratchCommitResult.refused")
        if self.committed == self.refused:
            raise ValueError("ScratchCommitResult requires exactly one of committed/refused")
        if type(self.final_path) is not str:
            raise TypeError("ScratchCommitResult.final_path must be a string")
        if type(self.reason) is not str or len(self.reason) > _MAX_OP_ARGUMENT_CHARS:
            raise ValueError("ScratchCommitResult.reason is invalid")
        gates = tuple(self.gates)
        if len(gates) > 16:
            raise ValueError("ScratchCommitResult.gates exceeds the maximum count")
        object.__setattr__(self, "gates", gates)
        if not isinstance(self.receipt, Mapping):
            raise TypeError("ScratchCommitResult.receipt must be a mapping")
        object.__setattr__(self, "receipt", dict(self.receipt))
        if len(canonical_json_dumps(self.to_dict())) > _MAX_RESULT_BYTES:
            raise ValueError("ScratchCommitResult exceeds the maximum result size")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchCommitResult":
        d = _require_exact_dict(data, "ScratchCommitResult")
        _require_exact_keys(d, _COMMIT_RESULT_FIELDS, "ScratchCommitResult")
        gates_raw = d["gates"]
        if type(gates_raw) is not list:
            raise TypeError("ScratchCommitResult gates must be a list")
        receipt_raw = d["receipt"]
        if not isinstance(receipt_raw, Mapping):
            raise TypeError("ScratchCommitResult receipt must be a mapping")
        return cls(
            committed=d["committed"],  # type: ignore[arg-type]
            refused=d["refused"],  # type: ignore[arg-type]
            final_path=d["final_path"],  # type: ignore[arg-type]
            reason=d["reason"],  # type: ignore[arg-type]
            gates=tuple(dict(g) for g in gates_raw),  # type: ignore[arg-type]
            receipt=dict(receipt_raw),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed": self.committed,
            "refused": self.refused,
            "final_path": self.final_path,
            "reason": self.reason,
            "gates": [dict(g) for g in self.gates],
            "receipt": dict(self.receipt),
        }


@dataclass(frozen=True, slots=True)
class ScratchCommitResponse:
    """The ``scratch.commit`` response envelope (mirrors ScratchResponse)."""

    request_id: str
    result: ScratchCommitResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchCommitResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError("ScratchCommitResponse must carry exactly one of result/error")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchCommitResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            d["result"] = self.result.to_dict()
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchCommitResponse":
        envelope = _require_exact_dict(data, "ScratchCommitResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("ScratchCommitResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("ScratchCommitResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchCommitResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("ScratchCommitResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "ScratchCommitResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError(
                    "ScratchCommitResponse ok=true requires result and no error"
                )
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchCommitResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError(
                "ScratchCommitResponse ok=false requires error and no result"
            )
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# destroy DTOs (scratch.destroy — best-effort sandbox cleanup)
# --------------------------------------------------------------------------

_DESTROY_PAYLOAD_FIELDS = frozenset({"sandbox_id"})
_DESTROY_RESULT_FIELDS = frozenset({"destroyed_paths", "missing"})


@dataclass(frozen=True, slots=True)
class ScratchDestroyRequest:
    """A parsed, validated ``scratch.destroy`` request envelope.

    Best-effort cleanup of one run-scoped sandbox container
    (``/obj/eee_scratch_<sandbox_id>``). Run-end/cancel/restart hooks call this
    to avoid leaking scratch containers across runs. Unlike exec/commit, destroy
    bypasses the write-freeze gate: cleanup MUST run even after an uncertain
    recovery, otherwise a crashed run leaks its sandbox forever.
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    sandbox_id: str

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchDestroyRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchDestroyRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchDestroyRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchDestroyRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchDestroyRequest.scene_epoch must be >= 1")
        _require_sandbox_id(self.sandbox_id, "ScratchDestroyRequest.sandbox_id")

    @property
    def container_path(self) -> str:
        return f"/obj/eee_scratch_{self.sandbox_id}"

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        sandbox_id: str,
    ) -> "ScratchDestroyRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            sandbox_id=sandbox_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_DESTROY_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {"sandbox_id": self.sandbox_id},
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDestroyRequest":
        envelope = _require_exact_dict(data, "ScratchDestroyRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchDestroyRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchDestroyRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchDestroyRequest kind must be request")
        if envelope["operation"] != SCRATCH_DESTROY_OPERATION:
            raise ValueError("ScratchDestroyRequest operation must be scratch.destroy")
        payload = _require_exact_dict(envelope["payload"], "ScratchDestroyRequest payload")
        _require_exact_keys(payload, _DESTROY_PAYLOAD_FIELDS, "ScratchDestroyRequest payload")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            sandbox_id=payload["sandbox_id"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchDestroyResult:
    """The bounded result of a ``scratch.destroy`` call.

    ``destroyed_paths`` lists the containers that were removed (usually one);
    ``missing`` is True when the sandbox did not exist (already committed or
    cleaned up) — this is a normal, non-error outcome.
    """

    destroyed_paths: tuple[str, ...]
    missing: bool

    def __post_init__(self) -> None:
        paths = tuple(self.destroyed_paths)
        if len(paths) > 16:
            raise ValueError("ScratchDestroyResult.destroyed_paths exceeds the maximum count")
        for p in paths:
            if type(p) is not str or not p.startswith("/obj/"):
                raise ValueError("ScratchDestroyResult.destroyed_paths must be /obj/ paths")
        object.__setattr__(self, "destroyed_paths", paths)
        _require_exact_bool(self.missing, "ScratchDestroyResult.missing")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDestroyResult":
        d = _require_exact_dict(data, "ScratchDestroyResult")
        _require_exact_keys(d, _DESTROY_RESULT_FIELDS, "ScratchDestroyResult")
        paths_raw = d["destroyed_paths"]
        if type(paths_raw) is not list:
            raise TypeError("ScratchDestroyResult destroyed_paths must be a list")
        return cls(
            destroyed_paths=tuple(paths_raw),  # type: ignore[arg-type]
            missing=d["missing"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "destroyed_paths": list(self.destroyed_paths),
            "missing": self.missing,
        }


@dataclass(frozen=True, slots=True)
class ScratchDestroyResponse:
    """The ``scratch.destroy`` response envelope (mirrors ScratchResponse)."""

    request_id: str
    result: ScratchDestroyResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchDestroyResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError("ScratchDestroyResponse must carry exactly one of result/error")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchDestroyResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            d["result"] = self.result.to_dict()
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDestroyResponse":
        envelope = _require_exact_dict(data, "ScratchDestroyResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("ScratchDestroyResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("ScratchDestroyResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchDestroyResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("ScratchDestroyResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "ScratchDestroyResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError(
                    "ScratchDestroyResponse ok=true requires result and no error"
                )
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchDestroyResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError(
                "ScratchDestroyResponse ok=false requires error and no result"
            )
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# entrypoints
# --------------------------------------------------------------------------


def parse_scratch_request(raw: str | bytes) -> ScratchRequest:
    return ScratchRequest.from_dict(_load_strict_dict(raw, "Scratch request"))


def parse_scratch_response(raw: str | bytes) -> ScratchResponse:
    return ScratchResponse.from_dict(_load_strict_dict(raw, "Scratch response"))


def parse_scratch_commit_request(raw: str | bytes) -> ScratchCommitRequest:
    return ScratchCommitRequest.from_dict(_load_strict_dict(raw, "Scratch commit request"))


def parse_scratch_commit_response(raw: str | bytes) -> ScratchCommitResponse:
    return ScratchCommitResponse.from_dict(_load_strict_dict(raw, "Scratch commit response"))


def parse_scratch_destroy_request(raw: str | bytes) -> ScratchDestroyRequest:
    return ScratchDestroyRequest.from_dict(_load_strict_dict(raw, "Scratch destroy request"))


def parse_scratch_destroy_response(raw: str | bytes) -> ScratchDestroyResponse:
    return ScratchDestroyResponse.from_dict(_load_strict_dict(raw, "Scratch destroy response"))
