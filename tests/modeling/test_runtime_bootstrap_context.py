from __future__ import annotations

import asyncio
from pathlib import Path

from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.modeling.catalog import houdini_21_minimal_catalog
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.service import RuntimeService
from tests.runtime.test_service import FakeRunner, _factory_for, _success_items


def test_runtime_builds_bootstrap_context_when_session_has_no_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(tmp_path / "home"))
    paths = RuntimePaths.from_environment()
    binding = SceneBinding(
        instance_id="houdini_1",
        scene_epoch=1,
        hip_path=None,
        observed_revision="a" * 64,
    )

    async def scenario() -> None:
        runner = FakeRunner(_success_items())
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(runner),
            changeset_binding_provider=lambda: binding,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        ) as service:
            session = await service.create_session("bootstrap")
            started = await service.start_run(session.session_id, "prepare")
            await service.wait_for_run(started.run_id)
            tool_context = await service._build_modeling_context(  # noqa: SLF001
                session.session_id, started.run_id
            )
            assert tool_context is not None
            proposal_context = tool_context.coordinator._context  # noqa: SLF001
            assert proposal_context.workspace is None
            assert proposal_context.bootstrap is not None
            assert proposal_context.bootstrap.workspace_id.startswith("ws_")
            assert proposal_context.bootstrap.root_name.startswith("eee_model_")
            assert proposal_context.scene_binding == binding

    asyncio.run(scenario())
