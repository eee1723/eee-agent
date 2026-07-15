# EEE Agent Runtime Migration Handoff - 2026-07-15

## Purpose

This is the current repository-owned handoff for continuing the persistent
Runtime milestone on another computer. It supersedes the progress snapshot and
resume prompt in `2026-07-14-runtime-migration.md`. The older handoff remains the
detailed history for Tasks 1-9.

Generated: 2026-07-15 (Asia/Shanghai)

## Repository And Branch State

- Repository: `https://github.com/eee1723/eee-agent.git`
- Development branch: `feature/runtime`
- Branch must remain separate from `main` until the Runtime branch is pushed and
  the final integration decision is made.
- Task 10 accepted tip: `5b82082`.
- Task 11 accepted tip: `64fa688`.
- Task 12 accepted tip: `724a8fb` (the replay-boundary fix; the `025ac12`
  work-in-progress defect it resolved is documented in the Task 12 section
  below as historical detail).
- Task 13 accepted tip: `c8e6af1` (implementation `3b69532` plus the reviewed
  graceful-timeout/cleanup-order follow-up).
- Task 14 is in progress. The accepted branch has been pushed; clean restore
  and manual Runtime/Houdini acceptance remain. The detailed gate is in
  `docs/superpowers/plans/2026-07-15-runtime-next-milestones.md`.
- Task 15 read-only Bridge design is approved and recorded in
  `docs/superpowers/specs/2026-07-15-secure-houdini-bridge-readonly-design.md`;
  its executable plan is
  `docs/superpowers/plans/2026-07-15-secure-houdini-bridge-readonly.md`.
  No implementation has started and no Claude prompt has been issued.
- Task 15-A strict DTO/error contracts are Codex-accepted at `4c22d45`.
  Focused contract tests: 97 passed; Task 15-A regression slice: 261 passed;
  fresh full offline suite: 1208 passed. No transport, queue, Houdini-side
  adapter, UI, or scene-write code has started.
- The latest local tip is `4c22d45`; it is ready to push after this status
  update.

Do not merge this branch to `main` until the three accepted commits are pushed
and the final integration decision is made. Manual GLM/Houdini acceptance is
separate from the offline test gate and remains pending.

## Sources Of Truth

Read these files in order on the new computer:

1. `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`
2. `docs/superpowers/specs/2026-07-14-runtime-design.md`
3. `docs/superpowers/plans/2026-07-14-runtime.md`
4. `docs/handoffs/2026-07-15-runtime-migration.md`
5. `docs/handoffs/2026-07-14-runtime-migration.md` for Tasks 1-9 history
6. `docs/handoffs/2026-07-13-foundation-migration.md` for Foundation history

The approved design has not changed. Continue one plan task at a time. Claude
Code CLI with explicit model `glm-5.2[1m]` performs implementation; Codex owns
the plan, diff review, adversarial diagnostics, and independent acceptance.

## Milestone Snapshot

| Task | State | Tip |
| --- | --- | --- |
| 1-9 | Complete, Codex accepted | See `2026-07-14-runtime-migration.md` |
| 10 RuntimeService | Complete, Codex accepted | `5b82082` |
| 11 Protocol/Auth/RuntimeLock | Complete, Codex accepted | `64fa688` |
| 12 WebSocket server | Complete, Codex accepted | `724a8fb` |
| 13 CLI/restart E2E/docs/final verification | Complete, Codex accepted | `c8e6af1` |
| 14 Runtime v1 external acceptance and delivery | In progress: pushed, restore/smokes pending | See next-milestones plan |

## Task 10: Accepted RuntimeService

Authorized files:

- `eee_agent/runtime/service.py`
- `tests/runtime/test_service.py`

Implementation and review commits:

- `feffc5f feat: orchestrate persistent runtime runs`
- `1923797 fix: harden runtime service orchestration`
- `4939adc fix: make runtime orchestration cancellation-safe`
- `34ee40c fix: preserve runtime lifecycle under cancellation`
- `5b82082 fix: preserve runtime event chains under cancellation`

