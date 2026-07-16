from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eee_agent.changesets.contracts import ReceiptStatus
from eee_agent.changesets.repository import (
    ChangeSetRepository,
    ChangeSetState,
)
from eee_agent.changesets.service import ChangeSetService
from eee_agent.modeling.bootstrap import derive_bootstrap_manifest
from eee_agent.core import AgentException
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore
from tests.modeling.test_bootstrap import _receipt
from tests.modeling.test_compiler import RUN, SES, _compile_bootstrap
from tests.runtime.test_changeset_repository import add_run, add_session


def _run(coro):
    return asyncio.run(coro)


async def _repo(path: Path):
    db = await RuntimeDatabase.open(path)
    await add_session(db, SES)
    await add_run(db, RUN, SES)
    events = EventStore(db)
    return db, ChangeSetRepository(db, events=events)


def test_complete_bootstrap_apply_atomically_activates_workspace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db, repo = await _repo(tmp_path / "runtime.sqlite")
        try:
            changeset = _compile_bootstrap().changeset
            receipt = _receipt()
            manifest = derive_bootstrap_manifest(changeset, receipt)
            await repo.insert_changeset(
                changeset, state=ChangeSetState.APPLYING
            )
            completed = await repo.complete_bootstrap_apply(receipt, manifest)
            assert completed.state is ChangeSetState.APPLIED
            assert [event.event_type for event in completed.events] == [
                "changeset.state_changed",
                "changeset.applied",
                "workspace.created",
            ]
            assert await repo.get_workspace(manifest.workspace_id) == manifest
            active = await repo.get_active_workspace(SES)
            assert active is not None
            assert active.active_workspace_id == manifest.workspace_id
            assert (await repo.get_changeset(changeset.change_id)).state is (
                ChangeSetState.APPLIED
            )
            assert await repo.get_receipt(changeset.change_id) == receipt

            repeated = await repo.complete_bootstrap_apply(receipt, manifest)
            assert repeated.events == ()
            assert repeated.state is ChangeSetState.APPLIED
        finally:
            await db.close()

    _run(scenario())


def test_bootstrap_completion_rejects_unsuccessful_receipt_without_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db, repo = await _repo(tmp_path / "runtime.sqlite")
        try:
            changeset = _compile_bootstrap().changeset
            await repo.insert_changeset(
                changeset, state=ChangeSetState.APPLYING
            )
            receipt = _receipt(
                status=ReceiptStatus.ROLLED_BACK,
                applied_op_ids=(),
                scene_may_have_changed=False,
            )
            with pytest.raises(ValueError, match="must be applied"):
                await repo.complete_bootstrap_apply(
                    receipt,
                    derive_bootstrap_manifest(changeset, _receipt()),
                )
            assert (await repo.get_changeset(changeset.change_id)).state is (
                ChangeSetState.APPLYING
            )
            assert await repo.list_workspaces(SES) == ()
        finally:
            await db.close()

    _run(scenario())


def test_bootstrap_completion_conflict_rolls_back_receipt_and_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        db, repo = await _repo(tmp_path / "runtime.sqlite")
        try:
            changeset = _compile_bootstrap().changeset
            receipt = _receipt()
            manifest = derive_bootstrap_manifest(changeset, receipt)
            await repo.insert_changeset(
                changeset, state=ChangeSetState.APPLYING
            )
            # Seed the same exact Workspace without completing the ChangeSet;
            # the bootstrap transaction must reject this split state.
            await repo.create_workspace_lifecycle(manifest)
            with pytest.raises(AgentException):
                await repo.complete_bootstrap_apply(receipt, manifest)
            assert (await repo.get_changeset(changeset.change_id)).state is (
                ChangeSetState.APPLYING
            )
            with pytest.raises(AgentException):
                await repo.get_receipt(changeset.change_id)
        finally:
            await db.close()

    _run(scenario())


def test_changeset_recovery_finalizes_bootstrap_through_injected_factory(
    tmp_path: Path,
) -> None:
    class Bridge:
        async def receipt(self, _changeset):
            return _receipt()

        async def current_binding(self):
            return _compile_bootstrap().changeset.scene_binding

        async def preflight(self, _changeset, _workspace):  # pragma: no cover
            raise AssertionError("receipt recovery must not preflight")

        async def apply(self, _changeset, _workspace):  # pragma: no cover
            raise AssertionError("recovery must not replay Apply")

    async def scenario() -> None:
        db, repo = await _repo(tmp_path / "runtime.sqlite")
        try:
            changeset = _compile_bootstrap().changeset
            await repo.insert_changeset(
                changeset, state=ChangeSetState.APPLYING
            )
            service = ChangeSetService(
                repo,
                bridge_provider=Bridge(),
                bootstrap_manifest_factory=(
                    lambda exact_changeset, exact_receipt: (
                        derive_bootstrap_manifest(exact_changeset, exact_receipt)
                        if exact_receipt.is_success
                        else None
                    )
                ),
            )
            recovered = await service.recover_one(changeset.change_id)
            assert recovered.state is ChangeSetState.APPLIED
            assert [event.event_type for event in recovered.events] == [
                "changeset.state_changed",
                "changeset.applied",
                "workspace.created",
            ]
            active = await repo.get_active_workspace(SES)
            assert active is not None
            assert active.active_workspace_id.startswith("ws_")
        finally:
            await db.close()

    _run(scenario())
