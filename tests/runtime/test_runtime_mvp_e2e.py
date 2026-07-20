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
from eee_agent.core import AgentException
from eee_agent.runtime.artifacts import ArtifactStore
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore
from eee_agent.runtime.knowledge import KnowledgeRuntime, KnowledgeStatusCode

from tests.modeling.test_bootstrap import _compile_bootstrap, _receipt
from tests.runtime.test_changeset_recovery import (
    CHG,
    NOW,
    SES,
    FakeBridge,
    _changeset,
    _policy,
)


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
    manifest = __import__(
        "eee_agent.modeling.bootstrap", fromlist=["derive_bootstrap_manifest"]
    ).derive_bootstrap_manifest(changeset, _receipt())
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
            __import__("eee_agent.core", fromlist=["AgentError"]).AgentError(
                code="bridge.stale_scene",
                category=__import__("eee_agent.core", fromlist=["ErrorCategory"]).ErrorCategory.HOUDINI_BRIDGE,
                message_for_user="scene changed",
            )
        )
        db, repo, service = await _open_changeset_service(tmp_path / "stale.sqlite", bridge=bridge)
        try:
            changeset = _changeset()
            await service.propose(changeset, _policy(changeset))
            await service.approve(CHG, changeset.digest)
            with pytest.raises(Exception):
                await service.apply(CHG)
            # Apply never claims success when the typed Bridge reports stale
            # scene facts; recovery remains explicit for a later retry.
            assert (await repo.get_changeset(CHG)).state.value in {
                "Approved",
                "Applying",
                "CriticalRecovery",
            }
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
