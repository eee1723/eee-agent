"""Task 16-B2a: ChangeSet approval service, atomic events, and decisions.

Drives the trusted :class:`ChangeSetService` over the schema-v2
:class:`ChangeSetRepository` and a real :class:`EventStore` /
:class:`RuntimeDatabase` (no ``hou``, ``rpyc``, transport, or LLM). The clock
and scene-binding seams are injected so proposal/expiry and approval binding
are deterministic. Each test runs an async scenario through ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ChangeSet,
    CreateNode,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    PolicyDecision,
    RiskSummary,
)
from eee_agent.changesets.repository import (
    ChangeSetRepository,
    ChangeSetState,
)
from eee_agent.changesets.service import (
    ChangeSetService,
    summary_from_decision,
)
from eee_agent.core import AgentException
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore
from eee_agent.runtime.models import RetentionClass

# --------------------------------------------------------------------------
# constants + DTO factories (compact, self-contained)
# --------------------------------------------------------------------------

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
CHG = f"chg_{'4' * 32}"
REVISION = "a" * 64
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
SOON = NOW + timedelta(seconds=10)
LATER = NOW + timedelta(seconds=30)

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


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t


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


def _changeset(**overrides: object) -> ChangeSet:
    from eee_agent.changesets.contracts import CheckpointPlan

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
            ParmValueEquals(
                target=_noderef(node_id="n_new", path="/obj/ws/geo_new"),
                parm_name="tx",
                value=0,
            ),
        ),
        risk_summary=_risk(),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )
    values.update(overrides)
    return ChangeSet(**values)  # type: ignore[arg-type]


def _policy(changeset: ChangeSet, **overrides: object) -> PolicyDecision:
    values: dict[str, object] = dict(
        allowed=True,
        mode=PermissionMode.OWNED_WORKSPACE,
        normalized_effects=("node.create",),
        approval_required=True,
        backup_required=False,
        denial_codes=(),
        changeset_digest=changeset.digest,
    )
    values.update(overrides)
    return PolicyDecision(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# DB + service helpers
# --------------------------------------------------------------------------


async def add_session(db: RuntimeDatabase, sid: str = SES) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _SESSION_INSERT, (sid, "T", "active", NOW.isoformat(), NOW.isoformat(), 0, 0)
        )


async def add_run(db: RuntimeDatabase, rid: str = RUN) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _RUN_INSERT,
            (rid, SES, "Completed", "in", None, NOW.isoformat(), None, NOW.isoformat(), None, "{}"),
        )


async def fresh_service(
    db_path: Path,
    *,
    clock=None,
    binding_provider=None,
    ttl: float = 300.0,
) -> tuple[RuntimeDatabase, ChangeSetService, ChangeSetRepository, EventStore]:
    db = await RuntimeDatabase.open(db_path)
    await add_session(db)
    await add_run(db)
    events = EventStore(db)
    repo = ChangeSetRepository(db, events=events)
    service = ChangeSetService(
        repo,
        clock=clock,
        binding_provider=binding_provider,
        approval_ttl_seconds=ttl,
    )
    return db, service, repo, events


def _err_code(exc: BaseException) -> str:
    assert isinstance(exc, AgentException), f"expected AgentException, got {type(exc)}"
    return exc.error.code


async def _event_types(events: EventStore, sid: str = SES) -> list[str]:
    replay = await events.replay(sid, after_seq=0, limit=100)
    return [e.event_type for e in replay.events]


# --------------------------------------------------------------------------
# proposal
# --------------------------------------------------------------------------


def test_propose_persists_awaiting_with_pending_and_bounded_events(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            result = await service.propose(cs, _policy(cs))
            assert result.state is ChangeSetState.AWAITING_APPROVAL
            assert result.approval.decision is ApprovalDecision.PENDING
            assert result.approval.changeset_digest == cs.digest
            assert result.approval.requested_at == NOW
            # exactly two events, in order, durable
            assert [e.event_type for e in result.events] == [
                "changeset.proposed",
                "approval.requested",
            ]
            for e in result.events:
                assert e.retention_class is RetentionClass.DURABLE
                assert e.session_id == SES
                assert e.run_id == RUN
            proposed = result.events[0]
            proposed_payload = proposed.to_dict()["payload"]
            assert set(proposed_payload.keys()) == {
                "change_id", "session_id", "run_id", "changeset_digest",
                "state", "required_permission", "risk_summary",
            }
            assert proposed_payload["state"] == "AwaitingApproval"
            assert proposed_payload["changeset_digest"] == cs.digest
            assert proposed_payload["risk_summary"] == cs.risk_summary.to_dict()
            assert "operations" not in proposed_payload
            requested = result.events[1]
            requested_payload = requested.to_dict()["payload"]
            assert set(requested_payload.keys()) == {
                "approval_id", "change_id", "changeset_digest", "expires_at",
            }
            assert requested_payload["approval_id"] == result.approval.approval_id
            # persisted state mirrors the returned result
            stored = await repo.get_changeset(CHG)
            assert stored.state is ChangeSetState.AWAITING_APPROVAL
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.PENDING
            assert await _event_types(events) == [
                "changeset.proposed", "approval.requested",
            ]
        finally:
            await db.close()

    _run(scenario())


def test_propose_is_idempotent_for_same_digest(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            first = await service.propose(cs, _policy(cs))
            second = await service.propose(cs, _policy(cs))
            assert second.events == ()  # no new events on idempotent re-propose
            assert second.state is ChangeSetState.AWAITING_APPROVAL
            assert second.approval.approval_id == first.approval.approval_id
            # only the original two events exist
            assert await _event_types(events) == [
                "changeset.proposed", "approval.requested",
            ]
        finally:
            await db.close()

    _run(scenario())


def test_panel_summaries_are_bounded_and_exclude_raw_changeset(
    db_path: Path,
) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            paths = tuple(f"/obj/ws/node_{index}" for index in range(14))
            effects = ("node.create", "parm.set", "wire.connect")
            cs = _changeset(
                risk_summary=_risk(
                    changes_wiring=True,
                    requires_backup=True,
                    effect_names=effects,
                    affected_paths=paths,
                )
            )
            proposed = await service.propose(cs, _policy(cs))
            summaries = await service.list_panel_summaries(SES, limit=50)
            assert len(summaries) == 1
            data = summaries[0].to_dict()
            assert data["change_id"] == cs.change_id
            assert data["changeset_digest"] == cs.digest
            assert data["state"] == "AwaitingApproval"
            assert data["approval"]["approval_id"] == (
                proposed.approval.approval_id
            )
            assert data["approval"]["decision"] == "Pending"
            assert data["receipt"] is None
            risk = data["risk"]
            assert risk["effect_count"] == 3
            assert risk["effect_names"] == list(effects)
            assert risk["affected_path_count"] == 14
            assert len(risk["affected_paths"]) == 12
            assert risk["affected_paths_truncated"] is True
            encoded = str(data)
            for forbidden in (
                "operations",
                "checkpoint_plan",
                "preconditions",
                "expected_postconditions",
                "parm_name",
                "expected_old_value",
            ):
                assert forbidden not in encoded
        finally:
            await db.close()

    _run(scenario())


def test_propose_rejects_conflicting_digest(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            # same change_id, different content -> different digest -> conflict
            conflict = _changeset(operations=(
                _create(op_id="op_other", node_id="n_other", node_name="geo_other"),
            ), affected_nodes=(
                _noderef(node_id="n_other", path="/obj/ws/geo_other"),
            ))
            assert conflict.change_id == cs.change_id
            assert conflict.digest != cs.digest
            with pytest.raises(AgentException) as exc:
                await service.propose(conflict, _policy(conflict))
            assert _err_code(exc.value) == "changeset.proposal_conflict"
            # original proposal intact
            stored = await repo.get_changeset(CHG)
            assert stored.state is ChangeSetState.AWAITING_APPROVAL
            assert stored.changeset.digest == cs.digest
        finally:
            await db.close()

    _run(scenario())


def test_propose_rejects_denied_policy(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            with pytest.raises(AgentException) as exc:
                await service.propose(
                    cs, _policy(cs, allowed=False, denial_codes=("policy.scope_violation",))
                )
            assert _err_code(exc.value) == "policy.denied"
        finally:
            await db.close()

    _run(scenario())


def test_propose_rejects_policy_digest_mismatch(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            with pytest.raises(AgentException) as exc:
                await service.propose(cs, _policy(cs, changeset_digest="b" * 64))
            assert _err_code(exc.value) == "changeset.invalid"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# approve / reject
# --------------------------------------------------------------------------


def _proposed_service(db_path, *, clock=None, binding_provider=None):
    return fresh_service(db_path, clock=clock, binding_provider=binding_provider)


def test_approve_transitions_and_binds_current_scene(db_path: Path) -> None:
    clock = FakeClock(NOW)
    binding = _binding(instance_id="hou_instance_7", scene_epoch=9)

    async def scenario() -> None:
        db, service, repo, events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: binding
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            result = await service.approve(CHG, cs.digest)
            assert result.outcome is ApprovalDecision.APPROVED
            assert result.changeset_state is ChangeSetState.APPROVED
            assert result.approval.approved_instance_id == "hou_instance_7"
            assert result.approval.approved_scene_epoch == 9
            assert result.approval.decided_by == "local_user"
            assert result.approval.decided_at == NOW
            assert [e.event_type for e in result.events] == [
                "approval.approved", "changeset.state_changed",
            ]
            state_changed = result.events[1]
            assert state_changed.payload == {
                "change_id": CHG, "from": "AwaitingApproval", "to": "Approved",
            }
            approved_ev = result.events[0]
            assert approved_ev.payload["approved_instance_id"] == "hou_instance_7"
            assert approved_ev.payload["approved_scene_epoch"] == 9
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.APPROVED
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.APPROVED
            assert await _event_types(events) == [
                "changeset.proposed", "approval.requested",
                "approval.approved", "changeset.state_changed",
            ]
        finally:
            await db.close()

    _run(scenario())


def test_approve_without_binding_seam_fails_closed(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=None
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            with pytest.raises(AgentException) as exc:
                await service.approve(CHG, cs.digest)
            assert _err_code(exc.value) == "bridge.capability_unavailable"
        finally:
            await db.close()

    _run(scenario())


def test_reject_transitions_to_rejected(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            result = await service.reject(CHG, cs.digest)
            assert result.outcome is ApprovalDecision.REJECTED
            assert result.changeset_state is ChangeSetState.REJECTED
            assert result.approval.decided_by == "local_user"
            assert result.approval.approved_instance_id is None
            assert [e.event_type for e in result.events] == [
                "approval.rejected", "changeset.state_changed",
            ]
            assert result.events[1].payload == {
                "change_id": CHG, "from": "AwaitingApproval", "to": "Rejected",
            }
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.REJECTED
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.REJECTED
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# expiry
# --------------------------------------------------------------------------


def test_expired_pending_transitions_once_and_reports_expired(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding(), ttl=10.0
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            clock.t = NOW + timedelta(seconds=11)  # past the 10s expiry
            result = await service.approve(CHG, cs.digest)
            assert result.outcome is ApprovalDecision.EXPIRED
            assert result.changeset_state is ChangeSetState.EXPIRED
            assert [e.event_type for e in result.events] == [
                "approval.expired", "changeset.state_changed",
            ]
            assert result.events[1].payload == {
                "change_id": CHG, "from": "AwaitingApproval", "to": "Expired",
            }
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.EXPIRED
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.EXPIRED
        finally:
            await db.close()

    _run(scenario())


def test_cannot_approve_after_expiry(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding(), ttl=10.0
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            clock.t = NOW + timedelta(seconds=11)
            await service.approve(CHG, cs.digest)  # expires once
            # a second attempt finds the approval already Expired -> error
            with pytest.raises(AgentException) as exc:
                await service.approve(CHG, cs.digest)
            assert _err_code(exc.value) == "approval.expired"
            # unchanged
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.EXPIRED
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# stale digest + already decided
# --------------------------------------------------------------------------


def test_stale_digest_is_rejected(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            with pytest.raises(AgentException) as exc:
                await service.approve(CHG, "b" * 64)
            assert _err_code(exc.value) == "approval.digest_mismatch"
        finally:
            await db.close()

    _run(scenario())


def test_cannot_decide_already_decided_records(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            await service.approve(CHG, cs.digest)
            # re-approve an Approved record
            with pytest.raises(AgentException) as exc:
                await service.approve(CHG, cs.digest)
            assert _err_code(exc.value) == "approval.already_consumed"
            # reject an Approved record
            with pytest.raises(AgentException) as exc:
                await service.reject(CHG, cs.digest)
            assert _err_code(exc.value) == "approval.already_consumed"

            cs2 = _changeset(change_id=f"chg_{'8' * 32}")
            await service.propose(cs2, _policy(cs2))
            await service.reject(cs2.change_id, cs2.digest)
            # re-reject a Rejected record
            with pytest.raises(AgentException) as exc:
                await service.reject(cs2.change_id, cs2.digest)
            assert _err_code(exc.value) == "approval.already_consumed"
            # approve a Rejected record
            with pytest.raises(AgentException) as exc:
                await service.approve(cs2.change_id, cs2.digest)
            assert _err_code(exc.value) == "approval.already_consumed"
        finally:
            await db.close()

    _run(scenario())


def test_unknown_changeset_and_missing_approval_errors(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            with pytest.raises(AgentException) as exc:
                await service.approve(f"chg_{'9' * 32}", "a" * 64)
            assert _err_code(exc.value) == "runtime.changeset_not_found"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# atomic rollback when event insertion fails
# --------------------------------------------------------------------------


def test_event_insert_failure_rolls_back_state_and_approves(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            async with db.write_transaction() as conn:
                await conn.execute(
                    "CREATE TRIGGER fail_event_insert BEFORE INSERT ON events "
                    "BEGIN SELECT RAISE(ABORT, 'forced event insert failure'); END"
                )
            with pytest.raises(sqlite3.Error):
                await service.approve(CHG, cs.digest)
            # nothing committed: approval still Pending, changeset still
            # AwaitingApproval, and no approval/changeset decision events.
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.PENDING
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.AWAITING_APPROVAL
            async with db.write_transaction() as conn:
                await conn.execute("DROP TRIGGER fail_event_insert")
            # service works again afterwards
            result = await service.approve(CHG, cs.digest)
            assert result.outcome is ApprovalDecision.APPROVED
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# concurrency: exactly one transition wins
# --------------------------------------------------------------------------


def test_concurrent_approve_has_single_winner(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            results = await asyncio.gather(
                service.approve(CHG, cs.digest),
                service.approve(CHG, cs.digest),
                return_exceptions=True,
            )
            wins = [r for r in results if not isinstance(r, BaseException)]
            errs = [r for r in results if isinstance(r, BaseException)]
            assert len(wins) == 1
            assert wins[0].outcome is ApprovalDecision.APPROVED
            assert len(errs) == 1
            assert _err_code(errs[0]) == "approval.already_consumed"
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.APPROVED
        finally:
            await db.close()

    _run(scenario())


def test_concurrent_approve_and_reject_single_winner(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            results = await asyncio.gather(
                service.approve(CHG, cs.digest),
                service.reject(CHG, cs.digest),
                return_exceptions=True,
            )
            wins = [r for r in results if not isinstance(r, BaseException)]
            errs = [r for r in results if isinstance(r, BaseException)]
            assert len(wins) == 1
            assert len(errs) == 1
            assert _err_code(errs[0]) == "approval.already_consumed"
            final = (await repo.get_approval(CHG)).decision
            assert final in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED)
        finally:
            await db.close()

    _run(scenario())


def test_concurrent_expiry_transitions_once(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding(), ttl=10.0
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            clock.t = NOW + timedelta(seconds=11)
            results = await asyncio.gather(
                service.approve(CHG, cs.digest),
                service.approve(CHG, cs.digest),
                return_exceptions=True,
            )
            expired_wins = [
                r for r in results
                if not isinstance(r, BaseException)
                and r.outcome is ApprovalDecision.EXPIRED  # type: ignore[union-attr]
            ]
            errs = [r for r in results if isinstance(r, BaseException)]
            assert len(expired_wins) == 1
            assert len(errs) == 1
            assert _err_code(errs[0]) == "approval.expired"
            assert (await repo.get_approval(CHG)).decision is ApprovalDecision.EXPIRED
            assert (await repo.get_changeset(CHG)).state is ChangeSetState.EXPIRED
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# restart preserves state + event sequence
# --------------------------------------------------------------------------


def test_restart_preserves_state_and_event_sequence(db_path: Path) -> None:
    db_path_str = str(db_path)
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        cs = _changeset()
        await service.propose(cs, _policy(cs))
        await service.approve(CHG, cs.digest)
        await db.close()
        # reopen as if recovering
        db2 = await RuntimeDatabase.open(db_path_str)
        try:
            repo2 = ChangeSetRepository(db2, events=EventStore(db2))
            events2 = EventStore(db2)
            stored = await repo2.get_changeset(CHG)
            assert stored.state is ChangeSetState.APPROVED
            assert stored.changeset.to_dict() == cs.to_dict()
            approval = await repo2.get_approval(CHG)
            assert approval.decision is ApprovalDecision.APPROVED
            assert approval.changeset_digest == cs.digest
            assert await _event_types(events2) == [
                "changeset.proposed", "approval.requested",
                "approval.approved", "changeset.state_changed",
            ]
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# RuntimeService wiring: post-commit subscriber notification
# --------------------------------------------------------------------------


async def _seed_session_run(db: RuntimeDatabase) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _SESSION_INSERT,
            (SES, "T", "active", NOW.isoformat(), NOW.isoformat(), 0, 0),
        )
        await conn.execute(
            _RUN_INSERT,
            (RUN, SES, "Completed", "in", None, NOW.isoformat(), None,
             NOW.isoformat(), None, "{}"),
        )


def test_runtime_service_approve_notifies_subscribers_post_commit(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(db_path.parent / "home_rt"))
    clock = FakeClock(NOW)
    binding = _binding(instance_id="hou_instance_1", scene_epoch=1)

    async def scenario() -> None:
        from eee_agent.runtime.paths import RuntimePaths
        from eee_agent.runtime.service import RuntimeService

        paths = RuntimePaths.from_environment()

        def factory(_saver):  # runner never used: no runs are started
            return object()

        async with RuntimeService.open(
            paths,
            runner_factory=factory,
            changeset_clock=clock,
            changeset_binding_provider=lambda: binding,
        ) as service:
            await _seed_session_run(service._database)
            cs = _changeset()
            await service._changesets.propose(cs, _policy(cs))
            received: list = []
            unsub = service.subscribe(lambda r: received.append(r))
            try:
                result = await service.approve_changeset(CHG, cs.digest)
            finally:
                unsub()
            assert result["decision"] == "Approved"
            assert result["state"] == "Approved"
            assert result["approved_instance_id"] == "hou_instance_1"
            assert result["approved_scene_epoch"] == 1
            # subscribers received the committed approve events AFTER commit
            assert [r.event_type for r in received] == [
                "approval.approved", "changeset.state_changed",
            ]

    _run(scenario())


def test_runtime_service_approve_failure_notifies_nobody(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(db_path.parent / "home_rt2"))
    clock = FakeClock(NOW)
    binding = _binding(instance_id="hou_instance_1", scene_epoch=1)

    async def scenario() -> None:
        from eee_agent.runtime.paths import RuntimePaths
        from eee_agent.runtime.service import RuntimeService

        paths = RuntimePaths.from_environment()

        def factory(_saver):
            return object()

        async with RuntimeService.open(
            paths,
            runner_factory=factory,
            changeset_clock=clock,
            changeset_binding_provider=lambda: binding,
        ) as service:
            await _seed_session_run(service._database)
            cs = _changeset()
            await service._changesets.propose(cs, _policy(cs))
            async with service._database.write_transaction() as conn:
                await conn.execute(
                    "CREATE TRIGGER fail_event_insert BEFORE INSERT ON events "
                    "BEGIN SELECT RAISE(ABORT, 'forced event insert failure'); END"
                )
            received: list = []
            unsub = service.subscribe(lambda r: received.append(r))
            try:
                with pytest.raises(sqlite3.Error):
                    await service.approve_changeset(CHG, cs.digest)
            finally:
                unsub()
            # rolled-back decision notifies no subscriber and leaves no event
            assert received == []
            async with service._database.write_transaction() as conn:
                await conn.execute("DROP TRIGGER fail_event_insert")

    _run(scenario())


def test_runtime_modeling_approval_runs_internal_apply(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modeling Runtime owns Apply after the durable approval decision."""
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(db_path.parent / "home_rt_auto"))
    clock = FakeClock(NOW)
    binding = _binding(instance_id="hou_instance_1", scene_epoch=1)

    async def scenario() -> None:
        from eee_agent.runtime.paths import RuntimePaths
        from eee_agent.runtime.service import RuntimeService

        paths = RuntimePaths.from_environment()

        async with RuntimeService.open(
            paths,
            runner_factory=lambda _saver: object(),
            changeset_clock=clock,
            changeset_binding_provider=lambda: binding,
        ) as service:
            await _seed_session_run(service._database)
            cs = _changeset()
            await service._changesets.propose(cs, _policy(cs))
            service._modeling_catalog_provider = lambda: object()  # type: ignore[assignment]
            monkeypatch.setattr(service, "_changesets_has_bridge", lambda: True)
            calls: list[str] = []

            async def fake_apply(change_id: str):
                calls.append(change_id)
                return SimpleNamespace(
                    state=SimpleNamespace(value="Applied"),
                    receipt=SimpleNamespace(
                        status=SimpleNamespace(value="Applied"),
                        scene_may_have_changed=False,
                    ),
                )

            monkeypatch.setattr(service, "apply_changeset_trusted", fake_apply)
            result = await service.approve_changeset(CHG, cs.digest)
            assert calls == [CHG]
            assert result["decision"] == "Approved"
            assert result["apply"] == {
                "state": "Applied",
                "receipt_status": "Applied",
                "scene_may_have_changed": False,
            }

    _run(scenario())


