"""Phase 1.7: ``scratch.exec`` Bridge DTO/contracts + client + coordinator tests.

Mirrors the structure of :mod:`tests.runtime.test_capture_bridge`:
- DTO strictness (frozen, exact fields, bounded counts/sizes, canonical JSON,
  duplicate-key rejection);
- request/response round-trip through ``parse_scratch_request`` /
  ``parse_scratch_response``;
- :meth:`BridgeClient.scratch_exec` driven through an injectable fake transport
  (no real socket, no ``hou``, no ``rpyc``); and
- the agent-facing :class:`ScratchCoordinator` + ``scratch_build`` tool:
  input validation, bounded summary, provider-failure fail-closed, and
  context gating.

Async scenarios run via ``asyncio.run`` (this suite deliberately avoids
pytest-asyncio, matching the rest of the Runtime tests).
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from eee_agent.core.errors import AgentError, AgentException, ErrorCategory
from eee_agent.houdini_bridge.auth import create_bridge_identity
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import PROTOCOL
from eee_agent.houdini_bridge.scratch import (
    SCRATCH_COMMIT_OPERATION,
    SCRATCH_DESTROY_OPERATION,
    SCRATCH_EXEC_OPERATION,
    SCRATCH_V1,
    SCRATCH_V2,
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
    parse_scratch_commit_request,
    parse_scratch_commit_response,
    parse_scratch_destroy_request,
    parse_scratch_destroy_response,
    parse_scratch_request,
    parse_scratch_response,
)
from eee_agent.modeling.scratch_coordinator import (
    ScratchCoordinator,
    ScratchError,
    ScratchSessionContext,
    ScratchToolContext,
    scratch_build,
    scratch_commit,
)
from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.models import canonical_json_dumps

_PROTO = PROTOCOL


# --------------------------------------------------------------------------
# test helpers
# --------------------------------------------------------------------------


def async_test(coro: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    @functools.wraps(coro)
    def wrapper() -> None:
        asyncio.run(coro())

    return wrapper


def _frame(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


def _dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ack_frame(ok: bool = True, caps: list[str] | None = None) -> bytes:
    ack: dict[str, object] = {"protocol": _PROTO, "kind": "hello", "ok": ok}
    if caps is not None:
        ack["capabilities"] = caps
    return _frame(_dumps(ack))


def _op_create(node_name: str = "box1", node_type: str = "box", parent: str = "") -> dict[str, object]:
    d: dict[str, object] = {"kind": "create_node", "node_name": node_name, "node_type": node_type}
    if parent:
        d["parent"] = parent
    return d


def _bridge_down_exc() -> AgentException:
    """A transport-level bridge-not-available failure (sandbox never reached)."""
    return AgentException(
        AgentError(
            code="bridge.not_available",
            category=ErrorCategory.HOUDINI_BRIDGE,
            message_for_user="The Houdini Bridge is not currently available.",
            retryable=True,
        )
    )


def _scratch_op_failed_exc() -> AgentException:
    """An operation-level failure raised after partial application."""
    return AgentException(
        AgentError(
            code="bridge.scratch_failed",
            category=ErrorCategory.HOUDINI_BRIDGE,
            message_for_user="scratch operation failed after 2 op(s): bad parm",
            retryable=True,
            scene_may_have_changed=True,
        )
    )


def _geometry_dict(
    *,
    points: int = 8,
    prims: int = 6,
    vertices: int = 24,
) -> dict[str, object]:
    return {
        "point_count": points,
        "prim_count": prims,
        "vertex_count": vertices,
        "bbox_min": [-1.0, -1.0, -1.0],
        "bbox_max": [1.0, 1.0, 1.0],
    }


def _result_dict(
    *,
    sandbox_root: str = "/obj/eee_scratch_run1",
    applied_ops: int = 1,
    output_node: str = "/obj/eee_scratch_run1/box1",
    errors: list[str] | None = None,
    geometry: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "sandbox_root": sandbox_root,
        "applied_ops": applied_ops,
        "output_node": output_node,
        "errors": errors if errors is not None else [],
        "geometry": geometry,
    }


# ==========================================================================
# ScratchOp DTO strictness
# ==========================================================================


class TestScratchOp:
    def test_create_node_round_trip(self) -> None:
        op = ScratchOp(kind="create_node", node_name="box1", node_type="box")
        d = op.to_dict()
        assert d == {"kind": "create_node", "node_name": "box1", "node_type": "box"}
        # from_dict must tolerate the omitted optional fields.
        assert ScratchOp.from_dict(d) == op

    def test_create_node_with_parent_round_trip(self) -> None:
        op = ScratchOp(kind="create_node", node_name="top", node_type="geo", parent="frame")
        d = op.to_dict()
        assert d["parent"] == "frame"
        assert ScratchOp.from_dict(d) == op

    def test_set_parm_round_trip(self) -> None:
        op = ScratchOp(kind="set_parm", node_name="box1", parm="sizex", value=120.0)
        d = op.to_dict()
        assert d == {"kind": "set_parm", "node_name": "box1", "parm": "sizex", "value": 120.0}
        assert ScratchOp.from_dict(d) == op

    def test_set_parm_list_value_round_trip(self) -> None:
        op = ScratchOp(kind="set_parm", node_name="t", parm="p", value=[1.0, 2.0, 3.0])
        assert ScratchOp.from_dict(op.to_dict()) == op

    def test_connect_round_trip(self) -> None:
        op = ScratchOp(
            kind="connect",
            node_name="merge1",
            input_index=0,
            source="box1",
            source_output_index=0,
        )
        d = op.to_dict()
        assert d == {
            "kind": "connect",
            "node_name": "merge1",
            "input_index": 0,
            "source": "box1",
            "source_output_index": 0,
        }
        assert ScratchOp.from_dict(d) == op

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(kind="move_node", node_name="x")

    def test_bad_node_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(kind="create_node", node_name="123bad", node_type="box")
        with pytest.raises(ValueError):
            ScratchOp(kind="create_node", node_name="has space", node_type="box")

    def test_create_node_requires_node_type(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(kind="create_node", node_name="x")

    def test_from_dict_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp.from_dict({"kind": "create_node", "node_name": "x", "bogus": 1})

    def test_from_dict_requires_kind_and_node_name(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp.from_dict({"kind": "create_node"})

    def test_frozen(self) -> None:
        import dataclasses

        op = ScratchOp(kind="create_node", node_name="x", node_type="box")
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.node_name = "y"  # type: ignore[misc]


# ==========================================================================
# ScratchExpr DTO strictness (scratch.v2 C1 typed parameter expressions)
# ==========================================================================


def _expr_dict() -> dict[str, object]:
    """mul(ref ../box1/sizex, 2.0) — the canonical smoke expression."""
    return {
        "kind": "op",
        "name": "mul",
        "args": [
            {"kind": "ref", "path": "../box1/sizex"},
            {"kind": "num", "value": 2.0},
        ],
    }


class TestScratchExpr:
    def test_num_round_trip(self) -> None:
        expr = ScratchExpr(kind="num", value=2.5)
        assert expr.to_dict() == {"kind": "num", "value": 2.5}
        assert ScratchExpr.from_dict(expr.to_dict()) == expr
        assert ScratchExpr.from_dict({"kind": "num", "value": 2}).value == 2

    def test_ref_round_trip_relative_and_absolute(self) -> None:
        for path in ("../ctrl/sizex", "ctrl/sizex", "sizex", "/obj/eee_scratch_r/box1/sizex"):
            expr = ScratchExpr(kind="ref", path=path)
            assert expr.to_dict() == {"kind": "ref", "path": path}
            assert ScratchExpr.from_dict(expr.to_dict()) == expr

    def test_nested_op_func_round_trip(self) -> None:
        expr = ScratchExpr(
            kind="func",
            name="clamp",
            args=(
                ScratchExpr(kind="op", name="add", args=(
                    ScratchExpr(kind="ref", path="../a/width"),
                    ScratchExpr(kind="num", value=1),
                )),
                ScratchExpr(kind="num", value=0.0),
                ScratchExpr(kind="func", name="sqrt", args=(
                    ScratchExpr(kind="num", value=16.0),
                )),
            ),
        )
        assert ScratchExpr.from_dict(expr.to_dict()) == expr

    def test_num_rejects_bool_nan_inf(self) -> None:
        for bad in (True, float("nan"), float("inf"), float("-inf")):
            with pytest.raises((TypeError, ValueError)):
                ScratchExpr(kind="num", value=bad)

    def test_ref_rejects_injection_and_file_path_chars(self) -> None:
        for bad in (
            "C:/temp/x.hip",          # ':' drive path
            "..\\ctrl\\sizex",        # backslash
            "$HIP/x",                 # variable expansion
            "`ls`",                   # backtick execution
            'ch("x")',                # quotes/parens
            "../ctrl/size x",         # whitespace
            "a;b",                    # statement separator
        ):
            with pytest.raises(ValueError):
                ScratchExpr(kind="ref", path=bad)

    def test_ref_rejects_malformed_paths(self) -> None:
        for bad in ("", "x" * 257, "../box1/", "../box1//sizex", "./sizex"):
            with pytest.raises(ValueError):
                ScratchExpr(kind="ref", path=bad)
        with pytest.raises(TypeError):
            ScratchExpr(kind="ref", path=1)  # type: ignore[arg-type]

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchExpr(kind="python", value=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ScratchExpr.from_dict({"kind": "python", "value": 1})

    def test_from_dict_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValueError):
            ScratchExpr.from_dict({"kind": "num", "value": 1.0, "bogus": 1})
        with pytest.raises(ValueError):
            ScratchExpr.from_dict({"kind": "ref", "path": "a/b", "name": "x"})

    def test_non_whitelist_func_rejected(self) -> None:
        for name in ("python", "eval", "exec", "ch", "opinputpath", "bbox"):
            with pytest.raises(ValueError):
                ScratchExpr(kind="func", name=name, args=(ScratchExpr(kind="num", value=1),))

    def test_arity_rules(self) -> None:
        num = ScratchExpr(kind="num", value=1.0)
        with pytest.raises(ValueError):
            ScratchExpr(kind="op", name="add", args=(num,))  # noqa: E501
        with pytest.raises(ValueError):
            ScratchExpr(kind="op", name="neg", args=(num, num))
        with pytest.raises(ValueError):
            ScratchExpr(kind="func", name="clamp", args=(num, num))
        with pytest.raises(ValueError):
            ScratchExpr(kind="func", name="pow", args=(num,))
        with pytest.raises(ValueError):
            ScratchExpr(kind="func", name="sqrt", args=(num, num))
        with pytest.raises(ValueError):
            ScratchExpr(kind="func", name="min", args=())
        with pytest.raises(ValueError):
            ScratchExpr(kind="func", name="max", args=(num,) * 5)
        # min/max accept 1..4 args
        ScratchExpr(kind="func", name="max", args=(num, num, num, num))

    def test_depth_bound(self) -> None:
        expr = ScratchExpr(kind="num", value=1.0)
        for _ in range(7):  # depth 8 total — allowed
            expr = ScratchExpr(kind="op", name="neg", args=(expr,))
        with pytest.raises(ValueError):
            ScratchExpr(kind="op", name="neg", args=(expr,))  # depth 9

    def test_node_count_bound(self) -> None:
        # Wide (not deep) trees: max() with 4 args grows the node count
        # without growing depth. A 21-node tree is allowed; 85 is not.
        def wide(children: tuple[ScratchExpr, ...]) -> ScratchExpr:
            return ScratchExpr(kind="func", name="max", args=children)

        nums = tuple(ScratchExpr(kind="num", value=i) for i in range(4))
        inner = tuple(wide(nums) for _ in range(4))  # 5 nodes each, depth 2
        tree = wide(inner)  # 21 nodes, depth 3 — allowed
        with pytest.raises(ValueError):
            wide((tree, tree, tree, tree))  # 85 nodes

    def test_args_must_be_exprs(self) -> None:
        with pytest.raises(TypeError):
            ScratchExpr(kind="op", name="add", args=(
                ScratchExpr(kind="num", value=1), 1.0,  # type: ignore[arg-type]
            ))
        with pytest.raises(TypeError):
            ScratchExpr.from_dict({"kind": "op", "name": "add", "args": [{"kind": "num", "value": 1}, 2]})

    def test_frozen(self) -> None:
        import dataclasses

        expr = ScratchExpr(kind="num", value=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            expr.kind = "ref"  # type: ignore[misc]


class TestScratchOpExpr:
    def test_set_parm_expr_round_trip(self) -> None:
        op = ScratchOp(
            kind="set_parm", node_name="box2", parm="sizex",
            expr=ScratchExpr.from_dict(_expr_dict()),
        )
        d = op.to_dict()
        assert d == {
            "kind": "set_parm", "node_name": "box2", "parm": "sizex",
            "expr": _expr_dict(),
        }
        assert "value" not in d
        assert ScratchOp.from_dict(d) == op

    def test_value_and_expr_conflict_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(
                kind="set_parm", node_name="box1", parm="sizex",
                value=1.0, expr=ScratchExpr(kind="num", value=2.0),
            )
        with pytest.raises(ValueError):
            ScratchOp.from_dict({
                "kind": "set_parm", "node_name": "box1", "parm": "sizex",
                "value": 1.0, "expr": {"kind": "num", "value": 2.0},
            })

    def test_neither_value_nor_expr_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(kind="set_parm", node_name="box1", parm="sizex")
        with pytest.raises(ValueError):
            ScratchOp.from_dict({
                "kind": "set_parm", "node_name": "box1", "parm": "sizex",
            })

    def test_expr_only_valid_for_set_parm(self) -> None:
        with pytest.raises(ValueError):
            ScratchOp(
                kind="create_node", node_name="box1", node_type="box",
                expr=ScratchExpr(kind="num", value=1.0),
            )

    def test_expr_must_be_typed(self) -> None:
        with pytest.raises(TypeError):
            ScratchOp(
                kind="set_parm", node_name="box1", parm="sizex",
                expr={"kind": "num", "value": 1.0},  # type: ignore[arg-type]
            )
        with pytest.raises((TypeError, ValueError)):
            ScratchOp.from_dict({
                "kind": "set_parm", "node_name": "box1", "parm": "sizex",
                "expr": "ch('../box1/sizex')",
            })

    def test_request_round_trip_with_expr_op(self) -> None:
        request = ScratchRequest.build(
            request_id="req_expr",
            deadline_ms=5000,
            scene_epoch=1,
            sandbox_id="run1",
            operations=(
                ScratchOp(kind="create_node", node_name="box1", node_type="box"),
                ScratchOp(
                    kind="set_parm", node_name="box2", parm="sizex",
                    expr=ScratchExpr.from_dict(_expr_dict()),
                ),
            ),
            purpose="expr round trip",
        )
        parsed = ScratchRequest.from_dict(request.to_dict())
        assert parsed.operations[1].expr == request.operations[1].expr



class TestScratchV2DtoExtensions:
    def test_delete_node_round_trip(self) -> None:
        op = ScratchOp(kind="delete_node", node_name="draft1")
        assert ScratchOp.from_dict(op.to_dict()) == op
        assert op.to_dict() == {"kind": "delete_node", "node_name": "draft1"}

    def test_create_note_is_bounded_and_kind_specific(self) -> None:
        op = ScratchOp(
            kind="create_node",
            node_name="box1",
            node_type="box",
            note="桌面粗模",
        )
        assert ScratchOp.from_dict(op.to_dict()).note == "桌面粗模"
        with pytest.raises(ValueError):
            ScratchOp(kind="delete_node", node_name="x", note="nope")
        with pytest.raises(ValueError):
            ScratchOp(
                kind="create_node",
                node_name="box1",
                node_type="box",
                note="x" * 201,
            )

    def test_purpose_is_required_and_bounded(self) -> None:
        base = {
            "request_id": "req_purpose",
            "deadline_ms": 5000,
            "scene_epoch": 1,
            "sandbox_id": "run1",
            "operations": (
                ScratchOp(kind="create_node", node_name="b", node_type="box"),
            ),
        }
        with pytest.raises(ValueError):
            ScratchRequest(**base, purpose="")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ScratchRequest(**base, purpose="x" * 201)  # type: ignore[arg-type]
        request = ScratchRequest(**base, purpose="创建桌腿")  # type: ignore[arg-type]
        assert ScratchRequest.from_dict(request.to_dict()).purpose == "创建桌腿"

    def test_annotations_and_warnings_round_trip(self) -> None:
        request = ScratchCommitRequest.build(
            request_id="req_annotations",
            deadline_ms=5000,
            scene_epoch=1,
            sandbox_id="run1",
            target_parent_path="/obj",
            target_name="table1",
            annotations={"box1": "桌面", "blast1": "最终输出"},
        )
        parsed = ScratchCommitRequest.from_dict(request.to_dict())
        assert dict(parsed.annotations) == {
            "blast1": "最终输出",
            "box1": "桌面",
        }
        result = ScratchCommitResult(
            committed=True,
            refused=False,
            final_path="/obj/table1",
            reason="",
            gates=(),
            receipt={"passed": True},
            warnings=("layout skipped: unavailable",),
        )
        assert ScratchCommitResult.from_dict(result.to_dict()).warnings == (
            "layout skipped: unavailable",
        )

    def test_annotations_reject_bad_key_and_value(self) -> None:
        common = {
            "request_id": "req_annotations_bad",
            "deadline_ms": 5000,
            "scene_epoch": 1,
            "sandbox_id": "run1",
            "target_parent_path": "/obj",
            "target_name": "table1",
        }
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                **common, annotations={"not a name!": "x"}  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                **common, annotations={"box1": "x" * 501}  # type: ignore[arg-type]
            )


# ==========================================================================
# ScratchGeometry DTO
# ==========================================================================


class TestScratchGeometry:
    def test_round_trip(self) -> None:
        geo = ScratchGeometry(
            point_count=8, prim_count=6, vertex_count=24,
            bbox_min=(0.0, 0.0, 0.0), bbox_max=(2.0, 2.0, 2.0),
        )
        d = geo.to_dict()
        assert ScratchGeometry.from_dict(d) == geo
        assert d["bbox_min"] == [0.0, 0.0, 0.0]

    def test_negative_counts_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchGeometry(-1, 0, 0, (0, 0, 0), (1, 1, 1))

    def test_wrong_bbox_arity_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchGeometry(1, 1, 1, (0, 0), (1, 1, 1))

    def test_non_numeric_bbox_rejected(self) -> None:
        with pytest.raises(TypeError):
            ScratchGeometry(1, 1, 1, ("a", 0, 0), (1, 1, 1))


# ==========================================================================
# ScratchRequest DTO
# ==========================================================================


class TestScratchRequest:
    def _req(self, **overrides: object) -> ScratchRequest:
        kwargs: dict[str, object] = {
            "request_id": "req_scratch_001",
            "deadline_ms": 5000,
            "scene_epoch": 42,
            "sandbox_id": "run1",
            "operations": (ScratchOp(kind="create_node", node_name="box1", node_type="box"),),
            "purpose": "build the tabletop",
            "preserve_on_failure": True,
        }
        kwargs.update(overrides)
        return ScratchRequest(**kwargs)  # type: ignore[arg-type]

    def test_container_path(self) -> None:
        req = self._req(sandbox_id="run_abc")
        assert req.container_name == "eee_scratch_run_abc"
        assert req.container_path == "/obj/eee_scratch_run_abc"

    def test_round_trip(self) -> None:
        req = self._req()
        parsed = parse_scratch_request(req.to_json())
        assert parsed == req
        assert parsed.operations[0].kind == "create_node"

    def test_to_dict_shape(self) -> None:
        req = self._req()
        d = req.to_dict()
        assert d["protocol"] == _PROTO
        assert d["kind"] == "request"
        assert d["operation"] == SCRATCH_EXEC_OPERATION
        assert d["payload"]["sandbox_id"] == "run1"
        assert d["payload"]["preserve_on_failure"] is True

    def test_empty_operations_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._req(operations=())

    def test_too_many_operations_rejected(self) -> None:
        ops = tuple(
            ScratchOp(kind="create_node", node_name=f"n{i}", node_type="box")
            for i in range(65)
        )
        with pytest.raises(ValueError):
            self._req(operations=ops)

    def test_bad_sandbox_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._req(sandbox_id="has space")
        with pytest.raises(ValueError):
            self._req(sandbox_id="")

    def test_deadline_bounds(self) -> None:
        with pytest.raises(ValueError):
            self._req(deadline_ms=0)
        with pytest.raises(ValueError):
            self._req(deadline_ms=30_001)

    def test_scene_epoch_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            self._req(scene_epoch=0)

    def test_non_scratchop_operations_rejected(self) -> None:
        with pytest.raises(TypeError):
            self._req(operations=("not_an_op",))  # type: ignore[arg-type]

    def test_canonical_json_is_sorted(self) -> None:
        req = self._req()
        # canonical_json_dumps produces sorted, compact JSON.
        assert req.to_json() == canonical_json_dumps(req.to_dict())


# ==========================================================================
# ScratchResponse / ScratchResult DTO
# ==========================================================================


class TestScratchResult:
    def test_round_trip_with_geometry(self) -> None:
        result = ScratchResult(
            sandbox_root="/obj/eee_scratch_run1",
            applied_ops=2,
            output_node="/obj/eee_scratch_run1/box1",
            errors=(),
            geometry=ScratchGeometry(8, 6, 24, (0, 0, 0), (1, 1, 1)),
        )
        parsed = ScratchResult.from_dict(result.to_dict())
        assert parsed == result
        assert parsed.geometry is not None
        assert parsed.geometry.prim_count == 6

    def test_round_trip_without_geometry(self) -> None:
        result = ScratchResult(
            sandbox_root="/obj/eee_scratch_run1",
            applied_ops=0,
            output_node="/obj/eee_scratch_run1",
            errors=("cook failed",),
            geometry=None,
        )
        parsed = ScratchResult.from_dict(result.to_dict())
        assert parsed == result
        assert parsed.geometry is None

    def test_bad_sandbox_root_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchResult("/obj_bad", 1, "out", (), None)

    def test_negative_applied_ops_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchResult("/obj/eee_scratch_run1", -1, "out", (), None)


class TestScratchResponse:
    def _envelope(self, *, ok: bool, result: dict | None = None, error: dict | None = None) -> dict[str, object]:
        env: dict[str, object] = {
            "protocol": _PROTO,
            "kind": "response",
            "request_id": "req_scratch_001",
            "ok": ok,
        }
        if result is not None:
            env["result"] = result
        if error is not None:
            env["error"] = error
        return env

    def test_success_round_trip(self) -> None:
        env = self._envelope(ok=True, result=_result_dict(geometry=_geometry_dict()))
        resp = parse_scratch_response(_dumps(env))
        assert resp.result is not None
        assert resp.result.applied_ops == 1
        assert resp.error is None

    def test_error_round_trip(self) -> None:
        error = {
            "code": "bridge.write_frozen",
            "category": "houdini_bridge",
            "message_for_user": "writes are frozen",
            "retryable": True,
            "technical_detail_ref": "td_001",
        }
        env = self._envelope(ok=False, error=error)
        resp = parse_scratch_response(_dumps(env))
        assert resp.result is None
        assert resp.error is not None
        assert resp.error.code == "bridge.write_frozen"

    def test_ok_true_without_result_rejected(self) -> None:
        env = self._envelope(ok=True)
        with pytest.raises(ValueError):
            parse_scratch_response(_dumps(env))

    def test_ok_false_without_error_rejected(self) -> None:
        env = self._envelope(ok=False)
        with pytest.raises(ValueError):
            parse_scratch_response(_dumps(env))

    def test_both_result_and_error_rejected(self) -> None:
        env = self._envelope(
            ok=True,
            result=_result_dict(),
            error={"code": "x", "category": "houdini_bridge", "message_for_user": "m", "retryable": False, "technical_detail_ref": "t"},
        )
        with pytest.raises(ValueError):
            parse_scratch_response(_dumps(env))


# ==========================================================================
# BridgeClient.scratch_exec — capability gating + happy path + error path
#
# Mirrors test_capture_bridge: real BridgeClient + in-memory FakeTransport
# (read_exactly/write/close), inbox pre-loaded with hello-ack + response frame.
# ==========================================================================


class FakeTransport:
    """In-memory framed transport (mirrors the capture-bridge test fake)."""

    def __init__(self, inbox: bytes = b"") -> None:
        self._inbox = bytearray(inbox)
        self.outbox = bytearray()
        self.close_count = 0

    async def read_exactly(self, n: int) -> bytes:
        while len(self._inbox) < n:
            partial = bytes(self._inbox)
            self._inbox.clear()
            raise asyncio.IncompleteReadError(partial, n)
        chunk = bytes(self._inbox[:n])
        del self._inbox[:n]
        return chunk

    async def write(self, data: bytes) -> None:
        self.outbox.extend(data)

    async def close(self) -> None:
        self.close_count += 1


def _client(fake: FakeTransport) -> BridgeClient:
    return BridgeClient(
        host="127.0.0.1",
        port=18811,
        identity=create_bridge_identity(),
        transport_factory=lambda: fake,
    )


def _parse_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    i = 0
    while i + 4 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        start = i + 4
        frames.append(bytes(data[start : start + length]))
        i = start + length
    return frames


def _result_envelope_frame(
    *,
    request_id: str = "req_scratch_001",
    result: dict[str, object] | None = None,
) -> bytes:
    if result is None:
        result = _result_dict()
    env = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": result,
    }
    return _frame(_dumps(env))


def _error_envelope_frame(
    *,
    request_id: str = "req_scratch_001",
    code: str = "bridge.write_frozen",
) -> bytes:
    env = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": code,
            "category": "houdini_bridge",
            "message_for_user": "frozen",
            "retryable": True,
            "technical_detail_ref": "td",
        },
    }
    return _frame(_dumps(env))


def _scratch_request() -> ScratchRequest:
    return ScratchRequest(
        request_id="req_scratch_001",
        deadline_ms=5000,
        scene_epoch=42,
        sandbox_id="run1",
        operations=(ScratchOp(kind="create_node", node_name="box1", node_type="box"),),
        purpose="build the tabletop",
    )


@async_test
async def test_scratch_without_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["changeset.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_exec(_scratch_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert exc.value.retryable is False
    # Only the hello frame was sent; the gated request never hit the wire.
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_round_trip_returns_typed_result() -> None:
    result_dict = _result_dict(
        applied_ops=3,
        output_node="/obj/eee_scratch_run1/extrude1",
        geometry=_geometry_dict(points=32, prims=28),
    )
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["changeset.v1", "scratch.v1"])
        + _result_envelope_frame(result=result_dict)
    )
    client = _client(fake)
    await client.open()
    resp = await client.scratch_exec(_scratch_request())
    assert type(resp) is ScratchResult
    assert resp.applied_ops == 3
    assert resp.geometry is not None
    assert resp.geometry.prim_count == 28


@async_test
async def test_scratch_server_error_raised_as_client_error() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SCRATCH_V1])
        + _error_envelope_frame(code="bridge.write_frozen")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_exec(_scratch_request())
    assert exc.value.code == "bridge.write_frozen"
    assert exc.value.retryable is True


@async_test
async def test_scratch_wrong_request_id_aborts() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SCRATCH_V1])
        + _result_envelope_frame(request_id="other_id")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_exec(_scratch_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_scratch_malformed_response_aborts() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SCRATCH_V1]) + _frame("not json")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_exec(_scratch_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_scratch_sent_frame_carries_scratch_operation() -> None:
    result_dict = _result_dict()
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SCRATCH_V1])
        + _result_envelope_frame(result=result_dict)
    )
    client = _client(fake)
    await client.open()
    await client.scratch_exec(_scratch_request())
    frames = _parse_frames(bytes(fake.outbox))
    # frame 0 is hello, frame 1 is the scratch request
    assert len(frames) == 2
    sent_obj = json.loads(frames[1])
    assert sent_obj["operation"] == SCRATCH_EXEC_OPERATION
    assert sent_obj["payload"]["sandbox_id"] == "run1"


def test_scratch_requires_typed_request_and_open_client() -> None:
    async def run() -> None:
        fake = FakeTransport(inbox=_ack_frame(ok=True, caps=[SCRATCH_V1]))
        client = _client(fake)
        await client.open()
        with pytest.raises(TypeError):
            await client.scratch_exec("not a request")  # type: ignore[arg-type]
        with pytest.raises(BridgeClientError):
            unopened = _client(FakeTransport(inbox=_ack_frame(ok=True, caps=[SCRATCH_V1])))
            await unopened.scratch_exec(_scratch_request())

    asyncio.run(run())


# ==========================================================================
# ScratchCoordinator — input validation, bounded summary, fail-closed
# ==========================================================================


class _FakeScratchProvider:
    """Records the last scratch_exec call; returns a scripted ScratchResult or raises."""

    def __init__(
        self,
        *,
        result: ScratchResult | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._result = result
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def scratch_exec(
        self,
        *,
        sandbox_id: str,
        operations: tuple[ScratchOp, ...],
        purpose: str,
        preserve_on_failure: bool = True,
    ) -> ScratchResult:
        self.calls.append(
            {
                "sandbox_id": sandbox_id,
                "operations": operations,
                "purpose": purpose,
                "preserve_on_failure": preserve_on_failure,
            }
        )
        if self._raise is not None:
            raise self._raise
        if self._result is None:
            return ScratchResult(
                sandbox_root=f"/obj/eee_scratch_{sandbox_id}",
                applied_ops=len(operations),
                output_node=f"/obj/eee_scratch_{sandbox_id}/out",
                errors=(),
                geometry=ScratchGeometry(1, 1, 1, (0, 0, 0), (1, 1, 1)),
            )
        return self._result

    async def scratch_commit(
        self,
        *,
        sandbox_id: str,
        target_parent_path: str,
        target_name: str,
        orientation_checks: tuple = (),
        skip_structure_check: bool = False,
        annotations: tuple[tuple[str, str], ...] = (),
    ) -> ScratchCommitResult:
        # Minimal stub so the provider satisfies the ScratchProvider Protocol.
        # Commit-specific behavior is tested via the dedicated commit tests.
        return ScratchCommitResult(
            committed=True,
            refused=False,
            final_path=f"{target_parent_path}/{target_name}",
            reason="",
            gates=({"gate": "health", "passed": True, "hard": True, "reason": "", "detail": {}},),
            receipt={"passed": True, "orientation": {"passed": 0, "failed": 0, "total": 0}, "health": {"hard_errors_count": 0, "soft_warnings_count": 0}},
        )

    async def scratch_destroy(self, *, sandbox_id: str) -> ScratchDestroyResult:
        # Minimal stub so the provider satisfies the ScratchProvider Protocol.
        return ScratchDestroyResult(destroyed_paths=(), missing=True)


def _coordinator(provider: _FakeScratchProvider, *, sandbox_id: str = "run1") -> ScratchCoordinator:
    ctx = ScratchSessionContext(provider=provider, sandbox_id=sandbox_id)
    return ScratchCoordinator(ctx)


class TestScratchCoordinator:
    def test_parse_operations_accepts_typed_expr(self) -> None:
        coord = _coordinator(_FakeScratchProvider())
        ops = coord._parse_operations([
            {"kind": "create_node", "node_name": "box1", "node_type": "box"},
            {
                "kind": "set_parm", "node_name": "box2", "parm": "sizex",
                "expr": _expr_dict(),
            },
        ])
        assert ops[1].expr == ScratchExpr.from_dict(_expr_dict())
        assert ops[1].value is None

    def test_parse_operations_rejects_invalid_expr(self) -> None:
        coord = _coordinator(_FakeScratchProvider())
        bad_exprs = (
            {"kind": "func", "name": "python", "args": [{"kind": "num", "value": 1}]},
            {"kind": "ref", "path": "$HIP/file.hip"},
            {"kind": "num", "value": 1.0, "bogus": 1},
        )
        for bad in bad_exprs:
            with pytest.raises(ScratchError):
                coord._parse_operations([
                    {"kind": "set_parm", "node_name": "box1", "parm": "sizex", "expr": bad},
                ])
        # value/expr conflict is also a bounded input error
        with pytest.raises(ScratchError):
            coord._parse_operations([
                {
                    "kind": "set_parm", "node_name": "box1", "parm": "sizex",
                    "value": 1.0, "expr": _expr_dict(),
                },
            ])

    def test_happy_path_summarizes_result(self) -> None:
        provider = _FakeScratchProvider(
            result=ScratchResult(
                sandbox_root="/obj/eee_scratch_run1",
                applied_ops=2,
                output_node="/obj/eee_scratch_run1/box1",
                errors=(),
                geometry=ScratchGeometry(8, 6, 24, (-1, -1, -1), (1, 1, 1)),
            )
        )
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(
                purpose="build test geometry",
                operations=[
                    {"kind": "create_node", "node_name": "box1", "node_type": "box"},
                    {"kind": "set_parm", "node_name": "box1", "parm": "sizex", "value": 2.0},
                ]
            )

        result = asyncio.run(run())
        assert result["ok"] is True
        assert result["applied_ops"] == 2
        assert result["sandbox_root"] == "/obj/eee_scratch_run1"
        assert result["geometry"]["prim_count"] == 6
        assert result["errors"] == []
        # The provider received the typed ops.
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["sandbox_id"] == "run1"
        assert call["preserve_on_failure"] is True
        assert len(call["operations"]) == 2
        assert call["operations"][0].kind == "create_node"

    def test_preserve_on_failure_forwarded(self) -> None:
        provider = _FakeScratchProvider()
        coord = _coordinator(provider)

        async def run() -> None:
            await coord.build(
                purpose="test failure cleanup",
                operations=[_op_create()],
                preserve_on_failure=False,
            )

        asyncio.run(run())
        assert provider.calls[0]["preserve_on_failure"] is False

    def test_empty_operations_rejected(self) -> None:
        provider = _FakeScratchProvider()
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(purpose="test empty", operations=[])

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"
        assert provider.calls == []

    def test_non_list_operations_rejected(self) -> None:
        provider = _FakeScratchProvider()
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(  # type: ignore[arg-type]
                purpose="test invalid", operations="not a list"
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"

    def test_too_many_operations_rejected(self) -> None:
        provider = _FakeScratchProvider()
        coord = _coordinator(provider)
        ops = [_op_create(node_name=f"n{i}") for i in range(65)]

        async def run() -> dict[str, object]:
            return await coord.build(purpose="test bound", operations=ops)

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"
        assert provider.calls == []

    def test_malformed_op_rejected(self) -> None:
        provider = _FakeScratchProvider()
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(
                purpose="test malformed op",
                operations=[{"kind": "create_node", "node_name": "bad name", "node_type": "box"}]
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"
        assert provider.calls == []

    def test_provider_exception_fail_closed(self) -> None:
        # Transport-level failure: bridge unreachable, sandbox never modified.
        provider = _FakeScratchProvider(raise_exc=_bridge_down_exc())
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(
                purpose="test bridge failure", operations=[_op_create()]
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.bridge_unavailable"

    def test_provider_op_failure_reported_honestly(self) -> None:
        # Operation-level failure: an op raised after partial application.
        # Must NOT read as success and must surface the executor's message.
        provider = _FakeScratchProvider(raise_exc=_scratch_op_failed_exc())
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(
                purpose="test operation failure", operations=[_op_create()]
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.op_failed"
        assert "failed after 2 op(s)" in result["message"]

    def test_no_geometry_in_summary(self) -> None:
        provider = _FakeScratchProvider(
            result=ScratchResult(
                sandbox_root="/obj/eee_scratch_run1",
                applied_ops=0,
                output_node="/obj/eee_scratch_run1",
                errors=("node not found",),
                geometry=None,
            )
        )
        coord = _coordinator(provider)

        async def run() -> dict[str, object]:
            return await coord.build(
                purpose="test missing geometry", operations=[_op_create()]
            )

        result = asyncio.run(run())
        # M2: a result with per-op errors reports ok=False (partial failure is
        # not a clean success), while geometry is still surfaced as None.
        assert result["ok"] is False
        assert result["geometry"] is None
        assert result["errors"] == ["node not found"]


# ==========================================================================
# scratch_build tool — context gating + input validation + delegation
# ==========================================================================


def _runtime_with_context(context: object | None) -> SimpleNamespace:
    return SimpleNamespace(context=context)


def _read_only_provider() -> Any:
    class _RO:
        async def scene_status(self) -> dict[str, object]:
            return {"ok": True}

        async def query_scene(self, node_paths: list[str]) -> dict[str, object]:
            return {"ok": True}

        async def inspect_workspace(self, workspace_id: str) -> dict[str, object]:
            return {"ok": True}

        async def geometry_stats(self, node_path: str) -> dict[str, object]:
            return {"ok": True}

        async def work_status(self, workspace_id: str) -> dict[str, object]:
            return {"ok": True}

    return _RO()


class _FakeKnowledge:
    def search(self, query: str, *, limit: int = 5) -> dict[str, object]:
        return {"ok": True, "results": []}

    def get(self, entity_id: str, *, max_body_bytes: int = 8_000) -> dict[str, object]:
        return {"ok": True, "entity_id": entity_id}


class TestScratchBuildTool:
    def _ctx(self, provider: _FakeScratchProvider) -> RuntimeToolContext:
        scratch = ScratchToolContext(_coordinator(provider))
        return RuntimeToolContext(
            read_only=_read_only_provider(),
            knowledge=_FakeKnowledge(),
            scratch=scratch,
        )

    def test_no_context_fails_closed(self) -> None:
        async def run() -> dict[str, object]:
            return await scratch_build.coroutine(
                purpose="test missing context",
                operations=[_op_create()],
                runtime=_runtime_with_context(None),
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.context_invalid"

    def test_wrong_context_type_fails_closed(self) -> None:
        async def run() -> dict[str, object]:
            return await scratch_build.coroutine(
                purpose="test wrong context",
                operations=[_op_create()],
                runtime=_runtime_with_context("not a context"),
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.context_invalid"

    def test_no_scratch_slot_fails_closed(self) -> None:
        ctx = RuntimeToolContext(
            read_only=_read_only_provider(), knowledge=_FakeKnowledge()
        )

        async def run() -> dict[str, object]:
            return await scratch_build.coroutine(
                purpose="test missing scratch",
                operations=[_op_create()],
                runtime=_runtime_with_context(ctx),
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.context_invalid"

    def test_bad_preserve_on_failure_type_rejected(self) -> None:
        provider = _FakeScratchProvider()

        async def run() -> dict[str, object]:
            return await scratch_build.coroutine(
                purpose="test bad preserve",
                operations=[_op_create()],
                runtime=_runtime_with_context(self._ctx(provider)),
                preserve_on_failure="yes",  # type: ignore[arg-type]
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"

    def test_delegates_to_coordinator(self) -> None:
        provider = _FakeScratchProvider(
            result=ScratchResult(
                sandbox_root="/obj/eee_scratch_run1",
                applied_ops=1,
                output_node="/obj/eee_scratch_run1/box1",
                errors=(),
                geometry=ScratchGeometry(8, 6, 24, (0, 0, 0), (2, 2, 2)),
            )
        )

        async def run() -> dict[str, object]:
            return await scratch_build.coroutine(
                purpose="build one box",
                operations=[_op_create()],
                runtime=_runtime_with_context(self._ctx(provider)),
            )

        result = asyncio.run(run())
        assert result["ok"] is True
        assert result["output_node"] == "/obj/eee_scratch_run1/box1"
        assert result["geometry"]["bbox_max"] == [2.0, 2.0, 2.0]
        assert len(provider.calls) == 1


# ==========================================================================
# ScratchSessionContext validation
# ==========================================================================


class TestScratchSessionContext:
    def test_valid(self) -> None:
        ctx = ScratchSessionContext(provider=_FakeScratchProvider(), sandbox_id="run1")
        assert ctx.sandbox_id == "run1"

    def test_none_provider_rejected(self) -> None:
        with pytest.raises(TypeError):
            ScratchSessionContext(provider=None, sandbox_id="run1")  # type: ignore[arg-type]

    def test_empty_sandbox_id_rejected(self) -> None:
        with pytest.raises(TypeError):
            ScratchSessionContext(provider=_FakeScratchProvider(), sandbox_id="")

    def test_coordinator_rejects_wrong_context_type(self) -> None:
        with pytest.raises(TypeError):
            ScratchCoordinator("not a context")  # type: ignore[arg-type]


# ==========================================================================
# Phase 2.3: scratch.commit DTO round-trips
# ==========================================================================


def _commit_result_dict(
    *,
    committed: bool = True,
    refused: bool = False,
    final_path: str = "/obj/my_asset",
    reason: str = "",
) -> dict[str, object]:
    return {
        "committed": committed,
        "refused": refused,
        "final_path": final_path,
        "reason": reason,
        "gates": [
            {"gate": "bake", "passed": True, "hard": True, "reason": "", "detail": {}},
            {"gate": "structure", "passed": True, "hard": True, "reason": "", "detail": {}},
            {"gate": "orientation", "passed": True, "hard": True, "reason": "", "detail": {}},
            {"gate": "health", "passed": True, "hard": True, "reason": "", "detail": {}},
        ],
        "receipt": {
            "passed": True,
            "orientation": {"passed": 1, "failed": 0, "total": 1},
            "health": {"hard_errors_count": 0, "soft_warnings_count": 0},
        },
        "warnings": [],
    }


def _commit_result_envelope_frame(
    *,
    request_id: str = "req_commit_001",
    result: dict[str, object] | None = None,
) -> bytes:
    if result is None:
        result = _commit_result_dict()
    env = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": result,
    }
    return _frame(_dumps(env))


class TestScratchCommitRequest:
    def test_round_trip(self) -> None:
        req = ScratchCommitRequest.build(
            request_id="req_commit_001",
            deadline_ms=10000,
            scene_epoch=42,
            sandbox_id="run1",
            target_parent_path="/obj",
            target_name="my_asset",
            orientation_checks=[
                {"component_id": "wheel", "kind": "radial", "expected_axis": "Y"}
            ],
        )
        parsed = parse_scratch_commit_request(req.to_json())
        assert parsed == req

    def test_to_dict_shape(self) -> None:
        req = ScratchCommitRequest.build(
            request_id="req_commit_001",
            deadline_ms=10000,
            scene_epoch=42,
            sandbox_id="run1",
            target_parent_path="/obj",
            target_name="my_asset",
        )
        d = req.to_dict()
        assert d["operation"] == SCRATCH_COMMIT_OPERATION
        assert d["payload"]["target_name"] == "my_asset"
        assert d["payload"]["skip_structure_check"] is False

    def test_container_path(self) -> None:
        req = ScratchCommitRequest.build(
            request_id="req_commit_001",
            deadline_ms=10000,
            scene_epoch=42,
            sandbox_id="run_abc",
            target_parent_path="/obj",
            target_name="asset",
        )
        assert req.container_path == "/obj/eee_scratch_run_abc"

    def test_bad_target_parent_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                request_id="req_commit_001",
                deadline_ms=10000,
                scene_epoch=42,
                sandbox_id="run1",
                target_parent_path="obj",  # not absolute
                target_name="asset",
            )

    def test_bad_target_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                request_id="req_commit_001",
                deadline_ms=10000,
                scene_epoch=42,
                sandbox_id="run1",
                target_parent_path="/obj",
                target_name="123bad",  # invalid node name
            )

    def test_empty_checks_allowed(self) -> None:
        req = ScratchCommitRequest.build(
            request_id="req_commit_001",
            deadline_ms=10000,
            scene_epoch=42,
            sandbox_id="run1",
            target_parent_path="/obj",
            target_name="asset",
        )
        assert req.orientation_checks == ()

    def test_unknown_orientation_check_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchCommitRequest.build(
                request_id="req_commit_001",
                deadline_ms=10000,
                scene_epoch=42,
                sandbox_id="run1",
                target_parent_path="/obj",
                target_name="asset",
                orientation_checks=[
                    {"component_id": "wheel", "expected_axe": "Y"}  # typo
                ],
            )

    def test_skip_structure_flag_round_trips(self) -> None:
        req = ScratchCommitRequest.build(
            request_id="req_commit_001",
            deadline_ms=10000,
            scene_epoch=42,
            sandbox_id="run1",
            target_parent_path="/obj",
            target_name="asset",
            skip_structure_check=True,
        )
        assert req.skip_structure_check is True
        assert parse_scratch_commit_request(req.to_json()).skip_structure_check is True


class TestScratchCommitResult:
    def test_committed_round_trip(self) -> None:
        result = ScratchCommitResult(
            committed=True, refused=False, final_path="/obj/my_asset", reason="",
            gates=({"gate": "health", "passed": True, "hard": True, "reason": "", "detail": {}},),
            receipt={"passed": True, "orientation": {"passed": 0, "failed": 0, "total": 0}},
        )
        assert ScratchCommitResult.from_dict(result.to_dict()) == result

    def test_refused_round_trip(self) -> None:
        result = ScratchCommitResult(
            committed=False, refused=True, final_path="/obj/eee_scratch_run1",
            reason="health: orphan_points count 3",
            gates=[{"gate": "health", "passed": False, "hard": True, "reason": "orphan_points", "detail": {}}],
            receipt={"passed": False, "orientation": {"passed": 0, "failed": 0, "total": 0}},
        )
        parsed = ScratchCommitResult.from_dict(result.to_dict())
        assert parsed.refused is True
        assert parsed.committed is False

    def test_both_committed_and_refused_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchCommitResult(
                committed=True, refused=True, final_path="/x", reason="",
                gates=(), receipt={},
            )

    def test_neither_committed_nor_refused_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchCommitResult(
                committed=False, refused=False, final_path="/x", reason="",
                gates=(), receipt={},
            )


class TestScratchCommitResponse:
    def test_success_round_trip(self) -> None:
        env = {
            "protocol": _PROTO, "kind": "response",
            "request_id": "req_commit_001", "ok": True,
            "result": _commit_result_dict(),
        }
        resp = parse_scratch_commit_response(_dumps(env))
        assert resp.result is not None
        assert resp.result.committed is True
        assert resp.error is None

    def test_error_round_trip(self) -> None:
        env = {
            "protocol": _PROTO, "kind": "response",
            "request_id": "req_commit_001", "ok": False,
            "error": {"code": "bridge.write_frozen", "category": "houdini_bridge",
                      "message_for_user": "frozen", "retryable": False,
                      "technical_detail_ref": "td"},
        }
        resp = parse_scratch_commit_response(_dumps(env))
        assert resp.result is None
        assert resp.error is not None
        assert resp.error.code == "bridge.write_frozen"

    def test_ok_true_without_result_rejected(self) -> None:
        env = {"protocol": _PROTO, "kind": "response",
               "request_id": "r", "ok": True}
        with pytest.raises(ValueError):
            parse_scratch_commit_response(_dumps(env))


# ==========================================================================
# Phase 2.3: BridgeClient.scratch_commit flow
# ==========================================================================


def _scratch_commit_request() -> ScratchCommitRequest:
    return ScratchCommitRequest.build(
        request_id="req_commit_001",
        deadline_ms=10000,
        scene_epoch=42,
        sandbox_id="run1",
        target_parent_path="/obj",
        target_name="my_asset",
    )


@async_test
async def test_scratch_commit_without_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["changeset.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_commit(_scratch_commit_request())
    assert exc.value.code == "bridge.capability_unavailable"


@async_test
async def test_scratch_commit_round_trip_returns_committed() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["changeset.v1", "scratch.v1"])
        + _commit_result_envelope_frame()
    )
    client = _client(fake)
    await client.open()
    result = await client.scratch_commit(_scratch_commit_request())
    assert type(result) is ScratchCommitResult
    assert result.committed is True
    assert result.refused is False
    assert result.final_path == "/obj/my_asset"


@async_test
async def test_scratch_commit_refused_result_returned() -> None:
    refused_dict = _commit_result_dict(
        committed=False, refused=True,
        final_path="/obj/eee_scratch_run1", reason="health failed",
    )
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["changeset.v1", "scratch.v1"])
        + _commit_result_envelope_frame(result=refused_dict)
    )
    client = _client(fake)
    await client.open()
    result = await client.scratch_commit(_scratch_commit_request())
    assert result.committed is False
    assert result.refused is True


@async_test
async def test_scratch_commit_server_error_raised() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["scratch.v1"])
        + _error_envelope_frame(request_id="req_commit_001", code="bridge.write_frozen")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_commit(_scratch_commit_request())
    assert exc.value.code == "bridge.write_frozen"


@async_test
async def test_scratch_commit_wrong_request_id_aborts() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["scratch.v1"])
        + _commit_result_envelope_frame(request_id="other_id")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_commit(_scratch_commit_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_scratch_commit_sent_frame_carries_commit_operation() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["changeset.v1", "scratch.v1"])
        + _commit_result_envelope_frame()
    )
    client = _client(fake)
    await client.open()
    await client.scratch_commit(_scratch_commit_request())
    frames = _parse_frames(bytes(fake.outbox))
    assert len(frames) == 2  # hello + commit request
    sent_obj = json.loads(frames[1])
    assert sent_obj["operation"] == SCRATCH_COMMIT_OPERATION
    assert sent_obj["payload"]["target_name"] == "my_asset"


# ==========================================================================
# Phase 2.3: ScratchCoordinator.commit
# ==========================================================================


class _CommitFakeProvider:
    """Provider whose scratch_commit returns a scripted result or raises."""

    def __init__(
        self,
        *,
        committed: bool = True,
        refused: bool = False,
        reason: str = "",
        raise_exc: Exception | None = None,
    ) -> None:
        self._committed = committed
        self._refused = refused
        self._reason = reason
        self._raise = raise_exc
        self.commit_calls: list[dict[str, Any]] = []

    async def scratch_exec(self, **kwargs):  # pragma: no cover - unused here
        raise RuntimeError("not used in commit tests")

    async def scratch_destroy(self, *, sandbox_id: str):  # pragma: no cover - unused here
        from eee_agent.houdini_bridge.scratch import ScratchDestroyResult
        return ScratchDestroyResult(destroyed_paths=(), missing=True)

    async def scratch_commit(
        self,
        *,
        sandbox_id: str,
        target_parent_path: str,
        target_name: str,
        orientation_checks: tuple = (),
        skip_structure_check: bool = False,
        # Mirrors the real ScratchProvider contract: a Mapping, NOT the
        # coordinator's internal pair-tuple.
        annotations: Mapping[str, str] | None = None,
    ) -> ScratchCommitResult:
        self.commit_calls.append({
            "sandbox_id": sandbox_id,
            "target_parent_path": target_parent_path,
            "target_name": target_name,
            "orientation_checks": orientation_checks,
            "skip_structure_check": skip_structure_check,
            "annotations": annotations,
        })
        if self._raise is not None:
            raise self._raise
        return ScratchCommitResult(
            committed=self._committed,
            refused=self._refused,
            final_path=f"{target_parent_path}/{target_name}" if self._committed else f"/obj/eee_scratch_{sandbox_id}",
            reason=self._reason,
            gates=({"gate": "health", "passed": self._committed, "hard": True, "reason": self._reason, "detail": {}},),
            receipt={"passed": self._committed, "orientation": {"passed": 0, "failed": 0, "total": 0}, "health": {"hard_errors_count": 0, "soft_warnings_count": 0}},
        )


class TestScratchCoordinatorCommit:
    def test_committed_summarizes_result(self) -> None:
        provider = _CommitFakeProvider(committed=True)
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(
                target_parent_path="/obj", target_name="my_asset"
            )

        result = asyncio.run(run())
        assert result["ok"] is True
        assert result["committed"] is True
        assert result["refused"] is False
        assert result["final_path"] == "/obj/my_asset"
        assert list(result).index("receipt") < list(result).index("gates")
        assert len(provider.commit_calls) == 1
        assert provider.commit_calls[0]["target_name"] == "my_asset"

    def test_refused_still_ok_true(self) -> None:
        provider = _CommitFakeProvider(committed=False, refused=True, reason="health failed")
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(target_parent_path="/obj", target_name="my_asset")

        result = asyncio.run(run())
        assert result["ok"] is True  # reached the bridge
        assert result["committed"] is False
        assert result["refused"] is True
        assert result["reason"] == "health failed"

    def test_provider_exception_fail_closed(self) -> None:
        # Transport-level failure: bridge unreachable.
        provider = _CommitFakeProvider(raise_exc=_bridge_down_exc())
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(target_parent_path="/obj", target_name="my_asset")

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.bridge_unavailable"

    def test_provider_op_failure_reported_honestly(self) -> None:
        # Operation-level failure during commit promotion.
        provider = _CommitFakeProvider(raise_exc=_scratch_op_failed_exc())
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(target_parent_path="/obj", target_name="my_asset")

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.op_failed"
        assert "failed after 2 op(s)" in result["message"]

    def test_bad_target_parent_rejected(self) -> None:
        provider = _CommitFakeProvider()
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(target_parent_path="obj", target_name="x")

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"
        assert provider.commit_calls == []

    def test_empty_target_name_rejected(self) -> None:
        provider = _CommitFakeProvider()
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(target_parent_path="/obj", target_name="")

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"

    def test_orientation_checks_forwarded(self) -> None:
        provider = _CommitFakeProvider()
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(
                target_parent_path="/obj", target_name="asset",
                orientation_checks=[{"component_id": "w", "kind": "radial", "expected_axis": "Y"}],
            )

        asyncio.run(run())
        call = provider.commit_calls[0]
        assert len(call["orientation_checks"]) == 1
        assert call["orientation_checks"][0]["component_id"] == "w"

    def test_skip_structure_forwarded(self) -> None:
        provider = _CommitFakeProvider()
        coord = ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))

        async def run() -> dict[str, object]:
            return await coord.commit(
                target_parent_path="/obj", target_name="asset",
                skip_structure_check=True,
            )

        asyncio.run(run())
        assert provider.commit_calls[0]["skip_structure_check"] is True

    def test_commit_annotations_reach_provider_as_mapping(self, tmp_path: Path) -> None:
        # Regression: the coordinator used to forward its internal pair-tuple
        # straight into the provider's Mapping[str, str] contract; the real
        # request builder then crashed with "'tuple' object has no attribute
        # 'items'" on every commit that carried task-graph annotations.
        from eee_agent.runtime.database import RuntimeDatabase
        from eee_agent.runtime.task_graph import TaskGraphStore

        async def run() -> tuple[_CommitFakeProvider, dict[str, object]]:
            database = await RuntimeDatabase.open(tmp_path / "app.sqlite")
            try:
                store = TaskGraphStore(database)
                now = datetime.now(timezone.utc).isoformat()
                async with database.write_transaction() as conn:
                    await conn.execute(
                        "INSERT INTO sessions(session_id,title,status,created_at,"
                        "updated_at,last_seq,replay_floor_seq) VALUES "
                        "('sess_1','t','active',?,?,0,0)",
                        (now, now),
                    )
                    await conn.execute(
                        "INSERT INTO runs(run_id,session_id,status,user_input,"
                        "created_at,model_snapshot_json) VALUES "
                        "('run_1','sess_1','Planning','build',?,'{}')",
                        (now,),
                    )
                step = await store.record_step(
                    run_id="run_1", tool="scratch_build", purpose="build tabletop"
                )
                await store.record_nodes(
                    step_id=step.step_id,
                    nodes=[("/obj/eee_scratch_run1/tabletop", "box", "输出")],
                )
                provider = _CommitFakeProvider(committed=True)
                coord = ScratchCoordinator(
                    ScratchSessionContext(
                        provider=provider,
                        sandbox_id="run1",
                        run_id="run_1",
                        task_store=store,
                    )
                )
                result = await coord.commit(
                    target_parent_path="/obj", target_name="asset"
                )
                return provider, result
            finally:
                await database.close()

        provider, result = asyncio.run(run())
        assert result["committed"] is True
        annotations = provider.commit_calls[0]["annotations"]
        assert type(annotations) is dict
        assert annotations == {"tabletop": "输出"}


# ==========================================================================
# Phase 2.3: scratch_commit tool
# ==========================================================================


class TestScratchCommitTool:
    def _ctx(self, provider: _CommitFakeProvider) -> RuntimeToolContext:
        scratch = ScratchToolContext(
            ScratchCoordinator(ScratchSessionContext(provider=provider, sandbox_id="run1"))
        )
        return RuntimeToolContext(
            read_only=_read_only_provider(),
            knowledge=_FakeKnowledge(),
            scratch=scratch,
        )

    def test_no_context_fails_closed(self) -> None:
        async def run() -> dict[str, object]:
            return await scratch_commit.coroutine(
                target_parent_path="/obj", target_name="x",
                runtime=_runtime_with_context(None),
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.context_invalid"

    def test_no_scratch_slot_fails_closed(self) -> None:
        ctx = RuntimeToolContext(
            read_only=_read_only_provider(), knowledge=_FakeKnowledge()
        )

        async def run() -> dict[str, object]:
            return await scratch_commit.coroutine(
                target_parent_path="/obj", target_name="x",
                runtime=_runtime_with_context(ctx),
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.context_invalid"

    def test_bad_skip_structure_type_rejected(self) -> None:
        provider = _CommitFakeProvider()

        async def run() -> dict[str, object]:
            return await scratch_commit.coroutine(
                target_parent_path="/obj", target_name="x",
                runtime=_runtime_with_context(self._ctx(provider)),
                skip_structure_check="yes",  # type: ignore[arg-type]
            )

        result = asyncio.run(run())
        assert result["ok"] is False
        assert result["code"] == "scratch.input_invalid"

    def test_delegates_committed(self) -> None:
        provider = _CommitFakeProvider(committed=True)

        async def run() -> dict[str, object]:
            return await scratch_commit.coroutine(
                target_parent_path="/obj", target_name="my_asset",
                runtime=_runtime_with_context(self._ctx(provider)),
            )

        result = asyncio.run(run())
        assert result["ok"] is True
        assert result["committed"] is True
        assert result["final_path"] == "/obj/my_asset"
        assert len(provider.commit_calls) == 1

    def test_delegates_refused(self) -> None:
        provider = _CommitFakeProvider(committed=False, refused=True, reason="bake failed")

        async def run() -> dict[str, object]:
            return await scratch_commit.coroutine(
                target_parent_path="/obj", target_name="my_asset",
                runtime=_runtime_with_context(self._ctx(provider)),
            )

        result = asyncio.run(run())
        assert result["ok"] is True
        assert result["refused"] is True
        assert result["reason"] == "bake failed"


# ==========================================================================
# Phase 3.2: scratch.destroy DTOs + client + service cleanup hook
# ==========================================================================


def _destroy_result_envelope_frame(
    *,
    request_id: str = "req_destroy_001",
    destroyed_paths: tuple[str, ...] = ("/obj/eee_scratch_run1",),
    missing: bool = False,
) -> bytes:
    env = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": {"destroyed_paths": list(destroyed_paths), "missing": missing},
    }
    return _frame(_dumps(env))


class TestScratchDestroyRequest:
    def test_round_trip(self) -> None:
        req = ScratchDestroyRequest.build(
            request_id="req_destroy_001", deadline_ms=5000,
            scene_epoch=42, sandbox_id="run1",
        )
        assert parse_scratch_destroy_request(req.to_json()) == req

    def test_operation_is_scratch_destroy(self) -> None:
        req = ScratchDestroyRequest.build(
            request_id="req_destroy_001", deadline_ms=5000,
            scene_epoch=42, sandbox_id="run1",
        )
        assert req.to_dict()["operation"] == SCRATCH_DESTROY_OPERATION

    def test_container_path(self) -> None:
        req = ScratchDestroyRequest.build(
            request_id="r", deadline_ms=5000, scene_epoch=1, sandbox_id="run_abc",
        )
        assert req.container_path == "/obj/eee_scratch_run_abc"

    def test_bad_sandbox_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchDestroyRequest.build(
                request_id="r", deadline_ms=5000, scene_epoch=1, sandbox_id="bad id",
            )


class TestScratchDestroyResult:
    def test_destroyed_round_trip(self) -> None:
        result = ScratchDestroyResult(
            destroyed_paths=("/obj/eee_scratch_run1",), missing=False
        )
        assert ScratchDestroyResult.from_dict(result.to_dict()) == result

    def test_missing_round_trip(self) -> None:
        result = ScratchDestroyResult(destroyed_paths=(), missing=True)
        parsed = ScratchDestroyResult.from_dict(result.to_dict())
        assert parsed.missing is True
        assert parsed.destroyed_paths == ()

    def test_bad_path_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchDestroyResult(destroyed_paths=("not_a_path",), missing=False)


class TestScratchDestroyResponse:
    def test_success_round_trip(self) -> None:
        env = {
            "protocol": _PROTO, "kind": "response",
            "request_id": "r", "ok": True,
            "result": {"destroyed_paths": ["/obj/eee_scratch_run1"], "missing": False},
        }
        resp = parse_scratch_destroy_response(_dumps(env))
        assert resp.result is not None
        assert resp.result.destroyed_paths == ("/obj/eee_scratch_run1",)

    def test_error_round_trip(self) -> None:
        env = {
            "protocol": _PROTO, "kind": "response",
            "request_id": "r", "ok": False,
            "error": {"code": "bridge.capability_unavailable", "category": "houdini_bridge",
                      "message_for_user": "no scratch", "retryable": False,
                      "technical_detail_ref": "td"},
        }
        resp = parse_scratch_destroy_response(_dumps(env))
        assert resp.result is None
        assert resp.error is not None


def _scratch_destroy_request() -> ScratchDestroyRequest:
    return ScratchDestroyRequest.build(
        request_id="req_destroy_001", deadline_ms=5000,
        scene_epoch=42, sandbox_id="run1",
    )


@async_test
async def test_scratch_destroy_without_capability_raises() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["changeset.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_destroy(_scratch_destroy_request())
    assert exc.value.code == "bridge.capability_unavailable"


@async_test
async def test_scratch_destroy_round_trip_returns_result() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["changeset.v1", "scratch.v1"])
        + _destroy_result_envelope_frame()
    )
    client = _client(fake)
    await client.open()
    result = await client.scratch_destroy(_scratch_destroy_request())
    assert type(result) is ScratchDestroyResult
    assert result.destroyed_paths == ("/obj/eee_scratch_run1",)
    assert result.missing is False


@async_test
async def test_scratch_destroy_missing_is_normal_outcome() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["scratch.v1"])
        + _destroy_result_envelope_frame(destroyed_paths=(), missing=True)
    )
    client = _client(fake)
    await client.open()
    result = await client.scratch_destroy(_scratch_destroy_request())
    assert result.missing is True
    assert result.destroyed_paths == ()


@async_test
async def test_scratch_destroy_server_error_raised() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["scratch.v1"])
        + _error_envelope_frame(request_id="req_destroy_001", code="bridge.unauthorized")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_destroy(_scratch_destroy_request())
    assert exc.value.code == "bridge.unauthorized"


@async_test
async def test_scratch_destroy_sent_frame_carries_operation() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["scratch.v1"])
        + _destroy_result_envelope_frame()
    )
    client = _client(fake)
    await client.open()
    await client.scratch_destroy(_scratch_destroy_request())
    frames = _parse_frames(bytes(fake.outbox))
    assert len(frames) == 2
    sent_obj = json.loads(frames[1])
    assert sent_obj["operation"] == SCRATCH_DESTROY_OPERATION
    assert sent_obj["payload"]["sandbox_id"] == "run1"


# --------------------------------------------------------------------------
# Phase 3.2: service-level cleanup hook (_cleanup_scratch_sandbox)
# --------------------------------------------------------------------------


class TestServiceScratchCleanup:
    def test_cleanup_calls_provider_scratch_destroy(self) -> None:
        from eee_agent.runtime.service import RuntimeService

        calls: list[str] = []

        class _DestroyProvider:
            async def scratch_destroy(self, *, sandbox_id: str):
                calls.append(sandbox_id)
                from eee_agent.houdini_bridge.scratch import ScratchDestroyResult
                return ScratchDestroyResult(destroyed_paths=(), missing=True)

        service = object.__new__(RuntimeService)
        service._scratch_bridge_provider = _DestroyProvider()
        asyncio.run(service._cleanup_scratch_sandbox("run_abc123"))
        assert calls == ["run_abc123"]

    def test_cleanup_no_provider_is_noop(self) -> None:
        from eee_agent.runtime.service import RuntimeService

        service = object.__new__(RuntimeService)
        # _scratch_bridge_provider not set → getattr returns None → no-op.
        asyncio.run(service._cleanup_scratch_sandbox("run_abc"))
        # No exception, no crash.

    def test_cleanup_provider_exception_is_swallowed(self) -> None:
        from eee_agent.runtime.service import RuntimeService

        class _BoomProvider:
            async def scratch_destroy(self, *, sandbox_id: str):
                raise RuntimeError("bridge down")

        service = object.__new__(RuntimeService)
        service._scratch_bridge_provider = _BoomProvider()
        # Must NOT raise — cleanup failure is logged and swallowed.
        asyncio.run(service._cleanup_scratch_sandbox("run_abc"))

    def test_sandbox_id_derived_from_run_id(self) -> None:
        from eee_agent.runtime.service import _sandbox_id_from_run

        # The sandbox id is sanitized to the safe charset.
        assert _sandbox_id_from_run("run_abc123") == "run_abc123"
        # Unsafe chars are replaced.
        sid = _sandbox_id_from_run("run with spaces!")
        assert " " not in sid
        assert all(c.isalnum() or c in "_-" for c in sid)


class TestScratchDeleteTopologyDtos:
    def test_delete_request_round_trip(self) -> None:
        req = ScratchDeleteRequest.build(
            request_id="req_d1",
            deadline_ms=5000,
            scene_epoch=1,
            allowed_paths=("/obj/table1/box1", "/obj/table1/draft1"),
            paths=("/obj/table1/draft1",),
        )
        parsed = ScratchDeleteRequest.from_dict(req.to_dict())
        assert parsed.paths == ("/obj/table1/draft1",)
        assert parsed.allowed_paths == (
            "/obj/table1/box1",
            "/obj/table1/draft1",
        )

    def test_delete_request_paths_must_be_allowlisted(self) -> None:
        with pytest.raises(ValueError):
            ScratchDeleteRequest.build(
                request_id="req_d2",
                deadline_ms=5000,
                scene_epoch=1,
                allowed_paths=("/obj/table1/box1",),
                paths=("/obj/other/nope",),
            )

    def test_delete_request_rejects_duplicates(self) -> None:
        with pytest.raises(ValueError):
            ScratchDeleteRequest.build(
                request_id="req_d3",
                deadline_ms=5000,
                scene_epoch=1,
                allowed_paths=("/obj/table1/draft1",),
                paths=("/obj/table1/draft1", "/obj/table1/draft1"),
            )

    def test_delete_and_topology_paths_reject_traversal(self) -> None:
        with pytest.raises(ValueError):
            ScratchDeleteRequest.build(
                request_id="req_path",
                deadline_ms=5000,
                scene_epoch=1,
                allowed_paths=("/obj/../evil",),
                paths=("/obj/../evil",),
            )
        with pytest.raises(ValueError):
            ScratchTopologyRequest.build(
                request_id="req_path2",
                deadline_ms=5000,
                scene_epoch=1,
                paths=("/obj//bad",),
            )

    def test_delete_result_round_trip(self) -> None:
        result = ScratchDeleteResult(
            deleted_paths=("/obj/table1/draft1",),
            skipped=(
                {
                    "path": "/obj/table1/box1",
                    "reason": "still referenced by /obj/table1/out",
                },
            ),
        )
        parsed = ScratchDeleteResult.from_dict(result.to_dict())
        assert parsed.deleted_paths == ("/obj/table1/draft1",)
        assert parsed.skipped[0]["path"] == "/obj/table1/box1"

    def test_topology_request_round_trip(self) -> None:
        req = ScratchTopologyRequest.build(
            request_id="req_t1",
            deadline_ms=5000,
            scene_epoch=1,
            paths=("/obj/table1/box1",),
        )
        parsed = ScratchTopologyRequest.from_dict(req.to_dict())
        assert parsed.paths == ("/obj/table1/box1",)

    def test_topology_result_round_trip(self) -> None:
        result = ScratchTopologyResult(
            nodes=(
                {
                    "path": "/obj/table1/box1",
                    "exists": True,
                    "inputs": [],
                    "outputs": ["/obj/table1/xform1"],
                    "display_flag": False,
                },
                {
                    "path": "/obj/table1/gone",
                    "exists": False,
                    "inputs": [],
                    "outputs": [],
                    "display_flag": False,
                },
            ),
        )
        parsed = ScratchTopologyResult.from_dict(result.to_dict())
        assert parsed.nodes[0]["outputs"] == ["/obj/table1/xform1"]
        assert parsed.nodes[1]["exists"] is False


def _delete_request() -> ScratchDeleteRequest:
    return ScratchDeleteRequest.build(
        request_id="req_del_001",
        deadline_ms=5000,
        scene_epoch=42,
        allowed_paths=("/obj/table1/draft1",),
        paths=("/obj/table1/draft1",),
    )


def _topology_request() -> ScratchTopologyRequest:
    return ScratchTopologyRequest.build(
        request_id="req_topo_001",
        deadline_ms=5000,
        scene_epoch=42,
        paths=("/obj/table1/draft1",),
    )


@async_test
async def test_scratch_delete_without_v2_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=[SCRATCH_V1]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_delete_nodes(_delete_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_topology_without_v2_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=[SCRATCH_V1]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_topology(_topology_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_exec_delete_op_requires_v2_capability() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=[SCRATCH_V1]))
    client = _client(fake)
    await client.open()
    request = ScratchRequest(
        request_id="req_scratch_del",
        deadline_ms=5000,
        scene_epoch=42,
        sandbox_id="run1",
        operations=(ScratchOp(kind="delete_node", node_name="draft1"),),
        purpose="cleanup draft",
    )
    with pytest.raises(BridgeClientError) as exc:
        await client.scratch_exec(request)
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_scratch_delete_happy_path() -> None:
    response = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": "req_del_001",
        "ok": True,
        "result": {
            "deleted_paths": ["/obj/table1/draft1"],
            "skipped": [],
        },
    }
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SCRATCH_V1, SCRATCH_V2])
        + _frame(_dumps(response))
    )
    client = _client(fake)
    await client.open()
    result = await client.scratch_delete_nodes(_delete_request())
    assert result.deleted_paths == ("/obj/table1/draft1",)
    assert result.skipped == ()
