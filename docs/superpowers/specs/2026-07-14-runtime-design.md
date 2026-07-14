# EEE Agent Runtime Design

- Date: 2026-07-14
- Status: approved for implementation planning
- Branch: `feature/runtime`
- Parent milestone: Foundation at `b9ef66f`
- Governing architecture: `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`

## 1. Summary

This milestone adds the first persistent local Runtime without replacing the
existing CLI or exposing a new Houdini write path. It delivers a complete
vertical slice:

```text
WebSocket client
    -> authenticated loopback Runtime server
    -> Session / Run service
       -> application SQLite (sessions, runs, events)
       -> read-only Deep Agents compatibility runner
          -> LangGraph AsyncSqliteSaver (checkpoints SQLite)
```

The result supports multiple durable sessions, one active top-level run per
Runtime, provider event streaming, deterministic event replay, snapshot
fallback after retention gaps, and conversation continuation with
`thread_id = session_id`. It remains safe when Houdini, Phoenix, LangSmith, or
the Panel is unavailable.

The implementation preserves `selftest`, `prompt`, `stdio`, and `versions`.
The new path is additive and starts with `python -m eee_agent.runtime serve`.

## 2. Decisions And Alternatives

### 2.1 Chosen delivery shape

Implement a persistence-to-WebSocket vertical slice. Each implementation task
must leave a working, tested increment and must not create unused records for
later milestones.

Rejected alternatives:

- Persistence-first with the full final-product schema would create many
  unexercised contracts for Workspace, ChangeSet, approval, validation, and
  artifacts before their behavior is known.
- Protocol-first with an in-memory store would require replay, ordering, and
  snapshot behavior to be implemented twice and would make early protocol
  tests prove the wrong durability semantics.
- One monolithic implementation session would make TDD evidence, review, and
  rollback boundaries too coarse.

### 2.2 Execution model

Codex owns design, implementation planning, review, and independent
verification. Claude Code CLI performs implementation tasks with the explicit
model `glm-5.2[1m]`. Each Claude Code invocation receives one approved plan
task, uses TDD, and produces one focused commit. Codex reruns verification and
reviews the diff before the next task begins.

GLM gateway overload (`HTTP 529`, `overloaded_error`, code `1305`) is handled by
waiting and retrying the same task. It must not trigger an implicit model
fallback or parallel duplicate sessions.

## 3. Scope

### 3.1 Included

- Direct, exactly pinned Runtime dependencies and a reproducible `uv.lock`.
- A user-scoped Runtime data directory with a test override.
- Explicit application-database migrations.
- Durable SessionRecord, RunRecord, and EventRecord contracts.
- Global one-active-run enforcement.
- Atomic per-session event sequence allocation.
- Durable event replay and snapshot fallback.
- A separate LangGraph `AsyncSqliteSaver` database.
- A read-only tool registry and checkpointer-aware agent construction.
- A read-only compatibility runner that streams normalized provider events.
- Versioned WebSocket commands, responses, and events.
- Loopback-only serving and bearer-token handshake authentication.
- Runtime discovery, process token, and single-instance locking.
- Graceful stop, force-stop, shutdown, and restart reconciliation.
- Offline unit, integration, restart, concurrency, and WebSocket tests.
- Manual GLM and Houdini read-only smoke procedures.

### 3.2 Deferred

The following commands and records are intentionally unavailable in this
milestone:

- Workspace create, bind, switch, and inspect.
- SceneBinding and scene epoch recovery.
- ChangeSet approval, rejection, execution, receipt, and rollback.
- Secure HoudiniBridge DTOs and main-thread execution.
- Docked `.pypanel` and production Panel WebSocket client.
- Visual policy, capture, visual review, and artifact viewer.
- Strict modeling brief/spec/compiler/validator workflow.
- Session fork, because normalized context cloning is not defined until
  `NormalizeSessionContext` is implemented.
- Full provider-stream resumption after a Runtime process crash.
- Phoenix and LangSmith changes.

Deferred commands return the structured error
`runtime.capability_unavailable`; they are not accepted as no-ops.

### 3.3 Invariants

- Runtime binds only to `127.0.0.1` or `::1`; the first implementation uses
  `127.0.0.1` exclusively.