Accepted behavior includes:

- Ordered application DB, repository, checkpoint, runner, and shutdown
  lifecycles.
- Per-Run version snapshots, global active-Run ownership, exactly one Agent
  execution task, and shielded waiters.
- Complete success, stop, cancellation, failure, and interrupted-restart event
  sequences.
- Cancellation-deferred persistence regions so repository state and durable
  events do not split under single or repeated task cancellation.
- Committed-event callbacks with sync/async exception isolation.
- Session delete followed by checkpoint cleanup with structured partial-cleanup
  failure.

Independent acceptance at `5b82082`:

```text
Task 10 + RunRepository + EventStore focused: 232 passed
Runtime suite:                              538 passed
Full offline suite:                         912 passed, 1 skipped
uv lock --check:                            exit 0
compileall eee_agent tests:                 exit 0
git diff --check:                           exit 0
```

The skipped test was the machine-dependent WSL environment probe.

## Task 11: Accepted Protocol, Identity, And Lock

Authorized files:

- `eee_agent/runtime/protocol.py`
- `eee_agent/runtime/auth.py`
- `eee_agent/runtime/lock.py`
- `tests/runtime/test_protocol.py`
- `tests/runtime/test_auth.py`
- `tests/runtime/test_lock.py`

Implementation and review commits:

- `c463cc2 feat: secure runtime protocol and identity`
- `64fa688 fix: enforce strict runtime envelopes`

Accepted behavior includes:

- Exact `eee.runtime/1` commands, deferred commands, strict five-field parsing,
  1 MiB input limit, duplicate-key rejection at every JSON depth, structured
  errors, deep-snapshot envelopes, and deterministic compact UTF-8 encoding.
- A 32-byte-strength bearer token, fingerprint-only discovery, same-directory
  atomic replacement, guarded PID/nonce cleanup, and constant-time validation.
- A real cross-process Windows byte-range lock and POSIX `flock` implementation
  with contention mapped to `runtime.already_running`.

Independent acceptance at `64fa688`:

```text
Task 11 focused:       105 passed
Runtime suite:         643 passed
Full offline suite:    1017 passed, 1 skipped
uv lock --check:       exit 0
compileall:            exit 0
git diff --check:      exit 0
```

Non-blocking note: `parse_command` still annotates `bytearray` in its parameter
union even though runtime behavior correctly rejects it. This annotation cleanup
does not affect the wire contract and can be handled only when a later approved
file scope includes `protocol.py`.

## Task 12: Accepted (tip `724a8fb`)

The replay-boundary defect recorded below was fixed and independently accepted
at `724a8fb fix: advance websocket replay boundaries` (the only files touched
were `eee_agent/runtime/server.py` and `tests/runtime/test_server.py`). The
defect description is retained as historical detail.

Authorized files:

- `eee_agent/runtime/server.py`
- `tests/runtime/test_server.py`

Commits:

- `812e4af feat: serve authenticated runtime websocket`
- `025ac12 fix: make websocket recovery gap-free`
- `724a8fb fix: advance websocket replay boundaries` (accepted)

Implemented and currently green behavior includes:

- Exact `127.0.0.1` binding on an ephemeral or configured port.
- HTTP 401 bearer handshake rejection without logging secrets.
- One outbound queue and sender task per client.
- Strict command routing through RuntimeService only.
- Session CRUD, Run start/stop, replay, snapshots, subscriptions, deferred
  capability errors, and structured internal failures.
- Replay-before-live buffering, failed-subscription cleanup, gap snapshot
  recovery, session-deleted control events, and reconnect deduplication.
- Policy-error close for incompatible protocol and deterministic slow-consumer
  close with replayable committed events.

Historical verification before the final replay-boundary fix at `025ac12`:

```text
Server + Service + Protocol focused: 179 passed
Runtime suite:                       712 passed
Full offline suite:                  1086 passed, 1 skipped
uv lock --check:                     exit 0 (69 packages)
compileall eee_agent tests:          exit 0
git diff --check:                    exit 0
```

