# Task 16-B1 Independent Review Checklist

- Reviewer: Codex
- Review target: Task 16-B1 persistence implementation commit
- Promotion result: `Accepted`, `Changes requested`, or `Rejected`
- Rule: do not promote Task 16-B2 until every blocking item passes

## 1. Intake and scope

- [ ] Record implementation commit and parent; parent descends from accepted
      Task 16-A tip `79f281d`.
- [ ] Verify reported RED was caused by missing persistence behavior.
- [ ] Confirm implementation commit contains only:
      `eee_agent/runtime/migrations.py`, `eee_agent/runtime/database.py`,
      `eee_agent/changesets/repository.py`,
      `tests/runtime/test_changeset_repository.py`, and authorized additions
      to `tests/runtime/test_database.py`.
- [ ] Confirm no Codex-owned documentation was staged or overwritten.
- [ ] Confirm no protocol, Runtime service/server, Bridge, Houdini-side, UI,
      agent, dependency, lock, token, SQLite, or generated files were added.

## 2. Migration integrity

- [ ] Fresh database reaches schema v2 transactionally.
- [ ] Existing v1 database opens and preserves sessions, runs, events, replay
      floors, and runtime state.
- [ ] Every applied migration has a deterministic SHA-256 checksum tied to the
      exact SQL text; checksum comparison is performed before use.
- [ ] Legacy migration metadata without checksum is upgraded safely and
      backfilled only with the known checksum.
- [ ] Tampering, duplicate/non-contiguous history, future versions, and failed
      DDL fail closed.
- [ ] A migration failure leaves no half-applied v2 tables or migration row.
- [ ] `PRAGMA foreign_keys=ON` remains enabled and no `executescript` or
      implicit transaction path was introduced.

## 3. Schema and constraints

- [ ] Exactly the four approved v2 tables are added.
- [ ] Workspace rows bind manifest/session/creating-run/instance/epoch/revision.
- [ ] ChangeSet rows preserve exact canonical payload, digest, session/run,
      nullable workspace, and all approved state values.
- [ ] Approval rows bind one ChangeSet and exact digest with strict decision
      data.
- [ ] Receipt rows are unique by `change_id`, preserve bounded receipt JSON,
      and bind terminal status/instance/epoch.
- [ ] Foreign keys and session cascade behavior leave no orphan records.
- [ ] Unique constraints reject conflicting duplicate IDs and duplicate
      ChangeSet/approval/receipt identities.
- [ ] Stored timestamps are canonical UTC strings and status values are closed
      sets, not arbitrary text.

## 4. Repository behavior

- [ ] All mutations use `RuntimeDatabase.write_transaction()`.
- [ ] No repository method leaks a live SQLite connection.
- [ ] Read paths recompute and verify canonical DTO digest before returning.
- [ ] Round-trip preserves `to_dict()` exactly for WorkspaceManifest, ChangeSet,
      ApprovalRecord, and ChangeReceipt.
- [ ] Unknown JSON fields/tags, duplicate keys, wrong IDs/enums, malformed
      nested DTOs, wrong digest, and oversized payloads are rejected.
- [ ] Compare-and-set state transitions reject stale expected state and illegal
      transitions without changing the row.
- [ ] Approval consumption checks exact ChangeSet digest, expiry, binding, and
      decision; concurrent consumers allow exactly one successful consume.
- [ ] Receipt insertion is idempotent only for the same receipt and rejects a
      conflicting terminal receipt.
- [ ] Restart queries return Applying/Approved/CriticalRecovery records with no
      in-memory-only state.

## 5. Security and boundary review

Run:

```powershell
rg -n "\b(hou|rpyc|eval|exec|subprocess|socket)\b|run_houdini_python|pickle|executescript" eee_agent/changesets/repository.py eee_agent/runtime/migrations.py eee_agent/runtime/database.py
```

- [ ] Production persistence code imports no Houdini, legacy bridge, network,
      subprocess, UI, or agent modules.
- [ ] No token, raw traceback, arbitrary object, pickle, or unbounded payload is
      persisted.
- [ ] No public Runtime command or Apply path was activated accidentally.
- [ ] Existing Runtime v1 transaction/event behavior remains unchanged.

## 6. Independent commands

```powershell
uv sync --frozen --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest tests/runtime/test_changeset_repository.py tests/runtime/test_database.py -q
uv run --extra eval pytest tests/runtime/test_changeset_repository.py tests/runtime/test_database.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check <parent>..<commit>
git status --short --branch
```

- [ ] Focused and full tests pass with no new skip/xfail beyond the existing
      optional WSL probe.
- [ ] Lock, compile, and diff checks pass.
- [ ] Worktree contains only known Codex documentation plus expected review
      artifacts.

## 7. Promotion

Blocking findings include migration corruption, digest/round-trip mismatch,
foreign-key bypass, approval double-consumption, unauthorized files, any test
failure, or any activation of protocol/Bridge/write behavior.

- [ ] No blocking or major findings remain.
- [ ] Accepted commit and any authorized follow-up fixes are recorded.
- [ ] Only after this checklist passes may Task 16-B2 be prepared.
