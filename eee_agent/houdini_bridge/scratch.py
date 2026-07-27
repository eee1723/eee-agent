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
operations (create_node / declare_parm / set_parm / connect) against the
sandbox container.
Network/raw-Python ``exec`` mode is deferred to a later phase (it requires an
async job protocol to avoid the 30s deadline ceiling).

Strictness mirrors the rest of the bridge package: exact primitive types,
exact field sets, deep-frozen canonical JSON, duplicate-key rejection, finite
numbers, and an explicit size limit.
"""

from __future__ import annotations

import math
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
SCRATCH_V2 = "scratch.v2"
SCRATCH_EXEC_OPERATION = "scratch.exec"
SCRATCH_COMMIT_OPERATION = "scratch.commit"
SCRATCH_DESTROY_OPERATION = "scratch.destroy"
SCRATCH_DELETE_OPERATION = "scratch.delete"
SCRATCH_TOPOLOGY_OPERATION = "scratch.topology"

# Bounded operation counts: a single scratch.exec call may carry a small
# batch of ops (create a few nodes + set their parms + wire them), but never
# an unbounded graph. The agent iterates by calling scratch.exec repeatedly.
_MAX_OPS = 64
_MAX_NODE_NAME_LEN = 64
_MAX_NODE_TYPE_LEN = 64
_MAX_PARM_NAME_LEN = 64
_MAX_OP_ARGUMENT_CHARS = 4000
_MAX_SANDBOX_NAME_LEN = 128
_MAX_PURPOSE_CHARS = 200
_MAX_NOTE_CHARS = 200
_MAX_ANNOTATION_CHARS = 500
_MAX_ERRORS = 32
_MAX_ERROR_CHARS = 1000

# Typed parameter-expression bounds (scratch.v2 C1). Expressions are the ONLY
# way one parm may reference another: they render to a bounded Hscript string
# built solely from numeric literals, ch() references inside the sandbox,
# arithmetic, and whitelisted math functions. The ref charset makes file
# paths, $VAR/backtick expansion, and expression injection unrepresentable.
_MAX_EXPR_DEPTH = 8
_MAX_EXPR_NODES = 32
_MAX_EXPR_REF_CHARS = 256
_EXPR_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_EXPR_OP_NAMES = frozenset({"add", "sub", "mul", "div", "neg"})
_EXPR_FUNC_NAMES = frozenset(
    {
        "sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "abs",
        "min", "max", "floor", "ceil", "pow", "clamp",
    }
)
_EXPR_FIELDS = {
    "num": frozenset({"kind", "value"}),
    "ref": frozenset({"kind", "path"}),
    "op": frozenset({"kind", "name", "args"}),
    "func": frozenset({"kind", "name", "args"}),
}

# exact field sets for envelope + payload validation
_PAYLOAD_FIELDS = frozenset(
    {"sandbox_id", "operations", "purpose", "preserve_on_failure"}
)
_OP_FIELDS = frozenset(
    {
        "kind", "node_name", "node_type", "parent", "parm", "value", "expr",
        "input_index", "source", "source_output_index", "note", "label",
        "min", "max", "unit",
    }
)
_RESULT_FIELDS = frozenset(
    {"sandbox_root", "applied_ops", "output_node", "errors", "geometry"}
)
_GEOMETRY_FIELDS = frozenset({"point_count", "prim_count", "vertex_count", "bbox_min", "bbox_max"})

_OP_KINDS = frozenset(
    {"create_node", "declare_parm", "set_parm", "connect", "delete_node"}
)
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
    """A safe sandbox-relative node reference.

    Nested component paths (``wheel/tube1``) are intentionally supported.
    Absolute paths, traversal segments, empty segments, and backslashes are
    rejected so every reference remains inside the current scratch container.
    """
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(segment in ("", ".", "..") for segment in value.split("/"))
        or len(value) > _MAX_NODE_NAME_LEN * 4
        or any(_NODE_NAME_RE.fullmatch(segment) is None for segment in value.split("/"))
    ):
        raise ValueError(f"{label} must be a safe sandbox-relative node reference")


_PARAMETER_FIELDS = frozenset(
    {"name", "tab", "classification", "binding", "default", "min", "max", "depends_on", "unit"}
)
_PARAMETER_BINDING_FIELDS = frozenset({"node", "parm"})
_PARAMETER_CLASSIFICATIONS = frozenset({"design_intent", "derived", "constant"})
_PARAMETER_UNITS = frozenset({"m", "deg", "count"})
_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_PARAMETERS = 64
_MAX_PARAMETER_MANIFEST_BYTES = 8 * 1024


def _require_finite_number(value: object, label: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise TypeError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


@dataclass(frozen=True, slots=True)
class ScratchParmDeclaration:
    """One bounded Parameter Manifest entry carried by ``scratch.commit``."""

    name: str
    tab: str = "Main"
    classification: str = "design_intent"
    binding: tuple[tuple[str, str], ...] = ()
    default: float | int | None = None
    min: float | int | None = None
    max: float | int | None = None
    depends_on: tuple[str, ...] = ()
    unit: str | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str or _PARAMETER_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("ScratchParmDeclaration.name is invalid")
        if type(self.tab) is not str or not self.tab or len(self.tab) > 64:
            raise ValueError("ScratchParmDeclaration.tab must be a non-empty bounded string")
        if self.classification not in _PARAMETER_CLASSIFICATIONS:
            raise ValueError("ScratchParmDeclaration.classification is invalid")
        binding = dict(self.binding)
        if set(binding) != {"node", "parm"}:
            raise ValueError("ScratchParmDeclaration.binding requires node and parm")
        _require_node_ref(binding["node"], "ScratchParmDeclaration.binding.node")
        _require_parm_name(binding["parm"], "ScratchParmDeclaration.binding.parm")
        object.__setattr__(self, "binding", tuple(sorted(binding.items())))
        if self.default is None:
            raise ValueError("ScratchParmDeclaration.default is required")
        default = _require_finite_number(self.default, "ScratchParmDeclaration.default")
        object.__setattr__(self, "default", default)
        if self.classification == "design_intent":
            if self.min is None or self.max is None:
                raise ValueError("design_intent requires min and max")
            minimum = _require_finite_number(self.min, "ScratchParmDeclaration.min")
            maximum = _require_finite_number(self.max, "ScratchParmDeclaration.max")
            if minimum > default or default > maximum:
                raise ValueError("design_intent requires min <= default <= max")
            object.__setattr__(self, "min", minimum)
            object.__setattr__(self, "max", maximum)
            if self.depends_on:
                raise ValueError("design_intent must not declare depends_on")
        elif self.classification == "derived":
            if not self.depends_on:
                raise ValueError("derived requires non-empty depends_on")
            deps = tuple(self.depends_on)
            if any(type(dep) is not str or _PARAMETER_NAME_RE.fullmatch(dep) is None for dep in deps):
                raise ValueError("derived depends_on contains an invalid name")
            object.__setattr__(self, "depends_on", deps)
            if self.min is not None:
                minimum = _require_finite_number(self.min, "ScratchParmDeclaration.min")
                object.__setattr__(self, "min", minimum)
            if self.max is not None:
                maximum = _require_finite_number(self.max, "ScratchParmDeclaration.max")
                object.__setattr__(self, "max", maximum)
            if self.min is not None and self.max is not None and self.min > self.max:
                raise ValueError("derived requires min <= max")
        else:
            if self.min is not None or self.max is not None or self.depends_on:
                raise ValueError("constant must not declare min/max/depends_on")
        if self.unit is not None and self.unit not in _PARAMETER_UNITS:
            raise ValueError("ScratchParmDeclaration.unit is invalid")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchParmDeclaration":
        d = _require_exact_dict(data, "ScratchParmDeclaration")
        unknown = set(d) - _PARAMETER_FIELDS
        if unknown:
            raise ValueError(f"ScratchParmDeclaration has unknown fields: {sorted(unknown)}")
        binding = _require_exact_dict(d.get("binding"), "ScratchParmDeclaration.binding")
        _require_exact_keys(binding, _PARAMETER_BINDING_FIELDS, "ScratchParmDeclaration.binding")
        deps = d.get("depends_on", [])
        if type(deps) is not list:
            raise TypeError("ScratchParmDeclaration.depends_on must be a list")
        return cls(
            name=d.get("name"), tab=d.get("tab", "Main"),
            classification=d.get("classification", "design_intent"),
            binding=tuple(binding.items()),
            default=d.get("default"), min=d.get("min"), max=d.get("max"),
            depends_on=tuple(deps), unit=d.get("unit"),
        )

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "name": self.name, "tab": self.tab, "classification": self.classification,
            "binding": dict(self.binding), "default": self.default,
        }
        if self.min is not None: d["min"] = self.min
        if self.max is not None: d["max"] = self.max
        if self.depends_on: d["depends_on"] = list(self.depends_on)
        if self.unit is not None: d["unit"] = self.unit
        return d


def _require_parameters(value: object, label: str = "ScratchCommitRequest.parameters") -> tuple[ScratchParmDeclaration, ...]:
    if type(value) not in (list, tuple):
        raise TypeError(f"{label} must be a list")
    if len(value) > _MAX_PARAMETERS:
        raise ValueError(f"{label} exceeds {_MAX_PARAMETERS} entries")
    out = tuple(item if type(item) is ScratchParmDeclaration else ScratchParmDeclaration.from_dict(item) for item in value)
    names = [item.name for item in out]
    if len(set(names)) != len(names):
        raise ValueError(f"{label} contains duplicate names")
    if len(canonical_json_dumps([item.to_dict() for item in out])) > _MAX_PARAMETER_MANIFEST_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_PARAMETER_MANIFEST_BYTES} bytes")
    return out


def _require_error_text(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if len(value) > _MAX_ERROR_CHARS:
        raise ValueError(f"{label} exceeds the maximum length")


def _require_bounded_text(value: object, label: str, max_len: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > max_len:
        raise ValueError(f"{label} must be 1..{max_len} characters")


def _require_expr_ref(value: object, label: str) -> None:
    """A parm reference path: relative (``../ctrl/sizex``) or absolute.

    The charset ``[A-Za-z0-9_./-]`` explicitly rejects ``:``, ``\\``, ``$``,
    backticks, quotes, and whitespace, so file paths, variable/backtick
    expansion, and Hscript/Python injection are unrepresentable.
    """
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > _MAX_EXPR_REF_CHARS:
        raise ValueError(f"{label} must be 1..{_MAX_EXPR_REF_CHARS} characters")
    if _EXPR_REF_RE.fullmatch(value) is None:
        raise ValueError(f"{label} has characters outside [A-Za-z0-9_./-]")
    body = value[1:] if value.startswith("/") else value
    segments = body.split("/")
    if any(segment in ("", ".") for segment in segments):
        raise ValueError(f"{label} must not contain empty or '.' segments")
    if _PARM_NAME_RE.fullmatch(segments[-1]) is None:
        raise ValueError(f"{label} must end with a parm name")


# --------------------------------------------------------------------------
# parameter-expression DTO (scratch.v2 C1)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchExpr:
    """One typed parameter-expression node (an AST, not a string).

    kinds:
      - num: a finite int/float literal (bool/nan/inf rejected).
      - ref: a ``ch()`` parm reference path, relative to the target node
        (``../ctrl/sizex``) or absolute (``/obj/...``); resolved and
        sandbox-checked on the Houdini side.
      - op: arithmetic ``add|sub|mul|div`` (exactly 2 args) or ``neg`` (1 arg).
      - func: a whitelisted math function; ``clamp`` takes exactly 3 args,
        ``pow`` exactly 2, ``min``/``max`` 1..4, the rest exactly 1.

    The whole tree is bounded: depth <= 8, total nodes <= 32.
    """

    kind: str
    value: object = None
    path: str = ""
    name: str = ""
    args: tuple["ScratchExpr", ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _EXPR_FIELDS:
            raise ValueError("ScratchExpr.kind must be num|ref|op|func")
        if self.kind == "num":
            if type(self.value) not in (int, float) or type(self.value) is bool:
                raise TypeError("ScratchExpr num value must be an int or float")
            if not math.isfinite(self.value):  # type: ignore[arg-type]
                raise ValueError("ScratchExpr num value must be finite")
        elif self.kind == "ref":
            _require_expr_ref(self.path, "ScratchExpr.path")
        else:
            if self.kind == "op":
                if self.name not in _EXPR_OP_NAMES:
                    raise ValueError("ScratchExpr op name must be add|sub|mul|div|neg")
                expected = 1 if self.name == "neg" else 2
            else:
                if self.name not in _EXPR_FUNC_NAMES:
                    raise ValueError(
                        "ScratchExpr func name is not in the whitelist"
                    )
                expected = {"clamp": 3, "pow": 2}.get(self.name, 1)
            args = tuple(self.args)
            for arg in args:
                if type(arg) is not ScratchExpr:
                    raise TypeError("ScratchExpr args must be ScratchExpr instances")
            if self.kind == "func" and self.name in ("min", "max"):
                if not 1 <= len(args) <= 4:
                    raise ValueError("ScratchExpr min/max take 1..4 args")
            elif len(args) != expected:
                raise ValueError(
                    f"ScratchExpr {self.name} takes exactly {expected} arg(s)"
                )
            object.__setattr__(self, "args", args)
        _, total = self._measure(1)
        if total > _MAX_EXPR_NODES:
            raise ValueError(
                f"ScratchExpr exceeds the maximum node count ({_MAX_EXPR_NODES})"
            )

    def _measure(self, depth: int) -> tuple[int, int]:
        """Return (deepest depth, node count) of the subtree; enforce depth."""
        if depth > _MAX_EXPR_DEPTH:
            raise ValueError(
                f"ScratchExpr exceeds the maximum depth ({_MAX_EXPR_DEPTH})"
            )
        deepest, total = depth, 1
        for arg in self.args:
            child_depth, child_total = arg._measure(depth + 1)
            deepest = max(deepest, child_depth)
            total += child_total
        return deepest, total

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchExpr":
        d = _require_exact_dict(data, "ScratchExpr")
        kind = d.get("kind")
        if type(kind) is not str or kind not in _EXPR_FIELDS:
            raise ValueError("ScratchExpr.kind must be num|ref|op|func")
        _require_exact_keys(d, _EXPR_FIELDS[kind], f"ScratchExpr[{kind}]")
        if kind == "num":
            return cls(kind="num", value=d["value"])
        if kind == "ref":
            return cls(kind="ref", path=d["path"])  # type: ignore[arg-type]
        args_raw = d["args"]
        if type(args_raw) is not list:
            raise TypeError("ScratchExpr args must be a list")
        args = tuple(cls.from_dict(item) for item in args_raw)
        return cls(
            kind=kind,
            name=d["name"],  # type: ignore[arg-type]
            args=args,
        )

    def to_dict(self) -> dict[str, object]:
        if self.kind == "num":
            return {"kind": "num", "value": self.value}
        if self.kind == "ref":
            return {"kind": "ref", "path": self.path}
        return {
            "kind": self.kind,
            "name": self.name,
            "args": [arg.to_dict() for arg in self.args],
        }


# --------------------------------------------------------------------------
# operation DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScratchOp:
    """One structured sandbox operation.

    kinds:
      - create_node: create ``node_type`` named ``node_name`` under ``parent``
        (a sandbox-relative ref or the container root).
      - declare_parm: add one public numeric parameter to a ``subnet`` or
        sandbox ``geo`` container. ``value`` is its default and ``min`` /
        ``max`` define the bounded UI range.
      - set_parm: set ``parm`` on ``node_name`` to a literal ``value``, or to
        a typed ``expr`` (exactly one of value/expr must be set).
      - connect: wire input ``input_index`` of ``node_name`` from ``source``
        output ``source_output_index``.
    """

    kind: str
    node_name: str
    node_type: str = ""
    parent: str = ""
    parm: str = ""
    value: object = None
    expr: ScratchExpr | None = None
    input_index: int = 0
    source: str = ""
    source_output_index: int = 0
    note: str = ""
    label: str = ""
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _OP_KINDS:
            raise ValueError(
                "ScratchOp.kind must be create_node|declare_parm|set_parm|connect|delete_node"
            )
        if self.kind == "create_node":
            _require_node_name(self.node_name, "ScratchOp.node_name")
        else:
            _require_node_ref(self.node_name, "ScratchOp.node_name")
        if self.expr is not None:
            if type(self.expr) is not ScratchExpr:
                raise TypeError("ScratchOp.expr must be a ScratchExpr")
            if self.kind != "set_parm":
                raise ValueError("ScratchOp.expr is only valid for set_parm")
        if self.kind == "create_node":
            _require_node_type(self.node_type, "ScratchOp.node_type")
            # parent may be "" (= sandbox root) or a sandbox-relative ref
            if self.parent:
                _require_node_ref(self.parent, "ScratchOp.parent")
        elif self.kind == "declare_parm":
            _require_parm_name(self.parm, "ScratchOp.parm")
            if self.value is None:
                raise ValueError("ScratchOp declare_parm requires a default value")
            _require_finite_number(self.value, "ScratchOp.value")
            if self.minimum is None or self.maximum is None:
                raise ValueError("ScratchOp declare_parm requires min and max")
            minimum = _require_finite_number(self.minimum, "ScratchOp.minimum")
            maximum = _require_finite_number(self.maximum, "ScratchOp.maximum")
            default = float(self.value)
            if minimum > default or default > maximum:
                raise ValueError("ScratchOp declare_parm requires min <= value <= max")
            _require_bounded_text(self.label, "ScratchOp.label", 80)
            if self.unit not in _PARAMETER_UNITS:
                raise ValueError(
                    "ScratchOp.unit must be one of m|deg|count"
                )
        elif self.kind == "set_parm":
            _require_parm_name(self.parm, "ScratchOp.parm")
            if self.expr is not None:
                if self.value is not None:
                    raise ValueError(
                        "ScratchOp set_parm accepts exactly one of value/expr"
                    )
            else:
                if self.value is None:
                    raise ValueError(
                        "ScratchOp set_parm requires exactly one of value/expr"
                    )
                _require_op_value(self.value, "ScratchOp.value")
        elif self.kind == "connect":
            _require_exact_int(self.input_index, "ScratchOp.input_index")
            if self.input_index < 0:
                raise ValueError("ScratchOp.input_index must be >= 0")
            _require_node_ref(self.source, "ScratchOp.source")
            _require_exact_int(self.source_output_index, "ScratchOp.source_output_index")
            if self.source_output_index < 0:
                raise ValueError("ScratchOp.source_output_index must be >= 0")
        if self.note:
            if self.kind != "create_node":
                raise ValueError("ScratchOp.note is only valid for create_node")
            _require_bounded_text(self.note, "ScratchOp.note", _MAX_NOTE_CHARS)
        if self.kind != "declare_parm" and (self.label or self.minimum is not None or self.maximum is not None or self.unit):
            raise ValueError(
                "ScratchOp label/min/max/unit are only valid for declare_parm"
            )

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
            expr=(
                ScratchExpr.from_dict(d["expr"])  # type: ignore[arg-type]
                if d.get("expr") is not None
                else None
            ),
            input_index=d.get("input_index", 0),  # type: ignore[arg-type]
            source=d.get("source", ""),  # type: ignore[arg-type]
            source_output_index=d.get("source_output_index", 0),  # type: ignore[arg-type]
            note=d.get("note", ""),  # type: ignore[arg-type]
            label=d.get("label", ""),  # type: ignore[arg-type]
            minimum=d.get("min", None),  # type: ignore[arg-type]
            maximum=d.get("max", None),  # type: ignore[arg-type]
            unit=d.get("unit", ""),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"kind": self.kind, "node_name": self.node_name}
        if self.kind == "create_node":
            d["node_type"] = self.node_type
            if self.parent:
                d["parent"] = self.parent
            if self.note:
                d["note"] = self.note
        elif self.kind == "declare_parm":
            d["parm"] = self.parm
            d["value"] = self.value
            d["label"] = self.label
            d["min"] = self.minimum
            d["max"] = self.maximum
            d["unit"] = self.unit
        elif self.kind == "set_parm":
            d["parm"] = self.parm
            if self.expr is not None:
                d["expr"] = self.expr.to_dict()
            else:
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
    purpose: str
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
        _require_bounded_text(
            self.purpose, "ScratchRequest.purpose", _MAX_PURPOSE_CHARS
        )
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
        purpose: str,
        preserve_on_failure: bool = True,
    ) -> "ScratchRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            sandbox_id=sandbox_id,
            operations=tuple(operations),
            purpose=purpose,
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
                "purpose": self.purpose,
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
            purpose=payload["purpose"],  # type: ignore[arg-type]
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
     "orientation_checks", "skip_structure_check", "annotations", "parameters"}
)
_COMMIT_RESULT_FIELDS = frozenset(
    {"committed", "refused", "final_path", "reason", "gates", "receipt", "warnings"}
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
    if (
        not value.startswith("/")
        or "\\" in value
        or any(segment in ("", ".", "..") for segment in value.split("/")[1:])
    ):
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
    annotations: tuple[tuple[str, str], ...] = ()
    parameters: tuple[ScratchParmDeclaration, ...] = ()

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
        annotations = tuple(self.annotations)
        if len(annotations) > _MAX_OPS:
            raise ValueError(
                "ScratchCommitRequest.annotations exceeds the maximum count"
            )
        names: set[str] = set()
        for item in annotations:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError(
                    "ScratchCommitRequest.annotations items must be pairs"
                )
            name, comment = item
            _require_node_ref(name, "ScratchCommitRequest.annotations key")
            _require_bounded_text(
                comment,
                "ScratchCommitRequest.annotations value",
                _MAX_ANNOTATION_CHARS,
            )
            if name in names:
                raise ValueError("ScratchCommitRequest.annotations has duplicate keys")
            names.add(name)
        object.__setattr__(self, "annotations", tuple(sorted(annotations)))
        object.__setattr__(self, "parameters", _require_parameters(self.parameters))

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
        annotations: Mapping[str, str] | None = None,
        parameters: Sequence[ScratchParmDeclaration | Mapping[str, object]] | None = None,
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
            annotations=tuple((annotations or {}).items()),
            parameters=tuple(parameters or ()),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sandbox_id": self.sandbox_id,
            "target_parent_path": self.target_parent_path,
            "target_name": self.target_name,
            "orientation_checks": [dict(c) for c in self.orientation_checks],
            "skip_structure_check": self.skip_structure_check,
            "annotations": dict(self.annotations),
        }
        if self.parameters:
            payload["parameters"] = [item.to_dict() for item in self.parameters]
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_COMMIT_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": payload,
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
        required = _COMMIT_PAYLOAD_FIELDS - {"parameters"}
        if not required.issubset(payload) or set(payload) - _COMMIT_PAYLOAD_FIELDS:
            raise ValueError("ScratchCommitRequest payload has invalid fields")
        annotations = _require_exact_dict(
            payload["annotations"], "ScratchCommitRequest.annotations"
        )
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
            annotations=tuple(annotations.items()),  # type: ignore[arg-type]
            parameters=_require_parameters(payload.get("parameters", [])),
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
    warnings: tuple[str, ...] = ()

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
        warnings = tuple(self.warnings)
        if len(warnings) > _MAX_ERRORS:
            raise ValueError("ScratchCommitResult.warnings exceeds the maximum count")
        for warning in warnings:
            _require_error_text(warning, "ScratchCommitResult.warnings")
        object.__setattr__(self, "warnings", warnings)
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
        warnings_raw = d["warnings"]
        if type(warnings_raw) is not list:
            raise TypeError("ScratchCommitResult warnings must be a list")
        return cls(
            committed=d["committed"],  # type: ignore[arg-type]
            refused=d["refused"],  # type: ignore[arg-type]
            final_path=d["final_path"],  # type: ignore[arg-type]
            reason=d["reason"],  # type: ignore[arg-type]
            gates=tuple(dict(g) for g in gates_raw),  # type: ignore[arg-type]
            receipt=dict(receipt_raw),  # type: ignore[arg-type]
            warnings=tuple(warnings_raw),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "committed": self.committed,
            "refused": self.refused,
            "final_path": self.final_path,
            "reason": self.reason,
            "gates": [dict(g) for g in self.gates],
            "receipt": dict(self.receipt),
            "warnings": list(self.warnings),
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
    (``/obj/eee_scratch_<sandbox_id>``). It is reserved for explicit disposal
    and recovery flows; ordinary Run completion/cancellation preserves the
    sandbox for inspection. Unlike exec/commit, destroy bypasses the
    write-freeze gate so an explicitly requested cleanup can still run after
    uncertain recovery.
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
# delete + topology DTOs (scratch.v2 node cleanup surfaces)
# --------------------------------------------------------------------------

_DELETE_PAYLOAD_FIELDS = frozenset({"allowed_paths", "paths"})
_DELETE_RESULT_FIELDS = frozenset({"deleted_paths", "skipped"})
_SKIPPED_FIELDS = frozenset({"path", "reason"})
_TOPOLOGY_PAYLOAD_FIELDS = frozenset({"paths"})
_TOPOLOGY_RESULT_FIELDS = frozenset({"nodes"})
_TOPOLOGY_NODE_FIELDS = frozenset(
    {"path", "exists", "inputs", "outputs", "display_flag"}
)
_MAX_DELETE_PATHS = 64
_MAX_TOPOLOGY_PATHS = 64
_MAX_TOPOLOGY_EDGES = 64
_MAX_ALLOWED_DELETE_PATHS = 256


def _require_node_path_list(
    value: object, label: str, max_count: int
) -> tuple[str, ...]:
    if type(value) is not list and type(value) is not tuple:
        raise TypeError(f"{label} must be a list")
    paths = tuple(value)
    if not paths or len(paths) > max_count:
        raise ValueError(f"{label} must contain 1..{max_count} paths")
    if len(set(paths)) != len(paths):
        raise ValueError(f"{label} must not contain duplicate paths")
    for path in paths:
        _require_node_path(path, label)
    return paths


def _require_skipped_entry(value: object) -> dict[str, object]:
    d = _require_exact_dict(value, "ScratchDeleteResult.skipped entry")
    _require_exact_keys(d, _SKIPPED_FIELDS, "ScratchDeleteResult.skipped entry")
    _require_node_path(d["path"], "ScratchDeleteResult.skipped path")
    _require_error_text(d["reason"], "ScratchDeleteResult.skipped reason")
    return {"path": d["path"], "reason": d["reason"]}


def _require_topology_entry(value: object) -> dict[str, object]:
    d = _require_exact_dict(value, "ScratchTopologyResult entry")
    _require_exact_keys(d, _TOPOLOGY_NODE_FIELDS, "ScratchTopologyResult entry")
    _require_node_path(d["path"], "ScratchTopologyResult path")
    _require_exact_bool(d["exists"], "ScratchTopologyResult exists")
    _require_exact_bool(d["display_flag"], "ScratchTopologyResult display_flag")
    for field in ("inputs", "outputs"):
        edges = d[field]
        if type(edges) is not list or len(edges) > _MAX_TOPOLOGY_EDGES:
            raise ValueError(f"ScratchTopologyResult {field} must be a bounded list")
        if len(set(edges)) != len(edges):
            raise ValueError(f"ScratchTopologyResult {field} must not contain duplicates")
        for edge in edges:
            _require_node_path(edge, f"ScratchTopologyResult {field}")
    return {
        "path": d["path"],
        "exists": d["exists"],
        "inputs": list(d["inputs"]),
        "outputs": list(d["outputs"]),
        "display_flag": d["display_flag"],
    }


@dataclass(frozen=True, slots=True)
class ScratchDeleteRequest:
    """Validated ``scratch.delete`` request with a Runtime-owned allowlist."""

    request_id: str
    deadline_ms: int
    scene_epoch: int
    allowed_paths: tuple[str, ...]
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchDeleteRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchDeleteRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchDeleteRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchDeleteRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchDeleteRequest.scene_epoch must be >= 1")
        allowed = _require_node_path_list(
            self.allowed_paths,
            "ScratchDeleteRequest.allowed_paths",
            _MAX_ALLOWED_DELETE_PATHS,
        )
        paths = _require_node_path_list(
            self.paths, "ScratchDeleteRequest.paths", _MAX_DELETE_PATHS
        )
        if not set(paths) <= set(allowed):
            raise ValueError("ScratchDeleteRequest.paths must be a subset of allowed_paths")
        object.__setattr__(self, "allowed_paths", allowed)
        object.__setattr__(self, "paths", paths)

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        allowed_paths: Sequence[str],
        paths: Sequence[str],
    ) -> "ScratchDeleteRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            allowed_paths=tuple(allowed_paths),
            paths=tuple(paths),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_DELETE_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "allowed_paths": list(self.allowed_paths),
                "paths": list(self.paths),
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDeleteRequest":
        envelope = _require_exact_dict(data, "ScratchDeleteRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchDeleteRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchDeleteRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchDeleteRequest kind must be request")
        if envelope["operation"] != SCRATCH_DELETE_OPERATION:
            raise ValueError("ScratchDeleteRequest operation must be scratch.delete")
        payload = _require_exact_dict(envelope["payload"], "ScratchDeleteRequest payload")
        _require_exact_keys(payload, _DELETE_PAYLOAD_FIELDS, "ScratchDeleteRequest payload")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            allowed_paths=tuple(payload["allowed_paths"]),  # type: ignore[arg-type]
            paths=tuple(payload["paths"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchDeleteResult:
    """Bounded per-path outcome for ``scratch.delete``."""

    deleted_paths: tuple[str, ...]
    skipped: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        paths = tuple(self.deleted_paths)
        if len(paths) > _MAX_DELETE_PATHS or len(set(paths)) != len(paths):
            raise ValueError("ScratchDeleteResult.deleted_paths is invalid")
        for path in paths:
            _require_node_path(path, "ScratchDeleteResult.deleted_paths")
        skipped = tuple(self.skipped)
        if len(skipped) > _MAX_DELETE_PATHS:
            raise ValueError("ScratchDeleteResult.skipped exceeds the maximum count")
        object.__setattr__(self, "deleted_paths", paths)
        object.__setattr__(
            self, "skipped", tuple(_require_skipped_entry(item) for item in skipped)
        )
        if len(canonical_json_dumps(self.to_dict())) > _MAX_RESULT_BYTES:
            raise ValueError("ScratchDeleteResult exceeds the maximum result size")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDeleteResult":
        d = _require_exact_dict(data, "ScratchDeleteResult")
        _require_exact_keys(d, _DELETE_RESULT_FIELDS, "ScratchDeleteResult")
        if type(d["deleted_paths"]) is not list or type(d["skipped"]) is not list:
            raise TypeError("ScratchDeleteResult fields must be lists")
        return cls(
            deleted_paths=tuple(d["deleted_paths"]),  # type: ignore[arg-type]
            skipped=tuple(dict(item) for item in d["skipped"]),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_paths": list(self.deleted_paths),
            "skipped": [dict(item) for item in self.skipped],
        }


@dataclass(frozen=True, slots=True)
class ScratchDeleteResponse:
    request_id: str
    result: ScratchDeleteResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchDeleteResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError("ScratchDeleteResponse must carry exactly one of result/error")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchDeleteResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            data["result"] = self.result.to_dict()
        else:
            data["error"] = self.error.to_dict()  # type: ignore[union-attr]
        return data

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchDeleteResponse":
        return cls._from_envelope(data)

    @classmethod
    def _from_envelope(cls, data: Mapping[str, object]) -> "ScratchDeleteResponse":
        envelope = _require_exact_dict(data, "ScratchDeleteResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope):
            raise ValueError("ScratchDeleteResponse envelope is missing required fields")
        if set(envelope) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}:
            raise ValueError("ScratchDeleteResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL or envelope["kind"] != "response":
            raise ValueError("ScratchDeleteResponse envelope is invalid")
        _require_exact_bool(envelope["ok"], "ScratchDeleteResponse.ok")
        if envelope["ok"] is True:
            if envelope.get("result") is None or envelope.get("error") is not None:
                raise ValueError("ScratchDeleteResponse ok=true requires result only")
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchDeleteResult.from_dict(envelope["result"]),  # type: ignore[arg-type]
                error=None,
            )
        if envelope.get("error") is None or envelope.get("result") is not None:
            raise ValueError("ScratchDeleteResponse ok=false requires error only")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(envelope["error"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchTopologyRequest:
    request_id: str
    deadline_ms: int
    scene_epoch: int
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchTopologyRequest.request_id")
        _require_exact_int(self.deadline_ms, "ScratchTopologyRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("ScratchTopologyRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "ScratchTopologyRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ScratchTopologyRequest.scene_epoch must be >= 1")
        object.__setattr__(
            self,
            "paths",
            _require_node_path_list(
                self.paths, "ScratchTopologyRequest.paths", _MAX_TOPOLOGY_PATHS
            ),
        )

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        paths: Sequence[str],
    ) -> "ScratchTopologyRequest":
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            paths=tuple(paths),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": SCRATCH_TOPOLOGY_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {"paths": list(self.paths)},
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchTopologyRequest":
        envelope = _require_exact_dict(data, "ScratchTopologyRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "ScratchTopologyRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("ScratchTopologyRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("ScratchTopologyRequest kind must be request")
        if envelope["operation"] != SCRATCH_TOPOLOGY_OPERATION:
            raise ValueError("ScratchTopologyRequest operation must be scratch.topology")
        payload = _require_exact_dict(
            envelope["payload"], "ScratchTopologyRequest payload"
        )
        _require_exact_keys(
            payload, _TOPOLOGY_PAYLOAD_FIELDS, "ScratchTopologyRequest payload"
        )
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            paths=tuple(payload["paths"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ScratchTopologyResult:
    nodes: tuple[dict[str, object], ...]

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        if len(nodes) > _MAX_TOPOLOGY_PATHS:
            raise ValueError("ScratchTopologyResult.nodes exceeds the maximum count")
        object.__setattr__(
            self, "nodes", tuple(_require_topology_entry(node) for node in nodes)
        )
        if len(canonical_json_dumps(self.to_dict())) > _MAX_RESULT_BYTES:
            raise ValueError("ScratchTopologyResult exceeds the maximum result size")

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchTopologyResult":
        d = _require_exact_dict(data, "ScratchTopologyResult")
        _require_exact_keys(d, _TOPOLOGY_RESULT_FIELDS, "ScratchTopologyResult")
        if type(d["nodes"]) is not list:
            raise TypeError("ScratchTopologyResult.nodes must be a list")
        return cls(nodes=tuple(dict(node) for node in d["nodes"]))  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {"nodes": [dict(node) for node in self.nodes]}


@dataclass(frozen=True, slots=True)
class ScratchTopologyResponse:
    request_id: str
    result: ScratchTopologyResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "ScratchTopologyResponse.request_id")
        if (self.result is None) == (self.error is None):
            raise ValueError(
                "ScratchTopologyResponse must carry exactly one of result/error"
            )
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("ScratchTopologyResponse.error must be an exact BridgeError")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": self.result is not None,
        }
        if self.result is not None:
            data["result"] = self.result.to_dict()
        else:
            data["error"] = self.error.to_dict()  # type: ignore[union-attr]
        return data

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ScratchTopologyResponse":
        envelope = _require_exact_dict(data, "ScratchTopologyResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope):
            raise ValueError("ScratchTopologyResponse envelope is missing required fields")
        if set(envelope) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}:
            raise ValueError("ScratchTopologyResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL or envelope["kind"] != "response":
            raise ValueError("ScratchTopologyResponse envelope is invalid")
        _require_exact_bool(envelope["ok"], "ScratchTopologyResponse.ok")
        if envelope["ok"] is True:
            if envelope.get("result") is None or envelope.get("error") is not None:
                raise ValueError("ScratchTopologyResponse ok=true requires result only")
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=ScratchTopologyResult.from_dict(envelope["result"]),  # type: ignore[arg-type]
                error=None,
            )
        if envelope.get("error") is None or envelope.get("result") is not None:
            raise ValueError("ScratchTopologyResponse ok=false requires error only")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(envelope["error"]),  # type: ignore[arg-type]
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


def parse_scratch_delete_request(raw: str | bytes) -> ScratchDeleteRequest:
    return ScratchDeleteRequest.from_dict(_load_strict_dict(raw, "Scratch delete request"))


def parse_scratch_delete_response(raw: str | bytes) -> ScratchDeleteResponse:
    return ScratchDeleteResponse.from_dict(_load_strict_dict(raw, "Scratch delete response"))


def parse_scratch_topology_request(raw: str | bytes) -> ScratchTopologyRequest:
    return ScratchTopologyRequest.from_dict(
        _load_strict_dict(raw, "Scratch topology request")
    )


def parse_scratch_topology_response(raw: str | bytes) -> ScratchTopologyResponse:
    return ScratchTopologyResponse.from_dict(
        _load_strict_dict(raw, "Scratch topology response")
    )
