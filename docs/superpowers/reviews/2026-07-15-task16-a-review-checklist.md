# Task 16-A Independent Review Checklist

- Reviewer: Codex
- Review target: implementation commit produced from the Task 16-A prompt
- Promotion result: `Accepted`, `Changes requested`, or `Rejected`
- Rule: never promote Task 16-B until every blocking item passes

## 1. Intake and provenance

- [ ] Record implementation commit hash and parent hash.
- [ ] Confirm parent contains or descends from pulled baseline `5915720`.
- [ ] Record developer-reported RED command/output and verify the failure was
      caused by missing Task 16-A behavior.
- [ ] Record reported focused/full test counts, skip list, compile, lock, and
      diff results. Treat reports as leads, not acceptance evidence.
- [ ] Preserve Codex-owned uncommitted documentation before inspecting or
      switching refs.

## 2. Changed-file scope

Run:

```powershell
git status --short
git diff --name-status <parent>..<commit>
git show --stat --oneline --decorate <commit>
```

Allowed implementation paths are only:

- `eee_agent/changesets/__init__.py`
- `eee_agent/changesets/contracts.py`
- `eee_agent/changesets/policy.py`
- `eee_agent/core/ids.py` when required for approval IDs
- `tests/runtime/test_changeset_contracts.py`
- `tests/runtime/test_changeset_policy.py`

- [ ] No design, plan, handoff, dependency, lock, generated, local-state,
      Runtime, Bridge, Houdini-side, tool, agent, UI, or legacy file is in the
      implementation commit.
- [ ] No Codex-owned documentation was staged, reverted, or overwritten.
- [ ] Commit contains one coherent Task 16-A slice and no Task 16-B work.

Any unauthorized production file is blocking until independently justified
and explicitly re-authorized; convenience is not justification.

## 3. Contract review

### Structure and strictness

- [ ] Public objects are frozen/slotted and expose only stable contract types.
- [ ] Exact primitives are enforced; bool cannot pass integer validation.
- [ ] Aware datetimes normalize to UTC; naive datetimes fail.
- [ ] JSON validation rejects NaN/Infinity, cycles, non-string keys, callables,
      bytes, and unsupported objects.
- [ ] Caller lists/dicts cannot mutate constructed objects.
- [ ] `to_dict()` returns fresh plain JSON and cannot mutate internal state.
- [ ] IDs and 64-character lowercase SHA-256 strings are exact.
- [ ] Only persisted top-level records (`WorkspaceManifest`, `ChangeSet`,
      `ApprovalRecord`, `ChangeReceipt`) carry exact `schema_version=1`; nested
      values and derived policy decisions do not invent redundant versions.
- [ ] Bounds are enforced at minimum, maximum, and maximum+1.
- [ ] Canonical serialization reuses accepted strict helpers and does not hash
      `repr()`, object identity, locale-specific text, or unordered sets.

### WorkspaceManifest

- [ ] Schema version is exactly 1.
- [ ] Roots/nodes are non-empty, bounded, and unique by both node ID and path.
- [ ] Root identity matches an entry in the node set.
- [ ] Revision recomputation covers all identity/ordered ownership facts and
      excludes only revision and `updated_at`.
- [ ] Rename/move preserves node ID but changes revision.
- [ ] Manifest facts cannot silently accept a node from another instance,
      epoch, session, or workspace.

### Operations and conditions

- [ ] Only concrete create/set/connect forward operations exist.
- [ ] No public forward delete/disconnect/code/user-data/file/HIP/HDA effect.
- [ ] Parameter values are bounded scalar or homogeneous scalar tuples; no
      nested values, `None`, expressions, or source blobs.
- [ ] Create cannot smuggle initial parameters or arbitrary userData.
- [ ] Connect contains exact current-wire expectation and strict indices.
- [ ] Conditions use a concrete tagged union and reject duplicates or
      contradictions.
- [ ] ChangeSet requires 1..256 unique operations and bounded canonical size.
- [ ] Affected nodes and read dependencies are unique and bounded.
- [ ] ChangeSet digest covers the full canonical schema-v1 payload, including
      `created_at`, without recursively including itself.

### Approval and receipt invariants

- [ ] Approval state-dependent fields are internally consistent.
- [ ] Expiry is after request time and digest/binding fields are exact.
- [ ] Applied/AlreadyApplied require all postconditions true and cannot claim
      scene uncertainty.
- [ ] RolledBack requires all rollback conditions true.
- [ ] Partial/CriticalRecovery set scene uncertainty and never serialize as a
      successful result.

## 4. Policy adversarial review

- [ ] `evaluate_policy` is pure and deterministic.
- [ ] Effects are derived from typed operations, not caller-provided summaries.
- [ ] All allowed decisions still require explicit approval.
- [ ] Denial codes and normalized effects are stable and deterministically
      ordered.
- [ ] OwnedWorkspace denies mismatched/missing manifest, cross-workspace changed
      nodes, locked/ambiguous changed targets, and external mutation.
- [ ] OwnedWorkspace permits external facts only where the design permits an
      unchanged parent/read dependency; it does not grant parent mutation.
- [ ] ScopedPatch denies create and requires every changed wiring endpoint and
      changed node in the exact scope.
- [ ] ScopedPatch never expands through affected nodes, dependencies, upstream,
      or downstream graph facts.
- [ ] ProjectChange requires every changed target/effect to be enumerated and
      grants no standing permission.
- [ ] Missing affected target, risk/effect contradiction, ambiguous ownership,
      locked target, unknown effect, or unavailable required backup denies.
- [ ] The pure policy layer does not pretend to have re-read current Houdini
      facts; explicit read facts are mandatory inputs.

Manually construct at least these reviewer-only cases, independent of developer
tests:

1. bool input index;
2. post-construction mutation of a parameter tuple source;
3. same manifest facts in different mapping insertion order;
4. duplicate node ID at a different path;
5. owned target with correct path but wrong workspace ID;
6. scoped wire whose source is just outside scope;
7. ProjectChange operation missing from affected nodes;
8. backup-required risk with no backup capability;
9. locked create parent versus locked changed target;
10. repeated policy evaluation after mutating a returned `to_dict()`.

## 5. Boundary and security scan

Run targeted searches and inspect AST/import tests:

```powershell
rg -n "\b(hou|rpyc|eval|exec|subprocess|socket|sqlite|aiosqlite)\b|run_houdini_python|node\.delete|file\.write|hip\.(save|load|clear)|hda" eee_agent/changesets tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py
rg -n "Any|dict\[str, object\].*operation|Mapping.*operation|repr\(" eee_agent/changesets
```

- [ ] Production package imports no Houdini, legacy bridge, transport,
      database, subprocess, filesystem-write, UI, or agent modules.
- [ ] No secrets, local paths, generated data, tokens, or raw tracebacks appear.
- [ ] No generic operation dictionary or arbitrary effect escape hatch exists.
- [ ] Package exports contain no forbidden operation/effect.
- [ ] Existing read-only Bridge and agent boundaries are untouched.

Search hits in test assertions/comments are acceptable only when they prove a
forbidden behavior is absent. Production hits require line-by-line review.

## 6. Independent commands

Run from the repository root with the implementation commit present and
Codex-owned docs preserved but unstaged from the implementation commit.

```powershell
uv sync --frozen --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_models.py tests/test_core_ids.py -q
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check <parent>..<commit>
git status --short --branch
```

- [ ] Sync and lock checks exit 0 with the expected locked package count.
- [ ] Focused modules pass with no skip/xfail.
- [ ] Regression slice passes with no new skip/xfail.
- [ ] Full offline suite has zero failures and no new skip beyond the optional
      existing WSL probe.
- [ ] Compileall and diff check exit 0.
- [ ] Worktree contains only known Codex docs plus any explicitly identified
      review artifacts; no SQLite/token/discovery/log/generated files.

## 7. Review result and promotion

Classify findings by severity:

- Blocking: security boundary bypass, wrong policy authorization, digest or
  immutability defect, missing stale/ownership constraint, forbidden effect,
  unauthorized file, test failure, or regression.
- Major: contract ambiguity that can produce divergent persisted/wire values,
  missing bound, incomplete receipt/approval invariant, or weak adversarial
  coverage.
- Minor: naming, documentation, duplication, or maintainability issue that
  cannot broaden authority or corrupt identity/digest facts.

For every blocking/major finding, provide:

1. exact file and line;
2. violated design invariant;
3. smallest reproducing test/command;
4. authorized fix files;
5. focused and regression acceptance commands.

Promotion criteria:

- [ ] Diff scope accepted.
- [ ] No blocking or major findings remain.
- [ ] Reviewer-only adversarial cases pass.
- [ ] Focused, regression, full, compile, lock, and diff gates pass.
- [ ] Accepted implementation commit and any follow-up fix commits are recorded
      in the handoff.
- [ ] Only then mark Task 16-A `Complete, Codex accepted` and prepare a separate
      Task 16-B prompt. Never let an implementation report update its own
      acceptance status.