- Runtime token and HoudiniBridge token remain separate.
- Full token values never enter logs, SQLite, events, traces, or artifacts.
- An event is broadcast only after its database transaction commits.
- `seq` is unique and strictly increasing within one session.
- One Runtime has at most one active top-level Run across all sessions.
- The Runtime agent cannot access Houdini write tools.
- Application SQLite and checkpoint SQLite are never treated as one atomic
  transaction.
- The current CLI remains a supported rollback path.
- Tests never require a live LLM or Houdini unless explicitly marked as manual
  smoke tests.

## 4. Package Layout

Create:

```text
eee_agent/runtime/
  __init__.py
  __main__.py
  paths.py
  models.py
  lock.py
  database.py
  migrations.py
  sessions.py
  runs.py
  events.py
  protocol.py
  auth.py
  checkpoints.py
  agent_runner.py
  service.py
  server.py
```

Responsibilities:

- `paths.py`: Resolve the Runtime home and child paths. Importing it has no
  filesystem side effect.
- `models.py`: Immutable record types, enums, JSON conversion, and legal Run
  transitions.
- `lock.py`: Hold an OS-level exclusive lock for one Runtime home. Use
  `msvcrt.locking` on Windows and `fcntl.flock` on POSIX. The lock is released
  by the OS on process exit.
- `database.py`: Own `aiosqlite` connection setup, PRAGMAs, transactions, and
  closure.
- `migrations.py`: Apply ordered, explicit SQL migrations and reject schemas
  newer than the executable.
- `sessions.py`: Session persistence and lifecycle operations.
- `runs.py`: Run creation, legal transitions, global-active-run ownership, and
  interrupted-run reconciliation.
- `events.py`: Atomic append, replay, retention-floor tracking, snapshot input,
  and canonical payload serialization.
- `protocol.py`: Strict parsing and serialization for protocol envelopes.
- `auth.py`: Token generation, secure comparison, token-file and discovery-file
  handling, and token fingerprinting.
- `checkpoints.py`: Create, set up, and close `AsyncSqliteSaver` against a
  separate database.
- `agent_runner.py`: Adapt the existing Deep Agents graph to Runtime events
  without importing WebSocket or SQLite repositories.
- `service.py`: Coordinate repositories, checkpointer, run tasks, stop signals,
  subscriptions, and snapshots.
- `server.py`: Perform handshake auth, parse commands, send responses, maintain
  bounded client queues, and broadcast committed events.
- `__main__.py`: Parse Runtime CLI arguments and manage startup/shutdown.

Modify:

- `eee_agent/app.py`: Add optional `tools` and `checkpointer` parameters while
  preserving current no-argument behavior.
- `eee_agent/tools/registry.py`: Add `read_only_tools()` with an exact allowlist.
- `pyproject.toml` and `uv.lock`: Directly pin Runtime dependencies.
- `eee_agent/core/versioning.py`: Report new direct Runtime dependencies.
- `README.md` and `CLAUDE.md`: Document the additive Runtime command only after
  the implementation is verified.

## 5. Dependencies

The implementation plan pins these verified versions exactly:

- `langgraph-checkpoint-sqlite==3.1.0`
- `aiosqlite==0.22.1`
- `websockets==15.0.1`

`websockets` is already present transitively at 15.0.1, but Runtime imports it
directly, so it becomes a direct dependency. Version 15.0.1 is retained to avoid
an unrelated dependency-graph upgrade to 16.0 during this milestone.

`langgraph-checkpoint-sqlite` supports sync and async SQLite savers and requires
Python 3.10 or newer. Runtime uses only `AsyncSqliteSaver`.

Checkpoint deserialization is treated as trusted-local-state with defense in
depth. Runtime sets `LANGGRAPH_STRICT_MSGPACK=true` before opening the saver and
tests a real Deep Agents checkpoint round trip. No arbitrary pickle fallback is
enabled by Runtime code.

## 6. Runtime Paths

Default root:

```text
%LOCALAPPDATA%\EEEAgent
```

Tests and explicit local runs may set:

```text
EEE_RUNTIME_HOME=<absolute directory>
```

Relative overrides, an empty override, parent traversal, and a filesystem path
that is not a directory are rejected.

Layout:

```text
config/
state/
  app.sqlite
  checkpoints.sqlite
  runtime.lock
  runtime.json
  runtime.token
artifacts/
logs/
```

This milestone creates only the directories and state files it uses. It does
not create empty model config, artifact, or log records.

## 7. Application Database

### 7.1 Connection policy

