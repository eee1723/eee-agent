# Task 16-A Implementation Prompt

Use Claude Code with the explicit model `glm-5.2[1m]` for this task. Execute
only this slice. Do not delegate, start a second worker, or continue into Task
16-B.

## Objective

Implement Task 16-A: immutable typed ChangeSet/WorkspaceManifest contracts and
a deterministic pure Policy Engine. Start with focused failing tests, implement
the minimum code that satisfies the approved design, run the required
acceptance commands, and create one focused code commit.

## Required reading

Read these files completely, in order, before editing:

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`
   sections 7.2, 7.3, 8, and 9.2
3. `docs/superpowers/specs/2026-07-15-secure-houdini-bridge-readonly-design.md`
4. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`
5. `docs/superpowers/plans/2026-07-15-typed-changeset-policy.md`, Task 16-A
6. `eee_agent/core/ids.py`
7. `eee_agent/core/artifacts.py`
8. `eee_agent/runtime/models.py`
9. `eee_agent/houdini_bridge/contracts.py`
10. Their focused tests, especially `tests/test_core_ids.py`,
    `tests/runtime/test_models.py`, and
    `tests/runtime/test_houdini_bridge_contracts.py`

The Task 16 design is authoritative. If it conflicts with an assumption, stop
and report the conflict; do not edit the design to fit your implementation.

## Starting state and worktree ownership

- Branch: `feature/runtime`
- Pulled baseline: `5915720`
- Restore verification: 69 locked packages; 1387 passed and one pre-existing
  optional WSL probe skipped; compileall and diff check passed.
- The worktree intentionally contains uncommitted Codex-owned documentation:
  the Task 16 design, plan, roadmap status, handoff, this prompt, and its review
  checklist. Preserve them exactly. Do not edit, stage, commit, stash, restore,
  or delete those files.
- Before editing, record `git status --short` and `git diff --name-only`.
- When committing, stage only the explicitly authorized Task 16-A code/test
  paths listed below. Never use `git add .` or `git add -A`.

## Authorized files

You may modify only:

- create `eee_agent/changesets/__init__.py`
- create `eee_agent/changesets/contracts.py`
- create `eee_agent/changesets/policy.py`
- modify `eee_agent/core/ids.py` only to add `IdKind.APPROVAL = "apr"` for the
  approved immutable `ApprovalRecord`
- create `tests/runtime/test_changeset_contracts.py`
- create `tests/runtime/test_changeset_policy.py`

No other production, test, configuration, lock, documentation, generated, or
local-state file is authorized. If an apparently necessary change falls outside
this list, stop and report it rather than expanding scope.

## Explicit non-goals and forbidden shortcuts

Do not add or modify:

- database schemas, repositories, Runtime models/services/protocol/server;
- Bridge contracts/client/queue/server, `hou`, `rpyc`, Houdini-side code;
- agent tools, `build_agent`, middleware, UI, panel, CLI, or dependencies;
- generic dictionaries as the public ChangeSet operation model;
- arbitrary `eval`, `exec`, Python/HOM/VEX source, shell, filesystem, HIP,
  HDA, save/load/clear/export, or forward node deletion effects;
- permissive parsing, ignored unknown fields, `isinstance(..., int)` where it
  accepts bool, mutable nested DTO state, or hashing via `repr()`;
- policy based only on a tool/operation name without checking targets,
  ownership, scope, and normalized effects;
- auto-approval. Every allowed policy decision has
  `approval_required=True`.

Do not weaken or rewrite existing tests. Do not mark tests skipped or xfailed.

## Required public surface

Use frozen, slotted dataclasses and `StrEnum` values. The package root should
export only the stable Task 16-A contracts and `evaluate_policy`; keep internal
validators private.

The implementation must provide these concepts with names that match the
design unless an existing repository convention requires a narrowly justified
alternative:

- `PermissionMode`: `OWNED_WORKSPACE="OwnedWorkspace"`,
  `SCOPED_PATCH="ScopedPatch"`, `PROJECT_CHANGE="ProjectChange"`
- `Effect`: exactly `NODE_CREATE="node.create"`, `PARM_SET="parm.set"`,
  `WIRE_CONNECT="wire.connect"`
- `ApprovalDecision`: `PENDING="Pending"`, `APPROVED="Approved"`,
  `REJECTED="Rejected"`, `CONSUMED="Consumed"`, `EXPIRED="Expired"`
- `ReceiptStatus`: `APPLIED="Applied"`, `ALREADY_APPLIED="AlreadyApplied"`,
  `ROLLED_BACK="RolledBack"`, `PARTIAL="Partial"`,
  `CRITICAL_RECOVERY="CriticalRecovery"`