The historical green suite did **not** constitute Task 12 acceptance because
the overlap case below was reproduced independently after the suite passed.

## Historical Task 12 Defect (Resolved at `724a8fb`)

File: `eee_agent/runtime/server.py`, `_do_subscribe` gap path.

Deterministic scenario:

1. Initial replay reports a retention gap at `last_seq=6`.
2. Snapshot establishes `snapshot_seq=6`.
3. The required second replay uses `after_seq=6` and returns event `seq=7` with
   `last_seq=7`.
4. The live initialization buffer also contains the same committed event
   `seq=7`.

Incorrect wire output from the pre-fix implementation:

```text
response result.last_seq = 6
session.snapshot snapshot_seq = 6
event seq = 7
event seq = 7
```

Root cause: after the second replay succeeds, `_do_subscribe` leaves `boundary`
at the snapshot sequence instead of advancing it to `replay.last_seq`. The
response reports a stale boundary and the buffered event is not deduplicated.

The required fix was implemented and independently accepted:

- After the final gap replay returns `snapshot_required=False`, set the final
  replay boundary to `replay.last_seq`.
- Return that boundary in the subscribe response.
- Send the second replay events once, set `sub.last_delivered` to the final
  replay boundary, then flush only buffered events with a greater sequence.
- A repeated subscribe must report the existing subscription's actual
  `last_delivered`, not echo an arbitrary new request value.
- Add real-socket coverage where the second replay and live buffer both contain
  `seq=7`, plus a buffered `seq=8`; wire order must be snapshot, 7, 8 with no
  duplicates.
- Optionally bound repeated gap/snapshot retries and convert exhaustion to
  `internal.runtime_failure` while cleaning the half-initialized subscription.

Only `server.py` and `test_server.py` were authorized for this Task 12 fix.
The focused commit was:

```text
fix: advance websocket replay boundaries
```

Codex independently reproduced the overlap case after the fix. The accepted
wire result was `session.snapshot` followed by `seq=7` and `seq=8`, with
`response.result.last_seq == 7` and replay calls `after_seq=[1, 6]`.

## Task 13: Accepted (`c8e6af1`)

Task 13 implementation commit: `3b69532 feat: complete persistent runtime
vertical slice`.

The reviewed follow-up commit `c8e6af1 fix: wire runtime graceful shutdown
timeout` additionally:

- wires CLI `--graceful-timeout` into `RuntimeService` and instance shutdown;
- preserves the default 10-second behavior for existing callers;
- removes identity/discovery files only after the WebSocket server context exits;
- adds deterministic tests for both contracts.

Accepted verification:

```text
Task 13 follow-up suite:             67 passed
Task 13 + E2E + Server + Protocol:  136 passed
Runtime suite:                      736 passed
Full offline suite:                1111 passed, 0 skipped, 0 xfailed
uv lock --check:                    exit 0 (69 packages)
compileall:                         exit 0
git diff --check:                   exit 0
```

All automated tests are offline. GLM-5.2 and Houdini read-only smokes remain
manual acceptance activities and have not been run by the test suite.

## Task 14: Planned Runtime v1 external acceptance and delivery

Task 14 is intentionally a delivery/acceptance gate rather than speculative
production code. It covers pushing the accepted branch, clean restore on the
next computer, the manual GLM-5.2 read-only continuity smoke, the manual
Houdini read-only smoke, and dated evidence. The user has explicitly authorized
the Task 15 read-only foundation while those external smokes remain pending;
Codex must still issue a bounded prompt before any implementation.

The authoritative checklist and the Task 15–19 roadmap are in
`docs/superpowers/plans/2026-07-15-runtime-next-milestones.md`.

## Task 15-A: Accepted strict DTO and error contracts (`4c22d45`)

Independent verification confirmed:

```text
Contract suite:              97 passed
Task 15-A regression slice:  261 passed
Full offline suite:          1208 passed
uv lock --check:             exit 0
compileall:                  exit 0
git diff --check:            exit 0
```