Application persistence uses one long-lived `aiosqlite.Connection` owned by the
Runtime service. Repository operations are async. Multi-statement writes use
explicit transactions and one service-level async write lock so cancellation
cannot interleave transaction bodies on the same connection.

On every connection:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

Tests assert these values where SQLite reports them.

### 7.2 Migration policy

Migration v1 is a static SQL script embedded in `migrations.py`. Startup:

1. Opens the database.
2. Creates `schema_migrations` if absent.
3. Reads applied versions.
4. Rejects duplicate or non-contiguous versions.
5. Rejects a database version newer than the executable.
6. Applies missing migrations in individual transactions.
7. Records each version and UTC timestamp only after its transaction succeeds.

Migrations are idempotent at the orchestrator level: a completed version is not
executed again. Migration SQL is not written as a collection of silent
`IF NOT EXISTS` statements that could hide a partially wrong schema.

### 7.3 Schema v1

All timestamps are canonical UTC ISO-8601 strings. JSON is UTF-8 text encoded
with sorted keys and compact separators. Boolean values are SQLite integers
constrained to 0 or 1.

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    replay_floor_seq INTEGER NOT NULL DEFAULT 0
        CHECK (replay_floor_seq >= 0 AND replay_floor_seq <= last_seq)
);

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'Created', 'PreparingContext', 'Planning', 'Finalizing', 'Completed',
        'StopRequested', 'Stopping', 'Cancelled', 'Retrying', 'Failed'
    )),
    user_input TEXT NOT NULL,
    final_response TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    failure_json TEXT,
    model_snapshot_json TEXT NOT NULL
);

CREATE INDEX runs_by_session_created
    ON runs(session_id, created_at, run_id);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq > 0),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    retention_class TEXT NOT NULL
        CHECK (retention_class IN ('durable', 'operational')),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    UNIQUE(session_id, seq)
);

CREATE INDEX events_for_replay ON events(session_id, seq);

