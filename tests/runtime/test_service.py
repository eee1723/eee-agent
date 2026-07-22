"""Task 10: RuntimeService orchestration and restart recovery tests.

These tests drive the real SQLite application database, EventStore, and
CheckpointManager with a controllable fake AgentRunner. No live LLM, Houdini,
or WebSocket is involved. Each test runs its async scenario through
``asyncio.run`` and treats the service as a black box.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from eee_agent.core import (
    AgentException,
    runtime_version_report,
)
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectResult,
    WorkspaceInspectionUnavailable,
    WorkspaceNodeObservation,
)
from eee_agent.runtime.agent_runner import RunnerCompleted, RunnerEvent
from eee_agent.runtime.checkpoints import CheckpointManager
from eee_agent.runtime.models import (
    RetentionClass,
    RunRecord,
    RunStatus,
    SessionStatus,
)
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.service import (
    RuntimeService,
    SessionSnapshot,
)

# A session id whose checkpoint thread the fake runner never touches; the real
# CheckpointManager still owns checkpoints.sqlite.
USAGE = {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# fakes and fixtures
# --------------------------------------------------------------------------


class FakeRunner:
    """A controllable stand-in for AgentRunner.

    ``items`` are yielded in order; an optional ``gate`` blocks the stream
    until set (for cancellation/shield tests); an optional ``error`` is raised
    after the items are drained (for failure tests).
    """

    def __init__(self, items=(), *, error=None, gate=None):
        self._items = list(items)
        self._error = error
        self._gate = gate
        self.stream_calls: list[tuple[str, str]] = []

    async def stream(self, *, session_id: str, user_input: str):
        self.stream_calls.append((session_id, user_input))
        if self._gate is not None:
            await self._gate.wait()
        for item in self._items:
            yield item
        if self._error is not None:
            raise self._error


def _success_items(final: str = "done"):
    return [
        RunnerEvent(
            "model.text_delta",
            {"text": final},
            RetentionClass.OPERATIONAL,
        ),
        RunnerEvent(
            "model.completed",
            {"usage": dict(USAGE)},
            RetentionClass.DURABLE,
        ),
        RunnerCompleted(final_response=final, usage=dict(USAGE)),
    ]


def _factory_for(runner: FakeRunner):
    def factory(_checkpointer):
        return runner

    return factory


async def _wait_until_streaming(runner: FakeRunner) -> None:
    """Yield until the runner's stream() has been entered.

    The service calls the runner only after the run reaches Planning, so once
    stream_calls is non-empty the run is active and (with a gate) blocked
    there. This makes stop/shield scenarios deterministic instead of racing
    the task's early transitions.
    """
    while not runner.stream_calls:
        await asyncio.sleep(0)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimePaths:
    monkeypatch.setenv("EEE_RUNTIME_HOME", str(tmp_path / "home"))
    return RuntimePaths.from_environment()


# --------------------------------------------------------------------------
# 1. successful run: full persisted event order
# --------------------------------------------------------------------------


def test_successful_run_emits_full_event_order(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("Chair")
            started = await service.start_run(session.session_id, "inspect")
            completed = await service.wait_for_run(started.run_id)
            assert completed.status is RunStatus.COMPLETED
            assert completed.final_response == "done"
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert [e.event_type for e in replay.events] == [
                "session.created",
                "run.created",
                "run.state_changed",
                "run.state_changed",
                "model.text_delta",
                "model.completed",
                "run.state_changed",
                "message.assistant_final",
                "run.state_changed",
            ]
            # the three final entries are Planning -> Finalizing, durable
            # assistant final message, then Finalizing -> Completed.
            state_changes = [
                e for e in replay.events if e.event_type == "run.state_changed"
            ]
            assert state_changes[0].payload["from"] == "Created"
            assert state_changes[0].payload["to"] == "PreparingContext"
            assert state_changes[1].payload["from"] == "PreparingContext"
            assert state_changes[1].payload["to"] == "Planning"
            assert state_changes[2].payload["from"] == "Planning"
            assert state_changes[2].payload["to"] == "Finalizing"
            assert state_changes[3].payload["from"] == "Finalizing"
            assert state_changes[3].payload["to"] == "Completed"
            final_msg = [
                e for e in replay.events
                if e.event_type == "message.assistant_final"
            ]
            assert len(final_msg) == 1
            assert final_msg[0].payload["text"] == "done"

    _run(scenario())


def test_runnercompleted_is_unique_and_durable(paths: RuntimePaths) -> None:
    # The success stream contains exactly one RunnerCompleted and exactly one
    # durable model.completed; no terminal duplication leaks into replay.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("Chair")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            completed = [
                e for e in replay.events if e.event_type == "model.completed"
            ]
            finals = [
                e for e in replay.events
                if e.event_type == "message.assistant_final"
            ]
            assert len(completed) == 1
            assert len(finals) == 1
            assert completed[0].retention_class is RetentionClass.DURABLE
            assert finals[0].retention_class is RetentionClass.DURABLE

    _run(scenario())


def test_completed_placeholder_session_is_auto_titled(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(_success_items("finished"))
    title_model = FakeListChatModel(responses=["unused"])
    title_calls: list[tuple[str, str]] = []

    async def fake_generate_title(
        _model: object, user_input: str, *, final_response: str = ""
    ) -> str:
        title_calls.append((user_input, final_response))
        return "Procedural chair"

    monkeypatch.setattr(
        "eee_agent.runtime.service.generate_session_title", fake_generate_title
    )

    async def scenario() -> None:
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(runner),
            title_model_provider=lambda: title_model,
        ) as service:
            session = await service.create_session("New session")
            started = await service.start_run(session.session_id, "build a chair")
            await service.wait_for_run(started.run_id)
            pending_titles = list(service._title_tasks.values())
            if pending_titles:
                await asyncio.gather(*pending_titles)

            renamed = await service.get_session(session.session_id)
            assert renamed.title == "Procedural chair"
            assert title_calls == [("build a chair", "finished")]

    _run(scenario())


# --------------------------------------------------------------------------
# 2. start_run: freeze once, atomic acquire, one task, non-blocking, concurrency
# --------------------------------------------------------------------------


def test_version_report_frozen_once_per_start_run(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each start_run call captures its own runtime_version_report snapshot and
    # freezes it into that RunRecord. Two consecutive runs make two distinct
    # calls with two distinct frozen snapshots.
    calls: list[int] = []
    real = runtime_version_report

    def spy() -> dict[str, object]:
        # Stamp each call with a counter so the two snapshots are distinguishable.
        calls.append(len(calls))
        report = real()
        return {**report, "report_call": len(calls) - 1}

    monkeypatch.setattr("eee_agent.runtime.service.runtime_version_report", spy)
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            r1 = await service.start_run(session.session_id, "one")
            await service.wait_for_run(r1.run_id)
            r2 = await service.start_run(session.session_id, "two")
            await service.wait_for_run(r2.run_id)
            # Each run froze its own call's snapshot.
            assert r1.model_snapshot_json["report_call"] == 0
            assert r2.model_snapshot_json["report_call"] == 1
            assert r1.model_snapshot_json != r2.model_snapshot_json
        # Exactly one runtime_version_report call per start_run.
        assert len(calls) == 2

    _run(scenario())


def test_start_run_returns_without_waiting_for_runner(paths: RuntimePaths) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            # The runner is blocked on the gate; start_run must still return
            # promptly with a Created record and one active task.
            started = await service.start_run(session.session_id, "x")
            assert started.status is RunStatus.CREATED
            assert started.run_id in service._tasks  # type: ignore[attr-defined]
            assert not service._tasks[started.run_id].done()  # type: ignore[attr-defined]
            gate.set()
            completed = await service.wait_for_run(started.run_id)
            assert completed.status is RunStatus.COMPLETED

    _run(scenario())


def test_start_run_creates_exactly_one_task(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            await service.start_run(session.session_id, "x")
            assert len(service._tasks) == 1  # type: ignore[attr-defined]

    _run(scenario())


def test_concurrent_start_run_only_one_wins(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            results = await asyncio.gather(
                service.start_run(session.session_id, "a"),
                service.start_run(session.session_id, "b"),
                return_exceptions=True,
            )
            wins = [r for r in results if type(r) is RunRecord]
            fails = [r for r in results if isinstance(r, AgentException)]
            assert len(wins) == 1
            assert len(fails) == 1
            assert fails[0].error.code == "runtime.run_already_active"
            # Let the winner converge so no task is orphaned.
            await service.wait_for_run(wins[0].run_id)

    _run(scenario())


def test_start_run_archived_session_rejected(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            await service.archive_session(session.session_id)
            with pytest.raises(AgentException) as exc:
                await service.start_run(session.session_id, "x")
            assert exc.value.error.code == "runtime.session_archived"

    _run(scenario())


# --------------------------------------------------------------------------
# 3. wait_for_run: shield, waiter cancel does not cancel the run
# --------------------------------------------------------------------------


def test_wait_for_run_shields_run_from_waiter_cancellation(
    paths: RuntimePaths,
) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)
            run_task = service._tasks[started.run_id]  # type: ignore[attr-defined]

            waiter = asyncio.create_task(service.wait_for_run(started.run_id))
            await asyncio.sleep(0)  # let the waiter reach the shield
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

            # Cancelling the waiter did not cancel the actual run.
            assert not run_task.done()

            # The run still completes normally once unblocked.
            gate.set()
            completed = await service.wait_for_run(started.run_id)
            assert completed.status is RunStatus.COMPLETED

    _run(scenario())


def test_wait_for_run_returns_final_persisted_record(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            completed = await service.wait_for_run(started.run_id)
            # Same identity as a fresh repository read.
            reloaded = await service.get_run(started.run_id)
            assert completed == reloaded
            assert completed.status is RunStatus.COMPLETED

    _run(scenario())


# --------------------------------------------------------------------------
# 4. stop_run: cooperative and force, StopRequested -> Stopping -> Cancelled
# --------------------------------------------------------------------------


def test_cooperative_stop_persists_full_cancel_path(paths: RuntimePaths) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)
            stopped = await service.stop_run(started.run_id, force=False)
            # Cooperative stop persists StopRequested before returning.
            assert stopped.status is RunStatus.STOP_REQUESTED
            cancelled = await service.wait_for_run(started.run_id)
            assert cancelled.status is RunStatus.CANCELLED
            assert await service._runs.active_run_id() is None  # type: ignore[attr-defined]

            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            changes = [
                e.payload for e in replay.events
                if e.event_type == "run.state_changed"
            ]
            targets = [(p["from"], p["to"]) for p in changes]
            assert ("Planning", "StopRequested") in targets
            assert ("StopRequested", "Stopping") in targets
            assert ("Stopping", "Cancelled") in targets

    _run(scenario())


def test_force_stop_persists_full_cancel_path(paths: RuntimePaths) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)
            stopped = await service.stop_run(started.run_id, force=True)
            # force=True must also persist StopRequested first and must not
            # return the pre-stop (Planning) state.
            assert stopped.status is RunStatus.STOP_REQUESTED
            cancelled = await service.wait_for_run(started.run_id)
            assert cancelled.status is RunStatus.CANCELLED
            assert await service._runs.active_run_id() is None  # type: ignore[attr-defined]
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            targets = [
                (e.payload["from"], e.payload["to"])
                for e in replay.events
                if e.event_type == "run.state_changed"
            ]
            assert ("Planning", "StopRequested") in targets
            assert ("StopRequested", "Stopping") in targets
            assert ("Stopping", "Cancelled") in targets

    _run(scenario())


def test_stop_run_idempotent_for_terminal(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            # Already Completed: stop_run must not perform an illegal
            # transition or duplicate a terminal event.
            result = await service.stop_run(started.run_id, force=False)
            assert result.status is RunStatus.COMPLETED
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            state_changes = [
                e for e in replay.events if e.event_type == "run.state_changed"
            ]
            assert len(state_changes) == 4  # unchanged from the success path

    _run(scenario())


def test_stop_run_twose_requests_no_duplicate_terminal(paths: RuntimePaths) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)
            await service.stop_run(started.run_id, force=False)
            # A second cooperative request, regardless of whether the run has
            # already advanced, must be a legal no-op that never duplicates the
            # terminal transition.
            second = await service.stop_run(started.run_id, force=False)
            assert second.status in (
                RunStatus.STOP_REQUESTED,
                RunStatus.STOPPING,
                RunStatus.CANCELLED,
            )
            cancelled = await service.wait_for_run(started.run_id)
            assert cancelled.status is RunStatus.CANCELLED
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            cancelled_events = [
                e for e in replay.events
                if e.event_type == "run.state_changed"
                and e.payload["to"] == "Cancelled"
            ]
            assert len(cancelled_events) == 1

    _run(scenario())


# --------------------------------------------------------------------------
# 5. runner exception -> internal.runtime_failure
# --------------------------------------------------------------------------


def test_runner_exception_becomes_internal_runtime_failure(
    paths: RuntimePaths,
) -> None:
    secret = "super-secret-token-12345"
    runner = FakeRunner((), error=ValueError(secret))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            failed = await service.wait_for_run(started.run_id)
            assert failed.status is RunStatus.FAILED
            assert failed.failure_json is not None
            assert failed.failure_json["code"] == "internal.runtime_failure"
            # Raw exception text never reaches the run record.
            assert secret not in str(failed.to_dict())

            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            types = [e.event_type for e in replay.events]
            assert "model.failed" in types
            assert "run.state_changed" in types
            assert "run.failed" in types
            # No event payload carries the raw exception text.
            for event in replay.events:
                assert secret not in str(event.payload)
                assert secret not in str(event.to_dict())

    _run(scenario())


def test_runner_failure_state_changed_records_from_state(
    paths: RuntimePaths,
) -> None:
    # The runner fails while in Planning, so the persisted state change records
    # Planning -> Failed (not a spurious from-state).
    runner = FakeRunner((), error=RuntimeError("boom"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            to_failed = [
                e for e in replay.events
                if e.event_type == "run.state_changed"
                and e.payload["to"] == "Failed"
            ]
            assert len(to_failed) == 1
            assert to_failed[0].payload["from"] == "Planning"

    _run(scenario())


# --------------------------------------------------------------------------
# 6. subscriptions: committed-only, sync+async, isolation, idempotent unsub
# --------------------------------------------------------------------------


def test_callback_receives_only_committed_events(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))
    received: list[str] = []

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:

            def cb(event):
                received.append(event.event_type)

            service.subscribe(cb)
            session = await service.create_session("A")
            # Every callback delivery corresponds to a committed, replayable
            # event.
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert received[:1] == ["session.created"]
            assert received[:1] == [e.event_type for e in replay.events[:1]]
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            full = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert received == [e.event_type for e in full.events]

    _run(scenario())


def test_sync_and_async_callbacks_both_supported(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            sync_seen: list[str] = []
            async_seen: list[str] = []

            def sync_cb(event):
                sync_seen.append(event.event_type)

            async def async_cb(event):
                async_seen.append(event.event_type)

            service.subscribe(sync_cb)
            service.subscribe(async_cb)
            await service.create_session("A")
            assert sync_seen == ["session.created"]
            assert async_seen == ["session.created"]

    _run(scenario())


def test_one_callback_failure_does_not_block_others_or_rollback(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            survivor: list[str] = []

            def bad_cb(event):
                raise RuntimeError("callback boom")

            def good_cb(event):
                survivor.append(event.event_type)

            service.subscribe(bad_cb)
            service.subscribe(good_cb)
            session = await service.create_session("A")
            # The bad callback did not block the good one.
            assert survivor == ["session.created"]
            # And the event was still committed despite the callback failure.
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert [e.event_type for e in replay.events] == ["session.created"]

    _run(scenario())


def test_unsubscribe_is_idempotent(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            seen: list[str] = []

            def cb(event):
                seen.append(event.event_type)

            unsub = service.subscribe(cb)
            session = await service.create_session("A")
            assert seen == ["session.created"]
            unsub()
            unsub()  # idempotent
            unsub()  # idempotent
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            # No further callbacks after unsubscribe.
            assert seen == ["session.created"]

    _run(scenario())


# --------------------------------------------------------------------------
# 7. startup reconciliation: interrupted -> Failed, idempotent reopen
# --------------------------------------------------------------------------


def _seed_interrupted_run(db_path: Path, *, status: str = "Planning") -> str:
    """Open the DB directly, seed an orphan non-terminal run + active slot."""

    async def seed() -> str:
        from eee_agent.runtime.database import RuntimeDatabase
        from eee_agent.runtime.sessions import SessionRepository

        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = await RuntimeDatabase.open(db_path)
        try:
            sessions = SessionRepository(db)
            session = await sessions.create("Orphan")
            run_id = f"run_{'a' * 32}"
            async with db.write_transaction() as conn:
                await conn.execute(
                    "INSERT INTO runs(run_id, session_id, status, user_input, "
                    "final_response, created_at, started_at, finished_at, "
                    "failure_json, model_snapshot_json) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, NULL, ?)",
                    (
                        run_id,
                        session.session_id,
                        status,
                        "in",
                        "2026-07-14T02:30:00+00:00",
                        "2026-07-14T03:00:00+00:00",
                        '{"m":1}',
                    ),
                )
                await conn.execute(
                    "UPDATE runtime_state SET active_run_id = ?, "
                    "updated_at = ? WHERE singleton_id = 1",
                    (run_id, "2026-07-14T02:30:00+00:00"),
                )
            return session.session_id
        finally:
            await db.close()

    return _run(seed())


def test_reconcile_emits_interrupted_events_and_preserves_from_state(
    paths: RuntimePaths,
) -> None:
    session_id = _seed_interrupted_run(paths.app_db, status="Planning")

    async def scenario() -> None:
        runner = FakeRunner(_success_items("done"))
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            replay = await service.replay(
                session_id, after_seq=0, limit=100
            )
            types = [e.event_type for e in replay.events]
            # Reconciliation appends run.state_changed and run.failed for the
            # orphaned run. (The seeded session was created through the
            # repository, so it has no session.created event.)
            assert "run.state_changed" in types
            assert "run.failed" in types
            to_failed = [
                e for e in replay.events
                if e.event_type == "run.state_changed"
                and e.payload["to"] == "Failed"
            ]
            assert len(to_failed) == 1
            # The from-state is the genuine pre-recovery state, not Failed.
            assert to_failed[0].payload["from"] == "Planning"
            run_failed = [
                e for e in replay.events if e.event_type == "run.failed"
            ]
            assert run_failed[0].payload["error"]["code"] == "runtime.interrupted"
            # Active slot cleared.
            assert await service._runs.active_run_id() is None  # type: ignore[attr-defined]

    _run(scenario())


def test_reconcile_is_idempotent_on_reopen(paths: RuntimePaths) -> None:
    session_id = _seed_interrupted_run(paths.app_db, status="Planning")

    async def scenario() -> None:
        runner = FakeRunner(_success_items("done"))
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            first = await service.replay(session_id, after_seq=0, limit=100)
            first_failed = [
                e for e in first.events if e.event_type == "run.failed"
            ]
            assert len(first_failed) == 1
            first_seq = first.events[-1].seq
        # Reopen: reconciliation finds nothing new to do.
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(FakeRunner(_success_items()))
        ) as service2:
            second = await service2.replay(session_id, after_seq=0, limit=100)
            second_failed = [
                e for e in second.events if e.event_type == "run.failed"
            ]
            assert len(second_failed) == 1  # not duplicated
            assert second.events[-1].seq == first_seq  # no new appends

    _run(scenario())


# --------------------------------------------------------------------------
# 8. session service interface: CRUD, archived rejection, replay, snapshot
# --------------------------------------------------------------------------


def test_session_crud_roundtrip(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            created = await service.create_session("Chair")
            assert created.status is SessionStatus.ACTIVE
            fetched = await service.get_session(created.session_id)
            assert fetched == created
            listed = await service.list_sessions()
            assert [s.session_id for s in listed] == [created.session_id]
            renamed = await service.rename_session(
                created.session_id, "Stool"
            )
            assert renamed.title == "Stool"
            assert (await service.get_session(created.session_id)).title == "Stool"
            archived = await service.archive_session(created.session_id)
            assert archived.status is SessionStatus.ARCHIVED
            archived_list = await service.list_sessions()
            assert archived_list == []
            all_list = await service.list_sessions(include_archived=True)
            assert [s.session_id for s in all_list] == [created.session_id]

    _run(scenario())


def test_snapshot_contains_version_report_and_consistent_seq(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            snap: SessionSnapshot = await service.snapshot(session.session_id)
            assert snap.snapshot_seq == (
                await service._sessions.get(session.session_id)  # type: ignore[attr-defined]
            ).last_seq
            assert snap.version_report["eee_agent"] is not None
            assert snap.version_report["dependencies"]["aiosqlite"] == "0.22.1"
            # The completed run is present.
            assert any(r.run_id == started.run_id for r in snap.runs)
            assert snap.active_run is None

    _run(scenario())


def test_replay_returns_ascending_committed_events(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            seqs = [e.seq for e in replay.events]
            assert seqs == sorted(seqs)
            assert seqs[0] == 1
            assert replay.last_seq == seqs[-1]

    _run(scenario())


# --------------------------------------------------------------------------
# 9. delete: app delete commits first, checkpoint cleanup, structured error
# --------------------------------------------------------------------------


def test_delete_session_removes_app_records_and_checkpoint_thread(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))
    deleted_threads: list[str] = []

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)

            original_delete = CheckpointManager.delete_thread

            async def spy_delete(manager_self, session_id):
                deleted_threads.append(session_id)
                await original_delete(manager_self, session_id)

            CheckpointManager.delete_thread = spy_delete  # type: ignore[assignment]
            try:
                await service.delete_session(session.session_id)
            finally:
                CheckpointManager.delete_thread = original_delete  # type: ignore[assignment]

            # App records are gone.
            with pytest.raises(AgentException) as exc:
                await service.get_session(session.session_id)
            assert exc.value.error.code == "runtime.session_not_found"
            # Checkpoint thread deletion was attempted.
            assert deleted_threads == [session.session_id]

    _run(scenario())


def test_delete_checkpoint_cleanup_failure_is_structured_and_keeps_app_deleted(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)

            async def boom_delete(self, session_id):
                raise RuntimeError("cleanup explosion secret")

            monkeypatch.setattr(CheckpointManager, "delete_thread", boom_delete)
            with pytest.raises(AgentException) as exc:
                await service.delete_session(session.session_id)
            error = exc.value.error
            assert error.code == "runtime.checkpoint_cleanup_failed"
            assert error.scene_may_have_changed is False
            # The raw cleanup exception text is never exposed.
            assert "cleanup explosion secret" not in str(error.to_dict())
            # The application session was already deleted and is NOT restored.
            with pytest.raises(AgentException) as fetch_exc:
                await service.get_session(session.session_id)
            assert fetch_exc.value.error.code == "runtime.session_not_found"

    _run(scenario())


# --------------------------------------------------------------------------
# 10. service lifecycle: open order, close convergence, init-failure cleanup
# --------------------------------------------------------------------------


def test_open_and_close_clean_with_active_run(paths: RuntimePaths) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            # Close while the run is still active (gate never set).
            stored_run_id = started.run_id
            stored_session_id = session.session_id
        # After close, the active run converged to Cancelled.
        from eee_agent.runtime.database import RuntimeDatabase
        from eee_agent.runtime.runs import RunRepository

        db = await RuntimeDatabase.open(paths.app_db)
        try:
            runs = RunRepository(db)
            reloaded = await runs.get(stored_run_id)
            assert reloaded.status is RunStatus.CANCELLED
            assert await runs.active_run_id() is None
        finally:
            await db.close()
        _ = stored_session_id

    _run(scenario())


def test_init_failure_cleans_up_database_and_checkpoints(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The runner factory raises after the DB and checkpoints are open. The
    # service must close both during cleanup rather than leaking them.
    close_calls: list[str] = []

    original_db_close = __import__(
        "eee_agent.runtime.database", fromlist=["RuntimeDatabase"]
    ).RuntimeDatabase.close

    async def spy_db_close(self):
        close_calls.append("db")
        await original_db_close(self)

    monkeypatch.setattr(
        "eee_agent.runtime.database.RuntimeDatabase.close", spy_db_close
    )

    checkpoint_exit_calls: list[str] = []
    original_checkpoint_exit = CheckpointManager.__aexit__

    async def spy_checkpoint_exit(manager_self, exc_type, exc, tb):
        checkpoint_exit_calls.append("exit")
        # Delegate to the real __aexit__ captured before patching (calling the
        # class attribute now would recurse into this spy).
        return await original_checkpoint_exit(manager_self, exc_type, exc, tb)

    monkeypatch.setattr(CheckpointManager, "__aexit__", spy_checkpoint_exit)

    def failing_factory(_checkpointer):
        raise RuntimeError("factory boom")

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="factory boom"):
            async with RuntimeService.open(
                paths, runner_factory=failing_factory
            ):
                pass

    _run(scenario())
    assert close_calls == ["db"]
    assert checkpoint_exit_calls == ["exit"]


# --------------------------------------------------------------------------
# hardening regressions (Codex review)
# --------------------------------------------------------------------------


def test_async_callback_cancellederror_isolated(paths: RuntimePaths) -> None:
    # An async subscriber that raises asyncio.CancelledError must not cancel
    # the Runtime operation, must not roll back the committed event, must not
    # block other subscribers, and must not leave start_run without a task.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            survivor: list[str] = []

            async def canceller(event):
                raise asyncio.CancelledError()

            async def recorder(event):
                survivor.append(event.event_type)

            service.subscribe(canceller)
            service.subscribe(recorder)
            session = await service.create_session("A")
            assert "session.created" in survivor
            started = await service.start_run(session.session_id, "x")
            # start_run completed despite the canceller callback: exactly one
            # task exists, no orphan Created active run.
            assert len(service._tasks) == 1  # type: ignore[attr-defined]
            assert started.run_id in service._tasks  # type: ignore[attr-defined]
            await service.wait_for_run(started.run_id)
            assert await service._runs.active_run_id() is None  # type: ignore[attr-defined]
            assert "run.created" in survivor

    _run(scenario())


def test_state_lock_serializes_transition_and_append_not_notify(
    paths: RuntimePaths,
) -> None:
    # The service-level state lock must be held across the transition write and
    # the durable state-event append (so the recorded from-state cannot go
    # stale) and must be released before user callbacks run.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            lock = service._state_lock  # type: ignore[attr-defined]
            held: dict[str, object] = {}
            original_transition = service._runs.transition
            original_append = service._events.append

            async def spy_transition(*args, **kwargs):
                if "transition" not in held:
                    held["transition"] = lock.locked()
                return await original_transition(*args, **kwargs)

            async def spy_append(**kwargs):
                record = await original_append(**kwargs)
                if (
                    kwargs.get("event_type") == "run.state_changed"
                    and "append" not in held
                ):
                    held["append"] = lock.locked()
                return record

            def sync_cb(event):
                if "notify" not in held:
                    held["notify"] = lock.locked()

            service._runs.transition = spy_transition  # type: ignore[assignment]
            service._events.append = spy_append  # type: ignore[assignment]
            service.subscribe(sync_cb)
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            assert held["transition"] is True
            assert held["append"] is True
            assert held["notify"] is False

    _run(scenario())


def test_state_changed_events_form_consistent_chain_on_cooperative_stop(
    paths: RuntimePaths,
) -> None:
    # Deterministic from-state invariant: consecutive run.state_changed events
    # on one run form a consistent chain (from[i] == to[i-1]) and every edge is
    # legal. (A barrier-forced concurrent race is structurally impossible once
    # the state lock serializes transitions, so the serialization mechanism test
    # above is the deterministic proof; this guards the resulting invariant.)
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)
            await service.stop_run(started.run_id, force=False)
            await service.wait_for_run(started.run_id)
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            edges = [
                (e.payload["from"], e.payload["to"])
                for e in replay.events
                if e.event_type == "run.state_changed"
            ]
            assert ("Planning", "StopRequested") in edges
            for prev, cur in zip(edges, edges[1:]):
                assert prev[1] == cur[0], f"inconsistent chain: {prev} -> {cur}"

    _run(scenario())


def test_normal_close_propagates_checkpoint_cleanup_error(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def raising_exit(manager_self, exc_type, exc, tb):
        raise RuntimeError("checkpoint cleanup boom")

    monkeypatch.setattr(CheckpointManager, "__aexit__", raising_exit)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="checkpoint cleanup boom"):
            async with RuntimeService.open(
                paths, runner_factory=_factory_for(runner)
            ) as service:
                await service.create_session("A")

    _run(scenario())


def test_business_exception_takes_priority_over_checkpoint_cleanup(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def raising_exit(manager_self, exc_type, exc, tb):
        raise RuntimeError("checkpoint cleanup boom")

    monkeypatch.setattr(CheckpointManager, "__aexit__", raising_exit)

    async def scenario() -> None:
        with pytest.raises(ValueError, match="business boom"):
            async with RuntimeService.open(
                paths, runner_factory=_factory_for(runner)
            ):
                raise ValueError("business boom")

    _run(scenario())


def test_replay_reports_snapshot_required_after_retention_gap(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await service.wait_for_run(started.run_id)
            # Prune the run's operational events (e.g. model.text_delta) for the
            # now-terminal run, advancing the replay floor past seq 0.
            future = datetime(2099, 1, 1, tzinfo=timezone.utc)
            await service._events.prune_operational(  # type: ignore[attr-defined]
                session.session_id,
                through_seq=10_000,
                older_than=future,
            )
            # after_seq=0 now falls below the replay floor -> snapshot required.
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert replay.snapshot_required is True
            assert replay.replay_floor_seq > 0
            # snapshot_seq is consistent with the replay boundary (both equal
            # the session's current last_seq).
            snap = await service.snapshot(session.session_id)
            assert snap.snapshot_seq == replay.last_seq

    _run(scenario())


def test_failure_and_stop_concurrency_reaches_terminal(
    paths: RuntimePaths,
) -> None:
    # Defect 2: the runner fails while a subscriber callback blocks on
    # model.failed, and stop_run runs concurrently. The run must end in a legal
    # terminal state (Failed or Cancelled), the active slot cleared, no orphan
    # task, and the state-event chain continuous.
    runner = FakeRunner((), error=RuntimeError("boom"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            model_failed_entered = asyncio.Event()
            model_failed_gate = asyncio.Event()

            async def blocking_cb(event):
                if event.event_type == "model.failed":
                    model_failed_entered.set()
                    await model_failed_gate.wait()

            service.subscribe(blocking_cb)
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            # Failure won the lock: model.failed is being notified (blocked).
            await model_failed_entered.wait()
            await service.stop_run(started.run_id, force=False)
            model_failed_gate.set()
            final = await service.wait_for_run(started.run_id)
            assert final.status in (RunStatus.FAILED, RunStatus.CANCELLED)
            assert await service._runs.active_run_id() is None  # type: ignore[attr-defined]
            assert len(service._tasks) == 0  # type: ignore[attr-defined]
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            edges = [
                (e.payload["from"], e.payload["to"])
                for e in replay.events
                if e.event_type == "run.state_changed"
            ]
            for prev, cur in zip(edges, edges[1:]):
                assert prev[1] == cur[0], f"chain break: {prev} -> {cur}"

    _run(scenario())


def test_handle_failure_yields_to_stop_owns_terminal_no_model_failed(
    paths: RuntimePaths,
) -> None:
    # If a stop owns the terminal state (run is StopRequested/Stopping when the
    # failure handler runs), the failure handler must NOT append model.failed or
    # run.failed; the stop/cancellation path owns the terminal state.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)  # run at Planning, blocked
            # Move the run into a stop-owned state without cancelling the task,
            # then invoke the failure handler directly.
            await service._runs.transition(  # type: ignore[attr-defined]
                started.run_id, RunStatus.STOP_REQUESTED
            )
            await service._handle_failure(  # type: ignore[attr-defined]
                session.session_id, started.run_id
            )
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            types = [e.event_type for e in replay.events]
            assert "model.failed" not in types
            assert "run.failed" not in types
            run = await service._runs.get(started.run_id)  # type: ignore[attr-defined]
            assert run.status is RunStatus.STOP_REQUESTED
            # Cleanup: converge the blocked run.
            await service.stop_run(started.run_id, force=False)
            await service.wait_for_run(started.run_id)

    _run(scenario())


def test_start_run_cancelled_during_append_preserves_run_created(
    paths: RuntimePaths,
) -> None:
    # Defect A: cancelling start_run during the run.created EventStore append
    # (single and double cancel) must still leave exactly one run.created and it
    # must be the run's first event. The Run was accepted, so it must complete;
    # it must never be persisted without run.created.
    async def one_case(cancel_count: int) -> None:
        runner = FakeRunner(_success_items("done"))
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            append_entered = asyncio.Event()
            append_gate = asyncio.Event()
            seen_run_id: list[str] = []
            original_append = service._events.append  # type: ignore[attr-defined]

            async def gated_append(**kwargs):
                if kwargs.get("event_type") == "run.created":
                    seen_run_id.append(kwargs["run_id"])
                    append_entered.set()
                    await append_gate.wait()
                return await original_append(**kwargs)

            service._events.append = gated_append  # type: ignore[assignment]
            session = await service.create_session("A")
            start_task = asyncio.create_task(
                service.start_run(session.session_id, "x")
            )
            await append_entered.wait()  # run.created append blocked pre-commit
            for _ in range(cancel_count):
                start_task.cancel()
            append_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await start_task
            # The Run was accepted and must reach a terminal state.
            final = await service.wait_for_run(seen_run_id[0])
            assert final.status in (
                RunStatus.COMPLETED,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
            )
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            run_types = [
                e.event_type
                for e in replay.events
                if e.run_id == seen_run_id[0]
            ]
            assert run_types.count("run.created") == 1
            assert run_types[0] == "run.created"

    async def scenario() -> None:
        await one_case(1)
        await one_case(2)

    _run(scenario())


def test_start_run_cancelled_during_notify_does_not_cancel_run(
    paths: RuntimePaths,
) -> None:
    # Defect B: a subscriber callback blocks during the run.created notify;
    # cancelling start_run must propagate to the caller but must NOT cancel the
    # accepted Run (disconnect never cancels a Run). The Run must have exactly
    # one execution task and continue to Completed once the runner is released.
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            cb_entered = asyncio.Event()
            notify_gate = asyncio.Event()

            async def blocking_cb(event):
                if event.event_type == "run.created":
                    cb_entered.set()
                    await notify_gate.wait()

            service.subscribe(blocking_cb)
            session = await service.create_session("A")
            start_task = asyncio.create_task(
                service.start_run(session.session_id, "x")
            )
            await cb_entered.wait()  # run.created committed; notify blocked
            start_task.cancel()
            notify_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await start_task
            # Exactly one execution task exists; the Run was not cancelled.
            assert len(service._tasks) == 1  # type: ignore[attr-defined]
            run_id = next(iter(service._tasks))  # type: ignore[attr-defined]
            gate.set()
            completed = await service.wait_for_run(run_id)
            assert completed.status is RunStatus.COMPLETED
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert [e.event_type for e in replay.events].count("run.created") == 1

    _run(scenario())


def test_double_cancel_during_convergence_keeps_no_orphan(
    paths: RuntimePaths,
) -> None:
    # Defect C: two consecutive cancels during terminal convergence must not
    # interrupt it. The run must end terminal with the active slot cleared and
    # no orphan task, within this process (not relying on restart reconcile).
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)  # run at Planning, blocked
            run_task = service._tasks[started.run_id]  # type: ignore[attr-defined]

            converge_entered = asyncio.Event()
            allow_converge = asyncio.Event()
            original_cancel = service._handle_cancellation  # type: ignore[attr-defined]

            async def pausing_cancel(sid, rid):
                converge_entered.set()
                await allow_converge.wait()
                await original_cancel(sid, rid)

            service._handle_cancellation = pausing_cancel  # type: ignore[assignment]
            await service.stop_run(started.run_id, force=False)
            await converge_entered.wait()
            run_task.cancel()
            run_task.cancel()  # double cancel mid-convergence
            allow_converge.set()
            final = await service.wait_for_run(started.run_id)
            assert final.status in (
                RunStatus.CANCELLED,
                RunStatus.FAILED,
            )
            assert await service._runs.active_run_id() is None  # type: ignore[attr-defined]
            assert len(service._tasks) == 0  # type: ignore[attr-defined]

    _run(scenario())


def test_shutdown_converges_active_run_without_inmemory_task(
    paths: RuntimePaths,
) -> None:
    # Defect D: an active run persisted in active_run_id but absent from _tasks
    # (a pre-registration orphan) must be converged to terminal on shutdown by
    # reading active_run_id, not by scanning only _tasks.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("A")
            # Persist an active run directly, with no execution task registered.
            run = await service._runs.create_and_acquire(  # type: ignore[attr-defined]
                session.session_id, "x", {"m": 1}
            )
            assert run.run_id not in service._tasks  # type: ignore[attr-defined]
            assert await service._runs.active_run_id() == run.run_id  # type: ignore[attr-defined]
            stored = run.run_id
        # After close, the orphan must be terminal with the slot cleared.
        from eee_agent.runtime.database import RuntimeDatabase
        from eee_agent.runtime.runs import RunRepository

        db = await RuntimeDatabase.open(paths.app_db)
        try:
            runs = RunRepository(db)
            reloaded = await runs.get(stored)
            assert reloaded.status in (
                RunStatus.CANCELLED,
                RunStatus.FAILED,
            )
            assert await runs.active_run_id() is None
        finally:
            await db.close()

    _run(scenario())


def test_failure_bundle_atomic_under_cancel(paths: RuntimePaths) -> None:
    # Defect E: cancelling the run task after fail() committed but before all
    # failure events are appended must leave either a complete Failed bundle
    # (model.failed + run.state_changed to Failed + run.failed) or a complete
    # Cancelled convergence with no failure events. No partial bundle.
    runner = FakeRunner((), error=RuntimeError("boom"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            fail_committed = asyncio.Event()
            allow_events = asyncio.Event()
            original_fail = service._runs.fail  # type: ignore[attr-defined]

            async def pausing_fail(run_id, error):
                result = await original_fail(run_id, error)
                fail_committed.set()
                await allow_events.wait()
                return result

            service._runs.fail = pausing_fail  # type: ignore[assignment]
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await fail_committed.wait()  # fail() committed; events paused
            service._tasks[started.run_id].cancel()  # type: ignore[attr-defined]
            allow_events.set()
            final = await service.wait_for_run(started.run_id)
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            types = [e.event_type for e in replay.events]
            if final.status is RunStatus.FAILED:
                assert "model.failed" in types
                assert "run.state_changed" in types
                assert "run.failed" in types
            else:
                assert final.status is RunStatus.CANCELLED
                assert "model.failed" not in types
                assert "run.failed" not in types

    _run(scenario())


def test_first_state_changed_event_preserved_under_cancel(
    paths: RuntimePaths,
) -> None:
    # Defect 4a: cancelling the Agent execution task after the
    # Created -> PreparingContext repository transition committed but before the
    # matching run.state_changed event appended must not lose that event. The run
    # must still reach Cancelled with a complete, continuous state chain.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            state_entered = asyncio.Event()
            state_gate = asyncio.Event()
            seen: list[str] = []
            original_append = service._events.append  # type: ignore[attr-defined]

            async def gated_append(**kwargs):
                if (
                    kwargs.get("event_type") == "run.state_changed"
                    and not seen
                ):
                    seen.append(kwargs["run_id"])
                    state_entered.set()
                    await state_gate.wait()
                return await original_append(**kwargs)

            service._events.append = gated_append  # type: ignore[assignment]
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await state_entered.wait()  # first state_changed append blocked
            service._tasks[started.run_id].cancel()  # type: ignore[attr-defined]
            state_gate.set()
            final = await service.wait_for_run(started.run_id)
            assert final.status is RunStatus.CANCELLED
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            edges = [
                (e.payload["from"], e.payload["to"])
                for e in replay.events
                if e.event_type == "run.state_changed"
            ]
            assert edges[0] == ("Created", "PreparingContext")
            for prev, cur in zip(edges, edges[1:]):
                assert prev[1] == cur[0], f"chain break: {prev} -> {cur}"
            assert edges[-1][1] == "Cancelled"

    _run(scenario())


def test_stop_run_stoprequested_event_preserved_under_cancel(
    paths: RuntimePaths,
) -> None:
    # Defect 4b: stop_run blocks after the StopRequested transition committed
    # but before the state event appended; double-cancelling stop_run's caller
    # must propagate, but the Agent task must be cancelled and converge to
    # Cancelled, with exactly one entry into StopRequested.
    gate = asyncio.Event()
    runner = FakeRunner(_success_items("done"), gate=gate)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            stop_entered = asyncio.Event()
            stop_gate = asyncio.Event()
            original_append = service._events.append  # type: ignore[attr-defined]

            async def gated_append(**kwargs):
                if (
                    kwargs.get("event_type") == "run.state_changed"
                    and kwargs.get("payload", {}).get("to") == "StopRequested"
                ):
                    stop_entered.set()
                    await stop_gate.wait()
                return await original_append(**kwargs)

            service._events.append = gated_append  # type: ignore[assignment]
            session = await service.create_session("A")
            started = await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)  # run at Planning, blocked
            stop_task = asyncio.create_task(
                service.stop_run(started.run_id, force=False)
            )
            await stop_entered.wait()  # StopRequested committed, event blocked
            stop_task.cancel()
            stop_task.cancel()  # double cancel stop_run caller
            stop_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await stop_task
            gate.set()  # release the runner so the Agent task can converge
            final = await service.wait_for_run(started.run_id)
            assert final.status is RunStatus.CANCELLED
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            stop_entries = [
                e
                for e in replay.events
                if e.event_type == "run.state_changed"
                and e.payload["to"] == "StopRequested"
            ]
            assert len(stop_entries) == 1

    _run(scenario())


@pytest.mark.parametrize(
    "event_type", ["session.created", "session.renamed", "session.archived"]
)
def test_session_durable_event_preserved_under_cancel(
    paths: RuntimePaths, event_type: str
) -> None:
    # Defect 4c: cancelling the caller after the session mutation committed but
    # before the durable event appended must not lose the event. When the
    # mutation is persisted, the event must be present exactly once.
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            append_entered = asyncio.Event()
            append_gate = asyncio.Event()
            seen_session_id: list[str] = []
            original_append = service._events.append  # type: ignore[attr-defined]

            async def gated_append(**kwargs):
                if kwargs.get("event_type") == event_type:
                    seen_session_id.append(kwargs["session_id"])
                    append_entered.set()
                    await append_gate.wait()
                return await original_append(**kwargs)

            service._events.append = gated_append  # type: ignore[assignment]
            if event_type == "session.created":
                op_task = asyncio.create_task(service.create_session("A"))
            else:
                setup = await service.create_session("Setup")
                if event_type == "session.renamed":
                    op_task = asyncio.create_task(
                        service.rename_session(setup.session_id, "Renamed")
                    )
                else:
                    op_task = asyncio.create_task(
                        service.archive_session(setup.session_id)
                    )
            await append_entered.wait()
            op_task.cancel()
            append_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await op_task
            replay = await service.replay(
                seen_session_id[0], after_seq=0, limit=100
            )
            matches = [
                e for e in replay.events if e.event_type == event_type
            ]
            assert len(matches) == 1

    _run(scenario())


def test_service_does_not_import_websocket_or_lock() -> None:
    import ast

    import eee_agent.runtime.service as service_mod

    with open(service_mod.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_names.add(node.module.split(".")[0])
            imported_names.update(alias.name for alias in node.names)
    # No transport/identity-layer imports leak into the service.
    assert "websockets" not in imported_names
    assert "RuntimeLock" not in imported_names
    assert "lock" not in imported_names
    assert "protocol" not in imported_names
    assert "auth" not in imported_names


# --------------------------------------------------------------------------
# configured graceful shutdown timeout
# --------------------------------------------------------------------------


def test_open_stores_configured_graceful_timeout(paths: RuntimePaths) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner), graceful_timeout=7.5
        ) as service:
            assert service.graceful_timeout == 7.5

    _run(scenario())


def test_open_defaults_graceful_timeout_to_ten(paths: RuntimePaths) -> None:
    from eee_agent.runtime.service import _GRACEFUL_TIMEOUT_SECONDS

    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            # Existing callers that omit graceful_timeout keep the 10s default
            # (Tasks 1-12 compatibility).
            assert service.graceful_timeout == _GRACEFUL_TIMEOUT_SECONDS == 10.0

    _run(scenario())


def test_shutdown_uses_instance_graceful_timeout(paths: RuntimePaths) -> None:
    # _shutdown must pass the service's graceful_timeout to the active-task
    # convergence wait, not the module default. A blocking runner keeps the run
    # active so the shutdown wait_for is exercised; a spy records its timeout.
    import eee_agent.runtime.service as service_mod

    runner = FakeRunner(_success_items("done"), gate=asyncio.Event())  # never set
    captured: list = []
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(awaitable, timeout=None):
        captured.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner), graceful_timeout=3.5
        ) as service:
            session = await service.create_session("A")
            await service.start_run(session.session_id, "x")
            await _wait_until_streaming(runner)  # run active, blocked on the gate
        # context exit -> _shutdown cancels + convergence wait_for(timeout=...)

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(service_mod.asyncio, "wait_for", spy_wait_for)
        _run(scenario())

    assert 3.5 in captured


# --------------------------------------------------------------------------
# trusted Workspace lifecycle wiring
# --------------------------------------------------------------------------


class FakeWorkspaceProvider:
    def __init__(self) -> None:
        self.selection: WorkspaceInspectResult | BaseException | None = None
        self.manifest: WorkspaceInspectResult | BaseException | None = None
        self.selection_epochs: list[int | None] = []
        self.manifest_calls: list[tuple[object, int | None]] = []

    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        self.selection_epochs.append(expected_scene_epoch)
        value = self.selection
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise AssertionError("selection result was not configured")
        return value

    async def inspect_manifest(
        self, manifest, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        self.manifest_calls.append((manifest, expected_scene_epoch))
        value = self.manifest
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise AssertionError("manifest result was not configured")
        return value


def _workspace_result(
    *,
    workspace_id: str,
    run_id: str,
    mode: str = "selection",
    path: str = "/obj/ws",
    epoch: int = 7,
) -> WorkspaceInspectResult:
    return WorkspaceInspectResult.build(
        binding=SceneBinding(
            instance_id="hou_instance_1",
            scene_epoch=epoch,
            hip_path=None,
            observed_revision=f"scene-{epoch}",
        ),
        mode=mode,
        observations=(
            WorkspaceNodeObservation(
                path=path,
                node_type="geo",
                parent_path="/obj",
                is_locked=False,
                workspace_id=workspace_id,
                node_id="n_root",
                capability="modeling",
                role="root",
                schema_version=1,
                created_by_run=run_id,
            ),
        ),
    )


def test_workspace_create_notifies_only_committed_event_and_noop_notifies_none(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))
    provider = FakeWorkspaceProvider()
    workspace_id = f"ws_{'a' * 32}"

    async def scenario() -> None:
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(runner),
            workspace_fact_provider=provider,
        ) as service:
            session = await service.create_session("Workspace")
            run = await service.start_run(session.session_id, "inspect")
            await service.wait_for_run(run.run_id)
            provider.selection = _workspace_result(
                workspace_id=workspace_id, run_id=run.run_id
            )
            seen = []
            service.subscribe(seen.append)

            first = await service.create_workspace(
                session.session_id, expected_scene_epoch=7
            )
            assert first.changed is True
            assert first.workspace.workspace_id == workspace_id
            assert provider.selection_epochs == [7]
            assert [event.event_type for event in seen] == ["workspace.created"]
            committed = seen[0]
            replay = await service.replay(
                session.session_id, after_seq=committed.seq - 1, limit=10
            )
            assert replay.events == (committed,)

            seen.clear()
            second = await service.create_workspace(
                session.session_id, expected_scene_epoch=7
            )
            assert second.changed is False
            assert seen == []

    _run(scenario())


def test_workspace_callback_failure_isolated_after_successful_commit(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))
    provider = FakeWorkspaceProvider()
    workspace_id = f"ws_{'b' * 32}"

    async def scenario() -> None:
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(runner),
            workspace_fact_provider=provider,
        ) as service:
            session = await service.create_session("Workspace")
            run = await service.start_run(session.session_id, "inspect")
            await service.wait_for_run(run.run_id)
            provider.selection = _workspace_result(
                workspace_id=workspace_id, run_id=run.run_id
            )
            good_seen = []

            def bad_callback(_record):
                raise RuntimeError("callback boom")

            service.subscribe(bad_callback)
            service.subscribe(good_seen.append)
            summary = await service.create_workspace(
                session.session_id, expected_scene_epoch=None
            )
            assert summary.changed is True
            assert [event.event_type for event in good_seen] == [
                "workspace.created"
            ]
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert "workspace.created" in [
                event.event_type for event in replay.events
            ]

    _run(scenario())


def test_workspace_provider_failure_emits_no_event_or_half_state(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))
    provider = FakeWorkspaceProvider()

    async def scenario() -> None:
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(runner),
            workspace_fact_provider=provider,
        ) as service:
            session = await service.create_session("Workspace")
            provider.selection = WorkspaceInspectionUnavailable("offline")
            seen = []
            service.subscribe(seen.append)
            with pytest.raises(AgentException) as exc:
                await service.create_workspace(
                    session.session_id, expected_scene_epoch=None
                )
            assert exc.value.error.code == "bridge.capability_unavailable"
            assert seen == []
            replay = await service.replay(
                session.session_id, after_seq=0, limit=100
            )
            assert not any(
                event.event_type.startswith("workspace.")
                for event in replay.events
            )

    _run(scenario())


def test_missing_workspace_provider_fails_closed_for_mutation(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))

    async def scenario() -> None:
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(runner)
        ) as service:
            session = await service.create_session("Workspace")
            with pytest.raises(AgentException) as exc:
                await service.create_workspace(
                    session.session_id, expected_scene_epoch=None
                )
            assert exc.value.error.code == "bridge.capability_unavailable"

    _run(scenario())


def test_workspace_bind_switch_and_inspect_delegate_without_duplicate_events(
    paths: RuntimePaths,
) -> None:
    runner = FakeRunner(_success_items("done"))
    provider = FakeWorkspaceProvider()
    workspace_id = f"ws_{'c' * 32}"

    async def scenario() -> None:
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(runner),
            workspace_fact_provider=provider,
        ) as service:
            session = await service.create_session("Workspace")
            run = await service.start_run(session.session_id, "inspect")
            await service.wait_for_run(run.run_id)
            provider.selection = _workspace_result(
                workspace_id=workspace_id, run_id=run.run_id
            )
            created = await service.create_workspace(
                session.session_id, expected_scene_epoch=7
            )

            provider.selection = _workspace_result(
                workspace_id=workspace_id,
                run_id=run.run_id,
                path="/obj/renamed",
                epoch=8,
            )
            seen = []
            service.subscribe(seen.append)
            bound = await service.bind_workspace(
                session.session_id,
                workspace_id,
                expected_manifest_revision=created.workspace.revision,
                expected_scene_epoch=8,
            )
            assert bound.changed is True
            assert provider.selection_epochs[-1] == 8
            assert [event.event_type for event in seen] == ["workspace.bound"]

            provider.manifest = _workspace_result(
                workspace_id=workspace_id,
                run_id=run.run_id,
                mode="manifest",
                path="/obj/renamed",
                epoch=8,
            )
            inspected = await service.inspect_workspace(
                session.session_id,
                None,
                expected_scene_epoch=8,
            )
            assert inspected.status.value == "Healthy"
            assert provider.manifest_calls[-1][1] == 8

            seen.clear()
            switched = await service.switch_workspace(
                session.session_id,
                workspace_id,
                expected_active_workspace_id=workspace_id,
                expected_scene_epoch=8,
            )
            assert switched.changed is False
            assert seen == []

    _run(scenario())


def test_missing_provider_inspect_reports_bridge_unavailable_after_restart(
    paths: RuntimePaths,
) -> None:
    provider = FakeWorkspaceProvider()
    workspace_id = f"ws_{'d' * 32}"

    async def scenario() -> None:
        first_runner = FakeRunner(_success_items("done"))
        async with RuntimeService.open(
            paths,
            runner_factory=_factory_for(first_runner),
            workspace_fact_provider=provider,
        ) as service:
            session = await service.create_session("Workspace")
            run = await service.start_run(session.session_id, "inspect")
            await service.wait_for_run(run.run_id)
            provider.selection = _workspace_result(
                workspace_id=workspace_id, run_id=run.run_id
            )
            await service.create_workspace(
                session.session_id, expected_scene_epoch=7
            )

        second_runner = FakeRunner(_success_items("done"))
        async with RuntimeService.open(
            paths, runner_factory=_factory_for(second_runner)
        ) as reopened:
            inspected = await reopened.inspect_workspace(
                session.session_id,
                workspace_id,
                expected_scene_epoch=None,
            )
            assert inspected.status.value == "BridgeUnavailable"
            assert inspected.active_workspace_id == workspace_id

    _run(scenario())