The commit changes exactly `eee_agent/houdini_bridge/__init__.py`,
`eee_agent/houdini_bridge/contracts.py`, and
`tests/runtime/test_houdini_bridge_contracts.py`. It imports neither `hou` nor
`rpyc`, reuses the Runtime canonical JSON freezer, and exposes only frozen
read-only DTOs. Task 15-B is the next implementation task; no Claude prompt
for it has been issued yet.

## Remaining Delivery Sequence

1. Push completed: `feature/runtime` is at `9293174` on GitHub.
2. On the next computer, restore the branch from `origin/feature/runtime` and
   rerun the clean baseline verification below.
3. Perform the separate manual GLM-5.2 and Houdini read-only smoke procedures
   from the Runtime plan, recording external-service failures separately.
4. Decide whether to merge `feature/runtime` into `main`; do not merge during
   the migration without an explicit integration decision.
5. Task 15-A is already accepted under the explicit external-smoke exception;
   issue a separate Codex-approved Task 15-B prompt only after reviewing this
   handoff and keeping the scope to Bridge identity/client behavior.

## New Computer Restore Procedure

Use Python 3.11. Do not copy `.venv`, Runtime SQLite databases, token/discovery
files, logs, or lock files between computers.

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
Set-Location E:\eee-agent
git fetch --all --prune
git switch --track origin/feature/runtime
uv sync --frozen --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent tests
git status --short --branch
```

Expected baseline before new development:

- Branch is `feature/runtime` and tracks `origin/feature/runtime`.
- Worktree is clean.
- `uv lock --check` exits 0 with 69 packages.
- Full suite reports at least `1111 passed` on the current environment. The
  optional WSL environment probe may be skipped or may run on another machine;
  either result is acceptable if there are no failures and no new skips.
- Compileall exits 0.

If the repository already has a local `feature/runtime` branch, use:

```powershell
git switch feature/runtime
git pull --ff-only
```

## Secrets And Machine-Local State

The following are intentionally ignored and are not transferred by Git:

- `.env`
- `.venv/`
- `.worktrees/`
- Runtime `app.sqlite` and `checkpoints.sqlite`
- `runtime.token`, `runtime.json`, and `runtime.lock`
- Runtime logs and machine-local state

Transfer credentials only through a password manager or another encrypted
channel. Recreate `.env` from `.env.example`; never commit or paste real API
keys into source, tests, documentation, issues, or chat transcripts.

## Resume Prompt

Use this prompt in the first conversation on the new computer:

```text
Resume the persistent Runtime milestone from origin/feature/runtime.

Tasks 1-13 are complete and Codex-accepted. Task 14 is planned and is the next
acceptance gate; it is not an implementation license. The accepted
implementation tips are 724a8fb (Task 12), 3b69532 (Task 13), and c8e6af1
(Task 13 follow-up).
Read, in order:

1. docs/superpowers/specs/2026-07-14-runtime-design.md
2. docs/superpowers/plans/2026-07-14-runtime.md
3. docs/handoffs/2026-07-15-runtime-migration.md
4. docs/handoffs/2026-07-14-runtime-migration.md for Tasks 1-9 history

Run `uv sync --frozen --extra eval --python 3.11`, `uv lock --check`, the full
pytest suite, compileall, and `git status` before any new work.

Do not modify production code or start Task 16/17 without a current Codex
instruction. Task 15 read-only implementation is authorized only through the
new executable plan and a single Claude prompt; manual GLM/Houdini smoke still
follows the Task 14 checklist.
Claude Code is responsible only for the concrete implementation task supplied
by Codex; Codex owns plan/status documents, diff review, adversarial checks,
acceptance, and push/merge decisions. Use model `glm-5.2[1m]` when Codex assigns
the next implementation task.
```

## Git Safety

- Push only `feature/runtime`; do not force-push.
- Do not merge to `main` during this migration.
- Do not rewrite Tasks 1-11 history.
- Do not delete other branches or worktrees.
- A Git push transfers committed files only; it never transfers ignored secrets
  or local Runtime state.
