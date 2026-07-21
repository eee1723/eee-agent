"""RuntimeService: orchestrates repositories, runs, subscriptions, snapshots.

The service is the single coordination layer above the durable repositories
(``SessionRepository``, ``RunRepository``, ``EventStore``), the LangGraph
``CheckpointManager``, and the provider-neutral ``AgentRunner``. It owns the
application-database connection lifetime, in-memory Run and typed Apply task
references, committed-event subscription delivery, restart reconciliation,
and successful Run/ChangeSet event ordering.

It deliberately stays below the Runtime transport and identity layers: no
WebSocket authentication or exclusive process lock is implemented here. It
never imports ``hou`` or returns a HOM object; injected runner and typed Bridge
provider seams keep offline tests deterministic.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ChangeSet,
    PolicyDecision,
    WorkspaceManifest,
)
from eee_agent.changesets.repository import (
    ApplyCompletionResult,
    ChangeSetRepository,
    ProposalResult,
)
from eee_agent.changesets.service import (
    ApplyOrchestrationError,
    ChangeSetBridgeProvider,
    ChangeSetRecoveryResult,
    ChangeSetService,
    expired_error,
    summary_from_decision,
)
from eee_agent.changesets.workspace_service import (
    WorkspaceFactProvider,
    WorkspaceHealth,
    WorkspaceInspectionSummary,
    WorkspaceLifecycleSummary,
    WorkspaceService,
)
from eee_agent.core import (
    AgentError,
    AgentException,
    ErrorCategory,
    runtime_version_report,
)
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.houdini_bridge.capture import CaptureFramingReport
from eee_agent.houdini_bridge.sensitivity import SensitivitySampleTarget
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectResult,
    WorkspaceInspectionUnavailable,
)
from eee_agent.core.ids import IdKind, new_id
from eee_agent.modeling.catalog import houdini_21_minimal_quality_profile
from eee_agent.modeling.bootstrap import derive_bootstrap_manifest
from eee_agent.modeling.compiler import NodeCatalog, WorkspaceBootstrapContext
from eee_agent.modeling.contracts import ValidatorKind
from eee_agent.vision.contracts import VisionRequest
from eee_agent.vision.evaluation import DeliveryEvaluation
from eee_agent.vision.router import VisionProvider, VisionRouter
from eee_agent.modeling.proposal import (
    ModelingProposalContext,
    ModelingProposalCoordinator,
    ModelingToolContext,
)
from eee_agent.modeling.validation import (
    ValidationStatus,
    ValidatorResult,
    derive_sensitivity_sample_plan,
    validate_applied_scene,
    validate_artifact_capture,
    validate_parameter_sensitivity,
)
from eee_agent.runtime.agent_context import ReadOnlyProvider, RuntimeToolContext
from eee_agent.runtime.agent_runner import (
    AgentRunner,
    RunnerCompleted,
    RunnerEvent,
    RunnerFactory,
)
from eee_agent.runtime.artifacts import ArtifactStore
from eee_agent.runtime.checkpoints import CheckpointManager
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore, ReplayResult
from eee_agent.runtime.knowledge import KnowledgeRuntime
from eee_agent.runtime.models import (
    EventRecord,
    RetentionClass,
    RunRecord,
    RunStatus,
    SessionRecord,
)
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.runs import RunRepository
from eee_agent.runtime.sessions import SessionRepository
from eee_agent.runtime.titles import generate_session_title

# A callback receives one committed EventRecord. It may be a plain function
# (returning None) or an async function (returning an awaitable); the service
# awaits awaitable results and isolates every callback failure.
EventCallback = Callable[[EventRecord], "Awaitable[None] | None"]

# Bounded wait for an active run to reach a terminal state during shutdown.
_GRACEFUL_TIMEOUT_SECONDS = 10.0

_TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}
)

# Generic failure surfaced at the service boundary. Raw exception text and
# tracebacks never reach persisted Run/Event records; only this structured
# error is stored and broadcast.
_RUNTIME_FAILURE_ERROR = AgentError(
    code="internal.runtime_failure",
    category=ErrorCategory.INTERNAL_INVARIANT,
    message_for_user="The runtime encountered an unexpected error.",
)

# Matches RunRepository's interrupted-run error so reconciliation emits the
# identical structured error that the run row already stores.
_INTERRUPTED_ERROR = AgentError(
    code="runtime.interrupted",
    category=ErrorCategory.INTERNAL_INVARIANT,
    message_for_user="The previous Runtime process stopped before this run completed.",
)


class _UnavailableWorkspaceFactProvider:
    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        raise WorkspaceInspectionUnavailable(
            "Trusted workspace inspection is not currently available."
        )

    async def inspect_manifest(
        self,
        manifest: WorkspaceManifest,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectResult:
        raise WorkspaceInspectionUnavailable(
            "Trusted workspace inspection is not currently available."
        )


class _UnavailableReadOnlyProvider:
    """Fail-closed Runtime provider; never falls back to the legacy bridge."""

    @staticmethod
    def _result() -> dict[str, object]:
        return {
            "ok": False,
            "code": "bridge.unavailable",
            "message": "The trusted read-only provider is unavailable.",
        }

    async def scene_status(self):
        return self._result()

    async def query_scene(self, node_paths: list[str]):
        return self._result()

    async def inspect_workspace(self, workspace_id: str):
        return self._result()

    async def geometry_stats(self, node_path: str):
        return self._result()

    async def work_status(self, workspace_id: str):
        return self._result()

    async def search_houdini_knowledge(self, query: str, limit: int):
        return self._result()

    async def get_houdini_knowledge(self, entity_id: str, max_body_bytes: int):
        return self._result()


class _RuntimeReadOnlyProvider:
    """Compose the injected live-scene provider with advisory Knowledge."""

    def __init__(self, base: ReadOnlyProvider, knowledge: KnowledgeRuntime) -> None:
        self._base = base
        self._knowledge = knowledge

    async def scene_status(self):
        return await self._base.scene_status()

    async def query_scene(self, node_paths: list[str]):
        return await self._base.query_scene(node_paths)

    async def inspect_workspace(self, workspace_id: str):
        return await self._base.inspect_workspace(workspace_id)

    async def geometry_stats(self, node_path: str):
        return await self._base.geometry_stats(node_path)

    async def work_status(self, workspace_id: str):
        return await self._base.work_status(workspace_id)

    async def search_houdini_knowledge(self, query: str, limit: int):
        return self._knowledge.search(query, limit=limit)

    async def get_houdini_knowledge(self, entity_id: str, max_body_bytes: int):
        return self._knowledge.get(entity_id, max_body_bytes=max_body_bytes)


def _checkpoint_cleanup_failed() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.checkpoint_cleanup_failed",
            category=ErrorCategory.INTERNAL_INVARIANT,
            message_for_user=(
                "The session was deleted but its checkpoint history could "
                "not be removed."
            ),
            scene_may_have_changed=False,
        )
    )


def _artifact_cleanup_failed() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.artifact_cleanup_failed",
            category=ErrorCategory.INTERNAL_INVARIANT,
            message_for_user=(
                "The session was deleted but its artifact files could "
                "not be removed."
            ),
            scene_may_have_changed=False,
        )
    )


_VISION_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

# Advisory Vision instruction prefix; the run brief is appended and the
# whole instruction is truncated to the strict VisionRequest budget.
_VISION_INSTRUCTION_PREFIX = (
    "Advisory visual evaluation of the post-Apply viewport capture. "
    "Assess whether the rendered result visibly matches the approved change. "
    "User brief: "
)


def _vision_instruction(brief: str) -> str:
    return (_VISION_INSTRUCTION_PREFIX + brief)[:4096]


def _changeset_delivery_spec(changeset: ChangeSet) -> str:
    """Bounded human-readable summary of the approved ChangeSet."""
    risk = changeset.risk_summary
    effects = ",".join(risk.effect_names) or "none"
    targets = ",".join(risk.affected_paths) or "none"
    return (
        f"operations={risk.operation_count}; effects={effects}; targets={targets}"
    )[:4096]


async def _run_uncancelled(coro: Awaitable[object]) -> object:
    """Run ``coro`` to completion, deferring cancellation of this task.

    Used to protect short critical persistence regions (run.created commit +
    task registration, the failure state/event bundle, terminal convergence)
    so a caller/external cancellation cannot leave a half-persisted run. The
    coroutine runs as its own short-lived helper task (NOT an Agent execution
    task) and is shielded; if this task is cancelled (once or repeatedly) the
    inner still completes, then CancelledError is re-raised so the caller
    observes the cancellation after the invariant is restored.
    """
    inner = asyncio.ensure_future(coro)
    me = asyncio.current_task()
    cancelled = False
    while True:
        try:
            await asyncio.shield(inner)
            break
        except asyncio.CancelledError:
            cancelled = True
            if me is not None:
                me.uncancel()
            if inner.done():
                break
            continue
    if cancelled:
        inner.result()  # surface an inner exception (if any) first
        raise asyncio.CancelledError()
    return inner.result()


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """A consistent point-in-time view of one session.

    ``snapshot_seq`` is the session's ``last_seq`` captured inside the same
    transaction that read the runs, so live events with ``seq > snapshot_seq``
    can be replayed without a gap. ``version_report`` is the frozen Runtime
    version report captured once at service open.
    """

    session: SessionRecord
    runs: tuple[RunRecord, ...]
    active_run: RunRecord | None
    snapshot_seq: int
    has_earlier_runs: bool
    earliest_included_run_id: str | None
    version_report: Mapping[str, object]


class RuntimeService:
    """Coordinates durable Runtime state and one active read-only run.

    Lifecycle: open via :meth:`open` (an async context manager). Inside the
    context, session/run operations are available. On exit the active run is
    cancelled and converged, then the checkpoint manager and application
    database close in reverse open order.
    """

    def __init__(
        self,
        database: RuntimeDatabase,
        paths: RuntimePaths,
        *,
        graceful_timeout: float = _GRACEFUL_TIMEOUT_SECONDS,
        changeset_clock: "Callable[[], datetime] | None" = None,
        changeset_binding_provider: "Callable[[], object] | None" = None,
        changeset_bridge_provider: ChangeSetBridgeProvider | None = None,
        workspace_fact_provider: WorkspaceFactProvider | None = None,
        modeling_catalog_provider: Callable[[], NodeCatalog] | None = None,
        read_only_provider: ReadOnlyProvider | None = None,
        knowledge_runtime: KnowledgeRuntime | None = None,
        vision_provider: VisionProvider | None = None,
        title_model_provider: Callable[[], object] | None = None,
    ) -> None:
        self._database = database
        self._paths = paths
        # Configured graceful shutdown timeout (seconds) used by _shutdown when
        # waiting for an active run to converge. Defaults to the historical 10s
        # so existing callers (Tasks 1-12) are unchanged.
        self._graceful_timeout = graceful_timeout
        self._sessions = SessionRepository(database)
        self._runs = RunRepository(database)
        self._events = EventStore(database)
        self._artifacts = ArtifactStore(database, paths.artifacts_dir)
        self._callbacks: set[EventCallback] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._apply_tasks: dict[str, asyncio.Task[ApplyCompletionResult]] = {}
        self._pending_change_recovery: tuple[str, ...] = ()
        # Serializes run-state transitions: the from-state read, the transition
        # write, and the matching durable state-changed event append happen
        # atomically so a concurrent stop cannot make the recorded from-state
        # diverge from the actual transitioned-from status.
        self._state_lock = asyncio.Lock()
        self._checkpoints: CheckpointManager | None = None
        self._runner: object | None = None
        self._modeling_catalog_provider = modeling_catalog_provider
        base_read_only_provider: ReadOnlyProvider = (
            read_only_provider
            if isinstance(read_only_provider, ReadOnlyProvider)
            else _UnavailableReadOnlyProvider()
        )
        self._knowledge = knowledge_runtime or KnowledgeRuntime(paths.knowledge_cache_path)
        self._read_only_provider: ReadOnlyProvider = _RuntimeReadOnlyProvider(
            base_read_only_provider, self._knowledge
        )
        self._modeling_validation_provider = (
            changeset_bridge_provider
            if modeling_catalog_provider is not None
            and hasattr(changeset_bridge_provider, "inspect_geometry")
            else None
        )
        # Trusted ChangeSet approval service. It shares this service's EventStore
        # so proposal/decision events commit in the same transaction as the
        # changeset/approval mutation, and it is constructed with injected
        # clock/binding seams. The binding provider stays None (fail-closed)
        # until a later task wires the read-only Bridge scene query.
        changeset_repository = ChangeSetRepository(database, events=self._events)
        self._changesets = ChangeSetService(
            changeset_repository,
            clock=changeset_clock,
            binding_provider=changeset_binding_provider,  # type: ignore[arg-type]
            bridge_provider=changeset_bridge_provider,
            bootstrap_manifest_factory=(
                None
                if modeling_catalog_provider is None
                else lambda changeset, receipt: (
                    derive_bootstrap_manifest(changeset, receipt)
                    if changeset.workspace_id is None and receipt.is_success
                    else None
                )
            ),
        )
        self._workspaces = WorkspaceService(
            changeset_repository,
            provider=(
                _UnavailableWorkspaceFactProvider()
                if workspace_fact_provider is None
                else workspace_fact_provider
            ),
        )
        self._changeset_repository = changeset_repository
        # Advisory Vision seam. The router resolves only ArtifactStore-
        # registered refs and hands providers the exact verified bytes; with
        # no provider it still records truthful unavailable evidence.
        self._vision_router = VisionRouter(self._artifacts, vision_provider)
        # Optional seam for auto-titling placeholder Sessions after the first
        # run completes. None disables it (tests / no-LLM contexts).
        self._title_model_provider = title_model_provider
        # In-flight auto-title tasks, keyed by session_id, so a Session is
        # titled at most once and a shutdown can await them.
        self._title_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def graceful_timeout(self) -> float:
        """The configured graceful-shutdown timeout (seconds)."""
        return self._graceful_timeout

    @property
    def knowledge_status(self):
        """Current advisory Knowledge cache status for UI/status consumers."""
        return self._knowledge.status

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        paths: RuntimePaths,
        *,
        runner_factory: RunnerFactory,
        graceful_timeout: float = _GRACEFUL_TIMEOUT_SECONDS,
        changeset_clock: "Callable[[], datetime] | None" = None,
        changeset_binding_provider: "Callable[[], object] | None" = None,
        changeset_bridge_provider: ChangeSetBridgeProvider | None = None,
        workspace_fact_provider: WorkspaceFactProvider | None = None,
        modeling_catalog_provider: Callable[[], NodeCatalog] | None = None,
        read_only_provider: ReadOnlyProvider | None = None,
        knowledge_runtime: KnowledgeRuntime | None = None,
        vision_provider: VisionProvider | None = None,
        title_model_provider: Callable[[], object] | None = None,
    ) -> AsyncIterator["RuntimeService"]:
        """Open a service in the approved order and close it in reverse.

        Order: directories, application database, repositories,
        interrupted-run reconciliation, checkpoint manager, runner factory.
        On exit (normal or exceptional) the active run is cancelled/converged,
        then the checkpoint manager and database close. A failure partway
        through initialization still closes whatever was opened.

        Checkpoint-manager cleanup errors propagate on a normal close; when a
        business exception is already unwinding, the business exception takes
        priority and a cleanup error is suppressed.
        """
        database: RuntimeDatabase | None = None
        checkpoints: CheckpointManager | None = None
        service: RuntimeService | None = None
        try:
            paths.create_used_directories()
            database = await RuntimeDatabase.open(paths.app_db)
            service = cls(
                database,
                paths,
                graceful_timeout=graceful_timeout,
                changeset_clock=changeset_clock,
                changeset_binding_provider=changeset_binding_provider,
                changeset_bridge_provider=changeset_bridge_provider,
                workspace_fact_provider=workspace_fact_provider,
                modeling_catalog_provider=modeling_catalog_provider,
                read_only_provider=read_only_provider,
                knowledge_runtime=knowledge_runtime,
                vision_provider=vision_provider,
                title_model_provider=title_model_provider,
            )
            await service._reconcile()
            checkpoints = CheckpointManager(paths.checkpoints_db)
            await checkpoints.__aenter__()
            service._checkpoints = checkpoints
            service._runner = runner_factory(checkpoints.require_saver())
            if hasattr(service._runner, "set_context_factory"):
                service._runner.set_context_factory(
                    service._build_runtime_context
                )
            try:
                yield service
            finally:
                await service._shutdown()
        finally:
            # Pass the active exception (if any) to the checkpoint context so it
            # gets correct close semantics. A checkpoint cleanup error is
            # propagated only on a normal close; it is suppressed when a business
            # exception is already unwinding so the business exception wins. The
            # database is always closed (even when checkpoint cleanup raised) so
            # no aiosqlite connection is leaked.
            exc_info = sys.exc_info()
            checkpoint_cleanup_error: BaseException | None = None
            if checkpoints is not None:
                try:
                    await checkpoints.__aexit__(
                        exc_info[0], exc_info[1], exc_info[2]
                    )
                except BaseException as cleanup_error:
                    if exc_info[1] is None:
                        checkpoint_cleanup_error = cleanup_error
            if database is not None:
                await database.close()
            if checkpoint_cleanup_error is not None:
                raise checkpoint_cleanup_error

    async def _shutdown(self) -> None:
        # A ChangeSet beyond the durable Applying boundary is independent of
        # its caller. Give it the full graceful window before cancelling the
        # client-side Bridge wait; cancellation leaves Applying for the next
        # process to reconcile and never claims the scene is unchanged.
        apply_items = [task for task in self._apply_tasks.values() if not task.done()]
        if apply_items:
            done, pending = await asyncio.wait(
                apply_items, timeout=self._graceful_timeout
            )
            del done
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # Cancel in-memory run tasks and let them converge within the graceful
        # timeout. A task cancelled before it ever started never enters its
        # body, so its terminal-guarantee finally does not run either.
        items = list(self._tasks.values())
        for task in items:
            if not task.done():
                task.cancel()
        if items:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*items, return_exceptions=True),
                    timeout=self._graceful_timeout,
                )
            except asyncio.TimeoutError:
                # Tasks that did not converge are left to the next process's
                # reconciliation; do not block shutdown indefinitely.
                pass
        # Persistent sweep: converge any active run that is still non-terminal.
        # This reads the durable active_run_id (the persistent authority), so it
        # also covers orphans that have no in-memory task (pre-registration or
        # tasks cancelled before they started).
        await self._sweep_active_run()

    async def _sweep_active_run(self) -> None:
        try:
            active_id = await self._runs.active_run_id()
        except Exception:
            return
        if active_id is None:
            return
        try:
            current = await self._runs.get(active_id)
        except Exception:
            return
        if current.status in _TERMINAL_STATUSES:
            return
        try:
            await _run_uncancelled(
                self._handle_cancellation(current.session_id, active_id)
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # session operations
    # ------------------------------------------------------------------

    async def create_session(self, title: str) -> SessionRecord:
        # The session mutation and its durable event form one
        # cancellation-deferred persistence region, so a caller cancel cannot
        # leave a persisted session without its session.created event.
        async def persist() -> tuple[str, EventRecord]:
            session = await self._sessions.create(title)
            record = await self._append(
                session.session_id,
                None,
                "session.created",
                {
                    "session_id": session.session_id,
                    "title": session.title,
                    "status": session.status.value,
                },
                RetentionClass.DURABLE,
            )
            return session.session_id, record

        session_id, record = await _run_uncancelled(persist())
        await self._notify(record)
        # Return the post-event record so last_seq reflects the committed event.
        return await self._sessions.get(session_id)

    async def list_sessions(
        self, include_archived: bool = False
    ) -> list[SessionRecord]:
        return await self._sessions.list(include_archived)

    async def get_session(self, session_id: str) -> SessionRecord:
        return await self._sessions.get(session_id)

    async def rename_session(
        self, session_id: str, title: str
    ) -> SessionRecord:
        async def persist() -> tuple[str, EventRecord]:
            session = await self._sessions.rename(session_id, title)
            record = await self._append(
                session.session_id,
                None,
                "session.renamed",
                {"session_id": session.session_id, "title": session.title},
                RetentionClass.DURABLE,
            )
            return session.session_id, record

        session_id, record = await _run_uncancelled(persist())
        await self._notify(record)
        return await self._sessions.get(session_id)

    async def archive_session(self, session_id: str) -> SessionRecord:
        async def persist() -> tuple[str, EventRecord]:
            session = await self._sessions.archive(session_id)
            record = await self._append(
                session.session_id,
                None,
                "session.archived",
                {"session_id": session.session_id},
                RetentionClass.DURABLE,
            )
            return session.session_id, record

        session_id, record = await _run_uncancelled(persist())
        await self._notify(record)
        return await self._sessions.get(session_id)

    async def delete_session(self, session_id: str) -> None:
        # Artifact metadata must be transitioned before deleting the session;
        # otherwise the FK cascade would erase the retryable cleanup state.
        # Once bytes are evicted, application rows can be deleted and the
        # checkpoint thread is cleaned independently.
        try:
            await self._artifacts.delete_session_artifacts(session_id)
        except Exception:
            raise _artifact_cleanup_failed() from None
        await self._sessions.delete_application_records(session_id)
        checkpoints = self._checkpoints
        if checkpoints is None:
            return
        try:
            await checkpoints.delete_thread(session_id)
        except Exception:
            raise _checkpoint_cleanup_failed() from None

    # ------------------------------------------------------------------
    # run operations
    # ------------------------------------------------------------------

    async def start_run(
        self, session_id: str, user_input: str
    ) -> RunRecord:
        # Freeze a fresh runtime_version_report into THIS run's model snapshot
        # (one call per start_run). Atomic global acquisition happens inside
        # create_and_acquire.
        model_snapshot = runtime_version_report()
        # Freeze Knowledge provenance into every Run snapshot.  These fields
        # are advisory metadata only and never grant scene creatability.
        model_snapshot.update(self._knowledge.snapshot_fields())
        run = await self._runs.create_and_acquire(
            session_id, user_input, model_snapshot
        )
        # Once the run is acquired it is ACCEPTED. Commit run.created and
        # register the single execution task in one cancellation-deferred
        # critical region: a caller cancellation (e.g. a panel disconnect) must
        # never cancel an accepted Run or leave it without its task. Only after
        # the task is registered do we notify subscribers.
        record = await _run_uncancelled(self._accept_run(run, user_input))
        await self._notify(record)
        return run

    async def _accept_run(
        self, run: RunRecord, user_input: str
    ) -> EventRecord:
        # Critical region: commit run.created, then synchronously create and
        # register the execution task (no await between create and register).
        record = await self._append(
            run.session_id,
            run.run_id,
            "run.created",
            {
                "run_id": run.run_id,
                "session_id": run.session_id,
                "user_input": user_input,
            },
            RetentionClass.DURABLE,
        )
        task = asyncio.create_task(
            self._run_guarded(run.session_id, run.run_id, user_input)
        )
        self._tasks[run.run_id] = task
        return record

    async def get_run(self, run_id: str) -> RunRecord:
        return await self._runs.get(run_id)

    async def wait_for_run(self, run_id: str) -> RunRecord:
        # Shield the active task so a cancelled waiter (e.g. a disconnecting
        # client) cannot cancel the actual run. A CancelledError here is either
        # the waiter being cancelled (run still non-terminal -> re-raise) or the
        # run itself being cancelled via stop_run (run now terminal -> return).
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = await self._runs.get(run_id)
                if current.status in _TERMINAL_STATUSES:
                    return current
                raise
        return await self._runs.get(run_id)

    async def stop_run(
        self, run_id: str, *, force: bool = False
    ) -> RunRecord:
        # Both cooperative and force stops persist a legal StopRequested
        # transition (with its durable event) and cancel the Agent task. The
        # whole region is cancellation-deferred so a caller cancel cannot lose
        # the StopRequested event or prevent the Run from actually stopping.
        # v1 has no dispatched write work, so both converge deterministically to
        # Cancelled; ``force`` is accepted for protocol parity and reserved for
        # stricter immediate cancellation later.
        current, record = await _run_uncancelled(self._stop_critical(run_id))
        if record is not None:
            await self._notify(record)
        return current

    async def _stop_critical(
        self, run_id: str
    ) -> tuple[RunRecord, EventRecord | None]:
        record: EventRecord | None = None
        async with self._state_lock:
            current = await self._runs.get(run_id)
            if current.status in _TERMINAL_STATUSES:
                # Already terminal: no-op, avoid an illegal duplicate transition.
                return current, None
            session_id = current.session_id
            if current.status not in (
                RunStatus.STOP_REQUESTED,
                RunStatus.STOPPING,
            ):
                from_status = current.status
                current = await self._runs.transition(
                    run_id, RunStatus.STOP_REQUESTED
                )
                record = await self._append(
                    session_id,
                    run_id,
                    "run.state_changed",
                    {
                        "from": from_status.value,
                        "to": RunStatus.STOP_REQUESTED.value,
                    },
                    RetentionClass.DURABLE,
                )
        # Cancel the Agent execution task outside the state lock. This must run
        # even if the caller was cancelled (the region is cancellation-deferred)
        # so the Run actually stops. Cancel at most once: a repeated request
        # must not deliver a second CancelledError into a task already running
        # its cancellation handler.
        task = self._tasks.get(run_id)
        if task is not None and not task.done() and task.cancelling() == 0:
            task.cancel()
        return current, record

    # ------------------------------------------------------------------
    # replay / snapshot / subscribe
    # ------------------------------------------------------------------

    async def replay(
        self, session_id: str, *, after_seq: int, limit: int
    ) -> ReplayResult:
        return await self._events.replay(
            session_id, after_seq=after_seq, limit=limit
        )

    async def snapshot(self, session_id: str) -> SessionSnapshot:
        data = await self._events.snapshot_data(session_id)
        version_report = runtime_version_report()
        version_report.update(self._knowledge.snapshot_fields())
        return SessionSnapshot(
            session=data.session,
            runs=data.runs,
            active_run=data.active_run,
            snapshot_seq=data.snapshot_seq,
            has_earlier_runs=data.has_earlier_runs,
            earliest_included_run_id=data.earliest_included_run_id,
            version_report=version_report,
        )

    def subscribe(self, callback: EventCallback) -> Callable[[], None]:
        """Register a committed-event callback; return idempotent unsubscribe."""
        self._callbacks.add(callback)

        def unsubscribe() -> None:
            # discard is idempotent: repeated calls are safe no-ops.
            self._callbacks.discard(callback)

        return unsubscribe

    # ------------------------------------------------------------------
    # changeset approval operations
    # ------------------------------------------------------------------

    async def propose_changeset_trusted(
        self,
        changeset: ChangeSet,
        policy_decision: PolicyDecision,
    ) -> ProposalResult:
        """Persist a typed proposal from a trusted in-process compiler only."""
        result = await self._changesets.propose(changeset, policy_decision)
        for record in result.events:
            await self._notify(record)
        return result

    async def record_vision_evaluation(
        self,
        session_id: str,
        run_id: str,
        evaluation: DeliveryEvaluation,
    ) -> EventRecord:
        """Persist one bounded advisory evaluation record for replay/UI use."""
        if type(evaluation) is not DeliveryEvaluation:
            raise TypeError("evaluation must be an exact DeliveryEvaluation")
        return await self._emit(
            session_id,
            run_id,
            "vision.evaluation_completed",
            evaluation.to_dict(),
            RetentionClass.DURABLE,
        )

    async def _validate_applied_changeset(
        self, result: ApplyCompletionResult
    ) -> None:
        """Persist bounded post-Apply quality evidence without replaying writes."""
        provider = self._modeling_validation_provider
        if provider is None or not result.receipt.is_success:
            return
        event_type = "modeling.validation_completed"
        capture: tuple[ArtifactRef, CaptureFramingReport] | None = None
        validator_results: list[ValidatorResult] = []
        try:
            query = await provider.inspect_geometry(result.changeset)
            validator_results = list(
                validate_applied_scene(
                    changeset=result.changeset,
                    query=query,
                )
            )
            # The typed Bridge sample-and-restore cycle resolves
            # ParameterSensitivity only when the changeset/catalog define
            # bounded sample targets and the quality profile enables it.
            sample_plan = await self._sensitivity_sample_plan(result.changeset)
            if sample_plan:
                evidence = await provider.sample_sensitivity(
                    result.changeset, sample_plan
                )
                validator_results.append(
                    validate_parameter_sensitivity(
                        changeset=result.changeset,
                        baseline=evidence.baseline,
                        samples=evidence.samples,
                        restored=evidence.restored,
                    )
                )
            # The typed Bridge capture resolves the Artifact stage only when
            # the quality profile enables it. A capture failure is durable and
            # explicit (modeling.capture_failed plus a Failed Artifact result)
            # and never masquerades as visual success.
            try:
                capture = await self._capture_applied_changeset(result.changeset)
            except Exception as exc:  # noqa: BLE001 - Apply is already durable
                capture = None
                code = (
                    exc.error.code
                    if isinstance(exc, AgentException)
                    else "modeling.capture.invalid_evidence"
                )
                validator_results.append(
                    ValidatorResult(
                        ValidatorKind.ARTIFACT,
                        ValidationStatus.FAILED,
                        "modeling.artifact.capture_failed",
                        "The post-Apply capture did not produce verifiable artifact evidence.",
                    )
                )
                await self._emit(
                    result.changeset.session_id,
                    result.changeset.run_id,
                    "modeling.capture_failed",
                    {
                        "change_id": result.changeset.change_id,
                        "changeset_digest": result.changeset.digest,
                        "code": code,
                    },
                    RetentionClass.DURABLE,
                )
            if capture is not None:
                artifact, framing = capture
                validator_results.append(
                    validate_artifact_capture(
                        changeset=result.changeset,
                        artifact=artifact,
                        framing=framing,
                    )
                )
                await self._emit(
                    result.changeset.session_id,
                    result.changeset.run_id,
                    "modeling.artifact_captured",
                    {
                        "change_id": result.changeset.change_id,
                        "changeset_digest": result.changeset.digest,
                        "artifact": artifact.to_dict(),
                        "framing": framing.to_dict(),
                    },
                    RetentionClass.DURABLE,
                )
            payload: dict[str, object] = {
                "change_id": result.changeset.change_id,
                "changeset_digest": result.changeset.digest,
                "complete": all(
                    item.status.value == "Passed" for item in validator_results
                ),
                "results": [item.to_dict() for item in validator_results],
            }
        except Exception as exc:  # noqa: BLE001 - Apply is already durable
            event_type = "modeling.validation_unavailable"
            code = (
                exc.error.code
                if isinstance(exc, AgentException)
                else "modeling.validation.invalid_evidence"
            )
            payload = {
                "change_id": result.changeset.change_id,
                "changeset_digest": result.changeset.digest,
                "complete": False,
                "code": code,
            }
        await self._emit(
            result.changeset.session_id,
            result.changeset.run_id,
            event_type,
            payload,
            RetentionClass.DURABLE,
        )
        validation_report = tuple(
            f"{item.validator.value}:{item.status.value}:{item.code}"
            for item in validator_results
        )
        if event_type == "modeling.validation_unavailable":
            validation_report = validation_report + (
                f"validation:unavailable:{payload['code']}",
            )
        await self._record_delivery_evaluation(
            result,
            capture=capture,
            deterministic_valid=bool(payload["complete"]),
            validation_report=validation_report,
        )

    async def _record_delivery_evaluation(
        self,
        result: ApplyCompletionResult,
        *,
        capture: tuple[ArtifactRef, CaptureFramingReport] | None,
        deterministic_valid: bool,
        validation_report: tuple[str, ...],
    ) -> None:
        """Record advisory Vision evidence over the registered capture.

        Runs only after the deterministic validation event is durable and
        only over the ArtifactStore-registered capture (never the Bridge
        source path). Expected provider/artifact failures are already
        bounded unavailable evidence inside the router outcome; any
        unexpected error is swallowed so advisory evaluation can never
        replay, undo, or fail the already-durable Apply.
        """
        if capture is None:
            return
        artifact, _framing = capture
        if artifact.media_type not in _VISION_MEDIA_TYPES:
            return
        changeset = result.changeset
        try:
            run = await self._runs.get(changeset.run_id)
            approval = await self._changeset_repository.get_approval(
                changeset.change_id
            )
            outcome = await self._vision_router.evaluate(
                VisionRequest(
                    request_id=f"vision-{changeset.change_id}",
                    artifact=artifact,
                    instruction=_vision_instruction(run.user_input),
                ),
                deterministic_valid=deterministic_valid,
            )
            evaluation = DeliveryEvaluation(
                brief=run.user_input[:4096],
                spec=_changeset_delivery_spec(changeset),
                changeset_digest=changeset.digest,
                approval=(
                    f"{approval.decision.value}:"
                    f"{approval.decided_by}:{approval.approval_id}"
                ),
                receipt=(
                    f"{result.receipt.status.value}:"
                    f"{result.receipt.after_revision}"
                ),
                validation_report=validation_report,
                artifact_refs=(artifact,),
                artifact_status=("available",),
                knowledge_manifest_sha256=self._knowledge.status.manifest_sha256,
                vision_status=outcome.decision.status,
                vision_report=outcome.report,
                final_decision=outcome.decision,
                recovery_evidence=(),
            )
            await self.record_vision_evaluation(
                changeset.session_id, changeset.run_id, evaluation
            )
        except Exception:  # noqa: BLE001 - Apply is already durable
            return


    async def _sensitivity_sample_plan(
        self, changeset: ChangeSet
    ) -> tuple[SensitivitySampleTarget, ...]:
        """Derive the catalog-bounded sample plan; empty when not applicable."""
        catalog_provider = self._modeling_catalog_provider
        if catalog_provider is None:
            return ()
        catalog = catalog_provider()
        if inspect.isawaitable(catalog):
            catalog = await catalog
        if type(catalog) is not NodeCatalog:
            return ()
        return derive_sensitivity_sample_plan(
            changeset=changeset,
            catalog=catalog,
            quality_profile=houdini_21_minimal_quality_profile(),
        )

    async def _capture_applied_changeset(
        self, changeset: ChangeSet
    ) -> tuple[ArtifactRef, CaptureFramingReport] | None:
        """Capture + register the post-Apply screenshot; None when not applicable.

        Runs only when the quality profile enables the Artifact validator and
        the bridge provider exposes the typed capture op. The bridge writes
        the PNG inside the Runtime-owned artifacts directory; the returned
        ArtifactRef is the registered canonical record, re-hash-verified
        against the bridge reference before registration (a mismatch fails
        closed and deletes the file). Never a replay of the durable Apply.
        """
        provider = self._modeling_validation_provider
        if provider is None or not hasattr(provider, "capture"):
            return None
        if ValidatorKind.ARTIFACT not in (
            houdini_21_minimal_quality_profile().validators
        ):
            return None
        if not changeset.affected_nodes:
            return None
        artifact_id = new_id(IdKind.ARTIFACT)
        target_dir = (
            self._paths.artifacts_dir / changeset.session_id / changeset.run_id
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        result = await provider.capture(
            changeset,
            target_dir=target_dir,
            artifact_id=artifact_id,
        )
        if (
            result.artifact_id != artifact_id
            or result.relative_path != f"{artifact_id}.png"
        ):
            raise AgentException(
                AgentError(
                    code="modeling.capture.invalid_evidence",
                    category=ErrorCategory.HOUDINI_BRIDGE,
                    message_for_user=(
                        "The bridge capture reference does not match the request."
                    ),
                    retryable=False,
                )
            )
        ref = await self._artifacts.register(
            session_id=changeset.session_id,
            run_id=changeset.run_id,
            artifact_id=artifact_id,
            source=target_dir / result.relative_path,
            media_type=result.media_type,
            expected_sha256=result.sha256,
            expected_size_bytes=result.size_bytes,
        )
        return ref, result.framing

    async def apply_changeset_trusted(
        self, change_id: str
    ) -> ApplyCompletionResult:
        """Join or start one trusted Apply task; caller cancellation is isolated."""
        task = self._apply_tasks.get(change_id)
        if task is None or task.done():
            task = asyncio.create_task(self._apply_changeset_task(change_id))
            self._apply_tasks[change_id] = task
            task.add_done_callback(
                lambda completed, cid=change_id: self._discard_apply_task(
                    cid, completed
                )
            )
        return await asyncio.shield(task)

    async def _apply_changeset_task(
        self, change_id: str
    ) -> ApplyCompletionResult:
        """Own Apply plus post-commit notifications independently of a waiter."""
        try:
            result = await self._changesets.apply(change_id)
        except ApplyOrchestrationError as exc:
            for record in exc.events:
                await self._notify(record)
            raise exc.cause from exc
        for record in result.events:
            await self._notify(record)
        # A terminal ChangeSet loaded with no new events was already finalized
        # and validated by its original owner; do not append duplicate quality
        # events on a later trusted join/read.
        if result.events:
            await self._validate_applied_changeset(result)
        return result

    def _discard_apply_task(
        self,
        change_id: str,
        task: asyncio.Task[ApplyCompletionResult],
    ) -> None:
        if not task.cancelled():
            # Mark a background exception retrieved even when the original
            # waiter disconnected. Awaiting the task elsewhere still raises it.
            task.exception()
        if self._apply_tasks.get(change_id) is task:
            self._apply_tasks.pop(change_id, None)

    async def recover_changesets_trusted(
        self,
    ) -> tuple[ChangeSetRecoveryResult, ...]:
        """Retry read-only reconciliation for every interrupted Apply."""
        results = await self._changesets.recover_applying()
        pending: list[str] = []
        for result in results:
            if result.pending:
                pending.append(result.change_id)
            for record in result.events:
                await self._notify(record)
        self._pending_change_recovery = tuple(pending)
        return results

    async def _decide_changeset(
        self,
        change_id: str,
        changeset_digest: str,
        *,
        approve: bool,
    ) -> dict[str, object]:
        # The ChangeSet service commits the approval/changeset mutation and its
        # durable events in one repository transaction before returning. Only
        # after that committed result is back do we notify subscribers, so an
        # event is never broadcast before it is durable and a failed decision
        # never notifies anyone. An expired decision still commits the expiry
        # transition + events; we broadcast those, then surface the
        # approval.expired error to the caller.
        if approve:
            result = await self._changesets.approve(change_id, changeset_digest)
        else:
            result = await self._changesets.reject(change_id, changeset_digest)
        for record in result.events:
            await self._notify(record)
        if result.outcome is ApprovalDecision.EXPIRED:
            raise expired_error()
        return summary_from_decision(result).to_dict()

    async def approve_changeset(
        self, change_id: str, changeset_digest: str
    ) -> dict[str, object]:
        """Approve a pending ChangeSet and run the trusted Apply boundary.

        Modeling mode deliberately has no public ``changeset.apply`` command:
        once the user approves the bounded proposal summary, Runtime owns the
        single-flight Apply task and publishes its durable state/receipt events.
        Legacy Runtime fixtures without both the modeling catalog and typed
        Bridge keep the historical approval-only behavior.
        """
        decision = await self._decide_changeset(
            change_id, changeset_digest, approve=True
        )
        if self._modeling_catalog_provider is None:
            return decision
        # A catalog without the typed Bridge is useful for offline proposal
        # tests, but cannot safely cross the write boundary. Keep approval
        # durable and let the normal explicit trusted seam remain unavailable.
        if not self._changesets_has_bridge():
            return decision
        applied = await self.apply_changeset_trusted(change_id)
        return {
            **decision,
            "apply": {
                "state": applied.state.value,
                "receipt_status": applied.receipt.status.value,
                "scene_may_have_changed": applied.receipt.scene_may_have_changed,
            },
        }

    def _changesets_has_bridge(self) -> bool:
        """Return whether the ChangeSet service has a typed write provider."""
        # Kept as a tiny seam instead of exposing the provider itself. This
        # avoids handing a model or transport caller any write capability.
        return getattr(self._changesets, "_bridge_provider", None) is not None

    async def list_changesets(
        self, session_id: str, *, limit: int
    ) -> tuple[dict[str, object], ...]:
        """Return bounded ChangeSet summaries for one panel Session."""
        summaries = await self._changesets.list_panel_summaries(
            session_id, limit=limit
        )
        return tuple(summary.to_dict() for summary in summaries)

    async def reject_changeset(
        self, change_id: str, changeset_digest: str
    ) -> dict[str, object]:
        """Reject a pending ChangeSet; returns the bounded approval summary."""
        return await self._decide_changeset(
            change_id, changeset_digest, approve=False
        )

    # ------------------------------------------------------------------
    # trusted Workspace lifecycle operations
    # ------------------------------------------------------------------

    async def create_workspace(
        self, session_id: str, *, expected_scene_epoch: int | None
    ) -> WorkspaceLifecycleSummary:
        result = await self._workspaces.create_committed(
            session_id, expected_scene_epoch=expected_scene_epoch
        )
        for record in result.events:
            await self._notify(record)
        return result.summary

    async def bind_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_manifest_revision: str,
        expected_scene_epoch: int | None,
    ) -> WorkspaceLifecycleSummary:
        result = await self._workspaces.bind_committed(
            session_id,
            workspace_id,
            expected_manifest_revision=expected_manifest_revision,
            expected_scene_epoch=expected_scene_epoch,
        )
        for record in result.events:
            await self._notify(record)
        return result.summary

    async def switch_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_active_workspace_id: str | None,
        expected_scene_epoch: int | None,
    ) -> WorkspaceLifecycleSummary:
        result = await self._workspaces.switch_committed(
            session_id,
            workspace_id,
            expected_active_workspace_id=expected_active_workspace_id,
            expected_scene_epoch=expected_scene_epoch,
        )
        for record in result.events:
            await self._notify(record)
        return result.summary

    async def inspect_workspace(
        self,
        session_id: str,
        workspace_id: str | None,
        *,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectionSummary:
        return await self._workspaces.inspect(
            session_id,
            workspace_id,
            expected_scene_epoch=expected_scene_epoch,
        )

    # ------------------------------------------------------------------
    # internal: append + notify, transitions, run task, handlers
    # ------------------------------------------------------------------

    async def _append(
        self,
        session_id: str,
        run_id: str | None,
        event_type: str,
        payload: dict[str, object],
        retention_class: RetentionClass,
    ) -> EventRecord:
        return await self._events.append(
            session_id=session_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            retention_class=retention_class,
        )

    async def _emit(
        self,
        session_id: str,
        run_id: str | None,
        event_type: str,
        payload: dict[str, object],
        retention_class: RetentionClass,
    ) -> EventRecord:
        # Append + notify for events that are not run-state transitions. The
        # event transaction commits before any subscriber is notified, so every
        # delivered EventRecord is durable and replayable.
        record = await self._append(
            session_id, run_id, event_type, payload, retention_class
        )
        await self._notify(record)
        return record

    async def _notify(self, record: EventRecord) -> None:
        # Run callbacks OUTSIDE the state lock so a callback cannot reenter and
        # deadlock the service. Sync callback failures (any exception, including
        # a self-raised CancelledError) are isolated per callback. Async
        # callbacks are awaited concurrently with full isolation: gather with
        # return_exceptions captures a callback's own exception (including
        # CancelledError) so it cannot cancel the Runtime operation, while an
        # external cancellation of this task still propagates through the await.
        coros: list[Awaitable[object]] = []
        for callback in list(self._callbacks):
            try:
                result = callback(record)
            except BaseException:
                continue
            if inspect.isawaitable(result):
                coros.append(result)
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)

    async def _transition_and_emit(
        self,
        session_id: str,
        run_id: str,
        target: RunStatus,
        *,
        final_response: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        # The from-state read, repository transition, and matching durable
        # state-changed event append form one persistence region: it runs under
        # the state lock AND cancellation-deferred, so a task cancel between the
        # transition commit and the event append can never lose the event (which
        # would break the state chain). Subscribers are notified only after the
        # region completes and the lock is released.
        updated, record = await _run_uncancelled(
            self._transition_persist(
                session_id, run_id, target, final_response, reason
            )
        )
        await self._notify(record)
        return updated

    async def _transition_persist(
        self,
        session_id: str,
        run_id: str,
        target: RunStatus,
        final_response: str | None,
        reason: str | None,
    ) -> tuple[RunRecord, EventRecord]:
        async with self._state_lock:
            current = await self._runs.get(run_id)
            from_status = current.status
            updated = await self._runs.transition(
                run_id, target, final_response=final_response
            )
            payload: dict[str, object] = {
                "from": from_status.value,
                "to": target.value,
            }
            if reason is not None:
                payload["reason"] = reason
            record = await self._append(
                session_id,
                run_id,
                "run.state_changed",
                payload,
                RetentionClass.DURABLE,
            )
        return updated, record

    async def _run_guarded(
        self, session_id: str, run_id: str, user_input: str
    ) -> None:
        try:
            await self._run(session_id, run_id, user_input)
        finally:
            # Guarantee a terminal state when the task ends, even if _run was
            # cancelled (including double-cancel) or a failure handler yielded.
            # Convergence is cancellation-deferred so it cannot be interrupted.
            await self._ensure_terminal(session_id, run_id)
            # Drop the in-memory reference only after confirming the run is
            # terminal; a non-terminal residual is left for _shutdown's
            # active-slot sweep / startup reconciliation.
            try:
                current = await self._runs.get(run_id)
            except Exception:
                return
            if current.status in _TERMINAL_STATUSES:
                self._tasks.pop(run_id, None)

    async def _ensure_terminal(
        self, session_id: str, run_id: str
    ) -> None:
        # Never raises: convergence runs cancellation-deferred. A residual
        # non-terminal run (only on an unexpected DB error) is left for the
        # shutdown sweep / reconciliation.
        async def converge() -> None:
            current = await self._runs.get(run_id)
            if current.status not in _TERMINAL_STATUSES:
                await self._handle_cancellation(session_id, run_id)

        try:
            await _run_uncancelled(converge())
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _build_modeling_context(
        self, session_id: str, run_id: str
    ) -> ModelingToolContext | None:
        """Build one trusted, frozen modeling context for an opt-in Run.

        Capability unavailability is represented as ``None`` so a read-only
        Run remains usable and the modeling tool fails closed if called.
        """
        provider = self._modeling_catalog_provider
        if provider is None:
            return None
        try:
            catalog = provider()
            if inspect.isawaitable(catalog):
                catalog = await catalog
            if type(catalog) is not NodeCatalog:
                return None
            binding = await self._changesets.current_binding()
            manifest = None
            bootstrap = None
            try:
                inspection = await self._workspaces.inspect(
                    session_id,
                    None,
                    expected_scene_epoch=binding.scene_epoch,
                )
            except AgentException as exc:
                if exc.error.code != "workspace.not_found":
                    return None
                workspace_id = new_id(IdKind.WORKSPACE)
                bootstrap = WorkspaceBootstrapContext(
                    workspace_id=workspace_id,
                    root_name=f"eee_model_{workspace_id[-8:]}",
                )
            else:
                if inspection.status is not WorkspaceHealth.HEALTHY:
                    return None
                manifest = inspection.target_manifest
                if (
                    manifest.session_id != session_id
                    or manifest.instance_id != binding.instance_id
                    or manifest.scene_epoch != binding.scene_epoch
                ):
                    return None

            async def persist(changeset, decision):
                return await self.propose_changeset_trusted(changeset, decision)

            return ModelingToolContext(
                ModelingProposalCoordinator(
                    ModelingProposalContext(
                        session_id=session_id,
                        run_id=run_id,
                        workspace=manifest,
                        bootstrap=bootstrap,
                        scene_binding=binding,
                        catalog=catalog,
                        quality_profile=houdini_21_minimal_quality_profile(),
                        propose_callback=persist,
                    )
                )
            )
        except Exception:
            return None

    async def _build_runtime_context(
        self, session_id: str, run_id: str
    ) -> RuntimeToolContext:
        """Build one secure context for a Run, including optional modeling."""
        modeling = None
        if self._modeling_catalog_provider is not None:
            modeling = await self._build_modeling_context(session_id, run_id)
        return RuntimeToolContext(
            read_only=self._read_only_provider,
            modeling=modeling,
        )

    async def _run(
        self, session_id: str, run_id: str, user_input: str
    ) -> None:
        runner = self._runner
        assert runner is not None  # opened in RuntimeService.open
        try:
            # Created -> PreparingContext -> Planning, then stream RunnerEvents,
            # then Planning -> Finalizing, durable assistant final message, and
            # Finalizing -> Completed. This ordering is the success contract.
            await self._transition_and_emit(
                session_id, run_id, RunStatus.PREPARING_CONTEXT
            )
            await self._transition_and_emit(
                session_id, run_id, RunStatus.PLANNING
            )
            final_response = ""
            usage: dict[str, int] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
            if isinstance(runner, AgentRunner):
                stream = runner.stream(
                    session_id=session_id,
                    run_id=run_id,
                    user_input=user_input,
                )
            else:
                stream = runner.stream(  # type: ignore[union-attr]
                    session_id=session_id, user_input=user_input
                )
            async for event in stream:
                if isinstance(event, RunnerEvent):
                    await self._emit(
                        session_id,
                        run_id,
                        event.event_type,
                        dict(event.payload),
                        event.retention_class,
                    )
                elif isinstance(event, RunnerCompleted):
                    final_response = event.final_response
                    usage = dict(event.usage)
            await self._transition_and_emit(
                session_id, run_id, RunStatus.FINALIZING
            )
            await self._emit(
                session_id,
                run_id,
                "message.assistant_final",
                {"text": final_response, "usage": dict(usage)},
                RetentionClass.DURABLE,
            )
            await self._transition_and_emit(
                session_id,
                run_id,
                RunStatus.COMPLETED,
                final_response=final_response,
            )
            # Best-effort: rename a placeholder Session to a meaningful title
            # derived from the prompt. Fire-and-forget; a failure or missing
            # model provider leaves the placeholder intact and never affects
            # the run result.
            self._maybe_autotitle_session(session_id, user_input, final_response)
        except Exception:
            # Any failure (a runner exception, or an invalid transition caused
            # by a concurrent stop) is decided by _handle_failure under the state
            # lock. A CancelledError is NOT caught here; it propagates to
            # _run_guarded, whose finally converges the run.
            await self._handle_failure(session_id, run_id)

    def _maybe_autotitle_session(
        self, session_id: str, user_input: str, final_response: str
    ) -> None:
        # Schedule a rename only for placeholder Sessions ("New session" is the
        # auto-create marker) with a configured model provider and no task
        # already running for that Session. Never raises.
        provider = self._title_model_provider
        if provider is None or session_id in self._title_tasks:
            return
        try:
            session = self._sessions.get(session_id)
        except Exception:  # noqa: BLE001 — best-effort scheduling
            return
        if session is None or session.get("title") != "New session":
            return
        task = asyncio.create_task(
            self._autotitle_session_task(
                session_id, user_input, final_response, provider
            )
        )
        self._title_tasks[session_id] = task
        task.add_done_callback(lambda _t, sid=session_id: self._title_tasks.pop(sid, None))

    async def _autotitle_session_task(
        self,
        session_id: str,
        user_input: str,
        final_response: str,
        provider: Callable[[], object],
    ) -> None:
        # Best-effort: any failure (provider down, bad output, rename race) is
        # swallowed so it can never affect the completed run.
        try:
            model = provider()
            title = await generate_session_title(
                model, user_input, final_response=final_response
            )
            if title:
                await self.rename_session(session_id, title)
        except Exception:  # noqa: BLE001 — auto-title must never block the run
            pass

    async def _handle_cancellation(
        self, session_id: str, run_id: str
    ) -> None:
        # Converge to Cancelled, emitting every required transition exactly
        # once. A concurrent move (e.g. a simultaneous stop) is handled by
        # re-reading on an invalid-transition error rather than failing.
        while True:
            current = await self._runs.get(run_id)
            if current.status in _TERMINAL_STATUSES:
                return
            if current.status is RunStatus.STOPPING:
                target = RunStatus.CANCELLED
            elif current.status is RunStatus.STOP_REQUESTED:
                target = RunStatus.STOPPING
            else:
                target = RunStatus.STOP_REQUESTED
            try:
                await self._transition_and_emit(session_id, run_id, target)
            except AgentException as exc:
                if exc.error.code == "runtime.invalid_run_transition":
                    continue
                raise

    async def _handle_failure(
        self, session_id: str, run_id: str
    ) -> None:
        error = _RUNTIME_FAILURE_ERROR
        # Decide failure-vs-stop and persist the full failure bundle (Failed
        # status + model.failed + run.state_changed + run.failed) in one
        # cancellation-deferred critical region, so a task cancel can never leave
        # a terminal Run with a partial event bundle. Subscriber notification
        # happens afterward (best-effort delivery; events are durable).
        records = await _run_uncancelled(
            self._failure_persist(session_id, run_id, error)
        )
        for record in records:
            await self._notify(record)

    async def _failure_persist(
        self, session_id: str, run_id: str, error: AgentError
    ) -> list[EventRecord]:
        records: list[EventRecord] = []
        async with self._state_lock:
            current = await self._runs.get(run_id)
            if current.status in _TERMINAL_STATUSES:
                # Already terminal: nothing to do.
                return records
            if current.status in (
                RunStatus.STOP_REQUESTED,
                RunStatus.STOPPING,
            ):
                # A stop owns the terminal state. Do not emit a contradictory
                # model.failed; the cancellation convergence reaches Cancelled.
                return records
            # Failure owns the terminal state: fail and append every failure
            # event under the lock, so a concurrent stop cannot interleave or
            # observe a half-failed run.
            from_status = current.status
            await self._runs.fail(run_id, error)
            records.append(
                await self._append(
                    session_id,
                    run_id,
                    "model.failed",
                    {"error": error.to_dict()},
                    RetentionClass.DURABLE,
                )
            )
            records.append(
                await self._append(
                    session_id,
                    run_id,
                    "run.state_changed",
                    {
                        "from": from_status.value,
                        "to": RunStatus.FAILED.value,
                        "reason": "runtime_failure",
                    },
                    RetentionClass.DURABLE,
                )
            )
            records.append(
                await self._append(
                    session_id,
                    run_id,
                    "run.failed",
                    {"error": error.to_dict()},
                    RetentionClass.DURABLE,
                )
            )
        return records

    async def _reconcile(self) -> None:
        # Artifact lifecycle recovery is independent from run reconciliation:
        # pending placement/eviction and missing bytes must be made explicit
        # before the service starts exposing runtime state.
        artifact_recoveries = await self._artifacts.reconcile()
        for recovery in artifact_recoveries:
            await self._emit(
                recovery["session_id"],
                recovery["run_id"],
                "artifact.reconciled",
                {
                    "artifact_id": recovery["artifact_id"],
                    "from_state": recovery["from_state"],
                    "state": recovery["state"],
                },
                RetentionClass.DURABLE,
            )
        # Capture the genuine pre-recovery status of every non-terminal run so
        # the emitted run.state_changed records the real "from" state rather
        # than the post-reconciliation Failed. reconcile_interrupted then fails
        # each non-terminal row and clears the active slot atomically.
        rows = await self._database.fetchall(
            "SELECT run_id, session_id, status FROM runs "
            "WHERE status NOT IN ('Completed', 'Cancelled', 'Failed')"
        )
        pre: dict[str, tuple[str, RunStatus]] = {}
        for row in rows:
            pre[row["run_id"]] = (row["session_id"], RunStatus(row["status"]))
        changed = await self._runs.reconcile_interrupted()
        for run in changed:
            session_id, from_status = pre.get(
                run.run_id, (run.session_id, RunStatus.FAILED)
            )
            await self._emit(
                session_id,
                run.run_id,
                "run.state_changed",
                {
                    "from": from_status.value,
                    "to": RunStatus.FAILED.value,
                    "reason": "interrupted",
                },
                RetentionClass.DURABLE,
            )
            await self._emit(
                session_id,
                run.run_id,
                "run.failed",
                {"error": _INTERRUPTED_ERROR.to_dict()},
                RetentionClass.DURABLE,
            )
        await self.recover_changesets_trusted()
