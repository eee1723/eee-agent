# Task 16-D Independent Review Checklist

- Dependency: Task 16-C accepted at `6050a00`
- Scope: typed apply/receipt, one transactional executor, rollback, bounded
  in-process receipt evidence, and disposable real-Houdini smoke only
- Promotion: do not start Task 16-E until all blocking items pass

## Scope and protocol

- [ ] Implementation commit contains only authorized D files.
- [ ] No Runtime DB/service/protocol, approval consumption, workspace public
      command, UI, dependency, docs, or general RPC/eval surface changed.
- [ ] Apply/receipt use strict frozen typed DTOs, canonical digest, exact
      fields/tags, bounded payload/evidence, and `changeset.v1` gating.
- [ ] Legacy scene.query and accepted preflight behavior remain compatible.
- [ ] Server dispatch is explicit; unknown operations and forward delete fail
      before HOM access.

## Authority and current facts

- [ ] Bridge receives no PolicyDecision/approval authority and independently
      checks binding, manifest, permission mode/scope, operation allowlist,
      identity/mirrors, locks, preconditions, checkpoint/backup facts.
- [ ] Stable ID precedes path; ambiguity, moved/stale identity, reused create ID
      and occupied create path fail closed.
- [ ] Every rejection before transaction start has a zero-write assertion.

## Transaction and recovery

- [ ] All HOM reads/writes use the accepted single FIFO/main-thread callable.
- [ ] Exactly CreateNode/SetParm/ConnectInput execute in declared order inside
      one undo group; created nodes mirror all six ownership keys.
- [ ] Executor captures bounded before facts and derives exact inverses; only
      same-transaction created nodes may be destroyed during rollback.
- [ ] Applied is returned only after postcondition re-read/reconciliation.
- [ ] Rollback runs in reverse order and receipts truthfully distinguish
      RolledBack, Partial, and CriticalRecovery.
- [ ] Before/after revisions are deterministic; receipt contract invariants and
      scene_may_have_changed are satisfied.
- [ ] Same ID/digest replay is zero-write AlreadyApplied; conflicting digest
      fails; receipt cache is bounded and deterministic.
- [ ] Cancellation before start is zero-write; cancellation after first write
      does not interrupt completion/rollback; shutdown drains; uncertain
      recovery freezes further writes.

## Safety

- [ ] No forward delete, arbitrary Python/VEX/expression, shell, file/HDA,
      save/load/clear/export/network, untyped operation, HOM proxy, traceback,
      token, or raw unbounded value can cross the protocol.
- [ ] Failure-injection fake tests cover each forward and rollback step.
- [ ] Real Houdini smoke uses a disposable fresh session, saves nothing, cleans
      created nodes, proves idempotency/receipt query, and records exact command.

## Independent commands

```powershell
uv lock --check
uv run --extra eval pytest tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check 6050a00..<candidate>
git status --short --branch
```

- [ ] Focused/full tests pass with only the pre-existing WSL skip.
- [ ] Compile, lock, diff, scope, and real hython smoke pass.

Any stale-fact acceptance, authority bypass, write outside the transaction,
false-success receipt, incomplete rollback evidence, queue interleaving,
receipt-id conflict, cancellation interruption, forbidden effect, fake-only
HOM assumption, unauthorized file, or new test failure is blocking.
