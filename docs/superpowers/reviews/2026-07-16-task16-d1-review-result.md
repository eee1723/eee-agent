# Task 16-D1 Independent Review Result

## Decision

Accepted at `39f7346` on `feature/runtime` (parent `2556b8a`).

Implementation commit:

- `39f7346 feat: support ordered created changeset references`

The commit contains exactly seven authorized D1 implementation/test files. It
does not add a DTO tag, RPC, effect, dependency, Runtime/B2b/16-E behavior, UI,
general execution surface, push, or merge.

## Accepted behavior

1. A NodeRef colliding with a declared create by stable ID or derived path must
   exactly match ID, path, type, and workspace, and operation dependencies must
   point to an earlier producer. Forward references, cycles, and contradictory
   refs fail at construction and strict wire parse.
2. The rule covers operation refs, nested expected-old wire sources, affected
   and read-dependency refs, pre/postconditions, and checkpoint refs.
   Preconditions/checkpoints that require nonexistent created-node state fail
   closed.
3. OwnedWorkspace treats only exact transaction-created refs as internal;
   existing nodes retain manifest checks, ScopedPatch still forbids create,
   and ProjectChange retains the accepted risk/effect rules.
4. Preflight proves every create stable ID/path absent, but emits no parm/wire
   facts for created targets. Ordered derivation requires initial facts only for
   the first write to an existing slot and emits only the final postcondition
   for repeated writes.
5. Every SetParm and ConnectInput compares its expected-old fact immediately
   before mutation. Every use of an earlier-created node rechecks path, type,
   all six mirrors, and creating run; created old wire sources receive the same
   full JIT identity check.
6. JIT failures make no current write, preserve journal-before-mutate, run the
   accepted reverse rollback, classify receipts truthfully, and freeze writes
   when recovery cannot prove restoration.

## Closed review findings

The first Claude pass validated only operation/affected refs, derived old facts
by all-created membership rather than ordered slot state, and used a real-smoke
`null` SOP as a child parent. Codex reproduced accepted contradictory refs and
the repeated existing-parm construction failure, then returned the findings.

The second pass covered the missing ref surfaces and ordered slot derivation,
but globally required optional wire ID/workspace fields and regressed path-only
old-source matching. It also did not verify the six non-NodeRef mirrors of an
earlier-created old wire source. Codex reproduced the path-only rejection and
returned both findings.

The third pass corrected production behavior, but its source-tamper test failed
at the first desired-source check and never reached the old-source branch.
Codex rejected that evidence. The final test hook tampers the created source
only after the first successful connect and proves the reconnect makes no
write, rollback is wire-then-create, the receipt is Partial, and writes freeze.

## Independent evidence

- Corrected branch tests: 3 passed.
- D1/fix small gate during review: 50 passed before the final evidence pass.
- Final focused Task 16 gate: 513 passed.
- Full offline suite: 1872 passed, 1 skipped. The only skip is the existing
  optional WSL environment probe; no new skip or xfail was added.
- `uv lock --check`: 69 packages, exit 0.
- `python -m compileall -q eee_agent houdini_side tests`: exit 0.
- `git diff --check 2556b8a`: exit 0.
- Scope check: exactly seven authorized implementation/test files.

Exact real smoke:

```powershell
& 'C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe' tests\runtime\changeset_houdini_smoke.py
```

Result: exit 0. A created SOP subnet accepted created box/xform children; the
created xform parm set and created-endpoint connection returned Applied in
declared order. Receipt lookup succeeded, same-digest replay returned
AlreadyApplied with zero operations, and `/obj/eee_task16d_smoke` was destroyed
without saving. Houdini emitted the same non-fatal Qt timer warning after the
successful cleanup line.

## Residual boundary

D1 closes only intra-ChangeSet created-node dependency semantics. It does not
start Runtime apply orchestration/restart recovery (16-E), workspace lifecycle
commands (B2b), UI/compiler work, or any new protocol/effect. The branch remains
local and unmerged pending a separate user decision.