CREATE TABLE runtime_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    active_run_id TEXT REFERENCES runs(run_id) ON DELETE SET NULL,
    updated_at TEXT NOT NULL
);
```

`runtime_state` always has exactly one row after migration. Acquiring an active
run and inserting its RunRecord occur in one transaction. A second acquisition
fails with `runtime.run_already_active` and identifies the existing run without
starting another task.

### 7.4 Size limits

Before database writes or broadcasts:

- Session title: 1 to 200 Unicode code points after trimming.
- User input: 1 to 262,144 UTF-8 bytes.
- Final response: at most 1,048,576 UTF-8 bytes.
- One event payload: at most 262,144 encoded bytes.
- One inbound WebSocket message: at most 1,048,576 bytes.

Oversized inputs produce `validation.payload_too_large`; data is not silently
truncated. Tool-result previews may be explicitly summarized before becoming an
event payload, but the summary contract marks them as truncated.

## 8. Domain Models

### 8.1 Session

`SessionStatus` values are `active` and `archived`.

Operations:

- Create: generates a Foundation `ses_` ID and emits `session.created`.
- List: defaults to active sessions and supports an explicit archived filter.
- Rename: validates the title and emits `session.renamed`.
- Archive: requires no active run in the session and emits
  `session.archived`.
- Delete: requires no active run, deletes application records in one cascade,
  deletes the checkpoint thread through `adelete_thread`, and is irreversible.
  The command response confirms the deleted ID. After the delete commits, the
  server sends a non-persisted `session.deleted` control event to current
  subscribers and removes their subscription; no event is inserted into the
  deleted session.
- Fork: returns `runtime.capability_unavailable` in v1.

Archiving does not delete checkpoints or events. Starting a run in an archived
session is rejected with `runtime.session_archived`.

### 8.2 Run

Implemented states:

```text
Created
PreparingContext
Planning
Finalizing
Completed
StopRequested
Stopping
Cancelled
Retrying
Failed
```

Legal transitions:

```text
Created -> PreparingContext | StopRequested | Failed
PreparingContext -> Planning | StopRequested | Retrying | Failed
Planning -> Finalizing | StopRequested | Retrying | Failed
Retrying -> PreparingContext | Planning | StopRequested | Failed
Finalizing -> Completed | StopRequested | Failed
StopRequested -> Stopping
Stopping -> Cancelled | Failed
```

`Completed`, `Cancelled`, and `Failed` are terminal. Every transition is checked
by one pure function before persistence. Illegal transitions raise
`runtime.invalid_run_transition`; they never update the row or emit an event.

`run.state_changed` is durable and includes `from`, `to`, and a structured
reason when one exists.

At process startup, a Runtime holding the exclusive lock reconciles any
non-terminal RunRecord left by a previous process:

1. Set status to `Failed`.
2. Set `finished_at`.
3. Store `AgentError(code='runtime.interrupted', ...)`.
4. Clear `runtime_state.active_run_id`.
5. Append a durable `run.state_changed` and `run.failed` event.

This is safe in Runtime v1 because the runner has no scene-write tools. It does
not claim to resume an interrupted provider HTTP stream.

## 9. Events, Sequence, Retention, And Snapshot

### 9.1 Runtime event envelope

Runtime wraps the Foundation DomainEvent with routing fields:

```json
{
  "protocol": "eee.runtime/1",
  "kind": "event",
  "event_id": "evt_...",
  "session_id": "ses_...",
  "run_id": "run_...",
  "seq": 184,
  "timestamp": "2026-07-14T02:30:00+00:00",
  "type": "run.state_changed",
  "payload": {},
  "schema_version": 1
}
```

`run_id` is nullable only for session-level events.

### 9.2 Atomic append

Under the database write lock:

1. `BEGIN IMMEDIATE`.
2. Validate the session and optional run relationship.
3. Increment `sessions.last_seq` by one.
4. Read the resulting value.
5. Insert EventRecord with that exact sequence.
6. Commit.
7. Return the immutable envelope for broadcast.

Rollback leaves both `last_seq` and the events table unchanged. Tests run
concurrent append coroutines and assert no duplicates, gaps, or commit-order
inversions.

### 9.3 Event classes

Durable:

- Session create, rename, and archive.
- User message and final assistant message.
- Run creation and every state transition.
- Structured warning and error.
- Model snapshot and final usage summary.

Operational:

- Reasoning delta.
- Text delta while the final message is not yet committed.
- Tool-call argument delta.
- Intermediate todo, tool, and usage updates.

Run completion persists one durable final response and usage summary before old
operational events become eligible for deletion.

### 9.4 Replay and retention floor

`events.replay` takes `session_id`, `after_seq`, and `limit`. `after_seq` is a
non-negative integer; `limit` is 1 through 1000. Results are ascending by seq.

Runtime v1 exposes a deterministic retention operation used by tests and later
maintenance. It deletes eligible operational events through a cutoff sequence
only after their run is terminal and older than the configured recovery grace
period. In the same transaction it advances `sessions.replay_floor_seq` to at
least the greatest deleted sequence.

Reconnection:

- If `last_seq >= replay_floor_seq`, replay committed events with
  `seq > last_seq`.
- If `last_seq < replay_floor_seq`, send a non-persisted `session.snapshot`
  event first, then replay committed events with
  `seq > replay_floor_seq`.

The snapshot carries `snapshot_seq = sessions.last_seq`. Events committed after
the snapshot query are replayed with `seq > snapshot_seq`, preventing a gap
between snapshot and live subscription.

### 9.5 Snapshot content

A snapshot contains:

- Session metadata and sequence bounds.
- The newest 100 RunRecords in chronological order, plus
  `has_earlier_runs` and `earliest_included_run_id` pagination metadata.
- Active run details when applicable.
- User input, final response, terminal error, and model snapshot for each
  included run.
- Runtime protocol and dependency version report.

Reasoning and provider-private blocks never enter a snapshot.

## 10. LangGraph Checkpoints

Checkpoint data lives only in `state/checkpoints.sqlite`.

Runtime opens one `AsyncSqliteSaver`, calls `await setup()`, and keeps it alive
for the service lifetime. The saver is passed to the existing Deep Agents graph
through the verified `create_deep_agent(..., checkpointer=...)` parameter.

Every run uses:

```python
config = {
    "configurable": {"thread_id": session_id},
    "recursion_limit": recursion_limit(),
}
```

The input to a new run contains only the new user message. Prior messages come
from the session checkpoint. `run_id` remains application metadata and is not
used as the checkpoint thread.

Deleting a session invokes `await checkpointer.adelete_thread(session_id)` after
the application delete commits. If checkpoint deletion fails, the command
returns a structured partial-cleanup error with `scene_may_have_changed=False`;
the already-deleted application session is not recreated. Startup maintenance
may safely retry orphan checkpoint deletion from a cleanup record in a later
milestone. Runtime v1 tests the successful path and explicit error reporting.

## 11. Read-Only Agent Runner

### 11.1 Tool boundary

Add `read_only_tools()` with exactly:

- `hou_status`
- `find_nodes`
- `describe_node_type`
- `geometry_stats`
- `validate_geometry`
- `work_status`
- `anchor_graph`

The list explicitly excludes scene reset/save, node mutation, parameter
mutation, connections, deletion, VEX writes, construction helpers, and export.

Tests compare tool names and inspect the compiled graph to prove that write tool
names and the implicit Deep Agents `task` tool are absent.

### 11.2 Agent construction compatibility

Change the public helper to:

```python
def build_agent(
    *,
    tools: Sequence[BaseTool] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    ...
```

`tools=None` preserves `all_tools()`. `checkpointer=None` preserves current CLI
behavior. Runtime passes the read-only list and AsyncSqliteSaver. Existing
Foundation harness configuration and middleware ordering remain intact.

### 11.3 Streaming adapter

`AgentRunner` exposes an async stream of Foundation provider-neutral events plus
one terminal result. It owns no database and no WebSocket connection.

Mapping:

- `ReasoningDelta` -> operational `model.reasoning_delta`
- `TextDelta` -> operational `model.text_delta`
- `ToolCallStarted` -> operational `tool.started`
- `ToolCallArgumentsDelta` -> operational `tool.arguments_delta`
- `ToolCallCompleted` -> operational `tool.completed`
- `UsageUpdated` -> operational `model.usage_updated`
- successful stream end -> durable `model.completed`
- exception -> durable `model.failed` with AgentError

The service accumulates final answer text independently of transient delta
retention, stores it on RunRecord, emits `message.assistant_final`, then marks
the run Completed.

If Houdini RPC is unavailable, a read tool returns its current structured bridge
failure. The runner never converts that condition into permission to use a write
tool.

## 12. Runtime Service

Startup order:

1. Resolve and validate Runtime paths.
2. Acquire the exclusive Runtime-home lock.
3. Create used directories.
4. Open and migrate application SQLite.
5. Reconcile interrupted runs.
6. Open and set up AsyncSqliteSaver.
7. Generate a process token and write token/discovery files atomically.
8. Start the loopback WebSocket server.

Shutdown order:

1. Stop accepting WebSocket connections.
2. Request cancellation of an active read-only run.
3. Wait up to the configured graceful timeout.
4. Force-cancel the task if needed and persist its terminal state.
5. Drain committed event broadcasts within a bounded timeout.
6. Close clients, checkpointer, and app database.
7. Delete discovery/token files only if they belong to this PID and process
   nonce.
8. Release the Runtime-home lock.

The service has one in-memory task reference for the active run, while
`runtime_state.active_run_id` remains the persistent authority.

## 13. Authentication And Discovery

At startup Runtime generates 32 random bytes with `secrets.token_urlsafe(32)`.
It writes the full token only to `state/runtime.token` using atomic replace and
mode `0o600`. On Windows the file inherits the current user's LocalAppData ACL;
the implementation also rejects a Runtime home that is not writable by the
current user.

`state/runtime.json` contains:

```json
{
  "protocol": "eee.runtime/1",
  "host": "127.0.0.1",
  "port": 49152,
  "pid": 1234,
  "process_nonce": "...",
  "token_file": "runtime.token",
  "token_fingerprint": "first-12-hex-chars-of-sha256",
  "started_at": "2026-07-14T02:30:00+00:00"
}
```

The full token is never present in `runtime.json`. Discovery and token files are
written through temporary files in the same directory followed by
`os.replace()`.

WebSocket handshake requires:

```text
Authorization: Bearer <token>
```

Authentication uses `hmac.compare_digest`. Missing or invalid credentials
return HTTP 401 during `process_request`. The request path and headers are not
logged. URL query tokens are unsupported.

## 14. WebSocket Protocol

### 14.1 Version

Protocol identifier is exactly `eee.runtime/1`. A different major identifier
returns `protocol.incompatible_version` and closes with a policy-error code.
Unknown event types within major version 1 may be ignored by clients; unknown
commands are never ignored by the server.

### 14.2 Command

```json
{
  "protocol": "eee.runtime/1",
  "kind": "command",
  "request_id": "client-opaque-id",
  "type": "session.create",
  "payload": {}
}
```

`request_id` is a non-empty string of at most 128 characters and is echoed in
the response. It is not a durable entity ID.

### 14.3 Response

Success:

```json
{
  "protocol": "eee.runtime/1",
  "kind": "response",
  "request_id": "client-opaque-id",
  "ok": true,
  "result": {}
}
```

Failure:

```json
{
  "protocol": "eee.runtime/1",
  "kind": "response",
  "request_id": "client-opaque-id",
  "ok": false,
  "error": {
    "code": "protocol.invalid_envelope",
    "category": "protocol",
    "message_for_user": "The Runtime command is invalid.",
    "retryable": false,
    "requires_user_action": false,
    "scene_may_have_changed": false,
    "suggested_actions": [],
    "cause_chain": [],
    "technical_detail_ref": null
  }
}
```

Every accepted command receives exactly one response. Persisted events caused
by that command are separate event envelopes.

### 14.4 Implemented commands

- `runtime.ping`
- `session.list`
- `session.create`
- `session.rename`
- `session.archive`
- `session.delete`
- `session.subscribe`
- `session.snapshot`
- `events.replay`
- `run.start`
- `run.stop`
- `run.force_stop`

`session.subscribe` accepts `session_id` and `last_seq`. It installs the
subscription only after snapshot/replay has established a consistent boundary.

`run.stop` requests cooperative cancellation. `run.force_stop` cancels the
provider task immediately. Because v1 has no dispatched Houdini write RPC, both
can deterministically end in Cancelled after task cleanup.

### 14.5 Connection behavior

- Server compression is disabled for predictable local resource use.
- Ping interval is 20 seconds and ping timeout is 20 seconds.
- Inbound max size is 1 MiB.
- Each connection has an outbound queue of 256 envelopes.
- A full outbound queue closes that connection with
  `runtime.slow_consumer`; committed events remain replayable.
- One connection may subscribe to multiple sessions.
- Disconnecting never cancels a Run.

## 15. Error Handling

External errors are converted to Foundation `AgentError`. Runtime code does not
send raw traceback text over WebSocket.

Required codes include:

- `runtime.already_running`
- `runtime.run_already_active`
- `runtime.session_archived`
- `runtime.session_not_found`
- `runtime.run_not_found`
- `runtime.invalid_run_transition`
- `runtime.interrupted`
- `runtime.capability_unavailable`
- `runtime.slow_consumer`
- `runtime.checkpoint_cleanup_failed`
- `protocol.invalid_json`
- `protocol.invalid_envelope`
- `protocol.incompatible_version`
- `protocol.unknown_command`
- `protocol.unauthorized`
- `validation.payload_too_large`

Unexpected internal exceptions become `internal.runtime_failure`, set the
affected run to Failed when possible, and preserve the original exception only
in local logs. Artifact-backed traceback storage is deferred to the Artifact
milestone.

## 16. Test Strategy

### 16.1 Dependency and path tests

- Exact direct versions are installed and locked.
- Runtime imports no undeclared direct dependency.
- Default path uses LocalAppData.
- Absolute override works.
- Relative and unsafe overrides fail.
- Importing Runtime path modules creates no files.

### 16.2 Migration and database tests

- Empty database reaches schema v1.
- Reopening does not rerun v1.
- A newer schema is rejected.
- A failed migration rolls back and is not recorded.
- Required PRAGMAs are active.
- Foreign keys and cascade behavior are real.
- Runtime singleton row exists.

### 16.3 Session and run tests

- Create, list, rename, archive, delete, and archived-run rejection.
- Invalid IDs, titles, and payload sizes fail before writes.
- Only one top-level run can be active globally under concurrent starts.
- Every legal transition passes; every illegal transition fails without an
  event.
- Reopen reconciliation marks an orphan active run Failed and clears ownership.
- Terminal runs cannot transition again.

### 16.4 Event tests

- Concurrent append yields unique, contiguous, increasing sequences.
- Rollback does not consume sequence numbers.
- Payload is an immutable JSON snapshot and round-trips exactly.
- Replay ordering, pagination, and bounds are correct.
- Retention advances the floor atomically.
- A replay gap sends one snapshot before increments.
- A non-gap sends no snapshot.
- Events are broadcast only after commit.

### 16.5 Checkpoint and runner tests

- `AsyncSqliteSaver` creates and reopens its database.
- A real minimal graph checkpoint survives process-level reopen.
- `thread_id` is the Session ID, not the Run ID.
- Read-only tool names are exact and write tools are absent.
- Existing no-argument `build_agent()` retains the old tool surface.
- Fake provider streams map to Runtime events without SQLite or WebSocket mocks
  inside AgentRunner.
- Final response remains durable after operational deltas are pruned.
- Cancellation produces Cancelled and a durable final state event.

### 16.6 WebSocket integration tests

Use a real loopback server on an ephemeral port and the real `websockets`
client:

- Missing, malformed, and wrong bearer tokens receive 401.
- Authenticated ping succeeds.
- Invalid JSON and invalid envelopes receive structured responses.
- Major mismatch is rejected.
- Session commands persist data.
- Subscribe performs replay before live events.
- Disconnect/reconnect with `last_seq` loses and duplicates no event.
- Slow-consumer closure leaves events recoverable.
- A non-loopback host is rejected before server creation.

### 16.7 Regression tests

- Entire Foundation suite remains green.
- `uv lock --check` passes.
- `compileall` passes.
- `versions` includes Runtime dependencies.
- Existing CLI subcommands remain registered.
- The environment probe remains side-effect-free and LF-safe.

### 16.8 Manual smoke tests

GLM smoke:

1. Start Runtime with configured GLM-5.2 credentials.
2. Create a Session over WebSocket.
3. Start a pure-text read-only Run.
4. Observe streamed events and Completed.
5. Restart Runtime and ask a follow-up in the same Session.
6. Confirm the answer uses persisted conversation context.

Houdini read-only smoke:

1. Start the existing Houdini RPC bridge.
2. Start Runtime.
3. Ask for current HIP/version and node inspection.
4. Confirm only allowlisted read tools execute.
5. Compare the Houdini scene before and after and confirm no graph mutation,
   save, export, or file write occurred.

Manual smokes are reported separately and never replace offline test evidence.

## 17. Acceptance Criteria

Runtime v1 is accepted only when all of the following are demonstrated:

1. Multiple Sessions survive Runtime restart.
2. Session runs share `thread_id = session_id` and have distinct Run IDs.
3. App state and checkpoints use separate SQLite files.
4. One active top-level Run is enforced atomically.
5. Event sequences are per-session, unique, contiguous for committed appends,
   and strictly increasing.
6. Authenticated WebSocket streaming, replay, and snapshot fallback work over a
   real loopback socket.
7. Unauthorized and incompatible clients are rejected without leaking token
   values.
8. Panel/client disconnect does not cancel a Run.
9. Runtime restart reconciles an interrupted read-only Run without claiming
   provider-stream resumption.
10. Runtime graph exposes no Houdini write tool and no implicit general-purpose
    subagent.
11. Existing CLI behavior remains available.
12. Full offline suite, lock check, compileall, and version report pass freshly.
13. No secret, token, `.env`, Runtime database, or machine-local Runtime file is
    tracked by Git.
14. GLM implementation commits contain only the files authorized by their plan
    task and pass independent Codex review.

## 18. Delivery Sequence

The implementation plan must order work so each step is independently testable:

1. Dependencies and Runtime paths.
2. Domain records and state transitions.
3. Database connection and migration v1.
4. Session repository.
5. Run repository and one-active-run ownership.
6. EventStore sequence, replay, and retention.
7. AsyncSqliteSaver lifecycle.
8. Read-only tool boundary and agent construction seam.
9. AgentRunner event mapping and cancellation.
10. Runtime service orchestration and restart reconciliation.
11. Protocol contracts and authentication/discovery.
12. WebSocket server, subscriptions, replay, and backpressure.
13. Runtime CLI, full integration tests, documentation, and manual smoke.

Secure HoudiniBridge work starts only after this Runtime slice reaches its
acceptance criteria.

## 19. Authoritative References

- Master architecture:
  `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`
- Foundation plan:
  `docs/superpowers/plans/2026-07-13-foundation.md`
- Foundation handoff:
  `docs/handoffs/2026-07-13-foundation-migration.md`
- LangGraph checkpoint reference:
  https://langchain-ai.github.io/langgraph/reference/checkpoints/
- `langgraph-checkpoint-sqlite` release and security guidance:
  https://pypi.org/project/langgraph-checkpoint-sqlite/3.1.0/
- `aiosqlite` release:
  https://pypi.org/project/aiosqlite/0.22.1/
- websockets asyncio server API:
  https://websockets.readthedocs.io/en/15.0/reference/asyncio/server.html
- websockets 15.0.1 release:
  https://pypi.org/project/websockets/15.0.1/
