# EEE Agent Runtime Migration Handoff

## Purpose

This is the repository-owned handoff for continuing the persistent Runtime
milestone on another computer. Chat transcripts and local Codex/Claude sessions
are not the source of truth. Resume from the pushed `feature/runtime` branch,
the approved design, the implementation plan, and this document.

Generated: 2026-07-14 (Asia/Shanghai)

## Repository And Branch State

- Repository: `https://github.com/eee1723/eee-agent.git`
- Runtime branch: `feature/runtime`
- Base branch: `main` at merge base `357bb0b`
- Accepted implementation HEAD before this handoff documentation commit:
  `95cb4513211613cefa85eba0c814424a2ddd2227`
- Runtime worktree on the source computer:
  `E:\eee-agent\.worktrees\runtime`
- Runtime implementation is not merged into `main`.
- Tasks 1-9 are complete and independently accepted by Codex.
- Tasks 10-13 have not started.
- The worktree was clean before preparing this handoff.

The handoff documentation commit is intentionally newer than `95cb451`. On the
new computer, use the current remote tip of `origin/feature/runtime`; treat
`95cb451` as the immutable code baseline that completed Task 9.

## Sources Of Truth

Read these files in order before changing code:

1. `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`
2. `docs/superpowers/specs/2026-07-14-runtime-design.md`
3. `docs/superpowers/plans/2026-07-14-runtime.md`
4. `docs/handoffs/2026-07-14-runtime-migration.md`
5. `docs/handoffs/2026-07-13-foundation-migration.md` for Foundation history

The design is approved. Do not restart architecture discovery or silently
broaden the Runtime milestone. Continue the plan one task at a time, with Claude
Code CLI using `glm-5.2[1m]` for implementation and Codex performing planning,
review, and independent verification after every focused commit.

## Completed Runtime Work

### Task 1: Dependencies And Runtime Paths

- `bf917f3 build: add persistent runtime dependencies`
- `a56d478 fix: reject unsafe runtime home overrides`
- Exact direct pins: `aiosqlite==0.22.1`,
  `langgraph-checkpoint-sqlite==3.1.0`, `websockets==15.0.1`.
- User-scoped Runtime paths, absolute override validation, parent-traversal
  rejection, side-effect-free resolution, and ignored local state.

### Task 2: Records And State Machine

- `abd63c2 feat: define runtime records and state machine`
- Immutable SessionRecord, RunRecord, EventRecord, enums, strict JSON snapshots,
  canonical serialization, and the complete legal Run transition graph.

### Task 3: Application Database

- `e5e3f8b feat: add runtime application database`
- `ba27930 fix: roll back failed database commits`
- Explicit migration v1, WAL/foreign keys/busy timeout/NORMAL synchronous mode,
  serialized write transactions, rollback on Exception/BaseException/cancel,
  and rollback when COMMIT itself fails.

### Task 4: SessionRepository

- `1277cdb feat: persist runtime sessions`
- `88b6324 fix: return transaction-local session snapshots`
- Create/get/list/rename/archive/delete, active-run guards, cascade deletion,
  strict title/ID validation, and transaction-local return snapshots.

### Task 5: RunRepository

- `5fabb54 feat: persist runtime run lifecycle`
- `fa166eb fix: snapshot run model before awaiting`
- Atomic global active-run ownership, legal transitions, timestamps, failures,
  restart reconciliation, immutable model/error JSON, and no repository events.

### Task 6: EventStore

- `f7c7aab feat: add atomic runtime event store`
- `72769ec fix: scope active run in session snapshots`
- Atomic per-session sequence allocation, replay, retention floor, operational
  pruning, snapshot data, newest-100 runs, cross-session active-run isolation,
  and transaction-consistent bounds.

### Task 7: LangGraph Checkpoints

- `1021a93 feat: persist langgraph runtime checkpoints`
- Separate `checkpoints.sqlite`, strict msgpack setup, real StateGraph persistence
  across reopen, per-session thread isolation/deletion, and robust async cleanup.

### Task 8: Read-Only Agent Boundary

- `41483ea feat: add read-only runtime agent boundary`
- Exact Houdini read-only allowlist: `hou_status`, `find_nodes`,
  `describe_node_type`, `geometry_stats`, `validate_geometry`, `work_status`,
  `anchor_graph`.
- `build_agent(*, tools=..., checkpointer=...)` preserves no-argument CLI
  behavior and injects the Runtime checkpointer without owning it.
- No Houdini write tool and no implicit `task` tool are exposed by Runtime.

Deep Agents still contributes `ls`, `read_file`, `write_file`, `edit_file`,
`glob`, `grep`, `write_todos`, and `execute`. With the current `StateBackend`,
file tools operate only on checkpointed agent-state files. `execute` cannot run
host commands because StateBackend does not implement `SandboxBackendProtocol`;
it returns an execution-unavailable tool error. Deep Agents can exclude built-in
tools through `HarnessProfile.excluded_tools`, but doing that globally was not
part of Task 8 and could change the existing CLI. Revisit only through an
explicit reviewed design change if the product later requires exactly seven
total compiled tools.

### Task 9: Provider-Neutral AgentRunner

- `434db84 feat: stream provider-neutral runtime events`
- `95cb451 fix: propagate agent stream cleanup failures`
- Maps reasoning, text, tool, usage, and completion output without owning a DB,
  WebSocket, RunRecord, or checkpoint lifecycle.
- Uses `thread_id=session_id`, accumulates final response/usage, caps tool-result
  previews, emits one successful terminal result, and propagates graph errors.
