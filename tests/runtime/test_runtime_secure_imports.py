from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_runtime_agent_import_does_not_load_legacy_tools_or_bridge() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = """
import sys
import eee_agent.runtime.agent_runner
legacy = sorted(
    name for name in sys.modules
    if name == 'eee_agent.bridge'
    or name.startswith('eee_agent.bridge.')
    or name == 'eee_agent.tools'
    or name.startswith('eee_agent.tools.')
)
if legacy:
    raise SystemExit('legacy modules loaded: ' + ','.join(legacy))
"""
    env = os.environ.copy()
    env["EEE_COMPACT_TOOL"] = "false"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


class _Knowledge:
    """Minimal trusted KnowledgeProvider for context-construction tests."""

    def search(self, query, *, limit=5):
        return {"ok": True, "results": []}

    def get(self, entity_id, *, max_body_bytes=8_000):
        return {"ok": True, "entity_id": entity_id}


def test_finish_rejects_non_status_mapping() -> None:
    from types import SimpleNamespace
    import asyncio

    from eee_agent.runtime.agent_context import RuntimeToolContext
    from eee_agent.runtime.agent_tools import build_read_only_tools

    class Provider:
        async def scene_status(self):
            return {"nodes": []}

        async def query_scene(self, node_paths):
            return {"ok": True, "nodes": []}

        async def inspect_workspace(self, workspace_id):
            return {"ok": True}

        async def geometry_stats(self, node_path):
            return {"ok": True}

        async def work_status(self, workspace_id):
            return {"ok": True}

    tool = next(item for item in build_read_only_tools() if item.name == "scene_status")
    result = asyncio.run(
        tool.coroutine(runtime=SimpleNamespace(context=RuntimeToolContext(Provider(), knowledge=_Knowledge())))
    )
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"


def test_finish_rejects_opaque_mapping_values() -> None:
    from types import SimpleNamespace
    import asyncio

    from eee_agent.runtime.agent_context import RuntimeToolContext
    from eee_agent.runtime.agent_tools import build_read_only_tools

    class Provider:
        async def scene_status(self):
            return {"ok": True, "opaque": object()}

        async def query_scene(self, node_paths):
            return {"ok": True}

        async def inspect_workspace(self, workspace_id):
            return {"ok": True}

        async def geometry_stats(self, node_path):
            return {"ok": True}

        async def work_status(self, workspace_id):
            return {"ok": True}

    tool = next(item for item in build_read_only_tools() if item.name == "scene_status")
    result = asyncio.run(
        tool.coroutine(runtime=SimpleNamespace(context=RuntimeToolContext(Provider(), knowledge=_Knowledge())))
    )
    assert result["ok"] is False
    assert result["code"] == "bridge.unavailable"


def test_runtime_service_creates_new_context_for_each_run() -> None:
    import asyncio

    from eee_agent.runtime.service import RuntimeService

    class Provider:
        async def scene_status(self):
            return {"ok": True}

        async def query_scene(self, node_paths):
            return {"ok": True}

        async def inspect_workspace(self, workspace_id):
            return {"ok": True}

        async def geometry_stats(self, node_path):
            return {"ok": True}

        async def work_status(self, workspace_id):
            return {"ok": True}

    service = object.__new__(RuntimeService)
    service._modeling_catalog_provider = None
    provider = Provider()
    service._read_only_provider = provider
    service._knowledge = _Knowledge()
    first, second = asyncio.run(
        _contexts(service)
    )
    assert first is not second
    assert first.read_only is provider
    assert second.read_only is provider


async def _contexts(service):
    return (
        await service._build_runtime_context("ses_1", "run_1"),
        await service._build_runtime_context("ses_1", "run_2"),
    )