- `OwnedNodeRef`, `WorkspaceManifest`, `NodeRef`, `WireRef`
- `CreateNode`, `SetParm`, `ConnectInput`
- typed preconditions/postconditions from the exact union in design section
  4.3; do not expose arbitrary condition dictionaries
- `RiskSummary`, `CheckpointPlan`, `ChangeSet`, `PolicyDecision`
- `ApprovalRecord`, `ConditionResult`, `ChangeReceipt`
- `evaluate_policy(...) -> PolicyDecision`

All public DTOs must have deterministic `to_dict()` output. The independently
persisted top-level records `WorkspaceManifest`, `ChangeSet`, `ApprovalRecord`,
and `ChangeReceipt` carry exact `schema_version=1`; nested value objects and
`PolicyDecision` do not add a redundant schema version. DTOs that are persisted
or hashed must support a deterministic canonical JSON representation using the
existing strict Runtime helpers instead of a second permissive JSON
implementation.

## Exact contract requirements

Implement and test all of the following.

### Primitive strictness and immutability

- Accept exact primitive types only. Bool is not an integer.
- Reject naive datetimes; normalize aware datetimes to UTC.
- Reject non-finite floats, non-string JSON keys, cycles, callables, classes,
  and unsupported objects.
- Deep-freeze every caller-owned list/dict at construction. Mutating an input
  after construction must not change the DTO, its dictionary form, revision,
  or digest.
- `to_dict()` returns fresh plain JSON trees; mutating a returned tree must not
  mutate the DTO or later output.
- IDs use existing `require_id` for session/run/workspace/change and the new
  approval kind if added. SHA-256 values are exactly 64 lowercase hex chars.
- Absolute Houdini node paths start with `/`, contain no NUL, and are bounded.
  Operation IDs, node IDs, names, roles, and capabilities are non-empty and
  bounded. Choose conservative private constants and test their boundaries.
- Collection limits and canonical payload limits from design section 4.2 are
  enforced before an object can be approved or hashed.

### WorkspaceManifest

- Require schema version 1, one to 16 unique roots, and one to 4096 unique
  nodes.
- Every root must also be represented in the owned node set with identical
  identity facts.
- Reject duplicate node IDs or duplicate paths even if the remaining facts
  differ.
- `revision` is verified against the canonical SHA-256 hash of manifest
  identity plus ordered roots/nodes, excluding `updated_at` and the revision
  field itself.
- Expose a deterministic helper or constructor path for computing that
  revision; callers/tests must not duplicate a private hashing algorithm.
- A renamed/moved node keeps its stable node ID but changes the manifest
  revision; tests must prove both facts.

### Operations and parameter values

- Accept exactly `node.create`, `parm.set`, and `wire.connect` through concrete
  typed classes. Unknown or mismatched tags fail closed.
- A ChangeSet contains 1..256 operations with unique `op_id` values.
- `CreateNode` targets an owned workspace, has a new stable node ID, and does
  not accept arbitrary initial parameter/user-data payloads.
- `SetParm` accepts only a bounded JSON scalar (`bool`, exact `int`, finite
  `float`, or bounded `str`) or a non-empty homogeneous tuple of those scalars.
  Do not accept `None`, dicts, nested tuples/lists, bytes, expressions, or
  source blobs. `value` and `expected_old_value` have the same scalar/tuple
  shape and exact element type.
- `ConnectInput` indices are exact non-negative ints and includes the exact
  expected previous `WireRef | None`.
- The public operation model contains no forward delete, disconnect, arbitrary
  user-data mutation, code install, or file effect.

### Conditions, ChangeSet, approvals, and receipts

- Conditions are concrete tagged DTOs for scene binding, workspace revision,
  node identity, parameter value, wire input, and node absence.
- Reject duplicate or contradictory condition identities.
- `ChangeSet` verifies exact IDs, binding, permission enum, base revision,
  operation/affected/dependency uniqueness, limits, schema version, and
  canonical payload size.
- Its digest is SHA-256 over the full canonical schema-v1 payload, including
  `created_at`. It must not include a digest field inside the hashed payload.
- Risk/checkpoint objects are bounded typed DTOs, not arbitrary mappings.
- `ApprovalRecord` enforces state-dependent nullability: pending has no
  decision facts; decided states have a UTC `decided_at` and `decided_by`;
  approved/consumed bind instance ID and scene epoch. Expiry must be after
  request time.
