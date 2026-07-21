# Task 16-B1 Independent Review Result

- Review date: 2026-07-16 (Asia/Shanghai)
- Implementation: `a3914f5` — `feat: persist typed changeset records`
- Follow-up: `54f2989` — `fix: preserve approval identity in repository`
- Parent chain: `79f281d` → `a3914f5` → `54f2989`
- Result: **Accepted, Codex accepted**

## Scope

The combined implementation chain changes exactly the authorized files:

- `eee_agent/runtime/migrations.py`
- `eee_agent/runtime/database.py`
- `eee_agent/changesets/repository.py`
- `tests/runtime/test_changeset_repository.py`
- `tests/runtime/test_database.py`

No Runtime protocol/service/server, Bridge, Houdini-side, UI, agent, dependency,
or documentation file was included in either implementation commit.

## Independent evidence

- B1 focused slice after the follow-up: **302 passed**
- Full offline suite after the follow-up: **1630 passed, 1 skipped**
- The only skip is the pre-existing optional WSL environment probe
  (`tests/test_env_probe.py:65`); no new skip or xfail was introduced.
- `compileall eee_agent houdini_side tests`: passed
- `uv lock --check`: passed, 69 packages
- `git diff --check 79f281d..54f2989`: passed

The focused tests cover fresh and legacy v1→v2 migration, checksum tampering,
atomic migration rollback, strict DTO/digest round-trip, foreign keys,
compare-and-set state transitions, approval expiry/digest/single-use and
concurrent consumption, receipt idempotency/conflicts, restart queries, and
strict decoder rejection.

## Finding and resolution

Independent review reproduced an identity drift in `update_approval`: a valid
record with a different `approval_id` could update `payload_json` while the
SQLite identity column retained the original ID. Claude fixed this in
`54f2989` by loading and enforcing the persisted approval identity, and added a
regression test proving the update is rejected and the original row remains
unchanged.

## Accepted boundary

The repository intentionally does not emit events or combine approval
consumption with the ChangeSet `Applying` transition. That atomic service-level
operation is deferred to Task 16-B2, which may extend the repository seam
within its explicitly authorized scope. No public approval or Apply command is
activated by B1.

Task 16-B1 is complete. Task 16-B2 is unblocked for a separate prompt and
implementation commit; it has not started.