# --------------------------------------------------------------------------
# summary + import boundary
# --------------------------------------------------------------------------


def test_summary_from_decision_is_bounded(db_path: Path) -> None:
    clock = FakeClock(NOW)

    async def scenario() -> None:
        db, service, _repo, _events = await fresh_service(
            db_path, clock=clock, binding_provider=lambda: _binding()
        )
        try:
            cs = _changeset()
            await service.propose(cs, _policy(cs))
            result = await service.approve(CHG, cs.digest)
            summary = summary_from_decision(result)
            blob = str(summary.to_dict())
            assert summary.to_dict() == {
                "change_id": CHG,
                "approval_id": result.approval.approval_id,
                "changeset_digest": cs.digest,
                "decision": "Approved",
                "state": "Approved",
                "approved_instance_id": "hou_instance_1",
                "approved_scene_epoch": 1,
            }
            # no full DTO / operations leak in the summary
            assert "operations" not in blob
            assert "checkpoint_plan" not in blob
        finally:
            await db.close()

    _run(scenario())


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


def test_service_module_does_not_import_forbidden() -> None:
    import ast
    import inspect

    roots = _imported_roots("eee_agent.changesets.service")
    for root in roots:
        assert root not in _FORBIDDEN_IMPORT_ROOTS, f"forbidden import root: {root}"
    module = __import__("eee_agent.changesets.service", fromlist=["x"])
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


def test_service_imports_without_heavy_deps() -> None:
    """A fresh interpreter importing the ChangeSet service must not load hou/rpyc.

    The changeset package is the write-gate boundary and must stay free of
    Houdini/rpyc. (``eee_agent.runtime.service`` legitimately resolves the
    read-only agent runner, which is outside this boundary and intentionally
    not asserted here.)
    """
    import json
    import subprocess
    import sys

    code = (
        "import sys, json; "
        "import eee_agent.changesets.service; "
        "print(json.dumps(sorted(k for k in ('hou', 'rpyc') if k in sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []
