# Task 16 Typed ChangeSet And Transactional Write Gate Plan

> **Execution rule:** Codex owns this plan, authorized-file scope, review, and
> acceptance. The repository workflow assigns each implementation slice to
> Claude Code with explicit model `glm-5.2[1m]`, one prompt and one focused
> commit at a time. Do not begin the next slice from a green report alone.

**Goal:** Implement the approved Task 16 design without exposing arbitrary
Houdini writes or changing the accepted read-only agent boundary.

**Design:**
`docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`

**Baseline:** `5915720`; 69 locked packages; 1387 passed, 1 optional WSL probe
skipped on the restore machine; Houdini 21.0.440 / Python 3.11.7 detected.

## Global constraints

- Preserve `eee_agent.bridge/*`, `eee_agent.tools/*`, `eee_agent/app.py`, and
  the no-argument/read-only Runtime agent behavior.
- Never add generic Python/HOM, node deletion as a forward operation, source
  install, shell, file, HIP, HDA, save, load, clear, or export effects.
- New persistence is additive schema v2; no rewrite of accepted schema v1.
- Every slice begins with focused RED tests and ends with focused tests, the
  relevant regression set, `git diff --check`, and authorized-file review.
- Do not update the approved design to fit an implementation defect.

## Task 16-A: Immutable contracts, WorkspaceManifest, and pure policy

**Status:** Complete, Codex-accepted at `79f281d` (implementation chain
`6b94cb5`, `9f750ae`, `6b860ff`, `dff573f`, `79f281d`). All F1-F8 findings are
closed; the final independent gate passed 187 focused, 423 regression, and
1574 full offline tests with only the pre-existing optional WSL probe skipped.
The review record is
`docs/superpowers/reviews/2026-07-15-task16-a-review-result.md`. Use
`docs/superpowers/prompts/2026-07-15-task16-a-implementation-prompt.md` and
`docs/superpowers/reviews/2026-07-15-task16-a-review-checklist.md`.

**Authorized files:**

- Create `eee_agent/changesets/__init__.py`
- Create `eee_agent/changesets/contracts.py`
- Create `eee_agent/changesets/policy.py`
- Modify `eee_agent/core/ids.py` only for approval IDs if required
- Create `tests/runtime/test_changeset_contracts.py`
- Create `tests/runtime/test_changeset_policy.py`

### RED

Write tests for every DTO field/type/bound, canonical digest stability,
deep-freezing, duplicate IDs/targets, operation and payload limits, unsupported
operations, manifest revision, rename/move identity, and all three policy
modes. Include explicit denial tests for external owned-workspace writes,
ScopedPatch expansion/create, unknown effects, locked targets, ambiguous
ownership, and every forbidden effect named in the design.

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
```

### GREEN

Implement only frozen contracts and a deterministic pure `evaluate_policy()`.
No database, transport, `hou`, Runtime protocol, UI, or executor work is
authorized. Export only stable DTOs/enums/evaluator from the new package.

### Acceptance

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_models.py tests/test_core_ids.py -q
uv run python -m compileall -q eee_agent tests
git diff --check
git status --short
```

Commit: `feat: define typed changeset policy contracts`

## Task 16-B: Persistence, approvals, and Runtime service protocol

**Dependency:** 16-A accepted.

**Status:** Split into B1/B2 for independent review. B1 is Codex-accepted at
`54f2989` after implementation `a3914f5` and the identity follow-up; B2 is now
unblocked and has not started.

### Task 16-B1: Schema v2 and typed ChangeSet repository

**Authorized files:**

- Modify `eee_agent/runtime/migrations.py`
- Modify `eee_agent/runtime/database.py` for migration checksum support
- Create `eee_agent/changesets/repository.py`
- Create `tests/runtime/test_changeset_repository.py`
- Modify `tests/runtime/test_database.py` only for migration/checksum coverage

**Scope:** Add the four schema-v2 tables (`workspaces`, `changesets`,
`approvals`, `change_receipts`), preserve and checksum-protect v1 migration
history, and implement strict round-trip/concurrency-safe repository
primitives. Do not activate Runtime commands, emit events, call Bridge, or
perform Houdini writes.

**Prompt:** `docs/superpowers/prompts/2026-07-15-task16-b1-persistence-prompt.md`

