# Task 16-D Implementation Prompt

Use Claude Code with explicit model `glm-5.2[1m]`. Implement only the typed
transactional Houdini ChangeSet executor, receipt query, rollback, and its
Bridge transport. Do not start Runtime apply orchestration (16-E), workspace
public commands (B2b), UI, or Task 17.

## Accepted base

Current accepted tip is `6050a00` on `feature/runtime`:

- Task 16-C implementation `86a6bb6`
- stable-ID/mirror follow-up `357d862`
- workspace/create-target follow-up `6050a00`

Preserve every Codex-owned uncommitted file under `docs/`; do not edit, stage,
restore, clean, or commit documentation.

## Required reading

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`,
   especially sections 2, 4, 5, 7, 8, and 9
3. `docs/superpowers/plans/2026-07-15-typed-changeset-policy.md`, Task 16-D
4. Accepted Task 16-A contracts/policy and Task 16-C Bridge contracts,
   preflight adapter, queue, server, client, and focused tests
5. `docs/superpowers/reviews/2026-07-16-task16-c-review-result.md`

## Authorized files

- Modify `eee_agent/houdini_bridge/changesets.py`
- Modify `eee_agent/houdini_bridge/client.py`
- Modify `eee_agent/houdini_bridge/__init__.py` only for stable typed exports
- Modify `houdini_side/changeset_executor.py`
- Modify `houdini_side/secure_bridge.py` only for explicit typed apply/receipt
  dispatch, FIFO admission, and executor lifecycle
- Create `tests/runtime/test_changeset_executor.py`
- Modify `tests/runtime/test_changeset_bridge_transport.py`
- Create `tests/runtime/changeset_houdini_smoke.py`
- Extend `tests/runtime/test_houdini_bridge_client.py`,
  `tests/runtime/test_houdini_bridge_queue.py`, and
  `tests/runtime/test_changeset_bridge_preflight.py` only where D regression
  coverage requires it

No Runtime database/service/protocol, ChangeSet contracts/policy/repository,
agent, UI, dependency, lock, or documentation file is authorized. Do not add a
general RPC/eval surface or a new queue/thread/task for HOM access.

## Typed Bridge surface

Keep protocol `eee.bridge/1`, token semantics, and capability
`changeset.v1`. Add strict frozen/slotted exact-field DTOs and parsers for:

- `changeset.apply`: full canonical schema-v1 ChangeSet, exact digest, and the
  matching WorkspaceManifest or null, with the same identity/session/binding
  checks as preflight;
- `changeset.receipt`: exact `change_id` plus expected digest, returning the
  bounded typed ChangeReceipt/evidence or a bounded not-found/conflict error.

The Bridge does not receive or trust a Runtime `PolicyDecision` or approval
record. Approval consumption remains Task 16-E Runtime orchestration. The
Bridge independently re-validates digest, binding, permission facts,
operation allowlist, manifest ownership/scope, every precondition, and current
scene facts immediately before the first write.

Client methods must require `changeset.v1` before sending a frame. Unknown
fields/tags/operations, duplicate JSON keys, bad digest, wrong manifest,
wrong scene epoch, oversized payload/evidence, or malformed receipts fail
closed. Existing `scene.query` and `changeset.preflight` wire behavior stays
compatible.

## Transaction algorithm

Use the one accepted main-thread FIFO. All reads and writes occur in one
synchronous queue callable; no HOM access may occur on the socket loop or a
worker.

For one admitted apply:

1. Reuse the accepted stable-ID-first resolver and mirror checks. Re-derive
   create-target absence and every precondition from the current scene.
2. Reject locked targets, stale binding/epoch/manifest/type/path/parent/
   ownership/parm/wire facts, unsupported operations, scope mismatch, and
   unprovable backup/checkpoint requirements before any write.
3. Capture a bounded before snapshot sufficient for exact inverse actions.
   Derive inverses internally; no inverse/delete operation is accepted from
   the wire.
4. Enter exactly one short `hou.undos.group("EEE Agent - <change_id>")` (or the
   verified Houdini equivalent) and execute only the accepted ordered effects:
   CreateNode, SetParm, ConnectInput.
5. Created nodes must use the declared type/name/parent and mirror exactly:
   `eee.workspace_id`, `eee.node_id`, `eee.capability`, `eee.role`,
   `eee.schema_version="1"`, and `eee.created_by_run`.
6. Re-read all expected postconditions and relevant identity facts. Return
   `Applied` only after reconciliation succeeds. Compute deterministic bounded
   before/after revisions from the facts actually read.
7. On execution or reconciliation failure, run derived inverses in strict
   reverse order and re-read rollback conditions. Classify exactly as
   `RolledBack`, `Partial`, or `CriticalRecovery`; never claim success when
   recovery is uncertain.

Forward deletion, arbitrary parm expressions, arbitrary Python/VEX, shell,
file/HDA/source/install, save/load/clear, hip export, network, untyped dict
operations, and unconstrained traversal are forbidden. Internal rollback may
destroy only a node created by this same transaction after verifying its
stable identity.

## Idempotency, receipt evidence, and cancellation

- Maintain a bounded process-local receipt/evidence cache keyed by change ID
  and digest. Same change ID plus same digest after terminal success returns an
  `AlreadyApplied` receipt and performs zero writes; same ID plus different
  digest fails closed.
- `changeset.receipt` reads this cache and never touches/mutates the scene.
  Cache bounds and deterministic eviction must be tested. Do not persist it to
  Runtime SQLite in this task.
- Cancellation or expiry before the transaction starts performs zero writes.
  Once the first write starts, client cancellation must not interrupt the
  short transaction: it completes/reconciles or rolls back and leaves receipt
  evidence queryable.
- Shutdown stops new admission, drains a running transaction, then closes the
  shared queue. A `Partial`/`CriticalRecovery` state must fail closed for
  further writes until explicit process restart or a narrowly designed safe
  recovery rule from the accepted spec.
- `scene_may_have_changed` and ChangeReceipt status invariants must exactly
  satisfy the accepted Task 16-A contract.

## RED-first tests

Begin with genuine failing tests. Cover at minimum:

- strict apply/receipt DTO round-trip, exact fields/tags/digest/manifest,
  duplicate keys, size limits, missing capability sends no frame;
- deterministic create/set/connect ordering and exact mirrored ownership;
- stale scene/manifest/identity/type/path/parent/workspace/schema/run/lock,
  parm and wire preconditions all reject before writes;
- checkpoint coverage and backup-required failure before writes;
- postcondition reconciliation and before/after revisions;
- same-digest duplicate apply performs zero writes and returns AlreadyApplied;
  different digest conflict and bounded receipt query/cache behavior;
- inverse ordering, successful rollback, rollback failure, partial and
  critical recovery receipts, and write freeze after uncertain recovery;
- cancellation/deadline before start, cancellation during the transaction,
  FIFO ordering with scene.query/preflight, queue capacity, and shutdown drain;
- forward delete/unknown effect/arbitrary code/file/HDA/shell/save/load/clear
  remain impossible to parse or call;
- existing Task 16-A/C and Bridge regressions remain green.

Use mutation/failure-injection fake HOM objects. Assert exact method calls and
that every error before transaction start produces zero writes.

## Real Houdini smoke

After offline tests pass, detect the local Houdini 21.0.440 `hython` without
changing dependencies or environment files. Run
`tests/runtime/changeset_houdini_smoke.py` in a disposable fresh process/scene.
It must exercise one minimal create/set/connect transaction, receipt query,
idempotent replay, and cleanup without saving or touching a user HIP. If
Houdini is unavailable, report the exact blocker and do not claim Task 16-D
complete or commit an unverified implementation.

## Verification

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Only the existing optional WSL environment probe may skip.

## Commit and handoff

Stage only authorized files and commit once:

```text
feat: apply typed houdini changesets transactionally
```

Return commit/parent, exact file list, RED evidence, focused/full counts,
offline transaction/rollback/idempotency/cancellation evidence, exact hython
command/result, and concerns. Do not push, merge, amend, clean docs, start
16-E/B2b, or update plan/status.