- `Applied` and `AlreadyApplied` receipts require all postcondition results to
  pass and `scene_may_have_changed=False`. `RolledBack` requires every rollback
  result to pass. `Partial` and `CriticalRecovery` must have
  `scene_may_have_changed=True` and cannot serialize as success.

## Policy API and rules

Keep policy pure and deterministic. It may consume only immutable ChangeSet,
WorkspaceManifest, and explicit read facts. A recommended signature is:

```python
def evaluate_policy(
    changeset: ChangeSet,
    *,
    workspace: WorkspaceManifest | None,
    locked_node_paths: Iterable[str] = (),
    ambiguous_node_paths: Iterable[str] = (),
) -> PolicyDecision:
    ...
```

Normalize iterable inputs immediately into immutable exact-string sets; reject
invalid inputs rather than silently ignoring them. Returning denial reasons is
not exceptional; malformed contract inputs remain constructor/type errors.

Apply these exact rules:

- Every decision echoes the exact ChangeSet digest and has
  `approval_required=True`.
- Normalized effects are derived from typed operations and sorted
  deterministically.
- OwnedWorkspace allows only one present, matching manifest; all changed nodes
  and created node workspace IDs must match it. External nodes may appear only
  as read dependencies or unchanged create parents. Locked/ambiguous changed
  targets deny.
- ScopedPatch requires no `node.create`; every changed existing target and
  both endpoints whose wiring is changed must be listed in
  `scoped_node_ids`. Scope does not expand via affected/read dependencies.
- ProjectChange permits only the same three typed effects, only enumerated
  affected targets, and always remains per-ChangeSet approval. It grants no
  standing permission.
- Missing/stale workspace facts, ownership mismatch/ambiguity, locked changed
  targets, affected-target omissions, effect/risk contradictions, or any
  forbidden/unknown effect deny with stable sorted namespaced denial codes.
- `backup_required` comes from the typed risk/checkpoint facts. If it is true,
  Task 16-A denies because no backup capability exists in this milestone.
- The evaluator never mutates inputs and repeated evaluation produces equal
  decisions.

## Test-first sequence

1. Create the two authorized test modules first.
2. Cover happy paths and adversarial boundaries from the design and this
   prompt. Prefer small factories local to the test files.
3. Run the two modules and record a genuine RED result caused by missing
   implementation, not syntax/import mistakes in unrelated code.
4. Implement contracts, rerun until contract tests pass.
5. Implement policy, rerun until both modules pass.
6. Run the focused regression and final checks below.

Required adversarial tests include:

- post-construction mutation and post-`to_dict()` mutation;
- bool-as-int, NaN/Infinity, naive datetime, wrong enum/string, wrong ID kind;
- duplicate op/node/path/condition identities and boundary+1 sizes;
- digest/revision equality across dict insertion order and inequality after any
  covered field changes;
- owned external mutation, scoped create/scope expansion, project omitted
  affected target, locked/ambiguous target, ownership mismatch, missing
  manifest, unavailable backup, and deterministic denial-code ordering;
- AST/import check proving `eee_agent.changesets` does not import `hou`, `rpyc`,
  `eee_agent.bridge`, database, transport, subprocess, or filesystem write
  modules;
- public export check proving forbidden operation/effect names are absent.

## Required verification

RED evidence:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
```

Focused acceptance:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_models.py tests/test_core_ids.py -q
uv run python -m compileall -q eee_agent tests
uv lock --check
git diff --check
git status --short
```

If focused acceptance is green, run the complete offline suite:

```powershell
uv run --extra eval pytest -q
```

The optional WSL probe may remain the only skip. No new skip or xfail is
accepted.

## Commit and handoff

Review the final diff before committing:

```powershell
git diff -- eee_agent/changesets eee_agent/core/ids.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py
git diff --check
```

Stage only authorized paths, for example:

```powershell
git add eee_agent/changesets/__init__.py eee_agent/changesets/contracts.py eee_agent/changesets/policy.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py
```

Add `eee_agent/core/ids.py` only if you actually changed it. Do not stage any
Codex-owned documentation. Commit exactly once:

```text
feat: define typed changeset policy contracts
```

Return a concise implementation handoff containing:

1. commit hash;
2. exact changed-file list;
3. RED command and the relevant failure summary;
4. focused and full-suite pass counts, skips, and durations;
5. compile/lock/diff results;
6. design decisions or assumptions made;
7. any remaining concern for Codex review.

Do not push, merge, amend unrelated commits, clean the worktree, update the
Task 16 plan/status, or begin Task 16-B. Codex will independently review and
accept or reject the slice.
