from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eee_agent.changesets import (
    OwnedNodeRef,
    WorkspaceManifest,
    WorkspaceHealth,
    WorkspaceInspectionSummary,
    WorkspaceLifecycleSummary,
    WorkspaceService,
    WorkspaceSummary,
)
from eee_agent.changesets.repository import ChangeSetRepository
from eee_agent.core import AgentException
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectResult,
    WorkspaceInspectionConflict,
    WorkspaceInspectionUnavailable,
    WorkspaceNodeObservation,
)
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.run(coro)


def _binding(instance_id: str = "hou_instance_1", epoch: int = 1) -> SceneBinding:
    return SceneBinding(
        instance_id=instance_id,
        scene_epoch=epoch,
        hip_path=None,
        observed_revision=f"scene-{epoch}",
    )


def _observation(**overrides: object) -> WorkspaceNodeObservation:
    values: dict[str, object] = {
        "path": "/obj/ws",
        "node_type": "geo",
        "parent_path": "/obj",
        "is_locked": False,
        "workspace_id": WS,
        "node_id": "n_root",
        "capability": "modeling",
        "role": "root",
        "schema_version": 1,
        "created_by_run": RUN,
    }
    values.update(overrides)
    return WorkspaceNodeObservation(**values)  # type: ignore[arg-type]


def _result(
    observations: tuple[WorkspaceNodeObservation, ...] = (_observation(),),
    *,
    mode: str = "selection",
    binding: SceneBinding | None = None,
) -> WorkspaceInspectResult:
    return WorkspaceInspectResult.build(
        binding=binding or _binding(), mode=mode, observations=observations
    )


class FakeProvider:
    def __init__(
        self,
        selection: WorkspaceInspectResult | BaseException,
        manifest: WorkspaceInspectResult | BaseException | None = None,
    ) -> None:
        self.selection = selection
        self.manifest = selection if manifest is None else manifest
        self.selection_calls = 0
        self.manifest_calls = 0

    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        self.selection_calls += 1
        if isinstance(self.selection, BaseException):
            raise self.selection
        return self.selection

    async def inspect_manifest(
        self, manifest, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        self.manifest_calls += 1
        if isinstance(self.manifest, BaseException):
            raise self.manifest
        return self.manifest


async def _fresh(
    db_path: Path, provider: FakeProvider
) -> tuple[RuntimeDatabase, ChangeSetRepository, WorkspaceService]:
    db = await RuntimeDatabase.open(db_path)
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id, title, status, created_at, updated_at, "
            "last_seq, replay_floor_seq) VALUES (?, 'T', 'active', ?, ?, 0, 0)",
            (SES, NOW.isoformat(), NOW.isoformat()),
        )
        await conn.execute(
            "INSERT INTO runs(run_id, session_id, status, user_input, final_response, "
            "created_at, started_at, finished_at, failure_json, model_snapshot_json) "
            "VALUES (?, ?, 'Completed', 'in', NULL, ?, NULL, ?, NULL, '{}')",
            (RUN, SES, NOW.isoformat(), NOW.isoformat()),
        )
    repo = ChangeSetRepository(db, events=EventStore(db))
    return db, repo, WorkspaceService(repo, provider=provider, clock=lambda: NOW)


def _code(exc: AgentException) -> str:
    return exc.error.code


def test_create_builds_exact_selection_manifest_and_activates(db_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        db, repo, service = await _fresh(db_path, provider)
        try:
            summary = await service.create(SES, expected_scene_epoch=None)
            assert type(summary) is WorkspaceLifecycleSummary
            assert summary.workspace.workspace_id == WS
            assert summary.workspace.active is True
            assert summary.active_workspace_id == WS
            assert summary.changed is True
            manifest = await repo.get_workspace(WS)
            assert manifest.roots == manifest.nodes
            assert [node.node_id for node in manifest.nodes] == ["n_root"]
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    ("observations", "code"),
    [
        ((), "workspace.no_selection"),
        ((_observation(workspace_id=None, node_id=None, capability=None, role=None,
                       schema_version=None, created_by_run=None),),
         "workspace.unowned_selection"),
        ((_observation(role=None),), "workspace.incomplete_selection"),
        ((_observation(workspace_id=f"ws_{'3' * 32}"), _observation(
            node_id="n_other", path="/obj/other"
        )),
         "workspace.mixed_selection"),
        ((_observation(is_locked=True),), "workspace.locked_selection"),
    ],
)
def test_create_rejects_invalid_selection(
    db_path: Path,
    observations: tuple[WorkspaceNodeObservation, ...],
    code: str,
) -> None:
    async def scenario() -> None:
        db, _, service = await _fresh(db_path, FakeProvider(_result(observations)))
        try:
            with pytest.raises(AgentException) as exc:
                await service.create(SES, expected_scene_epoch=1)
            assert _code(exc.value) == code
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM workspaces"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_bind_allows_only_locator_and_binding_refresh(db_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        db, repo, service = await _fresh(db_path, provider)
        try:
            created = await service.create(SES, expected_scene_epoch=1)
            provider.selection = _result(
                (_observation(path="/obj/renamed", parent_path="/obj"),),
                binding=_binding("hou_instance_2", 2),
            )
            bound = await service.bind(
                SES,
                WS,
                expected_manifest_revision=created.workspace.revision,
                expected_scene_epoch=None,
            )
            assert bound.changed is True
            manifest = await repo.get_workspace(WS)
            assert manifest.instance_id == "hou_instance_2"
            assert manifest.scene_epoch == 2
            assert manifest.nodes[0].path == "/obj/renamed"
        finally:
            await db.close()

    _run(scenario())


