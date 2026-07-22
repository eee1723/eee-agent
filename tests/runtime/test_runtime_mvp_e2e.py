"""Deterministic Runtime MVP acceptance boundaries.

These tests are intentionally small orchestration checks.  Detailed contract
and transport suites own the exhaustive matrix; this file proves that the
offline MVP surfaces one truthful result for each user-visible boundary and
does not turn unavailable optional systems into a false success.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from eee_agent.changesets.repository import ChangeSetRepository
from eee_agent.changesets.service import ChangeSetService
from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.modeling.bootstrap import derive_bootstrap_manifest
from eee_agent.modeling.contracts import RepairStatus, ValidatorKind
from eee_agent.modeling.validation import issue_repair_ticket, validate_applied_scene, validate_scene_query
from eee_agent.runtime.models import RetentionClass
from eee_agent.runtime.artifacts import ArtifactStore
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore
from eee_agent.runtime.knowledge import KnowledgeRuntime, KnowledgeStatusCode
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.service import RuntimeService

from tests.modeling.test_bootstrap import _compile_bootstrap, _receipt
from tests.modeling.test_validation import _preapply_report, _scene_query, _compile
from tests.runtime.test_changeset_recovery import (
    CHG,
    NOW,
    SES,
    FakeBridge,
    _changeset,
    _manifest,
    _policy,
)
from tests.runtime.runtime_mvp_provider_e2e import _validate_evidence


async def _open_changeset_service(
    db_path: Path,
    *,
    clock=None,
    bridge: FakeBridge | None = None,
) -> tuple[RuntimeDatabase, ChangeSetRepository, ChangeSetService]:
    db = await RuntimeDatabase.open(db_path)
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO sessions(session_id,title,status,created_at,updated_at,last_seq,replay_floor_seq) VALUES (?,?,?,?,?,?,?)",
            (SES, "MVP", "active", NOW.isoformat(), NOW.isoformat(), 0, 0),
        )
        await conn.execute(
            "INSERT INTO runs(run_id,session_id,status,user_input,final_response,created_at,started_at,finished_at,failure_json,model_snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _changeset().run_id,
                SES,
                "Completed",
                "mvp",
                None,
                NOW.isoformat(),
                None,
                NOW.isoformat(),
                None,
                "{}",
            ),
        )
    events = EventStore(db)
    repository = ChangeSetRepository(db, events=events)
    service = ChangeSetService(
        repository,
        clock=clock or (lambda: NOW),
        bridge_provider=bridge,
    )
    return db, repository, service


def test_mvp_empty_scene_bootstrap_is_offline_ready() -> None:
    """A typed applied receipt derives ownership without a live provider."""
    changeset = _compile_bootstrap().changeset
    manifest = derive_bootstrap_manifest(changeset, _receipt())
    assert manifest.workspace_id
    assert manifest.roots and manifest.roots[0].role == "root"
    assert manifest.nodes[0].path == "/obj/eee_model"


def test_mvp_workspace_proposal_reject_and_expired_are_durable(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge = FakeBridge()
        db, repo, service = await _open_changeset_service(tmp_path / "reject.sqlite", bridge=bridge)
        try:
            proposal = await service.propose(_changeset(), _policy(_changeset()))
            rejected = await service.reject(CHG, proposal.changeset.digest)
            assert rejected.outcome.value == "Rejected"
            assert (await repo.get_changeset(CHG)).state.value == "Rejected"
        finally:
            await db.close()

        class Clock:
            now = NOW

            def __call__(self):
                return self.now

        clock = Clock()
        expired_id = f"chg_{'8' * 32}"
        db, repo, service = await _open_changeset_service(
            tmp_path / "expired.sqlite", clock=clock, bridge=bridge
        )
        try:
            changeset = _changeset(expired_id)
            await service.propose(changeset, _policy(changeset))
            clock.now = NOW + timedelta(seconds=301)
            expired = await service.approve(expired_id, changeset.digest)
            assert expired.outcome.value == "Expired"
            assert (await repo.get_approval(expired_id)).decision.value == "Expired"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_mvp_bridge_unavailable_and_stale_apply_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, _repo, service = await _open_changeset_service(tmp_path / "offline.sqlite")
        try:
            changeset = _changeset()
            await service.propose(changeset, _policy(changeset))
            with pytest.raises(AgentException) as error:
                await service.approve(CHG, changeset.digest)
            assert error.value.error.code == "bridge.capability_unavailable"
        finally:
            await db.close()

        bridge = FakeBridge()
        bridge.apply_result = AgentException(
            AgentError(
                code="bridge.stale_scene",
                category=ErrorCategory.HOUDINI_BRIDGE,
                message_for_user="scene changed",
            )
        )
        db, repo, service = await _open_changeset_service(tmp_path / "stale.sqlite", bridge=bridge)
        try:
            changeset = _changeset()
            await repo.insert_workspace(_manifest())
            await service.propose(changeset, _policy(changeset))
            await service.approve(CHG, changeset.digest)
            runtime = RuntimeService(
                db,
                RuntimePaths(
                    home=tmp_path,
                    state_dir=tmp_path,
                    app_db=tmp_path / "stale.sqlite",
                    checkpoints_db=tmp_path / "checkpoints.sqlite",
                    lock_file=tmp_path / "runtime.lock",
                    discovery_file=tmp_path / "runtime.json",
                    token_file=tmp_path / "runtime.token",
                    artifacts_dir=tmp_path / "artifacts",
                ),
                changeset_clock=lambda: NOW,
                changeset_bridge_provider=bridge,
            )
            with pytest.raises(AgentException) as error:
                await runtime.apply_changeset_trusted(CHG)
            assert error.value.error.code == "bridge.stale_scene"
            await runtime._shutdown()
        finally:
            await db.close()

    asyncio.run(scenario())


def test_mvp_cook_and_validation_failures_are_explicit() -> None:
    compilation = _compile()
    missing_nodes = _scene_query(compilation)
    missing_nodes = missing_nodes.__class__(
        binding=missing_nodes.binding,
        selected_nodes=missing_nodes.selected_nodes,
        nodes=(),
    )
    cook, _geometry = validate_applied_scene(
        changeset=compilation.changeset, query=missing_nodes
    )
    assert cook.status.value == "Failed"
    assert cook.code == "modeling.cook.failed"

    failed_geometry = validate_scene_query(
        report=_preapply_report(compilation),
        changeset=compilation.changeset,
        query=_scene_query(compilation, empty_last=True),
    )
    geometry = next(item for item in failed_geometry.results if item.validator is ValidatorKind.GEOMETRY)
    assert geometry.status.value == "Failed"
    assert geometry.code == "modeling.geometry.empty_or_invalid"


def test_mvp_repair_exhaustion_is_not_a_success() -> None:
    from eee_agent.modeling.contracts import RepairBudget

    budget = RepairBudget(max_attempts_per_stage=2, attempts=())
    for attempt in (1, 2):
        budget, ticket = issue_repair_ticket(
            budget=budget,
            ticket_id=f"repair_mvp_{attempt}",
            validator=ValidatorKind.GRAPH,
            failure_code="modeling.graph.invalid",
            message="graph evidence failed",
            evidence_digests=("a" * 64,),
            failed_parameter_samples=(),
            replay_boundary_digest="a" * 64,
        )
        assert ticket.status is RepairStatus.OPEN
    _unchanged, exhausted = issue_repair_ticket(
        budget=budget,
        ticket_id="repair_mvp_3",
        validator=ValidatorKind.GRAPH,
        failure_code="modeling.graph.invalid",
        message="graph evidence failed",
        evidence_digests=("a" * 64,),
        failed_parameter_samples=(),
        replay_boundary_digest="a" * 64,
    )
    assert exhausted.status is RepairStatus.EXHAUSTED


def test_mvp_restart_replay_has_no_duplicate_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        db, _repo, _service = await _open_changeset_service(tmp_path / "replay.sqlite")
        try:
            events = EventStore(db)
            await events.append(
                session_id=SES,
                run_id=None,
                event_type="runtime.mvp_marker",
                payload={"status": "completed"},
                retention_class=RetentionClass.DURABLE,
            )
            first = await events.replay(SES, after_seq=0, limit=10)
            second = await events.replay(SES, after_seq=0, limit=10)
            assert [(item.seq, item.event_type) for item in first.events] == [
                (item.seq, item.event_type) for item in second.events
            ]
            assert len(second.events) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_mvp_artifact_and_knowledge_unavailable_are_explicit(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(tmp_path / "artifacts.sqlite")
        try:
            store = ArtifactStore(db, tmp_path / "artifacts")
            assert await store.get("art_" + "0" * 32) is None
        finally:
            await db.close()

    asyncio.run(scenario())
    knowledge = KnowledgeRuntime(tmp_path / "missing-kb.sqlite")
    assert knowledge.status.code is KnowledgeStatusCode.MISSING
    assert knowledge.search("box")["code"] == "kb_unavailable"
    assert knowledge.get("entity")["code"] == "kb_unavailable"


def test_mvp_provider_runner_reports_not_run_without_opt_in() -> None:
    script = Path(__file__).with_name("runtime_mvp_provider_e2e.py")
    env = dict(os.environ)
    env.pop("EEE_RUN_RUNTIME_MVP_PROVIDER_E2E", None)
    env.pop("EEE_RUNTIME_MVP_PROVIDER_COMMAND", None)
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "not_run"
    assert payload["reason"] == "explicit_opt_in_required"
    assert "API_KEY" not in completed.stdout


def test_mvp_provider_evidence_is_strict_and_cannot_be_a_noop(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    evidence = {
        "proposal_digest": "a" * 64,
        "approval_event": "approved",
        "receipt_status": "applied",
        "validation_status": "passed",
        "artifact_status": "available",
        "vision_status": "completed",
        "vision_accepted": True,
        "vision_reason_code": "vision.completed",
        "vision_artifact_digest_match": True,
        "replay_last_seq": 7,
        "scene_cleanup": "completed",
    }
    path.write_text(json.dumps(evidence), encoding="utf-8")
    assert _validate_evidence(path)
    path.write_text(json.dumps({**evidence, "provider_output": "unbounded"}), encoding="utf-8")
    assert not _validate_evidence(path)
    path.write_text(
        json.dumps({**evidence, "vision_artifact_digest_match": "yes"}),
        encoding="utf-8",
    )
    assert not _validate_evidence(path)
    path.unlink()
    assert not _validate_evidence(path)
