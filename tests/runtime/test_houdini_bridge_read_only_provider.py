"""Offline contract tests for the bridge-backed ReadOnlyProvider.

The Secure Bridge client is replaced by a stub that mimics the typed
``inspect_workspace`` / ``request`` surface, so every path — live facts,
bounded unavailability, stale-scene retry, and list truncation — is
deterministic without hython.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    BridgeTokenError,
)
from eee_agent.houdini_bridge.client import BridgeClientError
from eee_agent.houdini_bridge.contracts import SceneBinding, SceneQueryResult, SelectedNode
from eee_agent.houdini_bridge.read_only_provider import BridgeReadOnlyProvider
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectResult,
    WorkspaceNodeObservation,
)
from eee_agent.runtime.agent_context import ReadOnlyProvider, RuntimeToolContext
from eee_agent.runtime.agent_tools import build_read_only_tools

from types import SimpleNamespace

WS = f"ws_{'a' * 32}"
OTHER_WS = f"ws_{'b' * 32}"


class _Knowledge:
    """Minimal trusted KnowledgeProvider for context-construction tests."""

    def search(self, query, *, limit=5):
        return {"ok": True, "results": []}

    def get(self, entity_id, *, max_body_bytes=8_000):
        return {"ok": True, "entity_id": entity_id}


def _binding(epoch: int = 3) -> SceneBinding:
    return SceneBinding(
        instance_id="hou:21.0.440:pid1",
        scene_epoch=epoch,
        hip_path=None,
        observed_revision="sha256:" + "0" * 64,
    )


def _observation(index: int, workspace_id: str | None = WS) -> WorkspaceNodeObservation:
    return WorkspaceNodeObservation(
        path=f"/obj/eee_model/n{index}",
        node_type="box",
        parent_path="/obj/eee_model",
        is_locked=False,
        workspace_id=workspace_id,
        node_id=f"n{index}",
        capability="modeling",
        role="member",
        schema_version=1,
        created_by_run=None,
    )


def _selected_node(path: str = "/obj/eee_model/box1") -> SelectedNode:
    return SelectedNode(
        path=path,
        node_type="box",
        parent_path="/obj/eee_model",
        display_name="box1",
        is_locked=False,
        geometry_stats={"points": 8, "primitives": 6},
    )


def _inspection(
    count: int = 2, *, workspace_id: str | None = WS
) -> WorkspaceInspectResult:
    return WorkspaceInspectResult.build(
        binding=_binding(),
        mode="selection",
        observations=[_observation(index, workspace_id) for index in range(count)],
    )


def _query_result(count: int = 1) -> SceneQueryResult:
    return SceneQueryResult(
        binding=_binding(),
        selected_nodes=(),
        nodes=tuple(_selected_node(f"/obj/eee_model/box{i}") for i in range(count)),
    )


class _StubClient:
    """Mimics the typed client surface the provider relies on."""

    def __init__(
        self,
        *,
        inspection: WorkspaceInspectResult | None = None,
        query_result: SceneQueryResult | None = None,
        inspect_error: Exception | None = None,
        query_errors: list[Exception] | None = None,
    ) -> None:
        self.inspection = inspection or _inspection()
        self.query_result = query_result or _query_result()
        self.inspect_error = inspect_error
        self.query_errors = list(query_errors or ())
        self.capabilities = ("workspace.v1",)
        self.inspect_requests: list[object] = []
        self.query_requests: list[object] = []

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    async def inspect_workspace(self, request: object) -> WorkspaceInspectResult:
        self.inspect_requests.append(request)
        if self.inspect_error is not None:
            raise self.inspect_error
        return self.inspection

    async def request(self, request: object) -> SceneQueryResult:
        self.query_requests.append(request)
        if self.query_errors:
            raise self.query_errors.pop(0)
        return self.query_result


def _handoff(state_dir: Path) -> None:
    (state_dir / BRIDGE_TOKEN_FILENAME).write_text("token", encoding="utf-8")
    (state_dir / BRIDGE_DISCOVERY_FILENAME).write_text("{}", encoding="utf-8")


def _provider(
    state_dir: Path, clients: list[_StubClient] | None = None
) -> BridgeReadOnlyProvider:
    created: list[_StubClient] = clients if clients is not None else []

    def factory(_: Path) -> _StubClient:
        client = created.pop(0) if created else _StubClient()
        return client

    return BridgeReadOnlyProvider(state_dir, client_factory=factory)


def _bridge_error(code: str, message: str = "bounded bridge detail") -> BridgeClientError:
    return BridgeClientError(
        code=code,
        category="houdini_bridge",
        message_for_user=message,
        retryable=True,
    )


def test_provider_satisfies_read_only_protocol(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    assert isinstance(provider, ReadOnlyProvider)
    context = RuntimeToolContext(read_only=provider, knowledge=_Knowledge())
    assert context.read_only is provider


def test_scene_status_returns_live_binding_facts(tmp_path: Path) -> None:
    _handoff(tmp_path)
    client = _StubClient()
    provider = _provider(tmp_path, [client])

    async def scenario():
        return await provider.scene_status()

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["connected"] is True
    assert result["scene_epoch"] == 3
    assert result["instance_id"] == "hou:21.0.440:pid1"
    assert result["capabilities"] == ["workspace.v1"]
    assert len(client.inspect_requests) == 1
    assert client.inspect_requests[0].scene_epoch is None


def test_query_scene_uses_exact_epoch_and_returns_nodes(tmp_path: Path) -> None:
    _handoff(tmp_path)
    client = _StubClient(query_result=_query_result(count=2))
    provider = _provider(tmp_path, [client])

    async def scenario():
        return await provider.query_scene(["/obj/eee_model/box0"])

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["node_count"] == 2
    assert result["nodes"][0]["path"] == "/obj/eee_model/box0"
    assert len(client.query_requests) == 1
    request = client.query_requests[0]
    assert request.scene_epoch == 3
    assert list(request.payload["node_paths"]) == ["/obj/eee_model/box0"]


def test_query_scene_retries_once_on_stale_scene(tmp_path: Path) -> None:
    _handoff(tmp_path)
    stale = _StubClient(query_errors=[_bridge_error("bridge.stale_scene")])
    fresh = _StubClient()
    provider = _provider(tmp_path, [stale, fresh])

    async def scenario():
        return await provider.query_scene(["/obj/eee_model/box0"])

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert len(stale.query_requests) == 1
    assert len(fresh.query_requests) == 1


def test_geometry_stats_reshapes_single_node(tmp_path: Path) -> None:
    _handoff(tmp_path)
    provider = _provider(tmp_path, [_StubClient()])

    async def scenario():
        return await provider.geometry_stats("/obj/eee_model/box0")

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["path"] == "/obj/eee_model/box0"
    assert result["node_type"] == "box"
    assert result["geometry_stats"] == {"points": 8, "primitives": 6}


def test_inspect_workspace_filters_and_truncates(tmp_path: Path) -> None:
    _handoff(tmp_path)
    matching = [_observation(index) for index in range(35)]
    matching.append(_observation(100, OTHER_WS))
    inspection = WorkspaceInspectResult.build(
        binding=_binding(), mode="selection", observations=matching
    )
    provider = _provider(tmp_path, [_StubClient(inspection=inspection)])

    async def scenario():
        return await provider.inspect_workspace(WS)

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["workspace_id"] == WS
    assert result["node_count"] == 35
    assert result["truncated"] is True
    assert len(result["nodes"]) == 32
    assert all(node["workspace_id"] == WS for node in result["nodes"])


def test_work_status_returns_bounded_summary(tmp_path: Path) -> None:
    _handoff(tmp_path)
    provider = _provider(tmp_path, [_StubClient(inspection=_inspection(count=4))])

    async def scenario():
        return await provider.work_status(WS)

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["selected_node_count"] == 4
    assert result["node_count"] == 4
    assert result["locked_node_count"] == 0
    assert result["truncated"] is False
    assert len(result["paths"]) == 4


def test_missing_handoff_is_bounded_unavailable(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    async def scenario():
        return [
            await provider.scene_status(),
            await provider.query_scene(["/obj/geo1"]),
            await provider.inspect_workspace(WS),
            await provider.geometry_stats("/obj/geo1"),
            await provider.work_status(WS),
        ]

    results = asyncio.run(scenario())
    for result in results:
        assert result["ok"] is False
        assert result["code"] == "bridge.not_available"


def test_bridge_error_is_forwarded_bounded(tmp_path: Path) -> None:
    _handoff(tmp_path)
    client = _StubClient(
        inspect_error=_bridge_error("bridge.houdini_read_failed", "bounded detail")
    )
    provider = _provider(tmp_path, [client])

    async def scenario():
        return await provider.scene_status()

    result = asyncio.run(scenario())
    assert result == {
        "ok": False,
        "code": "bridge.houdini_read_failed",
        "message": "bounded detail",
    }


def test_token_failure_is_bounded_auth_failure(tmp_path: Path) -> None:
    _handoff(tmp_path)

    def factory(_: Path):
        raise BridgeTokenError("token file unreadable")

    provider = BridgeReadOnlyProvider(tmp_path, client_factory=factory)

    async def scenario():
        return await provider.scene_status()

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert result["code"] == "bridge.auth_failed"


def test_unexpected_exception_never_leaks_detail(tmp_path: Path) -> None:
    _handoff(tmp_path)
    client = _StubClient(inspect_error=RuntimeError("secret internal detail"))
    provider = _provider(tmp_path, [client])

    async def scenario():
        return await provider.scene_status()

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"
    assert "secret" not in str(result)


def test_invalid_inputs_fail_closed_without_client(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    async def scenario():
        return [
            await provider.query_scene([]),
            await provider.query_scene(["/obj"] * 65),
            await provider.geometry_stats("relative/path"),
            await provider.inspect_workspace(""),
            await provider.work_status("x" * 200),
        ]

    results = asyncio.run(scenario())
    for result in results:
        assert result["ok"] is False
        assert result["code"] == "bridge.invalid_request"


def test_provider_results_pass_the_real_tool_boundary(tmp_path: Path) -> None:
    _handoff(tmp_path)
    provider = _provider(tmp_path, [_StubClient()])
    tool = next(item for item in build_read_only_tools() if item.name == "scene_status")
    runtime = SimpleNamespace(context=RuntimeToolContext(read_only=provider, knowledge=_Knowledge()))

    result = asyncio.run(tool.coroutine(runtime=runtime))
    assert result["ok"] is True
    assert result["scene_epoch"] == 3