def test_bind_rejects_subset_or_identity_change(db_path: Path) -> None:
    async def scenario() -> None:
        second = _observation(
            node_id="n_child", path="/obj/ws/child", parent_path="/obj/ws", role="member"
        )
        provider = FakeProvider(_result((_observation(), second)))
        db, _, service = await _fresh(db_path, provider)
        try:
            created = await service.create(SES, expected_scene_epoch=1)
            provider.selection = _result((_observation(),))
            with pytest.raises(AgentException) as exc:
                await service.bind(
                    SES,
                    WS,
                    expected_manifest_revision=created.workspace.revision,
                    expected_scene_epoch=1,
                )
            assert _code(exc.value) == "workspace.identity_conflict"
        finally:
            await db.close()

    _run(scenario())


def test_switch_live_verifies_before_active_cas(db_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        db, repo, service = await _fresh(db_path, provider)
        try:
            created = await service.create(SES, expected_scene_epoch=1)
            provider.manifest = _result(mode="manifest")
            noop = await service.switch(
                SES,
                WS,
                expected_active_workspace_id=WS,
                expected_scene_epoch=1,
            )
            assert noop.changed is False
            provider.manifest = _result(
                (_observation(path="/obj/stale"),), mode="manifest"
            )
            with pytest.raises(AgentException) as exc:
                await service.switch(
                    SES,
                    WS,
                    expected_active_workspace_id=WS,
                    expected_scene_epoch=1,
                )
            assert _code(exc.value) == "workspace.revision_conflict"
            assert (await repo.get_active_workspace(SES)).active_workspace_id == WS
            assert created.workspace.active is True
        finally:
            await db.close()

    _run(scenario())


def test_switch_rejects_bind_committed_after_live_verification(db_path: Path) -> None:
    class RacingProvider(FakeProvider):
        repository: ChangeSetRepository
        rebound = None

        async def inspect_manifest(
            self, manifest, expected_scene_epoch: int | None
        ) -> WorkspaceInspectResult:
            self.manifest_calls += 1
            await self.repository.bind_workspace(
                self.rebound, expected_manifest_revision=manifest.revision
            )
            assert not isinstance(self.manifest, BaseException)
            return self.manifest

    async def scenario() -> None:
        target_ws = f"ws_{'3' * 32}"
        old_live = _result(
            (_observation(workspace_id=target_ws),), mode="manifest"
        )
        provider = RacingProvider(_result(), old_live)
        db, repo, service = await _fresh(db_path, provider)
        try:
            await service.create(SES, expected_scene_epoch=1)
            target_node = OwnedNodeRef(
                node_id="n_root",
                path="/obj/ws",
                node_type="geo",
                parent_path="/obj",
                capability="modeling",
                role="root",
            )
            target = await repo.insert_workspace(
                WorkspaceManifest.build(
                    workspace_id=target_ws,
                    session_id=SES,
                    instance_id="hou_instance_1",
                    scene_epoch=1,
                    roots=(target_node,),
                    nodes=(target_node,),
                    created_by_run=RUN,
                    updated_at=NOW,
                )
            )
            provider.repository = repo
            moved = OwnedNodeRef(
                node_id="n_root",
                path="/obj/moved",
                node_type="geo",
                parent_path="/obj",
                capability="modeling",
                role="root",
            )
            provider.rebound = WorkspaceManifest.build(
                workspace_id=target_ws,
                session_id=SES,
                instance_id="hou_instance_1",
                scene_epoch=1,
                roots=(moved,),
                nodes=(moved,),
                created_by_run=RUN,
                updated_at=NOW + timedelta(seconds=1),
            )
            with pytest.raises(AgentException) as exc:
                await service.switch(
                    SES,
                    target_ws,
                    expected_active_workspace_id=WS,
                    expected_scene_epoch=1,
                )
            assert _code(exc.value) == "workspace.revision_conflict"
            assert (await repo.get_active_workspace(SES)).active_workspace_id == WS
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    ("live", "status"),
    [
        (_result(mode="manifest"), WorkspaceHealth.HEALTHY),
        (_result((_observation(path="/obj/stale"),), mode="manifest"), WorkspaceHealth.STALE),
        (WorkspaceInspectionConflict("duplicate identity"), WorkspaceHealth.CONFLICT),
        (WorkspaceInspectionUnavailable("offline"), WorkspaceHealth.BRIDGE_UNAVAILABLE),
    ],
)
def test_inspect_reports_all_four_health_states(
    db_path: Path,
    live: WorkspaceInspectResult | BaseException,
    status: WorkspaceHealth,
) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        db, _, service = await _fresh(db_path, provider)
        try:
            await service.create(SES, expected_scene_epoch=1)
            provider.manifest = live
            result = await service.inspect(
                SES, None, expected_scene_epoch=None
            )
            assert type(result) is WorkspaceInspectionSummary
            assert result.status is status
            assert result.active_workspace_id == WS
            assert result.target_manifest.workspace_id == WS
            assert result.to_dict()["status"] == status.value
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_create_maps_provider_identity_conflict_to_bounded_error(db_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        provider.selection = WorkspaceInspectionConflict("duplicate")
        db, _, service = await _fresh(db_path, provider)
        try:
            with pytest.raises(AgentException) as exc:
                await service.create(SES, expected_scene_epoch=None)
            assert _code(exc.value) == "workspace.identity_conflict"
        finally:
            await db.close()

    _run(scenario())


def test_missing_session_fails_before_provider_io(db_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        db, _, service = await _fresh(db_path, provider)
        try:
            with pytest.raises(AgentException):
                await service.create(f"ses_{'9' * 32}", expected_scene_epoch=None)
            assert provider.selection_calls == 0
        finally:
            await db.close()

    _run(scenario())


def test_public_workspace_records_are_strict_and_json_safe() -> None:
    summary = WorkspaceSummary(
        workspace_id=WS,
        revision="a" * 64,
        node_count=1,
        instance_id="hou_instance_1",
        scene_epoch=1,
        active=True,
    )
    assert summary.to_dict()["workspace_id"] == WS
    with pytest.raises(TypeError):
        WorkspaceSummary(  # type: ignore[arg-type]
            workspace_id=WS,
            revision="a" * 64,
            node_count=True,
            instance_id="hou_instance_1",
            scene_epoch=1,
            active=True,
        )
    with pytest.raises(TypeError):
        WorkspaceSummary(  # type: ignore[arg-type]
            workspace_id=1,
            revision="a" * 64,
            node_count=1,
            instance_id="hou_instance_1",
            scene_epoch=1,
            active=True,
        )
    with pytest.raises(TypeError):
        WorkspaceLifecycleSummary(  # type: ignore[arg-type]
            workspace=summary,
            active_workspace_id=None,
            state_revision=None,
            changed=1,
        )


def test_bind_and_switch_missing_workspace_use_public_error(db_path: Path) -> None:
    async def scenario() -> None:
        db, _, service = await _fresh(db_path, FakeProvider(_result()))
        missing = f"ws_{'9' * 32}"
        try:
            with pytest.raises(AgentException) as bind_exc:
                await service.bind(
                    SES,
                    missing,
                    expected_manifest_revision="a" * 64,
                    expected_scene_epoch=None,
                )
            assert _code(bind_exc.value) == "workspace.not_found"
            with pytest.raises(AgentException) as switch_exc:
                await service.switch(
                    SES,
                    missing,
                    expected_active_workspace_id=None,
                    expected_scene_epoch=None,
                )
            assert _code(switch_exc.value) == "workspace.not_found"
        finally:
            await db.close()

    _run(scenario())


def test_service_rejects_wrong_id_type_before_provider_io(db_path: Path) -> None:
    async def scenario() -> None:
        provider = FakeProvider(_result())
        db, _, service = await _fresh(db_path, provider)
        try:
            with pytest.raises(TypeError):
                await service.create(1, expected_scene_epoch=None)  # type: ignore[arg-type]
            assert provider.selection_calls == 0
        finally:
            await db.close()

    _run(scenario())


def test_inspection_summary_rejects_unbounded_session_workspace_list(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, repo, service = await _fresh(db_path, FakeProvider(_result()))
        try:
            created = await service.create(SES, expected_scene_epoch=1)
            target = await repo.get_workspace(WS)
            summaries = (created.workspace,) + tuple(
                WorkspaceSummary(
                    workspace_id=f"ws_{index:032x}",
                    revision="a" * 64,
                    node_count=1,
                    instance_id="hou_instance_1",
                    scene_epoch=1,
                    active=False,
                )
                for index in range(5000)
                if f"ws_{index:032x}" != WS
            )
            with pytest.raises(ValueError, match="bounded"):
                WorkspaceInspectionSummary(
                    workspaces=summaries,
                    active_workspace_id=WS,
                    target_manifest=target,
                    status=WorkspaceHealth.HEALTHY,
                )
        finally:
            await db.close()

    _run(scenario())
