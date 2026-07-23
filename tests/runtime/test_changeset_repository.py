"""Task 16-B1: typed ChangeSet persistence repository tests.

Drives the schema-v2 :class:`ChangeSetRepository` against a real
:class:`RuntimeDatabase` (no ``hou``, ``rpyc``, transport, or LLM). Each test
runs an async scenario through ``asyncio.run`` and treats the database as a
synchronous black box, mirroring the existing Runtime repository tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eee_agent.changesets import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    ConnectInput,
    CreateNode,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    ReceiptStatus,
    RiskSummary,
    SetParm,
    WorkspaceManifest,
)
from eee_agent.changesets import repository as repo_mod
from eee_agent.changesets.repository import (
    ChangeSetState,
    ChangeSetRepository,
    StoredChangeSet,
    WorkspaceMutationResult,
    WorkspaceStateRecord,
)
from eee_agent.core import AgentException
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore

# --------------------------------------------------------------------------
# constants + DTO factories (compact, self-contained)
# --------------------------------------------------------------------------

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
CHG = f"chg_{'4' * 32}"
APR = f"apr_{'5' * 32}"
REVISION = "a" * 64
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(seconds=30)
SOON = NOW + timedelta(seconds=10)
NOW_ISO = NOW.isoformat()

_SESSION_INSERT = (
    "INSERT INTO sessions(session_id, title, status, created_at, updated_at, "
    "last_seq, replay_floor_seq) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_RUN_INSERT = (
    "INSERT INTO runs(run_id, session_id, status, user_input, final_response, "
    "created_at, started_at, finished_at, failure_json, model_snapshot_json) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _run(coro):
    return asyncio.run(coro)


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = dict(
        instance_id="hou_instance_1",
        scene_epoch=1,
        hip_path=None,
        observed_revision="rev-1",
    )
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _owned(**overrides: object) -> OwnedNodeRef:
    values: dict[str, object] = dict(
        node_id="n_root",
        path="/obj/ws",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    values.update(overrides)
    return OwnedNodeRef(**values)  # type: ignore[arg-type]


def _noderef(**overrides: object) -> NodeRef:
    values: dict[str, object] = dict(
        node_id="n_child",
        path="/obj/ws/geo1",
        expected_type="geo",
        expected_workspace_id=WS,
    )
    values.update(overrides)
    return NodeRef(**values)  # type: ignore[arg-type]


def _create(**overrides: object) -> CreateNode:
    values: dict[str, object] = dict(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="subnet"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    values.update(overrides)
    return CreateNode(**values)  # type: ignore[arg-type]


def _setparm(**overrides: object) -> SetParm:
    values: dict[str, object] = dict(
        op_id="op_parm",
        target=_noderef(),
        parm_name="tx",
        value=0,
        expected_old_value=0,
    )
    values.update(overrides)
    return SetParm(**values)  # type: ignore[arg-type]


def _connect(**overrides: object) -> ConnectInput:
    values: dict[str, object] = dict(
        op_id="op_wire",
        target=_noderef(node_id="n_in", path="/obj/ws/in1", expected_type="merge"),
        input_index=0,
        source=_noderef(node_id="n_src", path="/obj/ws/src1", expected_type="xform"),
        source_output_index=0,
        expected_old_source=None,
    )
    values.update(overrides)
    return ConnectInput(**values)  # type: ignore[arg-type]


def _roots_nodes() -> tuple[list[OwnedNodeRef], list[OwnedNodeRef]]:
    root = _owned()
    child = _owned(
        node_id="n_child_0",
        path="/obj/ws/c0",
        parent_path="/obj/ws",
        role="member",
    )
    return [root], [root, child]


def _manifest(**overrides: object) -> WorkspaceManifest:
    roots, nodes = _roots_nodes()
    values: dict[str, object] = dict(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=roots,
        nodes=nodes,
        created_by_run=RUN,
        updated_at=NOW,
    )
    values.update(overrides)
    return WorkspaceManifest.build(**values)  # type: ignore[arg-type]


def _risk(**overrides: object) -> RiskSummary:
    values: dict[str, object] = dict(
        touches_external_nodes=False,
        changes_wiring=False,
        requires_backup=False,
        operation_count=1,
        effect_names=("node.create",),
        affected_paths=("/obj/ws/geo_new",),
    )
    values.update(overrides)
    return RiskSummary(**values)  # type: ignore[arg-type]


def _checkpoint(**overrides: object) -> CheckpointPlan:
    values: dict[str, object] = dict(nodes=(), parameters=(), wires=())
    values.update(overrides)
    return CheckpointPlan(**values)  # type: ignore[arg-type]


def _changeset(**overrides: object) -> ChangeSet:
    values: dict[str, object] = dict(
        change_id=CHG,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=REVISION,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(_create(),),
        affected_nodes=(_noderef(node_id="n_new", path="/obj/ws/geo_new"),),
        read_dependencies=(),
        preconditions=(),
        expected_postconditions=(
            ParmValueEquals(target=_noderef(node_id="n_new", path="/obj/ws/geo_new"), parm_name="tx", value=0),
        ),
        risk_summary=_risk(),
        checkpoint_plan=_checkpoint(),
        created_at=NOW,
    )
    values.update(overrides)
    return ChangeSet(**values)  # type: ignore[arg-type]


def _approval(**overrides: object) -> ApprovalRecord:
    values: dict[str, object] = dict(
        approval_id=APR,
        change_id=CHG,
        changeset_digest=REVISION,
        decision=ApprovalDecision.PENDING,
        decided_by=None,
        requested_at=NOW,
        decided_at=None,
        expires_at=LATER,
        approved_instance_id=None,
        approved_scene_epoch=None,
    )
    values.update(overrides)
    return ApprovalRecord(**values)  # type: ignore[arg-type]


def _result(passed: bool = True, kind: str = "parm.value_equals") -> ConditionResult:
    return ConditionResult(kind=kind, passed=passed)


def _receipt(**overrides: object) -> ChangeReceipt:
    values: dict[str, object] = dict(
        change_id=CHG,
        status=ReceiptStatus.APPLIED,
        instance_id="hou_instance_1",
        scene_epoch=1,
        before_revision=REVISION,
        after_revision="b" * 64,
        applied_op_ids=("op_create",),
        postcondition_results=(_result(True),),
        rollback_results=(),
        scene_may_have_changed=False,
        completed_at=LATER,
    )
    values.update(overrides)
    return ChangeReceipt(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------


async def add_session(db: RuntimeDatabase, sid: str = SES) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _SESSION_INSERT, (sid, "T", "active", NOW_ISO, NOW_ISO, 0, 0)
        )


async def add_run(
    db: RuntimeDatabase, rid: str = RUN, sid: str = SES, model: str = "{}"
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _RUN_INSERT, (rid, sid, "Created", "in", None, NOW_ISO, None, None, None, model)
        )


async def fresh_repo(db_path: Path) -> tuple[RuntimeDatabase, ChangeSetRepository]:
    db = await RuntimeDatabase.open(db_path)
    await add_session(db)
    await add_run(db)
    return db, ChangeSetRepository(db)


def _err_code(exc: BaseException) -> str:
    assert isinstance(exc, AgentException), f"expected AgentException, got {type(exc)}"
    return exc.error.code


# --------------------------------------------------------------------------
# schema v2 surface
# --------------------------------------------------------------------------


def test_schema_v3_preserves_changeset_tables_and_adds_workspace_state(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            names = await db.table_names()
            for table in (
                "workspaces",
                "changesets",
                "approvals",
                "change_receipts",
                "session_workspace_state",
            ):
                assert table in names
            assert await db.schema_version() == 7
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# workspace manifest round-trip + FK/unique
# --------------------------------------------------------------------------


def test_workspace_manifest_round_trips_with_equal_dict_and_digest(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            manifest = _manifest()
            inserted = await repo.insert_workspace(manifest)
            assert inserted.to_dict() == manifest.to_dict()
            got = await repo.get_workspace(WS)
            assert got.to_dict() == manifest.to_dict()
            assert got.revision == manifest.revision
        finally:
            await db.close()

    _run(scenario())


def test_list_workspaces_scoped_to_session(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            other_ses = f"ses_{'7' * 32}"
            await add_session(db, other_ses)
            m1 = _manifest(workspace_id=f"ws_{'a' * 32}")
            m2 = _manifest(workspace_id=f"ws_{'b' * 32}", updated_at=LATER)
            m_other = _manifest(
                workspace_id=f"ws_{'c' * 32}", session_id=other_ses
            )
            await repo.insert_workspace(m1)
            await repo.insert_workspace(m2)
            await repo.insert_workspace(m_other)
            listed = await repo.list_workspaces(SES)
            assert {m.workspace_id for m in listed} == {m1.workspace_id, m2.workspace_id}
            listed_other = await repo.list_workspaces(other_ses)
            assert [m.workspace_id for m in listed_other] == [m_other.workspace_id]
        finally:
            await db.close()

    _run(scenario())


def test_workspace_rejects_duplicate_id(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_workspace(_manifest())
            with pytest.raises(AgentException):
                await repo.insert_workspace(_manifest())
        finally:
            await db.close()

    _run(scenario())


def test_workspace_rejects_missing_session_and_run_fk(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises(AgentException):
                await repo.insert_workspace(_manifest(session_id=f"ses_{'9' * 32}"))
            with pytest.raises(AgentException):
                await repo.insert_workspace(_manifest(created_by_run=f"run_{'9' * 32}"))
        finally:
            await db.close()

    _run(scenario())


def test_get_workspace_missing_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises(AgentException):
                await repo.get_workspace(f"ws_{'0' * 32}")
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# schema-v3 workspace lifecycle primitives
# --------------------------------------------------------------------------


def test_create_workspace_lifecycle_commits_manifest_state_and_event(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            result = await repo.create_workspace_lifecycle(_manifest())
            assert type(result) is WorkspaceMutationResult
            assert result.changed is True
            assert result.manifest.workspace_id == WS
            assert type(result.state) is WorkspaceStateRecord
            assert result.state.active_workspace_id == WS
            assert result.state.state_revision == 1
            assert [event.event_type for event in result.events] == ["workspace.created"]
            assert set(result.events[0].payload) == {
                "session_id",
                "workspace_id",
                "revision",
                "node_count",
                "instance_id",
                "scene_epoch",
                "active",
                "state_revision",
            }
            assert "roots" not in result.events[0].payload
            assert "nodes" not in result.events[0].payload
            assert (await repo.get_active_workspace(SES)).active_workspace_id == WS
        finally:
            await db.close()

    _run(scenario())


def test_create_workspace_lifecycle_exact_retry_is_event_free(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            first = await repo.create_workspace_lifecycle(_manifest())
            second = await repo.create_workspace_lifecycle(_manifest(updated_at=LATER))
            assert first.changed is True
            assert second.changed is False
            assert second.events == ()
            assert second.manifest.updated_at == first.manifest.updated_at
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_create_workspace_lifecycle_rejects_run_from_another_session(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        other_session = f"ses_{'7' * 32}"
        await add_session(db, other_session)
        other_run = f"run_{'8' * 32}"
        await add_run(db, other_run, other_session)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            with pytest.raises(AgentException) as exc:
                await repo.create_workspace_lifecycle(
                    _manifest(created_by_run=other_run)
                )
            assert _err_code(exc.value) == "workspace.session_mismatch"
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM workspaces"))["c"] == 0
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_bind_workspace_compare_update_and_noop(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            created = await repo.create_workspace_lifecycle(_manifest())
            moved_root = _owned(path="/obj/renamed")
            moved_child = _owned(
                node_id="n_child_0",
                path="/obj/renamed/c0",
                parent_path="/obj/renamed",
                role="member",
            )
            refreshed = _manifest(
                instance_id="hou_instance_2",
                scene_epoch=2,
                roots=[moved_root],
                nodes=[moved_root, moved_child],
                updated_at=LATER,
            )
            bound = await repo.bind_workspace(
                refreshed, expected_manifest_revision=created.manifest.revision
            )
            assert bound.changed is True
            assert [event.event_type for event in bound.events] == ["workspace.bound"]
            assert bound.state.state_revision == 1
            noop = await repo.bind_workspace(
                _manifest(
                    instance_id="hou_instance_2",
                    scene_epoch=2,
                    roots=[moved_root],
                    nodes=[moved_root, moved_child],
                    updated_at=LATER + timedelta(seconds=1),
                ),
                expected_manifest_revision=refreshed.revision,
            )
            assert noop.changed is False
            assert noop.events == ()
            assert noop.manifest.updated_at == LATER
        finally:
            await db.close()

    _run(scenario())


def test_bind_workspace_rejects_stale_revision_without_event(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            await repo.create_workspace_lifecycle(_manifest())
            with pytest.raises(AgentException) as exc:
                await repo.bind_workspace(
                    _manifest(), expected_manifest_revision="f" * 64
                )
            assert _err_code(exc.value) == "workspace.revision_conflict"
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_switch_workspace_uses_active_cas_and_suppresses_noop(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        other_ws = f"ws_{'3' * 32}"
        try:
            await repo.create_workspace_lifecycle(_manifest())
            other_manifest = _manifest(workspace_id=other_ws)
            await repo.insert_workspace(other_manifest)
            switched = await repo.switch_workspace(
                SES,
                other_ws,
                expected_active_workspace_id=WS,
                expected_manifest_revision=other_manifest.revision,
                updated_at=LATER,
            )
            assert switched.changed is True
            assert switched.state.active_workspace_id == other_ws
            assert switched.state.state_revision == 2
            assert [event.event_type for event in switched.events] == ["workspace.updated"]
            noop = await repo.switch_workspace(
                SES,
                other_ws,
                expected_active_workspace_id=other_ws,
                expected_manifest_revision=other_manifest.revision,
                updated_at=LATER + timedelta(seconds=1),
            )
            assert noop.changed is False
            assert noop.events == ()
            assert noop.state.updated_at == LATER
            with pytest.raises(AgentException) as exc:
                await repo.switch_workspace(
                    SES,
                    WS,
                    expected_active_workspace_id=WS,
                    expected_manifest_revision=_manifest().revision,
                    updated_at=LATER + timedelta(seconds=2),
                )
            assert _err_code(exc.value) == "workspace.active_conflict"
            assert (await repo.get_active_workspace(SES)).active_workspace_id == other_ws
        finally:
            await db.close()

    _run(scenario())


def test_switch_rejects_manifest_changed_after_live_verification(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        target_ws = f"ws_{'3' * 32}"
        try:
            await repo.create_workspace_lifecycle(_manifest())
            target = _manifest(workspace_id=target_ws)
            await repo.insert_workspace(target)
            moved_root = _owned(path="/obj/moved")
            moved_child = _owned(
                node_id="n_child_0",
                path="/obj/moved/c0",
                parent_path="/obj/moved",
                role="member",
            )
            rebound = _manifest(
                workspace_id=target_ws,
                roots=[moved_root],
                nodes=[moved_root, moved_child],
                updated_at=LATER,
            )
            await repo.bind_workspace(
                rebound, expected_manifest_revision=target.revision
            )
            with pytest.raises(AgentException) as exc:
                await repo.switch_workspace(
                    SES,
                    target_ws,
                    expected_active_workspace_id=WS,
                    expected_manifest_revision=target.revision,
                    updated_at=LATER + timedelta(seconds=1),
                )
            assert _err_code(exc.value) == "workspace.revision_conflict"
            assert (await repo.get_active_workspace(SES)).active_workspace_id == WS
        finally:
            await db.close()

    _run(scenario())


def test_workspace_lifecycle_event_failure_rolls_back_state(db_path: Path) -> None:
    class FailingEventStore(EventStore):
        async def _append_conn(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("injected event failure")

    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=FailingEventStore(db))
        try:
            with pytest.raises(RuntimeError, match="injected"):
                await repo.create_workspace_lifecycle(_manifest())
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM workspaces"))["c"] == 0
            assert (
                await db.fetchone(
                    "SELECT COUNT(*) AS c FROM session_workspace_state"
                )
            )["c"] == 0
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_list_workspace_state_verifies_manifest_digest(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            await repo.create_workspace_lifecycle(_manifest())
            manifests, state = await repo.list_workspace_state(SES)
            assert [manifest.workspace_id for manifest in manifests] == [WS]
            assert state.active_workspace_id == WS
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE workspaces SET payload_json = '{}' WHERE workspace_id = ?",
                    (WS,),
                )
            with pytest.raises(AgentException) as exc:
                await repo.list_workspace_state(SES)
            assert _err_code(exc.value) == "runtime.record_corrupt"
        finally:
            await db.close()

    _run(scenario())


def test_bind_rechecks_creating_run_session_inside_transaction(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        other_session = f"ses_{'7' * 32}"
        try:
            await add_session(db, other_session)
            created = await repo.create_workspace_lifecycle(_manifest())
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE runs SET session_id = ? WHERE run_id = ?",
                    (other_session, RUN),
                )
            with pytest.raises(AgentException) as exc:
                await repo.bind_workspace(
                    _manifest(updated_at=LATER),
                    expected_manifest_revision=created.manifest.revision,
                )
            assert _err_code(exc.value) == "workspace.session_mismatch"
        finally:
            await db.close()

    _run(scenario())


def test_switch_rechecks_creating_run_session_inside_transaction(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        other_session = f"ses_{'7' * 32}"
        try:
            await add_session(db, other_session)
            await repo.create_workspace_lifecycle(_manifest())
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE runs SET session_id = ? WHERE run_id = ?",
                    (other_session, RUN),
                )
            with pytest.raises(AgentException) as exc:
                await repo.switch_workspace(
                    SES,
                    WS,
                    expected_active_workspace_id=WS,
                    expected_manifest_revision=_manifest().revision,
                    updated_at=LATER,
                )
            assert _err_code(exc.value) == "workspace.session_mismatch"
        finally:
            await db.close()

    _run(scenario())


def test_cancelled_workspace_event_append_rolls_back_manifest_and_state(
    db_path: Path,
) -> None:
    entered = asyncio.Event()

    class BlockingEventStore(EventStore):
        async def _append_conn(self, *args, **kwargs):  # type: ignore[override]
            entered.set()
            await asyncio.Future()

    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=BlockingEventStore(db))
        try:
            task = asyncio.create_task(repo.create_workspace_lifecycle(_manifest()))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM workspaces"))["c"] == 0
            assert (
                await db.fetchone(
                    "SELECT COUNT(*) AS c FROM session_workspace_state"
                )
            )["c"] == 0
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_concurrent_switches_have_one_cas_winner(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        ws2 = f"ws_{'3' * 32}"
        ws3 = f"ws_{'4' * 32}"
        try:
            await repo.create_workspace_lifecycle(_manifest())
            manifest2 = _manifest(workspace_id=ws2)
            manifest3 = _manifest(workspace_id=ws3)
            await repo.insert_workspace(manifest2)
            await repo.insert_workspace(manifest3)
            results = await asyncio.gather(
                repo.switch_workspace(
                    SES,
                    ws2,
                    expected_active_workspace_id=WS,
                    expected_manifest_revision=manifest2.revision,
                    updated_at=LATER,
                ),
                repo.switch_workspace(
                    SES,
                    ws3,
                    expected_active_workspace_id=WS,
                    expected_manifest_revision=manifest3.revision,
                    updated_at=LATER + timedelta(seconds=1),
                ),
                return_exceptions=True,
            )
            winners = [result for result in results if isinstance(result, WorkspaceMutationResult)]
            losers = [result for result in results if isinstance(result, AgentException)]
            assert len(winners) == 1
            assert len(losers) == 1
            assert _err_code(losers[0]) == "workspace.active_conflict"
            state = await repo.get_active_workspace(SES)
            assert state.active_workspace_id in {ws2, ws3}
            assert state.state_revision == 2
        finally:
            await db.close()

    _run(scenario())


def test_active_workspace_state_survives_database_restart(db_path: Path) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        repo = ChangeSetRepository(db, events=EventStore(db))
        await repo.create_workspace_lifecycle(_manifest())
        await db.close()
        reopened = await RuntimeDatabase.open(db_path)
        try:
            restored = await ChangeSetRepository(reopened).get_active_workspace(SES)
            assert restored.active_workspace_id == WS
            assert restored.state_revision == 1
        finally:
            await reopened.close()

    _run(scenario())


def test_migrated_v2_workspace_can_bind_without_state_then_switch_from_null(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, _ = await fresh_repo(db_path)
        plain = ChangeSetRepository(db)
        repo = ChangeSetRepository(db, events=EventStore(db))
        try:
            stored = await plain.insert_workspace(_manifest())
            root = _owned(path="/obj/rebound")
            child = _owned(
                node_id="n_child_0",
                path="/obj/rebound/c0",
                parent_path="/obj/rebound",
                role="member",
            )
            refreshed = _manifest(
                roots=[root], nodes=[root, child], updated_at=LATER
            )
            bound = await repo.bind_workspace(
                refreshed, expected_manifest_revision=stored.revision
            )
            assert bound.changed is True
            assert bound.state is None
            switched = await repo.switch_workspace(
                SES,
                WS,
                expected_active_workspace_id=None,
                expected_manifest_revision=refreshed.revision,
                updated_at=LATER + timedelta(seconds=1),
            )
            assert switched.state.active_workspace_id == WS
            assert switched.state.state_revision == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# changeset round-trip + state machine
# --------------------------------------------------------------------------


def test_changeset_round_trips_with_equal_dict_and_digest(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            cs = _changeset()
            stored = await repo.insert_changeset(cs)
            assert stored.state is ChangeSetState.PROPOSED
            assert stored.changeset.to_dict() == cs.to_dict()
            got = await repo.get_changeset(CHG)
            assert got.changeset.to_dict() == cs.to_dict()
            assert got.changeset.digest == cs.digest
            assert got.state is ChangeSetState.PROPOSED
        finally:
            await db.close()

    _run(scenario())


def test_changeset_preserves_nullable_workspace_id(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            cs = _changeset(workspace_id=None)
            stored = await repo.insert_changeset(cs)
            assert stored.changeset.workspace_id is None
            got = await repo.get_changeset(CHG)
            assert got.changeset.workspace_id is None
        finally:
            await db.close()

    _run(scenario())


def test_changeset_persists_all_state_values(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            for index, state in enumerate(ChangeSetState):
                cid = f"chg_{index:032x}"
                cs = _changeset(change_id=cid)
                stored = await repo.insert_changeset(cs, state=state)
                assert stored.state is state
                got = await repo.get_changeset(cid)
                assert got.state is state
        finally:
            await db.close()

    _run(scenario())


def test_changeset_rejects_duplicate_id(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            with pytest.raises(AgentException):
                await repo.insert_changeset(_changeset())
        finally:
            await db.close()

    _run(scenario())


def test_changeset_rejects_missing_session_and_run_fk(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises(AgentException):
                await repo.insert_changeset(_changeset(session_id=f"ses_{'9' * 32}"))
            with pytest.raises(AgentException):
                await repo.insert_changeset(_changeset(run_id=f"run_{'9' * 32}"))
        finally:
            await db.close()

    _run(scenario())


def test_changeset_cas_transition_forward_path(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            await repo.transition_changeset(
                CHG, from_state=ChangeSetState.PROPOSED, to_state=ChangeSetState.AWAITING_APPROVAL
            )
            await repo.transition_changeset(
                CHG, from_state=ChangeSetState.AWAITING_APPROVAL, to_state=ChangeSetState.APPROVED
            )
            await repo.transition_changeset(
                CHG, from_state=ChangeSetState.APPROVED, to_state=ChangeSetState.APPLYING
            )
            stored = await repo.transition_changeset(
                CHG, from_state=ChangeSetState.APPLYING, to_state=ChangeSetState.APPLIED
            )
            assert stored.state is ChangeSetState.APPLIED
        finally:
            await db.close()

    _run(scenario())


def test_changeset_cas_rejects_wrong_expected_state(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            with pytest.raises(AgentException):
                await repo.transition_changeset(
                    CHG,
                    from_state=ChangeSetState.APPROVED,  # wrong: actually Proposed
                    to_state=ChangeSetState.APPLIED,
                )
            # unchanged after failed CAS
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.PROPOSED
        finally:
            await db.close()

    _run(scenario())


def test_changeset_cas_rejects_illegal_transition(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            with pytest.raises(AgentException):
                # Proposed cannot jump straight to Applied.
                await repo.transition_changeset(
                    CHG,
                    from_state=ChangeSetState.PROPOSED,
                    to_state=ChangeSetState.APPLIED,
                )
            with pytest.raises(AgentException):
                # Applying -> Approved is the documented recovery edge only; a
                # terminal->nonterminal jump like Applied->Approved is illegal.
                await repo.insert_changeset(
                    _changeset(change_id=f"chg_{'e' * 32}"),
                    state=ChangeSetState.APPLIED,
                )
                await repo.transition_changeset(
                    f"chg_{'e' * 32}",
                    from_state=ChangeSetState.APPLIED,
                    to_state=ChangeSetState.APPROVED,
                )
        finally:
            await db.close()

    _run(scenario())


def test_transition_missing_changeset_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises(AgentException):
                await repo.transition_changeset(
                    f"chg_{'0' * 32}",
                    from_state=ChangeSetState.PROPOSED,
                    to_state=ChangeSetState.AWAITING_APPROVAL,
                )
        finally:
            await db.close()

    _run(scenario())


def test_list_changesets_scoped_to_session(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            other_ses = f"ses_{'7' * 32}"
            await add_session(db, other_ses)
            await repo.insert_changeset(_changeset(change_id=f"chg_{'a' * 32}"))
            await repo.insert_changeset(_changeset(change_id=f"chg_{'b' * 32}"))
            await repo.insert_changeset(_changeset(change_id=f"chg_{'c' * 32}", session_id=other_ses))
            listed = await repo.list_changesets(SES)
            assert {s.changeset.change_id for s in listed} == {
                f"chg_{'a' * 32}",
                f"chg_{'b' * 32}",
            }
        finally:
            await db.close()

    _run(scenario())


def test_list_changeset_views_is_newest_limited_and_consistent(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            older = _changeset(
                change_id=f"chg_{'a' * 32}",
                created_at=NOW,
            )
            newer = _changeset(
                change_id=f"chg_{'b' * 32}",
                created_at=LATER,
            )
            await repo.insert_changeset(older)
            await repo.insert_changeset(
                newer, state=ChangeSetState.APPLIED
            )
            await repo.insert_approval(
                _approval(
                    change_id=newer.change_id,
                    changeset_digest=newer.digest,
                    decision=ApprovalDecision.CONSUMED,
                    decided_by="local_user",
                    decided_at=LATER,
                    approved_instance_id="hou_instance_1",
                    approved_scene_epoch=1,
                )
            )
            await repo.insert_receipt(
                _receipt(change_id=newer.change_id)
            )

            views = await repo.list_changeset_views(SES, limit=1)
            assert len(views) == 1
            assert views[0].stored.changeset.change_id == newer.change_id
            assert views[0].approval is not None
            assert views[0].approval.decision is ApprovalDecision.CONSUMED
            assert views[0].receipt is not None
            assert views[0].receipt.status is ReceiptStatus.APPLIED
            with pytest.raises(ValueError):
                await repo.list_changeset_views(SES, limit=True)
            with pytest.raises(ValueError):
                await repo.list_changeset_views(SES, limit=51)
        finally:
            await db.close()

    _run(scenario())


def test_list_changeset_views_rejects_cross_identity_receipt_payload(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            changeset = _changeset()
            await repo.insert_changeset(
                changeset, state=ChangeSetState.APPLIED
            )
            await repo.insert_receipt(_receipt())
            other = _receipt(change_id=f"chg_{'f' * 32}")
            payload = json.dumps(
                other.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE change_receipts SET payload_json = ?, digest = ? "
                    "WHERE change_id = ?",
                    (payload, digest, CHG),
                )
            with pytest.raises(AgentException) as exc:
                await repo.list_changeset_views(SES, limit=50)
            assert exc.value.error.code == "runtime.record_corrupt"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# restart-safe queries
# --------------------------------------------------------------------------


def test_nonterminal_changesets_restart_safe(db_path: Path) -> None:
    async def scenario() -> None:
        db_path_str = str(db_path)
        db, repo = await fresh_repo(db_path)
        await repo.insert_changeset(_changeset(change_id=f"chg_{'1' * 32}"))  # Proposed
        await repo.insert_changeset(
            _changeset(change_id=f"chg_{'2' * 32}"), state=ChangeSetState.AWAITING_APPROVAL
        )
        await repo.insert_changeset(
            _changeset(change_id=f"chg_{'3' * 32}"), state=ChangeSetState.APPROVED
        )
        await repo.insert_changeset(
            _changeset(change_id=f"chg_{'4' * 32}"), state=ChangeSetState.APPLYING
        )
        await repo.insert_changeset(
            _changeset(change_id=f"chg_{'5' * 32}"), state=ChangeSetState.APPLIED
        )
        await repo.insert_changeset(
            _changeset(change_id=f"chg_{'6' * 32}"), state=ChangeSetState.CRITICAL_RECOVERY
        )
        await db.close()
        # reopen and query as if recovering.
        db2 = await RuntimeDatabase.open(db_path_str)
        try:
            repo2 = ChangeSetRepository(db2)
            nonterminal = await repo2.nonterminal_changesets()
            ids = {s.changeset.change_id for s in nonterminal}
            # CriticalRecovery is non-terminal since schema v7: it has a manual
            # recover exit to Recovered, so it is an unresolved blocker that
            # must survive a restart (not a frozen terminal outcome).
            assert ids == {
                f"chg_{'1' * 32}",
                f"chg_{'2' * 32}",
                f"chg_{'3' * 32}",
                f"chg_{'4' * 32}",
                f"chg_{'6' * 32}",
            }
            applying = await repo2.changesets_in_states({ChangeSetState.APPLYING})
            assert [s.changeset.change_id for s in applying] == [f"chg_{'4' * 32}"]
            approved = await repo2.changesets_in_states({ChangeSetState.APPROVED})
            assert [s.changeset.change_id for s in approved] == [f"chg_{'3' * 32}"]
            critical = await repo2.changesets_in_states({ChangeSetState.CRITICAL_RECOVERY})
            assert [s.changeset.change_id for s in critical] == [f"chg_{'6' * 32}"]
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# approval insert / get / update
# --------------------------------------------------------------------------


async def _seed_proposed_with_pending_approval(repo: ChangeSetRepository) -> None:
    await repo.insert_changeset(_changeset())
    digest = (await repo.get_changeset(CHG)).changeset.digest
    await repo.insert_approval(_approval(changeset_digest=digest))


def test_approval_round_trips(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest
            got = await repo.get_approval(CHG)
            assert got.decision is ApprovalDecision.PENDING
            assert got.changeset_digest == digest
            assert got.to_dict() == _approval(changeset_digest=digest).to_dict()
        finally:
            await db.close()

    _run(scenario())


def test_insert_approval_rejects_wrong_changeset_digest(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            with pytest.raises(AgentException) as exc:
                await repo.insert_approval(_approval(changeset_digest="b" * 64))
            assert _err_code(exc.value) == "approval.digest_mismatch"
        finally:
            await db.close()

    _run(scenario())


def test_insert_approval_requires_changeset_fk(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises(AgentException):
                await repo.insert_approval(_approval())
        finally:
            await db.close()

    _run(scenario())


def test_insert_approval_rejects_duplicate_for_change(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest
            with pytest.raises(AgentException):
                await repo.insert_approval(
                    _approval(approval_id=f"apr_{'a' * 32}", changeset_digest=digest)
                )
        finally:
            await db.close()

    _run(scenario())


def test_update_approval_to_approved_with_digest_check(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest
            approved = _approval(
                approval_id=APR,
                change_id=CHG,
                changeset_digest=digest,
                decision=ApprovalDecision.APPROVED,
                decided_by="local_user",
                decided_at=LATER,
                approved_instance_id="hou_instance_1",
                approved_scene_epoch=1,
            )
            updated = await repo.update_approval(
                approved,
                expected_decision=ApprovalDecision.PENDING,
                expected_changeset_digest=digest,
            )
            assert updated.decision is ApprovalDecision.APPROVED
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.APPROVED
        finally:
            await db.close()

    _run(scenario())


def test_update_approval_rejects_wrong_expected_decision(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest
            approved = _approval(
                changeset_digest=digest,
                decision=ApprovalDecision.APPROVED,
                decided_by="local_user",
                decided_at=LATER,
                approved_instance_id="hou_instance_1",
                approved_scene_epoch=1,
            )
            with pytest.raises(AgentException):
                await repo.update_approval(
                    approved,
                    expected_decision=ApprovalDecision.APPROVED,  # wrong: currently Pending
                    expected_changeset_digest=digest,
                )
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.PENDING
        finally:
            await db.close()

    _run(scenario())


def test_update_approval_rejects_wrong_digest(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            approved = _approval(
                changeset_digest="b" * 64,
                decision=ApprovalDecision.APPROVED,
                decided_by="local_user",
                decided_at=LATER,
                approved_instance_id="hou_instance_1",
                approved_scene_epoch=1,
            )
            with pytest.raises(AgentException) as exc:
                await repo.update_approval(
                    approved,
                    expected_decision=ApprovalDecision.PENDING,
                    expected_changeset_digest="b" * 64,
                )
            assert _err_code(exc.value) == "approval.digest_mismatch"
        finally:
            await db.close()

    _run(scenario())


def test_update_approval_rejects_rejected_then_reapprove(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest
            rejected = _approval(
                changeset_digest=digest,
                decision=ApprovalDecision.REJECTED,
                decided_by="local_user",
                decided_at=LATER,
            )
            await repo.update_approval(
                rejected,
                expected_decision=ApprovalDecision.PENDING,
                expected_changeset_digest=digest,
            )
            # rejected is terminal: cannot move back to approved.
            approved = _approval(
                changeset_digest=digest,
                decision=ApprovalDecision.APPROVED,
                decided_by="local_user",
                decided_at=LATER,
                approved_instance_id="hou_instance_1",
                approved_scene_epoch=1,
            )
            with pytest.raises(AgentException):
                await repo.update_approval(
                    approved,
                    expected_decision=ApprovalDecision.REJECTED,
                    expected_changeset_digest=digest,
                )
        finally:
            await db.close()

    _run(scenario())


def test_update_approval_rejects_mismatched_approval_id(db_path: Path) -> None:
    """update_approval must not swap the immutable approval_id identity.

    Regression: a caller supplied a valid Approved ApprovalRecord for the same
    change_id but a different approval_id. The UPDATE omitted the approval_id
    column, so the column kept the original id while payload_json/digest were
    overwritten with the impostor id — leaving the row internally inconsistent.
    The update must be rejected and the original row left intact.
    """

    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest
            impostor = _approval(
                approval_id=f"apr_{'6' * 32}",  # differs from the persisted APR
                changeset_digest=digest,
                decision=ApprovalDecision.APPROVED,
                decided_by="local_user",
                decided_at=LATER,
                approved_instance_id="hou_instance_1",
                approved_scene_epoch=1,
            )
            with pytest.raises(AgentException) as exc:
                await repo.update_approval(
                    impostor,
                    expected_decision=ApprovalDecision.PENDING,
                    expected_changeset_digest=digest,
                )
            assert _err_code(exc.value) == "approval.identity_mismatch"
            # Original row untouched: identity, decision, and payload all intact.
            got = await repo.get_approval(CHG)
            assert got.approval_id == APR
            assert got.decision is ApprovalDecision.PENDING
            assert got.to_dict() == _approval(changeset_digest=digest).to_dict()
            raw = await db.fetchone(
                "SELECT approval_id, decision, payload_json FROM approvals "
                "WHERE change_id = ?",
                (CHG,),
            )
            # Column identity and payload identity must agree (the invariant the
            # bug broke: the column stayed APR while the payload became apr_666).
            assert raw["approval_id"] == APR
            assert raw["decision"] == "Pending"
            assert got.approval_id == raw["approval_id"]
        finally:
            await db.close()

    _run(scenario())


def test_read_paths_detect_tampered_approval_identity_column(db_path: Path) -> None:
    """Read paths must fail closed when an identity column is swapped under them.

    Regression: a direct ``UPDATE approvals SET approval_id = ...`` to another
    valid ``apr_*`` value leaves ``payload_json`` and its storage ``digest``
    untouched, so the payload still decodes and its digest still verifies — yet
    the denormalized row column and the decoded DTO disagree about whose
    approval this is. Every approval read path (``get_approval`` and the
    combined ``decide`` primitive, which reads via ``_fetch_approval_record``)
    must detect the inconsistency and raise ``runtime.record_corrupt`` before
    any state transition or event append.
    """

    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            digest = (await repo.get_changeset(CHG)).changeset.digest

            # Tamper ONLY the denormalized approval_id column: payload_json and
            # its digest are left intact, so a digest check alone cannot catch
            # this. The impostor is a distinct but valid apr_* identity.
            impostor_id = f"apr_{'6' * 32}"
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE approvals SET approval_id = ? WHERE change_id = ?",
                    (impostor_id, CHG),
                )
            raw = await db.fetchone(
                "SELECT approval_id, digest FROM approvals WHERE change_id = ?",
                (CHG,),
            )
            assert raw is not None
            assert raw["approval_id"] == impostor_id  # column swapped...

            # ...get_approval must NOT silently hand back a row whose column and
            # payload disagree: it must fail closed as a corrupt record.
            with pytest.raises(AgentException) as exc:
                await repo.get_approval(CHG)
            assert _err_code(exc.value) == "runtime.record_corrupt"

            # decide() reads the same row through _fetch_approval_record and
            # must fail closed too, before any state transition or event append.
            deciding_repo = ChangeSetRepository(db, events=EventStore(db))
            with pytest.raises(AgentException) as exc:
                await deciding_repo.decide(
                    CHG,
                    changeset_digest=digest,
                    decision=ApprovalDecision.REJECTED,
                    now=LATER,
                )
            assert _err_code(exc.value) == "runtime.record_corrupt"

            # No state/event mutation: the ChangeSet keeps its seeded state and
            # no decision/expiry events were appended to the log.
            stored = await repo.get_changeset(CHG)
            assert stored.state is ChangeSetState.PROPOSED
            event_rows = await db.fetchall(
                "SELECT event_type FROM events WHERE session_id = ? ORDER BY seq",
                (SES,),
            )
            assert [r["event_type"] for r in event_rows] == []
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# consume approval: expiry, single-use, concurrency
# --------------------------------------------------------------------------


async def _seed_approved(repo: ChangeSetRepository, *, expires_at: datetime = LATER) -> None:
    await repo.insert_changeset(_changeset(), state=ChangeSetState.AWAITING_APPROVAL)
    await repo.transition_changeset(
        CHG, from_state=ChangeSetState.AWAITING_APPROVAL, to_state=ChangeSetState.APPROVED
    )
    digest = (await repo.get_changeset(CHG)).changeset.digest
    await repo.insert_approval(_approval(changeset_digest=digest, expires_at=expires_at))
    await repo.update_approval(
        _approval(
            changeset_digest=digest,
            decision=ApprovalDecision.APPROVED,
            decided_by="local_user",
            decided_at=NOW,
            expires_at=expires_at,
            approved_instance_id="hou_instance_1",
            approved_scene_epoch=1,
        ),
        expected_decision=ApprovalDecision.PENDING,
        expected_changeset_digest=digest,
    )


def test_consume_approval_succeeds_once(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_approved(repo)
            consumed = await repo.consume_approval(CHG, now=SOON)
            assert consumed.decision is ApprovalDecision.CONSUMED
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.CONSUMED
            # second consume fails: already consumed.
            with pytest.raises(AgentException) as exc:
                await repo.consume_approval(CHG, now=SOON)
            assert _err_code(exc.value) == "approval.already_consumed"
        finally:
            await db.close()

    _run(scenario())


def test_consume_approval_rejects_expired(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_approved(repo, expires_at=SOON)
            with pytest.raises(AgentException) as exc:
                await repo.consume_approval(CHG, now=LATER)  # past expiry
            assert _err_code(exc.value) == "approval.expired"
            # not consumed after expiry rejection.
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.APPROVED
        finally:
            await db.close()

    _run(scenario())


def test_consume_approval_rejects_non_approved(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_proposed_with_pending_approval(repo)
            with pytest.raises(AgentException) as exc:
                await repo.consume_approval(CHG, now=SOON)
            assert _err_code(exc.value) == "approval.already_consumed"
        finally:
            await db.close()

    _run(scenario())


def test_consume_approval_concurrent_single_use(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_approved(repo)
            results = await asyncio.gather(
                repo.consume_approval(CHG, now=SOON),
                repo.consume_approval(CHG, now=SOON),
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, BaseException)]
            failures = [r for r in results if isinstance(r, BaseException)]
            assert len(successes) == 1
            assert isinstance(successes[0], ApprovalRecord)
            assert successes[0].decision is ApprovalDecision.CONSUMED
            assert len(failures) == 1
            assert _err_code(failures[0]) == "approval.already_consumed"
        finally:
            await db.close()

    _run(scenario())


def test_consume_approval_rejects_tampered_identity_column(db_path: Path) -> None:
    """consume_approval must fail closed on a tampered approval identity column.

    Regression: a direct ``UPDATE approvals SET approval_id = ...`` to another
    valid ``apr_*`` value leaves ``payload_json`` and its storage ``digest``
    untouched, so the payload still decodes and its digest still verifies — yet
    the denormalized row column and the decoded DTO disagree about whose
    approval this is. ``consume_approval`` is the future single-use Apply
    boundary, so it must detect that inconsistency via the same identity check
    as the read paths and raise ``runtime.record_corrupt`` BEFORE any
    expiry/digest handling or the Consumed state mutation, leaving
    decision/state/events unchanged.
    """

    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await _seed_approved(repo)

            # Tamper ONLY the denormalized approval_id column: payload_json and
            # its digest are left intact, so a digest check alone cannot catch
            # this. The impostor is a distinct but valid apr_* identity.
            impostor_id = f"apr_{'6' * 32}"
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE approvals SET approval_id = ? WHERE change_id = ?",
                    (impostor_id, CHG),
                )

            with pytest.raises(AgentException) as exc:
                await repo.consume_approval(CHG, now=SOON)
            assert _err_code(exc.value) == "runtime.record_corrupt"

            # No state mutation: the approval decision column stays Approved (not
            # Consumed), the ChangeSet keeps its seeded state, and no events were
            # appended (consume_approval is a plain primitive that never emits).
            raw = await db.fetchone(
                "SELECT approval_id, decision FROM approvals WHERE change_id = ?",
                (CHG,),
            )
            assert raw is not None
            assert raw["approval_id"] == impostor_id  # column still swapped...
            assert raw["decision"] == "Approved"  # ...but NOT consumed.
            stored = await repo.get_changeset(CHG)
            assert stored.state is ChangeSetState.APPROVED
            event_rows = await db.fetchall(
                "SELECT event_type FROM events WHERE session_id = ? ORDER BY seq",
                (SES,),
            )
            assert [r["event_type"] for r in event_rows] == []
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# receipts: idempotency + conflict + restart query
# --------------------------------------------------------------------------


def test_receipt_round_trips_and_idempotent(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            receipt = _receipt()
            inserted = await repo.insert_receipt(receipt)
            assert inserted.to_dict() == receipt.to_dict()
            # identical re-insert is idempotent: returns the equal receipt.
            again = await repo.insert_receipt(receipt)
            assert again.to_dict() == receipt.to_dict()
            assert (await repo.get_receipt(CHG)).to_dict() == receipt.to_dict()
        finally:
            await db.close()

    _run(scenario())


def test_receipt_rejects_conflicting_status(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            await repo.insert_receipt(_receipt(status=ReceiptStatus.APPLIED))
            # a different terminal receipt for the same change conflicts.
            conflicting = _receipt(
                status=ReceiptStatus.ROLLED_BACK,
                postcondition_results=(),
                rollback_results=(_result(True),),
                after_revision=REVISION,
            )
            with pytest.raises(AgentException):
                await repo.insert_receipt(conflicting)
        finally:
            await db.close()

    _run(scenario())


def test_receipt_requires_changeset_fk(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises((AgentException, sqlite3.IntegrityError)):
                await repo.insert_receipt(_receipt())
        finally:
            await db.close()

    _run(scenario())


def test_list_receipts_restart_safe(db_path: Path) -> None:
    async def scenario() -> None:
        db_path_str = str(db_path)
        db, repo = await fresh_repo(db_path)
        await repo.insert_changeset(_changeset(change_id=f"chg_{'1' * 32}"))
        await repo.insert_changeset(_changeset(change_id=f"chg_{'2' * 32}"))
        await repo.insert_receipt(_receipt(change_id=f"chg_{'1' * 32}"))
        await repo.insert_receipt(
            _receipt(
                change_id=f"chg_{'2' * 32}",
                status=ReceiptStatus.CRITICAL_RECOVERY,
                postcondition_results=(),
                scene_may_have_changed=True,
            )
        )
        await db.close()
        db2 = await RuntimeDatabase.open(db_path_str)
        try:
            repo2 = ChangeSetRepository(db2)
            receipts = await repo2.list_receipts()
            assert {r.change_id for r in receipts} == {
                f"chg_{'1' * 32}",
                f"chg_{'2' * 32}",
            }
            assert {r.status for r in receipts} == {
                ReceiptStatus.APPLIED,
                ReceiptStatus.CRITICAL_RECOVERY,
            }
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# cascade on session delete
# --------------------------------------------------------------------------


def test_deleting_session_cascades_changeset_records(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        await repo.insert_workspace(_manifest())
        await repo.insert_changeset(_changeset())
        digest = (await repo.get_changeset(CHG)).changeset.digest
        await repo.insert_approval(_approval(changeset_digest=digest))
        await repo.insert_receipt(_receipt())
        async with db.write_transaction() as conn:
            await conn.execute("DELETE FROM sessions WHERE session_id = ?", (SES,))
        assert (await db.fetchone("SELECT COUNT(*) AS c FROM workspaces"))["c"] == 0
        assert (await db.fetchone("SELECT COUNT(*) AS c FROM changesets"))["c"] == 0
        assert (await db.fetchone("SELECT COUNT(*) AS c FROM approvals"))["c"] == 0
        assert (await db.fetchone("SELECT COUNT(*) AS c FROM change_receipts"))["c"] == 0
        await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# storage digest verify-on-read + bounded payload
# --------------------------------------------------------------------------


def test_tampered_payload_digest_fails_closed(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        await repo.insert_workspace(_manifest())
        async with db.write_transaction() as conn:
            await conn.execute(
                "UPDATE workspaces SET payload_json = ? WHERE workspace_id = ?",
                ('{"workspace_id":"' + WS + '"}', WS),
            )
        with pytest.raises(AgentException) as exc:
            await repo.get_workspace(WS)
        assert _err_code(exc.value) == "runtime.record_corrupt"
        await db.close()

    _run(scenario())


def test_equivalent_noncanonical_manifest_json_fails_closed(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_workspace(_manifest())
            row = await db.fetchone(
                "SELECT payload_json FROM workspaces WHERE workspace_id = ?", (WS,)
            )
            value = json.loads(row["payload_json"])
            pretty = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)
            assert pretty != row["payload_json"]
            digest = hashlib.sha256(pretty.encode("utf-8")).hexdigest()
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE workspaces SET payload_json = ?, digest = ? "
                    "WHERE workspace_id = ?",
                    (pretty, digest, WS),
                )
            with pytest.raises(AgentException) as exc:
                await repo.get_workspace(WS)
            assert _err_code(exc.value) == "runtime.record_corrupt"
        finally:
            await db.close()

    _run(scenario())


def test_tampered_digest_column_fails_closed(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        await repo.insert_changeset(_changeset())
        async with db.write_transaction() as conn:
            await conn.execute(
                "UPDATE changesets SET digest = ? WHERE change_id = ?",
                ("0" * 64, CHG),
            )
        with pytest.raises(AgentException):
            await repo.get_changeset(CHG)
        await db.close()

    _run(scenario())


def test_rejects_oversize_changeset_payload(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            # Each parm value is under the 16 KiB parm cap, but 256 setparm
            # operations push the canonical payload past 128 KiB, which the
            # ChangeSet contract itself rejects before persistence.
            value = "x" * 512
            ops = tuple(
                _setparm(op_id=f"op_{i}", parm_name=f"p{i}", value=value, expected_old_value=value)
                for i in range(256)
            )
            with pytest.raises((ValueError, TypeError)):
                _changeset(operations=ops)
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# strict decoder unit tests (unknown fields / tags / dup keys / non-UTC)
# --------------------------------------------------------------------------


def test_strict_loader_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError):
        repo_mod._loads_canonical('{"a": 1, "a": 2}')


def test_strict_loader_rejects_malformed_json() -> None:
    with pytest.raises(ValueError):
        repo_mod._loads_canonical('{"a": malformed')


def test_decoder_rejects_unknown_top_level_field() -> None:
    data = _changeset().to_dict()
    data["unexpected_field"] = "x"  # type: ignore[assignment]
    with pytest.raises((ValueError, TypeError)):
        repo_mod._decode_changeset(data)


def test_decoder_rejects_wrong_operation_tag() -> None:
    data = _changeset().to_dict()
    data["operations"][0]["kind"] = "node.delete"  # type: ignore[index]
    with pytest.raises((ValueError, TypeError)):
        repo_mod._decode_changeset(data)


def test_decoder_rejects_wrong_enum() -> None:
    data = _changeset().to_dict()
    data["required_permission"] = "SuperUser"  # type: ignore[index]
    with pytest.raises((ValueError, TypeError)):
        repo_mod._decode_changeset(data)


def test_decoder_rejects_non_utc_timestamp() -> None:
    data = _manifest().to_dict()
    data["updated_at"] = "2026-07-15T12:00:00"  # naive -> rejected
    with pytest.raises((ValueError, TypeError)):
        repo_mod._decode_manifest(data)


def test_decoder_rejects_malformed_nested_operation() -> None:
    data = _changeset().to_dict()
    data["operations"][0] = {"kind": "node.create"}  # missing required fields
    with pytest.raises((ValueError, TypeError)):
        repo_mod._decode_changeset(data)


def test_decoder_round_trip_matches_original_for_every_dto() -> None:
    manifest = _manifest()
    assert repo_mod._decode_manifest(manifest.to_dict()).to_dict() == manifest.to_dict()
    cs = _changeset()
    assert repo_mod._decode_changeset(cs.to_dict()).to_dict() == cs.to_dict()
    assert repo_mod._decode_changeset(cs.to_dict()).digest == cs.digest
    approval = _approval(
        changeset_digest=cs.digest,
        decision=ApprovalDecision.APPROVED,
        decided_by="local_user",
        decided_at=LATER,
        approved_instance_id="hou_instance_1",
        approved_scene_epoch=1,
    )
    assert repo_mod._decode_approval(approval.to_dict()).to_dict() == approval.to_dict()
    receipt = _receipt()
    assert repo_mod._decode_receipt(receipt.to_dict()).to_dict() == receipt.to_dict()


# --------------------------------------------------------------------------
# input validation
# --------------------------------------------------------------------------


def test_repository_rejects_wrong_dto_types(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            with pytest.raises(TypeError):
                await repo.insert_workspace(_changeset())  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                await repo.insert_changeset(_manifest())  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                await repo.insert_approval(_receipt())  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                await repo.insert_receipt(_approval())  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_transition_rejects_wrong_state_type(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await fresh_repo(db_path)
        try:
            await repo.insert_changeset(_changeset())
            with pytest.raises(TypeError):
                await repo.transition_changeset(
                    CHG, from_state="Proposed", to_state=ChangeSetState.AWAITING_APPROVAL  # type: ignore[arg-type]
                )
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# import boundary
# --------------------------------------------------------------------------

_FORBIDDEN_IMPORT_ROOTS = {"hou", "rpyc", "subprocess", "os", "shutil", "socket", "pathlib"}
_FORBIDDEN_IMPORT_PREFIXES = ("eee_agent.bridge",)


def _imported_roots(module_name: str) -> set[str]:
    import ast
    import inspect

    module = __import__(module_name, fromlist=["x"])
    tree = ast.parse(inspect.getsource(module))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_repository_module_does_not_import_forbidden() -> None:
    import ast
    import inspect

    roots = _imported_roots("eee_agent.changesets.repository")
    for root in roots:
        assert root not in _FORBIDDEN_IMPORT_ROOTS, f"forbidden import root: {root}"
    module = __import__("eee_agent.changesets.repository", fromlist=["x"])
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                    assert not (alias.name == prefix or alias.name.startswith(prefix + ".")), (
                        f"forbidden import: {alias.name}"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                assert not (mod == prefix or mod.startswith(prefix + ".")), (
                    f"forbidden import: {mod}"
                )


def test_repository_imports_without_heavy_deps() -> None:
    """A fresh interpreter importing the repository must not load hou/rpyc.

    Uses an isolated subprocess so the assertion is independent of whatever
    other tests in the full suite may have imported ``rpyc`` into this
    process's ``sys.modules`` already.
    """
    import json
    import subprocess
    import sys

    code = (
        "import sys, json; "
        "import eee_agent.changesets.repository; "
        "print(json.dumps(sorted(k for k in ('hou', 'rpyc') if k in sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []


# keep imported symbols referenced for static analyzers
_ = (StoredChangeSet,)
