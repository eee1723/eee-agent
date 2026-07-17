from __future__ import annotations

import asyncio
import ast
import inspect
from types import SimpleNamespace

import pytest

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.agent_tools import build_read_only_tools


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


def _runtime(provider=None):
    provider = _Provider() if provider is None else provider
    return SimpleNamespace(context=RuntimeToolContext(read_only=provider))


def test_runtime_context_rejects_missing_readonly_provider() -> None:
    with pytest.raises(TypeError, match="read_only"):
        RuntimeToolContext(read_only=None)  # type: ignore[arg-type]


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
