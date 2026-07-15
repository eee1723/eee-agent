# Task 16-B2a Independent Review Result

- Review date: 2026-07-16 (Asia/Shanghai)
- Implementation: `8fed692` — `feat: persist changeset approvals in runtime`
- Follow-ups: `1230d07`, `7b3bff8`
- Accepted tip: `7b3bff8`
- Result: **Accepted, Codex accepted**

## Scope and behavior

The accepted chain implements trusted internal proposal plus public
`changeset.approve`/`changeset.reject`, with coupled ChangeSet/approval/event
transactions and post-commit subscriber notification. Workspace lifecycle,
Bridge preflight, Apply, and Houdini writes remain unavailable.

The implementation chain changes only the authorized B2a files. No migration,
contract, policy, Bridge, Houdini-side, UI, dependency, or documentation file
was included in its commits.

## Independent evidence

- Final B2a focused slice: **348 passed**
- Final full offline suite: **1670 passed, 1 skipped**
- The only skip is the existing optional WSL environment probe.
- `compileall eee_agent houdini_side tests`: passed
- `uv lock --check`: passed, 69 packages
- `git diff --check 54f2989..7b3bff8`: passed

## Findings and resolutions

Codex found that approval payload digests were checked but denormalized
approval identity columns were not compared with the decoded ApprovalRecord.
`1230d07` adds read-side identity/digest/decision consistency checks for normal
and combined decision reads. `7b3bff8` applies the same fail-closed check to
the future single-use `consume_approval()` boundary. Regression tests prove a
tampered approval identity cannot be read, decided, or consumed and produces
no state/event mutation.

Task 16-B2a is complete. Task 16-B2b workspace lifecycle protocol is unblocked
for a separate prompt; it has not started.
