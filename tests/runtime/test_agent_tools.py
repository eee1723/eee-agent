from __future__ import annotations

import asyncio
import ast
import inspect
from types import SimpleNamespace
from collections.abc import Mapping

import pytest

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.agent_tools import _finish, build_read_only_tools


def _run(coro):
    return asyncio.run(coro)


class _Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def scene_status(self):
        self.calls.append(("scene_status",))
        return {"ok": True, "connected": True, "version": "21.0"}

    async def query_scene(self, node_paths: list[str]):
        self.calls.append(("query_scene", node_paths))
        return {"ok": True, "nodes": [{"path": "/obj/geo1"}]}

    async def inspect_workspace(self, workspace_id: str):
        self.calls.append(("inspect_workspace", workspace_id))
        return {"ok": True, "workspace": None}

    async def geometry_stats(self, node_path: str):
        self.calls.append(("geometry_stats", node_path))
        return {"ok": True, "points": 8, "bbox": {"min": [0, 0, 0]}}

    async def work_status(self, workspace_id: str):
        self.calls.append(("work_status", workspace_id))
        return {"ok": True, "components": []}


class _Knowledge:
    """Minimal trusted KnowledgeProvider for tool tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def search(self, query: str, *, limit: int = 5):
        self.calls.append(("search", query, limit))
        # A result with more than 64 recursive items, which would have tripped
        # the bridge plain-value item budget before knowledge bypassed _finish.
        return {
            "ok": True,
            "results": [
                {"entity_id": f"node:{i}", "title": f"node {i}", "body": "x" * 40}
                for i in range(40)
            ],
        }

    def get(self, entity_id: str, *, max_body_bytes: int = 8_000):
        self.calls.append(("get", entity_id, max_body_bytes))
        return {"ok": True, "entity_id": entity_id, "body": "x" * 200}


def _runtime(provider=None, knowledge=None):
    provider = _Provider() if provider is None else provider
    knowledge = _Knowledge() if knowledge is None else knowledge
    return SimpleNamespace(
        context=RuntimeToolContext(read_only=provider, knowledge=knowledge)
    )


def test_runtime_context_rejects_missing_readonly_provider() -> None:
    with pytest.raises(TypeError, match="read_only"):
        RuntimeToolContext(read_only=None, knowledge=_Knowledge())  # type: ignore[arg-type]


def test_runtime_context_rejects_missing_knowledge_provider() -> None:
    with pytest.raises(TypeError, match="knowledge"):
        RuntimeToolContext(read_only=_Provider(), knowledge=None)  # type: ignore[arg-type]


def test_readonly_tools_return_bounded_plain_dicts() -> None:
    tools = {item.name: item for item in build_read_only_tools()}
    runtime = _runtime()
    results = [
        _run(tools["scene_status"].coroutine(runtime=runtime)),
        _run(
            tools["query_scene"].coroutine(node_paths=["/obj/geo1"], runtime=runtime)
        ),
        _run(
            tools["inspect_workspace"].coroutine(
                workspace_id="ws_test", runtime=runtime
            )
        ),
        _run(
            tools["geometry_stats"].coroutine(
                node_path="/obj/geo1", runtime=runtime
            )
        ),
        _run(tools["work_status"].coroutine(workspace_id="ws_test", runtime=runtime)),
    ]
    assert all(type(result) is dict for result in results)
    assert all(len(repr(result).encode("utf-8")) <= 16 * 1024 for result in results)
    assert all("proxy" not in result for result in results)


def test_readonly_tools_never_import_legacy_bridge() -> None:
    import eee_agent.runtime.agent_context as context_module
    import eee_agent.runtime.agent_tools as tools_module

    imported: set[str] = set()
    for module in (context_module, tools_module):
        tree = ast.parse(inspect.getsource(module))
        imported |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported |= {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
    assert not any(
        name == "hou"
        or name == "rpyc"
        or name.startswith("eee_agent.bridge")
        or name.startswith("eee_agent.tools")
        or name.startswith("sqlite")
        for name in imported
    )


def test_missing_secure_provider_returns_bridge_unavailable_without_write() -> None:
    tool = next(item for item in build_read_only_tools() if item.name == "scene_status")
    result = _run(tool.coroutine(runtime=SimpleNamespace(context=None)))
    assert result == {
        "ok": False,
        "code": "bridge.unavailable",
        "message": "A trusted read-only provider is unavailable.",
    }


def test_invalid_arguments_fail_before_provider_call() -> None:
    provider = _Provider()
    tools = {item.name: item for item in build_read_only_tools()}
    result = _run(
        tools["geometry_stats"].coroutine(
            node_path="relative/path", runtime=_runtime(provider)
        )
    )
    assert result["code"] == "runtime.tool_input_invalid"
    assert provider.calls == []


def test_provider_exception_is_bounded() -> None:
    class FailedProvider(_Provider):
        async def scene_status(self):
            raise RuntimeError("secret " + "x" * 20_000)

    tool = next(item for item in build_read_only_tools() if item.name == "scene_status")
    result = _run(tool.coroutine(runtime=_runtime(FailedProvider())))
    assert result["code"] == "bridge.unavailable"
    assert "secret" not in str(result)


def test_finish_fails_closed_when_custom_mapping_iteration_raises() -> None:
    class ExplodingMapping(Mapping):
        def __getitem__(self, key):
            if key == "ok":
                return True
            raise KeyError(key)

        def __iter__(self):
            raise RuntimeError("iterator exploded")

        def __len__(self):
            return 1

    result = _finish(ExplodingMapping())
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"


def test_finish_rejects_unbounded_item_budget() -> None:
    result = _finish({"ok": True, "items": list(range(10_000))})
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"


def test_finish_bounds_large_plain_payload_without_leaking_original_text() -> None:
    secret = "payload-secret-" + ("x" * 100_000)
    result = _finish({"ok": True, "payload": secret})
    assert len(repr(result).encode("utf-8")) <= 16 * 1024
    assert "payload-secret" not in repr(result)
    assert result["payload"] == "[truncated]"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_finish_rejects_non_finite_numbers(value: float) -> None:
    result = _finish({"ok": True, "value": value})

    assert result == {
        "ok": False,
        "code": "bridge.unavailable",
        "message": "The read-only provider returned unsupported data.",
    }


def test_finish_rejects_integer_with_excessive_decimal_digits() -> None:
    result = _finish({"ok": True, "value": 10**129})

    assert result == {
        "ok": False,
        "code": "bridge.unavailable",
        "message": "The read-only provider returned unsupported data.",
    }


def test_finish_rejects_integer_too_large_to_serialize() -> None:
    result = _finish({"ok": True, "value": 10**10_000})

    assert result == {
        "ok": False,
        "code": "bridge.unavailable",
        "message": "The read-only provider returned unsupported data.",
    }


def test_knowledge_search_bypasses_bridge_budget_validator() -> None:
    # Knowledge results come from a trusted local cache, not the live Houdini
    # process, so a large result (many items) must NOT be misreported as
    # bridge.unavailable the way the scene-tools budget validator would.
    tools = {item.name: item for item in build_read_only_tools()}
    knowledge = _Knowledge()
    result = _run(
        tools["search_houdini_knowledge"].coroutine(
            query="box node", runtime=_runtime(knowledge=knowledge)
        )
    )
    assert result["ok"] is True
    assert len(result["results"]) == 40
    assert knowledge.calls == [("search", "box node", 5)]


def test_knowledge_search_reports_unavailable_without_provider() -> None:
    tools = {item.name: item for item in build_read_only_tools()}
    runtime = SimpleNamespace(context=None)
    result = _run(
        tools["search_houdini_knowledge"].coroutine(
            query="box", runtime=runtime
        )
    )
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"


def test_knowledge_get_returns_bounded_entity() -> None:
    tools = {item.name: item for item in build_read_only_tools()}
    knowledge = _Knowledge()
    result = _run(
        tools["get_houdini_knowledge"].coroutine(
            entity_id="node:box", runtime=_runtime(knowledge=knowledge)
        )
    )
    assert result["ok"] is True
    assert result["entity_id"] == "node:box"
    assert knowledge.calls[0][0] == "get"
