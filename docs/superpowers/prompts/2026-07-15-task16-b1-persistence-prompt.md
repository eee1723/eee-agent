# Task 16-B1 Implementation Prompt

Use Claude Code with the explicit model `glm-5.2[1m]` for this task. Execute
only this persistence slice. Do not delegate, start a second worker, or begin
Task 16-B2, Task 16-C, or any Bridge/Houdini work.

## Objective

Implement the persistence foundation for Task 16: an additive, checksum-
protected schema v2 and a strict repository for `WorkspaceManifest`,
`ChangeSet`, approval records, and change receipts. The repository must round-
trip the frozen Task 16-A DTOs without changing their canonical JSON or digest,
enforce foreign keys and unique identities, and expose transaction-safe
primitives for the later approval service.

This slice does not activate any Runtime command and does not perform event
orchestration. Task 16-B2 will add the service/protocol seam after this commit
has been independently reviewed.

## Required reading

Read completely, in order:

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`
3. `docs/superpowers/plans/2026-07-15-typed-changeset-policy.md`
4. `docs/superpowers/reviews/2026-07-15-task16-a-review-result.md`
5. `eee_agent/runtime/migrations.py`
6. `eee_agent/runtime/database.py`
7. `eee_agent/runtime/models.py`
8. `eee_agent/runtime/events.py`
9. `eee_agent/runtime/sessions.py` and `eee_agent/runtime/runs.py`
10. `eee_agent/changesets/contracts.py` and `eee_agent/changesets/policy.py`
11. Existing database/event tests and the new Task 16-A contract tests

The approved Task 16 design is authoritative. If a required behavior is
ambiguous or conflicts with an existing database invariant, stop and report
the conflict instead of weakening the design or inventing a permissive schema.

## Starting state and worktree ownership

- Branch: `feature/runtime`
- Task 16-A is Codex-accepted at `79f281d`.
- Preserve all Codex-owned uncommitted documentation. Do not edit, stage,
  commit, stash, restore, or delete any file under `docs/`.
- Before editing, record `git status --short` and `git diff --name-only`.
- Stage only the authorized implementation/test paths below. Never use
  `git add .` or `git add -A`.

## Authorized files

You may modify only:

- `eee_agent/runtime/migrations.py`
- `eee_agent/runtime/database.py`
- create `eee_agent/changesets/repository.py`
- create `tests/runtime/test_changeset_repository.py`
- modify `tests/runtime/test_database.py` only for migration/checksum coverage

Do not modify `eee_agent/runtime/protocol.py`, `server.py`, `service.py`,
`runtime/__init__.py`, `eee_agent/changesets/contracts.py`, Bridge files,
Houdini-side files, UI, agent tools, dependencies, lock files, or docs. If a
change outside this list appears necessary, stop and report it.

## Explicit non-goals and forbidden shortcuts

- Do not activate `workspace.*` or `changeset.*` public commands.
- Do not add EventStore/service callbacks or broadcast events in this slice.
- Do not add proposal generation, policy evaluation, approval UI, Apply,
  preflight, rollback, Bridge transport, `hou`, `rpyc`, shell, filesystem,
  HIP/HDA, or arbitrary code execution.
- Do not add a second SQLite database or bypass `RuntimeDatabase`.
- Do not use `executescript`, implicit transactions, `repr()` hashing, or
  permissive JSON parsing.
- Do not store raw Python objects, pickles, arbitrary operation dictionaries,
  tokens, tracebacks, or unbounded payloads.
- Do not weaken existing v1 schema/data or existing Runtime database tests.

## Required persistence behavior

### Migration and checksum

- Keep schema v1 data valid and apply v2 additively and transactionally.
- Schema v2 adds exactly the four design tables: `workspaces`, `changesets`,
  `approvals`, and `change_receipts`.
- Preserve the existing migration history API, but make every applied
  migration row carry a deterministic SHA-256 checksum of its exact SQL
  script. Existing v1 databases without the checksum column must be upgraded
  transactionally and backfilled with the known v1 checksum.
- A tampered migration checksum, duplicate/non-contiguous history, future
  version, malformed DDL, or failed migration must fail closed and leave the
  database usable after reopening.
- Keep foreign keys enabled and use explicit `BEGIN IMMEDIATE`/`COMMIT`/
  `ROLLBACK`; no partial v2 schema may be visible after a failed migration.

### Stored records

- Store canonical DTO JSON and its digest together. The stored digest must be
  recomputed and checked on read; never trust a database digest blindly.
- `workspaces` must bind a manifest to its session, creating run, instance,
  scene epoch, revision, and update timestamp.
- `changesets` must bind a ChangeSet to its session/run, preserve nullable
  workspace IDs, store its exact digest, and persist the state machine values:
  `Proposed`, `AwaitingApproval`, `Approved`, `Applying`, `Applied`,
  `RolledBack`, `CriticalRecovery`, `Stale`, `Rejected`, and `Expired`.
- `approvals` must enforce one approval identity and one ChangeSet digest per
  persisted approval, with a foreign key to the ChangeSet and exact
  state-dependent DTO JSON.
- `change_receipts` must enforce one terminal receipt per `change_id`, bind
  status/instance/scene epoch, and preserve the complete bounded receipt DTO.
- IDs, digests, state/status values, timestamps, and JSON must have strict
  CHECK/UNIQUE/FOREIGN KEY constraints where SQLite can enforce them; repeat
  the same checks in Python before writes.
- Deleting a session must cascade its dependent workspace/ChangeSet/
  approval/receipt rows without leaving orphan records.

### Repository API

Create a focused repository with immutable return records or the original
DTOs and explicit methods for:

- insert/get/list workspace manifests;
- insert/get/list ChangeSets and compare-and-set state transitions;
- insert/get/update approvals with expected-decision and digest checks;
- atomically consume an approved, unexpired approval for a ChangeSet;
- insert/get change receipts with idempotent duplicate handling; and
- restart-safe queries for every non-terminal ChangeSet and every receipt.

Repository methods must validate exact IDs and DTO types, use the existing
`RuntimeDatabase.write_transaction()` for every mutation, and never expose a
live SQLite connection. Concurrent consumers must serialize so at most one
caller can consume an approval. A failed transaction must leave all affected
rows and state unchanged.

Private strict decoders may reconstruct Task 16-A DTOs from canonical JSON,
but they must reject unknown fields, wrong tags/enums, duplicate keys,
non-UTC timestamps, digest mismatches, and malformed nested DTOs. Round-trip
must satisfy `decoded.to_dict() == original.to_dict()` and equal digest.

## Test-first sequence

1. Add `tests/runtime/test_changeset_repository.py` and migration tests first.
2. Run the focused tests and record genuine RED failures caused by missing
   persistence implementation.
3. Implement checksum-aware migration support, then repository storage.
4. Add adversarial tests before declaring GREEN.

Required tests include:

- fresh v1→v2 migration, reopening, checksum verification, and unchanged v1
  session/run/event behavior;
- upgrade of a legacy `schema_migrations` table without checksum metadata;
- tampered checksum, future version, duplicate history, failed DDL, and
  rollback/no-partial-table behavior;
- exact DTO/digest round-trip for manifest, ChangeSet, approval, and receipt;
- foreign-key rejection, session cascade, unique IDs, duplicate digest
  mismatch, malformed JSON, unknown fields/tags, and bounded payloads;
- compare-and-set state transitions and invalid transition rejection;
- approval expiry, exact digest mismatch, rejected/consumed/expired states,
  and concurrent single-use consumption where exactly one caller succeeds;
- receipt idempotency and rejection of conflicting receipts;
- reopen/restart queries for Applying, Approved, and CriticalRecovery records;
- proof that the repository imports no `hou`, `rpyc`, legacy bridge, network,
  subprocess, or filesystem-write modules.

## Required verification

RED evidence:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_repository.py tests/runtime/test_database.py -q
```

Focused acceptance:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_repository.py tests/runtime/test_database.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run python -m compileall -q eee_agent tests
uv lock --check
git diff --check
git status --short
```

Then run the full offline suite:

```powershell
uv run --extra eval pytest -q
```

The pre-existing optional WSL probe may remain the only skip. No new skip or
xfail is accepted.

## Commit and handoff

Review the final diff and stage only the authorized paths. Commit exactly once:

```text
feat: persist typed changeset records
```

Return a concise handoff containing the commit hash/parent, exact file list,
genuine RED evidence, focused/full counts and skips, compile/lock/diff results,
migration checksum strategy, repository concurrency behavior, and any concern
for Codex review.

Do not push, merge, amend, clean the worktree, update Codex status documents,
or begin Task 16-B2. Codex will independently review this commit.