**Review:** `docs/superpowers/reviews/2026-07-15-task16-b1-review-checklist.md`

Commit: `feat: persist typed changeset records`

**Review result:** `docs/superpowers/reviews/2026-07-15-task16-b1-review-result.md`

### Task 16-B2: Approval service and Runtime protocol

**Dependency:** B1 accepted.

For independent review, B2 is executed as B2a followed by B2b. B2a owns the
approval state/event transaction and public approve/reject commands; B2b owns
workspace lifecycle commands after the B2a service seam is accepted.

#### Task 16-B2a: Approval service, atomic events, and approve/reject protocol

**Status:** Complete, Codex-accepted at `7b3bff8` after implementation
`8fed692` and approval-integrity follow-ups `1230d07`/`7b3bff8`. The final
independent gate passed 348 focused and 1670 full offline tests with only the
existing optional WSL probe skipped.

**Authorized files:**

- Modify `eee_agent/changesets/repository.py` for combined atomic state/approval
  operations
- Create `eee_agent/changesets/service.py`
- Modify `eee_agent/runtime/events.py` only to expose an internal atomic append
  primitive that preserves existing EventStore behavior
- Modify `eee_agent/runtime/protocol.py`
- Modify `eee_agent/runtime/server.py`
- Modify `eee_agent/runtime/service.py` only for injected ChangeSet service
  construction and committed-event notification
- Extend `eee_agent/runtime/__init__.py` only for stable public records
- Create `tests/runtime/test_changeset_service.py`
- Modify focused protocol/server/service/events tests

**Scope:** Activate only trusted internal proposal plus public
`changeset.approve` and `changeset.reject`. Require exact ChangeSet digest,
explicit local-user decision, expiry checks, single-use consumption, and
atomic ChangeSet/approval/event commits. Do not implement workspace lifecycle,
Bridge calls, preflight, Apply, or Houdini writes.

Commit: `feat: persist changeset approvals in runtime`

**Prompt:** `docs/superpowers/prompts/2026-07-15-task16-b2a-approval-prompt.md`

**Review:** `docs/superpowers/reviews/2026-07-15-task16-b2a-review-checklist.md`

**Result:** `docs/superpowers/reviews/2026-07-15-task16-b2a-review-result.md`

#### Task 16-B2b: Workspace lifecycle protocol

**Dependency:** B2a and Task 16-C preflight/provider seam accepted.

Implement `workspace.create`, `workspace.bind`, `workspace.switch`, and
`workspace.inspect` through an injected workspace/Bridge seam. Client payloads
must never carry an untrusted manifest that is accepted as scene fact. This
slice remains read-only with respect to Houdini and must fail closed when the
provider seam is unavailable.

The B2b authorized files and acceptance commands will be listed in its own
prompt after the preflight/provider seam is accepted. This prevents any public
command from treating a client-uploaded manifest as scene fact.

## Task 16-C: Bridge preflight and capability negotiation

**Dependency:** 16-B1 and 16-B2a accepted. B2b workspace lifecycle wiring may
follow this provider/preflight slice.

**Status:** Complete, Codex-accepted at `6050a00` after implementation
`86a6bb6` and identity-integrity follow-ups `357d862`/`6050a00`. The final
independent gate passed 410 focused and 1769 full offline tests with only the
existing optional WSL probe skipped.

**Prompt:** `docs/superpowers/prompts/2026-07-16-task16-c-preflight-prompt.md`

**Review:** `docs/superpowers/reviews/2026-07-16-task16-c-review-checklist.md`

**Result:** `docs/superpowers/reviews/2026-07-16-task16-c-review-result.md`

**Authorized files:**

- Create `eee_agent/houdini_bridge/changesets.py`
- Modify `eee_agent/houdini_bridge/client.py`
- Modify `eee_agent/houdini_bridge/__init__.py`
- Modify `houdini_side/secure_bridge.py` only for capability advertisement and
  strict dispatch, or extract read-only preflight adapter helpers to
  `houdini_side/changeset_executor.py`
- Modify `eee_agent/houdini_bridge/queue.py` only to generalize the single FIFO
  without changing accepted cancellation semantics
- Create `tests/runtime/test_changeset_bridge_contracts.py`
- Create `tests/runtime/test_changeset_bridge_preflight.py`
- Extend focused client/queue/transport tests

