"""Task 10: RuntimeService orchestration and restart recovery tests.

These tests drive the real SQLite application database, EventStore, and
CheckpointManager with a controllable fake AgentRunner. No live LLM, Houdini,
or WebSocket is involved. Each test runs its async scenario through
``asyncio.run`` and treats the service as a black box.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eee_agent.core import (
    AgentError,
    AgentException,
    ErrorCategory,
    runtime_version_report,
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
    EventCallback,
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


# --------------------------------------------------------------------------
# 2. start_run: freeze once, atomic acquire, one task, non-blocking, concurrency
# --------------------------------------------------------------------------


def test_version_report_frozen_once_per_service(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    real = runtime_version_report

    def spy() -> dict[str, object]:
        calls.append(True)
        return real()

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
            # The frozen snapshot is identical across runs.
            assert r1.model_snapshot_json == r2.model_snapshot_json
            assert r1.model_snapshot_json["eee_agent"] == real()["eee_agent"]
        # runtime_version_report is called exactly once, during open.
        assert len(calls) == 1

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
            await service.stop_run(started.run_id, force=True)
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
            session = await service.create_session("A")
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
