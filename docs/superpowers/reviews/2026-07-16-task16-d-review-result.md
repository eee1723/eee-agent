# Task 16-D Independent Review Result

## Decision

Accepted at `3435f4b` on `feature/runtime` (parent `6050a00`).

Implementation commit:

- `3435f4b feat: apply typed houdini changesets transactionally`

The commit contains exactly nine authorized Task 16-D files. It does not
modify Runtime orchestration, approval persistence, ChangeSet policy/contracts,
dependencies, workspace public commands, UI, or documentation.

## Closed findings

1. The initial executor evaluated supplied preconditions but did not rerun the
   accepted pure policy against current scene facts. The final implementation
   re-evaluates permission, scope, risk/effect summaries, affected nodes, locks,
   ownership, and manifest facts before the first write.
2. Mandatory operation-derived preconditions, postconditions, and checkpoint
   coverage could be omitted. The executor now derives exact required binding,
   workspace, identity, absence, parm, wire, and snapshot facts and rejects
   omissions or contradictions with zero writes.
3. Inverses were journaled after mutating HOM calls returned, so a
   mutate-then-raise or mirror failure could strand an effect. Create, parm, and
   wire inverses are now recorded as soon as restoration evidence exists and
   before the next mutation.
4. Reconciliation or rollback read failures could escape without a receipt.
   Transaction-phase failures are contained, reverse rollback continues across
   individual failures, uncertain recovery returns Partial/CriticalRecovery,
   and further writes freeze.
5. Any cached status was converted to AlreadyApplied. Only prior success now
   replays as AlreadyApplied; RolledBack returns its existing non-success
   receipt, while Partial/CriticalRecovery remains frozen and queryable.
6. Receipt lookup did not distinguish digest conflict and did not gate the
   tracked scene epoch. Both now fail closed with bounded errors without HOM
   access.
7. The initial test set did not prove cancellation after the first write call,
   queue capacity, or shutdown ordering. Cross-thread barriers now prove a
   running transaction completes and caches its receipt after caller
   cancellation, while shutdown rejects new/queued work and waits for the
   running item before adapter/identity cleanup.
8. Rollback-create identity verification treated user-data read exceptions as
   missing keys and could destroy without proof. Strict reads now refuse
   deletion on any read error, return CriticalRecovery, and freeze writes.
9. Rollback exception evidence used `node.absent` for every inverse kind. The
   fallback result now preserves node/parm/wire condition kinds.
10. Real Houdini 21.0.440 showed `inputConnections().outputNode()` was not a
    reliable source-node read for these objects. Preflight and executor use
    `node.inputs()[index]` for the source and the connection only for output
    index; the real smoke verifies the behavior.

## Independent evidence

- RED evidence: the new executor/transport tests initially failed collection
  before `ChangeSetExecutor` and apply/receipt dispatch existed.
- Focused gate across executor, transport, contracts, preflight, client, queue,
  Task 16-A contracts, and policy: 462 passed.
- Full offline suite: 1821 passed, 1 skipped. The skip is the pre-existing
  optional WSL environment probe; no new skip or xfail was added.
- `uv lock --check`: 69 packages, exit 0.
- `python -m compileall -q eee_agent houdini_side tests`: exit 0.
- `git diff --check`: exit 0.
- Scope check: nine authorized implementation files, with no queued production
  module change after the shutdown wait was kept inside `secure_bridge.py`.

Exact real smoke:

```powershell
$env:PYTHONPATH = (Get-Location).Path
& 'C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe' tests\runtime\changeset_houdini_smoke.py
```

Result: exit 0; ordered create/set/connect returned Applied, receipt lookup
returned the cached terminal evidence, same-digest replay returned
AlreadyApplied with zero operations, and `/obj/eee_task16d_smoke` was destroyed
without saving or clearing a user HIP. A Qt timer warning printed after the
successful cleanup and did not change the exit status.

## Residual boundary

The accepted Task 16-A pure policy does not currently treat a node created
earlier in one ChangeSet as manifest-owned when a later SetParm or ConnectInput
targets it; mandatory preflight also cannot read old parm/wire facts from a node
that is correctly absent before the transaction. Task 16-D therefore supports
and smokes ordered create/set/connect effects on independently valid targets,
but a future compiler must not emit dependent create-then-set/connect or
create-under-created-parent sequences until that cross-slice rule is explicitly
designed and reviewed. No Task 16-E or B2b work started here.