### RED

Test exact capability ack, old-server fail-closed behavior, strict preflight
DTOs, wrong token, absent capability, stale epoch, ambiguous identity,
ownership mismatch, locked nodes, parm/wire facts, bounds, FIFO with concurrent
read requests, cancellation, and proof that no preflight path writes the fake
scene.

### GREEN

Advertise sorted capabilities and add only `changeset.preflight`. Generalize
the one main-thread queue so reads and future writes cannot interleave. Do not
add apply, receipt, Runtime orchestration, or HOM mutation in this slice.

### Acceptance

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short
```

Commit: `feat: preflight typed houdini changesets`

## Task 16-D: Transactional executor, receipt, and rollback

**Dependency:** 16-C accepted.

**Status:** Complete, Codex-accepted at `3435f4b`. The final independent gate
passed 462 focused and 1821 full offline tests with only the existing optional
WSL probe skipped. Real Houdini 21.0.440 create/set/connect, receipt, replay,
and cleanup smoke passed.

**Prompt:** `docs/superpowers/prompts/2026-07-16-task16-d-transactional-executor-prompt.md`

**Review:** `docs/superpowers/reviews/2026-07-16-task16-d-review-checklist.md`

**Result:** `docs/superpowers/reviews/2026-07-16-task16-d-review-result.md`

**Authorized files:**

- Modify `eee_agent/houdini_bridge/changesets.py`
- Modify `eee_agent/houdini_bridge/client.py`
- Modify the selected Houdini-side executor/server files from 16-C
- Create `tests/runtime/test_changeset_executor.py`
- Create `tests/runtime/test_changeset_bridge_transport.py`
- Create `tests/runtime/changeset_houdini_smoke.py`
- Extend focused queue/client tests

### RED

Use a deterministic fake scene to test create/set/connect, operation ordering,
derived preconditions, stale scene/manifest/parm/wire/ownership, idempotent
duplicate apply, postcondition reconciliation, inverse ordering, rollback,
rollback failure, partial/critical receipts, cancellation before and during
transaction, shutdown drain, and receipt query. Assert forward deletion and
all forbidden effects remain impossible to parse.

### GREEN

Implement `changeset.apply` and `changeset.receipt` through the shared FIFO.
Use a single short undo group, executor-derived before snapshot/inverses, and
bounded receipt cache/evidence. Never report success before re-read.

### Acceptance

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short
```

Then run the real smoke with the detected Houdini 21.0.440 `hython` and record
the exact command/result in the handoff. Commit only after offline and hython
acceptance: `feat: apply typed houdini changesets transactionally`.

## Task 16-E: Runtime apply orchestration and restart recovery

**Dependency:** 16-D accepted.

**Authorized files:**

- Modify `eee_agent/changesets/service.py`
- Modify `eee_agent/changesets/repository.py` if recovery queries require it
- Modify `eee_agent/runtime/service.py`
- Modify `eee_agent/runtime/server.py` only for already-approved command routing
- Create `tests/runtime/test_changeset_recovery.py`
- Extend focused ChangeSet service and runtime process E2E tests
- Modify Task 16 status sections in README/CLAUDE/handoff/roadmap only after
  implementation acceptance

### RED

Test Pending-before-I/O, approval consumption, committed-event ordering,
disconnect independence, Stop/Force Stop around the write boundary,
Applied/AlreadyApplied/RolledBack/Partial/Critical mapping, Runtime crash after
Bridge effect but before app receipt, restart before-state/post-state/ambiguous
reconciliation, instance/epoch change, write freeze, and no automatic startup
replay.

### GREEN

Connect the accepted persistence seam to the accepted Bridge executor. Recovery
queries receipt and facts but never blindly retries. Keep the agent tool
allowlist read-only; trusted proposal APIs remain the only source of
ChangeSets until Task 18.

### Final acceptance

```powershell
uv sync --frozen --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
uv run python -m eee_agent.cli versions
uv run python -m eee_agent.runtime --help
git diff --check
git status --short
```

Run the Task 16 hython smoke, scan for secrets/local state, update the handoff
with exact evidence, and commit: `feat: recover transactional runtime changes`.
Do not merge `feature/runtime` into `main` without a separate user decision.
