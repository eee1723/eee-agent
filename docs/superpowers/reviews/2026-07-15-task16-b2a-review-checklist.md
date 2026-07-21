# Task 16-B2a Independent Review Checklist

- Target: approval service, atomic events, and approve/reject protocol
- Dependency: Task 16-B1 accepted at `54f2989`
- Promotion: B2b remains blocked until every blocking item passes

## Scope

- [ ] Changed files are limited to the B2a authorized list.
- [ ] No migrations, contracts, policy, Bridge, Houdini, UI, tool, dependency,
      or documentation file is in the implementation commit.
- [ ] Workspace lifecycle and Apply remain deferred/capability-unavailable.

## Persistence and transaction review

- [ ] Proposal, ApprovalRecord, ChangeSet state, and proposal events commit in
      one transaction.
- [ ] Approve/reject/expiry updates approval, ChangeSet state, and events in one
      transaction; no split repository calls can create partial state.
- [ ] Event sequence allocation and validation reuse existing EventStore rules.
- [ ] Event callbacks/broadcast happen only after COMMIT.
- [ ] Failed event or persistence writes roll back every related row and do not
      notify subscribers.
- [ ] Concurrent decisions have exactly one winner and preserve CAS semantics.
- [ ] Existing session/run event order and replay floors remain unchanged.

## Approval invariants

- [ ] Commands require exactly `change_id` and `changeset_digest`.
- [ ] Digest must match the persisted canonical ChangeSet.
- [ ] Only AwaitingApproval + Pending can be approved or rejected.
- [ ] Approval binds current instance/scene epoch from an injected seam.
- [ ] Expiry is checked against an injected UTC clock and transitions once.
- [ ] Rejected, Expired, Consumed, Stale, and terminal records cannot be reused.
- [ ] Every allowed decision remains explicit and per-ChangeSet.
- [ ] Approval identity, ChangeSet identity, and digest cannot drift between
      DTO, payload JSON, and database columns.

## Protocol and boundary review

- [ ] Unknown/missing fields, wrong IDs, wrong digest, operation lists, and
      direct Apply requests are rejected with structured errors.
- [ ] Public protocol parsing remains strict and does not import persistence
      internals directly.
- [ ] No token, traceback, raw exception, arbitrary DTO, or unbounded payload is
      emitted in response/events.
- [ ] Search confirms no `hou`, `rpyc`, shell, filesystem, network, or legacy
      unrestricted bridge path was added.

## Independent commands

```powershell
uv lock --check
uv run --extra eval pytest tests/runtime/test_changeset_service.py tests/runtime/test_changeset_repository.py tests/runtime/test_protocol.py tests/runtime/test_server.py tests/runtime/test_service.py tests/runtime/test_events_store.py -q
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check <b1-tip>..<commit>
git status --short --branch
```

- [ ] Focused and full suites pass with only the existing optional WSL skip.
- [ ] Compile, lock, and diff checks pass.
- [ ] No unauthorized worktree changes were introduced.

## Promotion

Any pre-commit event, partial approval transition, digest/identity mismatch,
public raw-operation path, unauthorized file, or new test failure is blocking.
Only after this checklist passes may B2b workspace lifecycle be started.
