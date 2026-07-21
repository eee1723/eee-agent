"""Task 16-E trusted Apply orchestration and restart recovery tests."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    PolicyDecision,
    ReceiptStatus,
    RiskSummary,
    SetParm,
    WorkspaceManifest,
)
from eee_agent.changesets.repository import ChangeSetRepository, ChangeSetState
from eee_agent.changesets.service import ChangeSetService
from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.houdini_bridge.changesets import (
    ApplyRequest,
    PreflightNodeFact,
    PreflightParmFact,
    PreflightRequest,
    PreflightResult,
    ReceiptRequest,
)
from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
)
from eee_agent.houdini_bridge.capture import (
    CaptureFramingReport,
    CaptureResult,
)
from eee_agent.houdini_bridge.changeset_provider import BridgeChangeSetProvider
from eee_agent.houdini_bridge.client import BridgeClient
from eee_agent.houdini_bridge.contracts import (
    BridgeRequest,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)
from eee_agent.houdini_bridge.sensitivity import SensitivitySampleResult
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectRequest,
    WorkspaceInspectResult,
)
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.service import RuntimeService
from eee_agent.modeling.catalog import houdini_21_minimal_catalog

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
CHG = f"chg_{'3' * 32}"
NOW = datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc)
BASE = "a" * 64


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = {
        "instance_id": "hou_instance_1",
        "scene_epoch": 1,
        "hip_path": None,
        "observed_revision": "scene-revision-1",
    }
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _owned() -> OwnedNodeRef:
    return OwnedNodeRef(
        node_id="n_root",
        path="/obj/ws",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )


def _target() -> NodeRef:
    return NodeRef(
        node_id="n_root",
        path="/obj/ws",
        expected_type="geo",
        expected_workspace_id=WS,
    )


def _manifest() -> WorkspaceManifest:
    node = _owned()
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=(node,),
        nodes=(node,),
        created_by_run=RUN,
        updated_at=NOW,
    )


def _changeset(change_id: str = CHG) -> ChangeSet:
    target = _target()
    return ChangeSet(
        change_id=change_id,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=BASE,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(
            SetParm(
                op_id="op_set_tx",
                target=target,
                parm_name="tx",
                value=1,
                expected_old_value=0,
            ),
        ),
        affected_nodes=(target,),
        read_dependencies=(),
        preconditions=(ParmValueEquals(target=target, parm_name="tx", value=0),),
        expected_postconditions=(
            ParmValueEquals(target=target, parm_name="tx", value=1),
        ),
        risk_summary=RiskSummary(
            touches_external_nodes=False,
            changes_wiring=False,
            requires_backup=False,
            operation_count=1,
            effect_names=("parm.set",),
            affected_paths=(target.path,),
        ),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )


def _box_changeset(change_id: str = CHG) -> ChangeSet:
    # A ChangeSet whose SetParm targets a catalog numeric parameter (box.sizex)
    # so the derived sensitivity sample plan is non-empty.
    target = NodeRef(
        node_id="n_box",
        path="/obj/ws/box1",
        expected_type="box",
        expected_workspace_id=WS,
    )
    return ChangeSet(
        change_id=change_id,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=BASE,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(
            SetParm(
                op_id="op_set_sizex",
                target=target,
                parm_name="sizex",
                value=2.0,
                expected_old_value=1.0,
            ),
        ),
        affected_nodes=(target,),
        read_dependencies=(),
        preconditions=(ParmValueEquals(target=target, parm_name="sizex", value=1.0),),
        expected_postconditions=(
            ParmValueEquals(target=target, parm_name="sizex", value=2.0),
        ),
        risk_summary=RiskSummary(
            touches_external_nodes=False,
            changes_wiring=False,
            requires_backup=False,
            operation_count=1,
            effect_names=("parm.set",),
            affected_paths=(target.path,),
        ),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )


def _sample_query(target: NodeRef, *, bbox_max: list[float]) -> SceneQueryResult:
    return SceneQueryResult(
        binding=_binding(),
        selected_nodes=(),
        nodes=(
            SelectedNode(
                path=target.path,
                node_type=target.expected_type,
                parent_path=target.path.rsplit("/", 1)[0],
                display_name=target.path.rsplit("/", 1)[1],
                is_locked=False,
                geometry_stats={
                    "points": 8,
                    "primitives": 6,
                    "bbox": {
                        "min": [0.0, 0.0, 0.0],
                        "max": bbox_max,
                    },
                },
            ),
        ),
    )


def _policy(changeset: ChangeSet) -> PolicyDecision:
    return PolicyDecision(
        allowed=True,
        mode=PermissionMode.OWNED_WORKSPACE,
        normalized_effects=("parm.set",),
        approval_required=True,
        backup_required=False,
        denial_codes=(),
        changeset_digest=changeset.digest,
    )


def _receipt(
    change_id: str = CHG,
    status: ReceiptStatus = ReceiptStatus.APPLIED,
) -> ChangeReceipt:
    success = status in (ReceiptStatus.APPLIED, ReceiptStatus.ALREADY_APPLIED)
    rolled_back = status is ReceiptStatus.ROLLED_BACK
    return ChangeReceipt(
        change_id=change_id,
        status=status,
        instance_id="hou_instance_1",
        scene_epoch=1,
        before_revision=BASE,
        after_revision="b" * 64,
        applied_op_ids=("op_set_tx",) if success else (),
        postcondition_results=(
            ConditionResult(kind="parm.value_equals", passed=success),
        ),
        rollback_results=(
            (ConditionResult(kind="parm.value_equals", passed=True),)
            if rolled_back
            else ()
        ),
        scene_may_have_changed=status in (
            ReceiptStatus.PARTIAL,
            ReceiptStatus.CRITICAL_RECOVERY,
        ),
        completed_at=NOW + timedelta(seconds=2),
    )


def _preflight(value: int, *, before_holds: bool) -> PreflightResult:
    target = _target()
    manifest = _manifest()
    return PreflightResult(
        binding=_binding(),
        workspace_id=WS,
        workspace_revision=manifest.revision,
        node_facts=(
            PreflightNodeFact(
                requested=target,
                exists=True,
                actual_path=target.path,
                actual_type=target.expected_type,
                parent_path="/obj",
                workspace_id=WS,
                node_id=target.node_id,
                capability="modeling",
                role="root",
                is_locked=False,
            ),
        ),
        parm_facts=(
            PreflightParmFact(
                target=target, parm_name="tx", exists=True, value=value
            ),
        ),
        wire_facts=(),
        condition_results=(
            ConditionResult(kind="parm.value_equals", passed=before_holds),
        ),
        all_preconditions_hold=before_holds,
        scene_may_have_changed=False,
    )


def _agent_error(code: str, *, retryable: bool = False) -> AgentException:
    return AgentException(
        AgentError(
            code=code,
            category=ErrorCategory.HOUDINI_BRIDGE,
            message_for_user="bounded bridge test error",
            retryable=retryable,
        )
    )


class FakeBridge:
    def __init__(self) -> None:
        self.apply_result: ChangeReceipt | BaseException = _receipt()
        self.receipt_result: ChangeReceipt | BaseException = _agent_error(
            "changeset.receipt_unavailable"
        )
        self.preflight_result: PreflightResult | BaseException = _preflight(
            0, before_holds=True
        )
        self.calls: list[str] = []
        self.on_apply = None
        self.geometry_error: BaseException | None = None
        self.sample_error: BaseException | None = None

    async def current_binding(self) -> SceneBinding:
        return _binding()

    async def sample_sensitivity(self, changeset, samples):
        self.calls.append("sample_sensitivity")
        if self.sample_error is not None:
            raise self.sample_error
        target = changeset.affected_nodes[0]
        baseline = _sample_query(target, bbox_max=[1.0, 1.0, 1.0])
        perturbed = _sample_query(target, bbox_max=[2.0, 1.0, 1.0])
        return SensitivitySampleResult(
            baseline=baseline,
            samples=tuple(perturbed for _ in samples),
            restored=baseline,
        )

    async def inspect_geometry(self, changeset):
        self.calls.append("inspect_geometry")
        if self.geometry_error is not None:
            raise self.geometry_error
        target = changeset.affected_nodes[0]
        return SceneQueryResult(
            binding=_binding(),
            selected_nodes=(),
            nodes=(
                SelectedNode(
                    path=target.path,
                    node_type=target.expected_type,
                    parent_path=target.path.rsplit("/", 1)[0],
                    display_name=target.path.rsplit("/", 1)[1],
                    is_locked=False,
                    geometry_stats={
                        "points": 8,
                        "primitives": 6,
                        "bbox": {
                            "min": [0.0, 0.0, 0.0],
                            "max": [1.0, 1.0, 1.0],
                        },
                    },
                ),
            ),
        )

    async def preflight(self, changeset, workspace):
        self.calls.append("preflight")
        if isinstance(self.preflight_result, BaseException):
            raise self.preflight_result
        return self.preflight_result

    async def apply(self, changeset, workspace):
        self.calls.append("apply")
        if self.on_apply is not None:
            await self.on_apply()
        if isinstance(self.apply_result, BaseException):
            raise self.apply_result
        return self.apply_result

    async def receipt(self, changeset):
        self.calls.append("receipt")
        if isinstance(self.receipt_result, BaseException):
            raise self.receipt_result
        return self.receipt_result


async def _seed(
    path: Path,
    changeset: ChangeSet | None = None,
) -> tuple[RuntimeDatabase, EventStore, ChangeSetRepository, ChangeSetService, FakeBridge]:
    db = await RuntimeDatabase.open(path)
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id,title,status,created_at,updated_at,last_seq,replay_floor_seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (SES, "T", "active", NOW.isoformat(), NOW.isoformat(), 0, 0),
        )
        await conn.execute(
            "INSERT INTO runs(run_id,session_id,status,user_input,final_response,created_at,"
            "started_at,finished_at,failure_json,model_snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (RUN, SES, "Completed", "x", None, NOW.isoformat(), None, NOW.isoformat(), None, "{}"),
        )
    events = EventStore(db)
    repo = ChangeSetRepository(db, events=events)
    await repo.insert_workspace(_manifest())
    bridge = FakeBridge()
    service = ChangeSetService(repo, clock=lambda: NOW, bridge_provider=bridge)
    changeset = changeset if changeset is not None else _changeset()
    await service.propose(changeset, _policy(changeset))
    await service.approve(CHG, changeset.digest)
    return db, events, repo, service, bridge


def test_apply_commits_boundary_before_io_and_terminal_bundle(db_path: Path) -> None:
    async def scenario() -> None:
        db, events, repo, service, bridge = await _seed(db_path)

        async def assert_boundary() -> None:
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPLYING
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.CONSUMED

        bridge.on_apply = assert_boundary
        result = await service.apply(CHG)
        assert result.state is ChangeSetState.APPLIED
        assert (await repo.get_receipt(CHG)).status is ReceiptStatus.APPLIED
        replay = await events.replay(SES, after_seq=0, limit=100)
        assert [event.event_type for event in replay.events][-3:] == [
            "changeset.state_changed",
            "changeset.state_changed",
            "changeset.applied",
        ]
        await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "expected_state", "expected_event"),
    [
        (ReceiptStatus.ALREADY_APPLIED, ChangeSetState.APPLIED, "changeset.applied"),
        (ReceiptStatus.ROLLED_BACK, ChangeSetState.ROLLED_BACK, "changeset.rolled_back"),
        (ReceiptStatus.PARTIAL, ChangeSetState.CRITICAL_RECOVERY, "recovery.critical"),
        (
            ReceiptStatus.CRITICAL_RECOVERY,
            ChangeSetState.CRITICAL_RECOVERY,
            "recovery.critical",
        ),
    ],
)
def test_receipt_status_mapping_is_truthful_and_atomic(
    db_path: Path,
    status: ReceiptStatus,
    expected_state: ChangeSetState,
    expected_event: str,
) -> None:
    async def scenario() -> None:
        db, events, repo, service, bridge = await _seed(db_path)
        bridge.apply_result = _receipt(status=status)
        result = await service.apply(CHG)
        assert result.state is expected_state
        assert (await repo.get_changeset(CHG)).state is expected_state
        assert (await repo.get_receipt(CHG)).status is status
        replay = await events.replay(SES, after_seq=0, limit=100)
        assert replay.events[-1].event_type == expected_event
        await db.close()

    asyncio.run(scenario())


def test_before_state_recovery_keeps_consumed_for_explicit_retry(db_path: Path) -> None:
    async def scenario() -> None:
        db, _events, repo, service, bridge = await _seed(db_path)
        await repo.begin_apply(CHG, now=NOW)
        recovered = await service.recover_one(CHG)
        assert recovered.state is ChangeSetState.APPROVED
        assert recovered.receipt is None
        assert bridge.calls == ["receipt", "preflight"]
        assert (await repo.get_approval(CHG)).decision is ApprovalDecision.CONSUMED

        bridge.calls.clear()
        bridge.apply_result = _receipt()
        completed = await service.apply(CHG)
        assert completed.state is ChangeSetState.APPLIED
        assert bridge.calls == ["apply"]
        assert (await repo.get_approval(CHG)).decision is ApprovalDecision.CONSUMED
        await db.close()

    asyncio.run(scenario())


def test_post_state_recovery_persists_already_applied_without_replay(db_path: Path) -> None:
    async def scenario() -> None:
        db, _events, repo, service, bridge = await _seed(db_path)
        await repo.begin_apply(CHG, now=NOW)
        bridge.preflight_result = _preflight(1, before_holds=False)
        recovered = await service.recover_one(CHG)
        assert recovered.state is ChangeSetState.APPLIED
        assert recovered.receipt is not None
        assert recovered.receipt.status is ReceiptStatus.ALREADY_APPLIED
        assert bridge.calls == ["receipt", "preflight"]
        assert (await repo.get_receipt(CHG)).status is ReceiptStatus.ALREADY_APPLIED
        await db.close()

    asyncio.run(scenario())


def test_invalid_receipt_identity_is_persisted_as_critical_recovery(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, _events, repo, service, bridge = await _seed(db_path)
        await repo.begin_apply(CHG, now=NOW)
        wrong = _receipt(change_id=f"chg_{'9' * 32}")
        bridge.receipt_result = wrong
        recovered = await service.recover_one(CHG)
        assert recovered.state is ChangeSetState.CRITICAL_RECOVERY
        assert recovered.receipt is not None
        assert recovered.receipt.status is ReceiptStatus.CRITICAL_RECOVERY
        assert bridge.calls == ["receipt"]
        await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mismatch", ["workspace", "condition", "duplicate"])
def test_malformed_recovery_fact_correlation_fails_closed(
    db_path: Path, mismatch: str
) -> None:
    async def scenario() -> None:
        db, _events, repo, service, bridge = await _seed(db_path)
        await repo.begin_apply(CHG, now=NOW)
        facts = _preflight(1, before_holds=False)
        if mismatch == "workspace":
            facts = replace(facts, workspace_revision="c" * 64)
        elif mismatch == "condition":
            facts = replace(facts, all_preconditions_hold=True)
        else:
            facts = replace(facts, node_facts=facts.node_facts * 2)
        bridge.preflight_result = facts
        recovered = await service.recover_one(CHG)
        assert recovered.state is ChangeSetState.CRITICAL_RECOVERY
        assert recovered.receipt is not None
        assert recovered.receipt.status is ReceiptStatus.CRITICAL_RECOVERY
        await db.close()

    asyncio.run(scenario())


def test_ambiguous_recovery_freezes_and_transient_outage_stays_applying(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db, _events, repo, service, bridge = await _seed(db_path)
        await repo.begin_apply(CHG, now=NOW)
        bridge.receipt_result = _agent_error("bridge.not_available", retryable=True)
        pending = await service.recover_one(CHG)
        assert pending.pending is True
        assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPLYING

        bridge.receipt_result = _agent_error("changeset.receipt_unavailable")
        bridge.preflight_result = _preflight(7, before_holds=False)
        critical = await service.recover_one(CHG)
        assert critical.state is ChangeSetState.CRITICAL_RECOVERY
        assert critical.receipt is not None
        assert critical.receipt.status is ReceiptStatus.CRITICAL_RECOVERY
        assert critical.receipt.scene_may_have_changed is True
        await db.close()

    asyncio.run(scenario())


def test_critical_record_blocks_an_unrelated_approved_changeset(db_path: Path) -> None:
    async def blocker_scenario() -> None:
        db, _events, repo, service, bridge = await _seed(db_path)
        bridge.apply_result = _receipt(status=ReceiptStatus.CRITICAL_RECOVERY)
        assert (await service.apply(CHG)).state is ChangeSetState.CRITICAL_RECOVERY

        other_id = f"chg_{'4' * 32}"
        other = _changeset(other_id)
        await service.propose(other, _policy(other))
        await service.approve(other_id, other.digest)
        bridge.calls.clear()
        with pytest.raises(AgentException) as exc:
            await service.apply(other_id)
        assert exc.value.error.code == "recovery.critical_required"
        assert bridge.calls == []
        assert (await repo.get_changeset(other_id)).state is ChangeSetState.APPROVED
        await db.close()

    asyncio.run(blocker_scenario())


def test_begin_event_failure_rolls_back_consumption_and_state(db_path: Path) -> None:
    async def scenario() -> None:
        db, events, repo, _service, _bridge = await _seed(db_path)
        original = events._append_conn

        async def fail(conn, **kwargs):
            if kwargs["event_type"] == "changeset.state_changed":
                raise RuntimeError("event failure")
            return await original(conn, **kwargs)

        events._append_conn = fail  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="event failure"):
            await repo.begin_apply(CHG, now=NOW)
        assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPROVED
        assert (await repo.get_approval(CHG)).decision is ApprovalDecision.APPROVED
        await db.close()

    asyncio.run(scenario())


def test_runtime_waiter_cancellation_does_not_cancel_apply_or_notifications(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, _events, repo, _service, bridge = await _seed(db_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block_apply() -> None:
            entered.set()
            await release.wait()

        bridge.on_apply = block_apply
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
        )
        notifications: list[str] = []
        runtime.subscribe(lambda event: notifications.append(event.event_type))

        try:
            waiter = asyncio.create_task(runtime.apply_changeset_trusted(CHG))
            await asyncio.wait_for(entered.wait(), timeout=1)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPLYING

            release.set()
            for _ in range(100):
                if (await repo.get_changeset(CHG)).state is ChangeSetState.APPLIED:
                    break
                await asyncio.sleep(0.01)
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPLIED
            for _ in range(100):
                if len(notifications) >= 3:
                    break
                await asyncio.sleep(0.01)
            assert notifications[-3:] == [
                "changeset.state_changed",
                "changeset.state_changed",
                "changeset.applied",
            ]
        finally:
            release.set()
            await runtime._shutdown()
            await db.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_production_provider_builds_exact_typed_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.requests: list[object] = []
            self.closed = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            self.closed += 1

        async def inspect_workspace(self, request):
            self.requests.append(request)
            return WorkspaceInspectResult.build(
                binding=_binding(), mode="selection", observations=()
            )

        async def request(self, request):
            self.requests.append(request)
            target = _target()
            return SceneQueryResult(
                binding=_binding(),
                selected_nodes=(),
                nodes=(
                    SelectedNode(
                        path=target.path,
                        node_type=target.expected_type,
                        parent_path="/obj",
                        display_name="ws",
                        is_locked=False,
                        geometry_stats={"points": 8, "primitives": 6},
                    ),
                ),
            )

        async def preflight(self, request):
            self.requests.append(request)
            return _preflight(0, before_holds=True)

        async def apply(self, request):
            self.requests.append(request)
            return _receipt()

        async def receipt(self, request):
            self.requests.append(request)
            return _receipt()

    async def scenario() -> None:
        (tmp_path / BRIDGE_DISCOVERY_FILENAME).write_text("{}", encoding="utf-8")
        (tmp_path / BRIDGE_TOKEN_FILENAME).write_text("x", encoding="utf-8")
        fake = FakeClient()
        monkeypatch.setattr(
            BridgeClient,
            "from_state_dir",
            classmethod(lambda cls, state_dir: fake),
        )
        provider = BridgeChangeSetProvider(tmp_path, deadline_ms=1000)
        changeset = _changeset()
        manifest = _manifest()
        assert await provider.current_binding() == _binding()
        geometry = await provider.inspect_geometry(changeset)
        assert geometry.nodes[0].geometry_stats["primitives"] == 6
        assert await provider.preflight(changeset, manifest) == _preflight(
            0, before_holds=True
        )
        assert (await provider.apply(changeset, manifest)).status is ReceiptStatus.APPLIED
        assert (await provider.receipt(changeset)).change_id == CHG
        assert [type(item) for item in fake.requests] == [
            WorkspaceInspectRequest,
            BridgeRequest,
            PreflightRequest,
            ApplyRequest,
            ReceiptRequest,
        ]
        geometry_request = fake.requests[1]
        assert geometry_request.to_dict()["payload"] == {
            "include_selection": False,
            "node_paths": ["/obj/ws"],
            "include_geometry_stats": True,
        }
        assert fake.closed == 5

    asyncio.run(scenario())


def test_production_provider_missing_handoff_fails_before_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        called = False

        def forbidden(cls, state_dir):
            nonlocal called
            called = True
            raise AssertionError("client must not be constructed")

        monkeypatch.setattr(BridgeClient, "from_state_dir", classmethod(forbidden))
        provider = BridgeChangeSetProvider(tmp_path)
        with pytest.raises(AgentException) as exc:
            await provider.receipt(_changeset())
        assert exc.value.error.code == "bridge.not_available"
        assert called is False

    asyncio.run(scenario())


def test_runtime_open_recovers_applying_from_bridge_receipt(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, _events, repo, _service, bridge = await _seed(db_path)
        await repo.begin_apply(CHG, now=NOW)
        bridge.receipt_result = _receipt()
        await db.close()

        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        async with RuntimeService.open(
            paths,
            runner_factory=lambda _checkpointer: object(),
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
        ) as runtime:
            recovered_repo = ChangeSetRepository(runtime._database)
            assert (await recovered_repo.get_changeset(CHG)).state is ChangeSetState.APPLIED
            assert (await recovered_repo.get_receipt(CHG)).status is ReceiptStatus.APPLIED
        assert bridge.calls == ["receipt"]

    asyncio.run(scenario())


def test_runtime_shutdown_timeout_leaves_applying_for_restart(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, _events, repo, _service, bridge = await _seed(db_path)
        entered = asyncio.Event()
        never = asyncio.Event()

        async def block_apply() -> None:
            entered.set()
            await never.wait()

        bridge.on_apply = block_apply
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            graceful_timeout=0.01,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
        )
        waiter = asyncio.create_task(runtime.apply_changeset_trusted(CHG))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await runtime._shutdown()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPLYING
        assert (await repo.get_approval(CHG)).decision is ApprovalDecision.CONSUMED
        await db.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_runtime_persists_post_apply_geometry_validation(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _service, bridge = await _seed(db_path)
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        )
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(SES, after_seq=0, limit=100)
            validation = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_completed"
            )
            assert validation.payload["complete"] is True
            assert [item["status"] for item in validation.payload["results"]] == [
                "Passed",
                "Passed",
            ]
            assert bridge.calls[-1] == "inspect_geometry"
            await runtime.apply_changeset_trusted(CHG)
            replay = await events.replay(SES, after_seq=0, limit=100)
            assert sum(
                event.event_type == "modeling.validation_completed"
                for event in replay.events
            ) == 1
            assert bridge.calls.count("inspect_geometry") == 1
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_runtime_persists_post_apply_sensitivity_validation(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _service, bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        )
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(SES, after_seq=0, limit=100)
            validation = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_completed"
            )
            assert validation.payload["complete"] is True
            assert [item["status"] for item in validation.payload["results"]] == [
                "Passed",
                "Passed",
                "Passed",
            ]
            assert [item["validator"] for item in validation.payload["results"]] == [
                "Cook",
                "Geometry",
                "ParameterSensitivity",
            ]
            assert bridge.calls[-1] == "sample_sensitivity"
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_runtime_records_sensitivity_unavailable_without_replaying_apply(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _service, bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge.sample_error = _agent_error("sensitivity.restore_failed")
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        )
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(SES, after_seq=0, limit=100)
            unavailable = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_unavailable"
            )
            assert unavailable.payload["code"] == "sensitivity.restore_failed"
            assert bridge.calls.count("apply") == 1
            assert bridge.calls.count("sample_sensitivity") == 1
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_runtime_records_validation_unavailable_without_replaying_apply(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _service, bridge = await _seed(db_path)
        bridge.geometry_error = _agent_error("bridge.not_available", retryable=True)
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        )
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(SES, after_seq=0, limit=100)
            unavailable = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_unavailable"
            )
            assert unavailable.payload["code"] == "bridge.not_available"
            assert bridge.calls.count("apply") == 1
            assert bridge.calls.count("inspect_geometry") == 1
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 19-A: post-Apply artifact capture integration
# --------------------------------------------------------------------------

_CAPTURE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(40))


class FakeBridgeWithCapture(FakeBridge):
    """FakeBridge plus the typed ``capture.capture`` seam (Task 19-A)."""

    def __init__(self) -> None:
        super().__init__()
        self.capture_error: BaseException | None = None

    async def capture(self, changeset, *, target_dir, artifact_id):
        self.calls.append("capture")
        if self.capture_error is not None:
            raise self.capture_error
        (target_dir / f"{artifact_id}.png").write_bytes(_CAPTURE_PNG)
        return CaptureResult(
            artifact_id=artifact_id,
            relative_path=f"{artifact_id}.png",
            sha256=hashlib.sha256(_CAPTURE_PNG).hexdigest(),
            media_type="image/png",
            size_bytes=len(_CAPTURE_PNG),
            framing=CaptureFramingReport(
                adjustments_used=1,
                margin_left=0.16,
                margin_right=0.17,
                margin_bottom=0.12,
                margin_top=0.12,
                longest_axis_ratio=0.78,
                center_offset=0.002,
            ),
        )


def test_runtime_persists_post_apply_artifact_capture(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _service, _bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FakeBridgeWithCapture()
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        )
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(SES, after_seq=0, limit=100)
            captured = next(
                event
                for event in replay.events
                if event.event_type == "modeling.artifact_captured"
            )
            assert captured.payload["change_id"] == CHG
            assert captured.payload["changeset_digest"] == result.changeset.digest
            artifact = captured.payload["artifact"]
            artifact_id = artifact["artifact_id"]
            assert artifact_id.startswith("art_")
            assert artifact["sha256"] == hashlib.sha256(_CAPTURE_PNG).hexdigest()
            assert artifact["media_type"] == "image/png"
            assert artifact["size_bytes"] == len(_CAPTURE_PNG)
            assert artifact["relative_path"] == f"{SES}/{RUN}/{artifact_id}.png"
            framing = captured.payload["framing"]
            assert framing["adjustments_used"] == 1
            assert framing["longest_axis_ratio"] == 0.78
            # The registered store record resolves the exact same bytes.
            ref = await runtime._artifacts.get(artifact_id)
            assert ref is not None
            assert runtime._artifacts.path_for(ref).read_bytes() == _CAPTURE_PNG
            validation = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_completed"
            )
            assert validation.payload["complete"] is True
            assert [item["validator"] for item in validation.payload["results"]] == [
                "Cook",
                "Geometry",
                "ParameterSensitivity",
                "Artifact",
            ]
            assert [item["status"] for item in validation.payload["results"]] == [
                "Passed",
                "Passed",
                "Passed",
                "Passed",
            ]
            assert bridge.calls == [
                "apply",
                "inspect_geometry",
                "sample_sensitivity",
                "capture",
            ]
            # A trusted join with no new events never re-captures.
            await runtime.apply_changeset_trusted(CHG)
            replay = await events.replay(SES, after_seq=0, limit=100)
            assert sum(
                event.event_type == "modeling.artifact_captured"
                for event in replay.events
            ) == 1
            assert bridge.calls.count("capture") == 1
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())


def test_runtime_records_capture_failed_without_replaying_apply(
    db_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        db, events, _repo, _service, _bridge = await _seed(
            db_path, changeset=_box_changeset()
        )
        bridge = FakeBridgeWithCapture()
        bridge.capture_error = _agent_error("capture.framing_failed")
        paths = RuntimePaths(
            home=tmp_path,
            state_dir=tmp_path,
            app_db=db_path,
            checkpoints_db=tmp_path / "checkpoints.sqlite",
            lock_file=tmp_path / "runtime.lock",
            discovery_file=tmp_path / "runtime.json",
            token_file=tmp_path / "runtime.token",
            artifacts_dir=tmp_path / "artifacts",
        )
        runtime = RuntimeService(
            db,
            paths,
            changeset_clock=lambda: NOW,
            changeset_bridge_provider=bridge,
            modeling_catalog_provider=houdini_21_minimal_catalog,
        )
        try:
            result = await runtime.apply_changeset_trusted(CHG)
            assert result.state is ChangeSetState.APPLIED
            replay = await events.replay(SES, after_seq=0, limit=100)
            failed = next(
                event
                for event in replay.events
                if event.event_type == "modeling.capture_failed"
            )
            assert failed.payload["code"] == "capture.framing_failed"
            assert failed.payload["change_id"] == CHG
            # The failure is explicit in the validation event: an Artifact
            # Failed result, never a guessed pass.
            validation = next(
                event
                for event in replay.events
                if event.event_type == "modeling.validation_completed"
            )
            assert validation.payload["complete"] is False
            artifact_result = next(
                item
                for item in validation.payload["results"]
                if item["validator"] == "Artifact"
            )
            assert artifact_result["status"] == "Failed"
            assert artifact_result["code"] == "modeling.artifact.capture_failed"
            assert sum(
                event.event_type == "modeling.artifact_captured"
                for event in replay.events
            ) == 0
            assert bridge.calls.count("apply") == 1
            assert bridge.calls.count("capture") == 1
            # No artifact rows or files were registered.
            assert await runtime._artifacts.list_for_session(SES) == ()
            # A later trusted join replays nothing.
            await runtime.apply_changeset_trusted(CHG)
            assert bridge.calls.count("capture") == 1
        finally:
            await runtime._shutdown()
            await db.close()

    asyncio.run(scenario())