- Cancellation, early consumer close, graph failures, and cleanup failures have
  reviewed exception priority. A normal cleanup failure cannot be reported as
  `model.completed`/RunnerCompleted.

## Accepted Verification Baseline

Fresh verification at `95cb451` on Windows/Python 3.11:

```text
Task 9 cleanup-priority tests: 3 passed
Task 9 suite:                  30 passed
Runner/provider/CLI focused:   34 passed
Task 8-9 boundary:             61 passed
Runtime focused:              493 passed
Full offline suite:           866 passed
Skipped/xfailed:              0
uv lock --check:              exit 0, 69 packages
compileall eee_agent tests:   exit 0
git diff --check:             exit 0
```

No automated test invoked a live LLM or Houdini. Manual GLM and Houdini smoke
tests remain deferred until the complete Runtime vertical slice exists.

## Next Task: Task 10 RuntimeService

Task 10 is the immediate next implementation task. It creates only:

- `eee_agent/runtime/service.py`
- `tests/runtime/test_service.py`

Do not start Task 11 in the same implementation session. Key review gates:

1. Open in order: directories, app DB, repositories/EventStore, interrupted-run
   reconciliation/events, CheckpointManager, RunnerFactory. Close in reverse.
2. `start_run` freezes one `runtime_version_report`, atomically acquires a run,
   commits `run.created`, creates exactly one task, and returns immediately.
3. Successful event order must retain Runner events, then Planning -> Finalizing,
   durable assistant final message, and Finalizing -> Completed.
4. `wait_for_run` must shield the active task so a disconnected/cancelled waiter
   does not cancel the actual run.
5. Stop/cancel must persist StopRequested -> Stopping -> Cancelled and clear the
   global active slot without illegal duplicate terminal transitions.
6. Runner failures become `internal.runtime_failure` at the service boundary;
   raw traceback text is not persisted or sent.
7. Subscribers receive only committed EventRecords. Sync/async callbacks are
   supported; one failure cannot roll back or block delivery to others.
8. Startup reconciliation emits durable interrupted-run events once and is
   idempotent on the next reopen.
9. Session application deletion commits before checkpoint thread deletion. A
   checkpoint cleanup failure returns `runtime.checkpoint_cleanup_failed` and
   never recreates the already-deleted session.
10. Use real SQLite/EventStore/CheckpointManager with a fake AgentRunner. No live
    LLM, Houdini, WebSocket, or test-only switch in production code.

The authoritative detailed steps and commit command are in Task 10 of
`docs/superpowers/plans/2026-07-14-runtime.md`.

## Remaining Delivery Sequence

- Task 10: RuntimeService orchestration and restart recovery.
- Task 11: strict protocol, token/discovery identity, exclusive Runtime lock.
- Task 12: authenticated loopback WebSocket server, subscriptions/backpressure.
- Task 13: Runtime CLI, process-level restart E2E, public exports, README/CLAUDE,
  secret scan, final offline verification, then manual smoke procedures.

Do not merge to `main` before Tasks 10-13 and final acceptance are complete.

## New Computer Restore Procedure

Use Python 3.11. Do not copy `.venv`, `.worktrees`, Runtime SQLite files, tokens,
or discovery files between computers.

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
Set-Location E:\eee-agent
git fetch --all --prune
git worktree add -b feature/runtime `
  E:\eee-agent\.worktrees\runtime origin/feature/runtime
Set-Location E:\eee-agent\.worktrees\runtime
uv sync --frozen --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent tests
git status --short --branch
```

Expected before new development:

- `uv lock --check` exits 0.
- Full suite reports at least 866 passing tests, with no failures.
- Compileall exits 0.
- Branch is `feature/runtime` and the worktree is clean.

If the local branch already exists after cloning, use:

```powershell
git worktree add E:\eee-agent\.worktrees\runtime feature/runtime
```

## Secrets And Machine-Local State

The following are intentionally not transferred by Git:

- `.env`
- `.venv/`
- `.worktrees/`
- Runtime `app.sqlite` / `checkpoints.sqlite`
- `runtime.token`, `runtime.json`, `runtime.lock`
- logs and machine-local Runtime state

Recreate `.env` from `.env.example` and transfer real credentials only through a
password manager or another encrypted channel. Never paste or commit API keys.
The offline Task 10 tests require no provider credentials.

## Resume Prompt

Use this in the first new-computer development conversation:

```text
Resume the persistent Runtime milestone from origin/feature/runtime. Tasks 1-9
are complete and Codex-accepted; implementation baseline is 95cb451 and the
remote branch includes the later migration handoff documentation commit.

Read, in order:
1. docs/superpowers/specs/2026-07-14-runtime-design.md
2. docs/superpowers/plans/2026-07-14-runtime.md
3. docs/handoffs/2026-07-14-runtime-migration.md

Run uv sync --frozen --extra eval --python 3.11, uv lock --check, the full pytest
suite (expect at least 866 passed), compileall, and git status. Do not change code
until the baseline is clean. Then execute only Task 10 with TDD using Claude Code
CLI model glm-5.2[1m]. Codex owns planning, review, and independent verification.
Do not start Task 11 until Task 10 has a focused commit and Codex acceptance.
```

## Git Safety Notes

- Push `feature/runtime`; do not force-push.
- Do not delete or clean other worktrees as part of this migration.
- Do not merge the incomplete Runtime branch into `main`.
- Existing `feature/foundation`, `feature/houdini-knowledge-graph`, and
  `wip/pre-migration-main` histories are separate and must not be rewritten.
- A normal Git push transfers committed history only, never ignored credentials
  or local Runtime state.
