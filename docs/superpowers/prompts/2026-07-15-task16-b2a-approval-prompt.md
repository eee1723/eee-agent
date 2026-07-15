# Task 16-B2a Implementation Prompt

Use Claude Code with explicit model `glm-5.2[1m]`. Execute only this slice;
do not begin B2b, Task 16-C, Apply, Bridge preflight, or Houdini writes.

## Objective

Build the approval service seam on top of the Codex-accepted B1 repository.
Activate only trusted internal proposal plus the public
`changeset.approve`/`changeset.reject` commands. Every state change and its
durable event must commit atomically, and Runtime notifications may happen
only after the transaction commits.

## Required reading

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`
3. `docs/superpowers/plans/2026-07-15-typed-changeset-policy.md` (Task 16-B2a)
4. `docs/superpowers/reviews/2026-07-15-task16-b1-review-result.md`
5. `eee_agent/changesets/repository.py`
6. `eee_agent/runtime/events.py`, `service.py`, `server.py`, and `protocol.py`
7. Existing event/service/server/protocol tests

The approved design is authoritative. Do not change contracts or weaken the
read-only agent/Bridge boundary to make this slice easier.

## Starting state and worktree ownership

- Task 16-A is accepted at `79f281d`.
- Task 16-B1 is accepted at `54f2989` after `a3914f5`.
- Preserve all Codex-owned documentation; do not edit, stage, commit, stash,
  restore, or delete any `docs/` file.
- Record `git status --short` and `git diff --name-only` before editing.

## Authorized files

- Modify `eee_agent/changesets/repository.py` for combined transaction-safe
  proposal/approval/state/event primitives
- Create `eee_agent/changesets/service.py`
- Modify `eee_agent/runtime/events.py` only for an internal atomic append
  primitive that reuses existing validation and sequence allocation
- Modify `eee_agent/runtime/protocol.py`
- Modify `eee_agent/runtime/server.py`
- Modify `eee_agent/runtime/service.py` only for injected ChangeSet service
  construction and post-commit event notification
- Extend `eee_agent/runtime/__init__.py` only for stable public records
- Create `tests/runtime/test_changeset_service.py`
- Modify focused protocol/server/service/events tests

No other file is authorized. In particular, do not modify migrations,
database.py, contracts.py, policy.py, Bridge, Houdini-side code, UI, tools,
dependencies, lock files, or documentation.

## Explicit non-goals

- Do not activate `workspace.create/bind/switch/inspect` yet; that is B2b.
- Do not accept raw operation JSON or direct Apply from any public command.
- Do not call Bridge, `hou`, `rpyc`, network, shell, filesystem, HIP/HDA, or
  arbitrary Python/code.
- Do not consume an approval in one transaction and transition the ChangeSet
  in another. The service must use one repository/database transaction for the
  coupled operation.
- Do not broadcast an event before commit or leak tokens, DTO internals,
  tracebacks, or unbounded payloads.

## Required behavior

### Internal proposal

Provide a trusted service method that accepts an immutable `ChangeSet`, its
already-computed `PolicyDecision`, and an injected clock/binding seam. It must
persist the ChangeSet in `Proposed`, create the exact-digest `Pending`
ApprovalRecord, transition to `AwaitingApproval`, and append bounded
`changeset.proposed` plus `approval.requested` events in one transaction.
Repeated proposal of the same `change_id`/digest is idempotent; a conflicting
digest is rejected.

### Approve/reject commands

The exact public payload for both commands is:

```json
{"change_id":"chg_<uuid>","changeset_digest":"<64 lowercase hex>"}
```

Reject every extra/missing field, wrong ID kind, non-string, bool-as-string,
or digest that differs from the persisted ChangeSet. No operation list or
manifest is accepted by these commands.

Approval requires current state `AwaitingApproval`, a `Pending` approval, an
unexpired clock value, and the exact ChangeSet digest. It binds
`approved_instance_id` and `approved_scene_epoch` from the injected current
SceneBinding seam. In one transaction it changes approval to `Approved`,
changeset to `Approved`, and appends `approval.approved` plus
`changeset.state_changed`.

Rejection changes `Pending` to `Rejected` and `AwaitingApproval` to `Rejected`
in one transaction, then appends `approval.rejected` and
`changeset.state_changed`. An expired pending approval transitions to
`Expired` exactly once and returns `approval.expired`; it must not be approved
after expiry.

Every allowed decision remains per-ChangeSet and explicit. A consumed,
rejected, expired, stale, or terminal record cannot be approved again. Errors
must use stable namespaced codes from the approved taxonomy, with no raw
exception details.

### Atomic events and Runtime integration

Extend EventStore with a private/internal connection-scoped append helper only
as needed. It must preserve event validation, per-session sequence allocation,
retention/schema fields, and returned `EventRecord` behavior. The service must
collect committed records and notify Runtime subscribers only after COMMIT.
Existing session/run event behavior and replay floors must remain unchanged.

Keep `RuntimeService` free of protocol parsing. The WebSocket server validates
the exact payload and maps service results/errors to existing strict envelopes.
Deferred workspace and Apply commands must remain capability-unavailable.

## Test-first requirements

Start with RED tests before implementation. Cover:

- exact payload fields, ID/digest validation, unknown command behavior;
- proposal idempotency/conflicting digest and bounded event payloads;
- approve/reject/expiry state transitions and exact binding/clock facts;
- atomic rollback when event insertion or DTO persistence fails;
- no event visible and no subscriber notification before commit;
- concurrent approve/reject/expiry where exactly one transition wins;
- stale digest, already consumed/rejected/expired, and repeated requests;
- Runtime restart/reopen preserving state and event sequence;
- public commands reject operation lists and direct Apply;
- existing Runtime protocol/server/service/event regression tests;
- import scan proving no Bridge/Houdini/network/write path was introduced.

## Verification

RED:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_service.py tests/runtime/test_protocol.py tests/runtime/test_server.py -q
```

Focused:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_service.py tests/runtime/test_changeset_repository.py tests/runtime/test_protocol.py tests/runtime/test_server.py tests/runtime/test_service.py tests/runtime/test_events_store.py -q
uv run python -m compileall -q eee_agent tests
uv lock --check
git diff --check
```

Then run `uv run --extra eval pytest -q`. The existing optional WSL probe may be
the only skip; no new skip/xfail is allowed.

## Commit and handoff

Stage only authorized files and commit exactly once:

```text
feat: persist changeset approvals in runtime
```

Return commit/parent, exact file list, genuine RED, focused/full results,
atomicity/concurrency notes, and concerns. Do not push, merge, amend, clean
the worktree, update Codex status docs, or start B2b.
