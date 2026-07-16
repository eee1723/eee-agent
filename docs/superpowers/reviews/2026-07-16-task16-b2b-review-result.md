# Task 16-B2b Trusted Workspace Lifecycle Review Result

## Verdict

Task 16-B2b is accepted on `feature/runtime`.

The accepted implementation tip is:

```text
b2a1b80 test: harden workspace lifecycle acceptance
```

The implementation chain is:

```text
17d9371 feat: inspect trusted houdini workspaces
c9b2047 feat: persist trusted workspace lifecycle
776e42b feat: expose trusted workspace lifecycle
b2a1b80 test: harden workspace lifecycle acceptance
```

Independent final review found:

- Critical findings: none
- Important findings: none
- Blocking minor findings: none

The acceptance authority remains:

1. `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
2. `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`
3. `docs/superpowers/reviews/2026-07-16-task16-b2b-review-checklist.md`

## Accepted Behavior

B2b activates exactly four Runtime commands:

```text
workspace.create
workspace.bind
workspace.switch
workspace.inspect
```

The accepted lifecycle has these boundaries:

- A Workspace identifies the trusted Houdini scene context for a Session. It
  does not grant write authority.
- Explicit user intent outranks incidental node selection.
- Selection supplies context only for `workspace.create` and
  `workspace.bind`. `workspace.switch` inspects the complete stored manifest
  and ignores current selection.
- `workspace.create` registers exactly the selected nodes that already carry
  all six EEE ownership mirrors. It does not create, mark, adopt, or traverse
  Houdini nodes.
- Only nodes created through the accepted typed EEE ChangeSet executor receive
  the six ownership mirrors:
  `eee.workspace_id`, `eee.node_id`, `eee.capability`, `eee.role`,
  `eee.schema_version`, and `eee.created_by_run`.
- `workspace.bind` requires the exact stable-node-ID set. It can refresh
  location and scene binding facts, but cannot change ownership identity.
- `workspace.switch` performs a live complete-manifest proof before atomically
  changing the Session's active Workspace.
- `workspace.inspect` reports `Healthy`, `Stale`, `Conflict`, or
  `BridgeUnavailable` without changing Houdini, persistence, or events.
- Create, bind, and switch fail closed when live Bridge facts are unavailable.
- Workspace persistence and its durable event commit atomically. Event
  notifications happen only after commit. Failures and exact no-ops emit no
  event.
- Existing typed policy, approval, preflight, single-FIFO Apply, receipt,
  rollback, and recovery boundaries remain the only write path.

## Architecture Accepted

### Bridge and Houdini inspection

- The Bridge capability is exactly `workspace.v1`; the operation is exactly
  `workspace.inspect`.
- Workspace inspection shares the existing authenticated, bounded,
  main-thread FIFO used by scene queries and typed ChangeSet operations.
- There is no second HOM worker, queue, generic method dispatcher, arbitrary
  Python execution, shell path, or Houdini write surface.
- The production fact provider opens a fresh discovered Bridge connection for
  each inspection, so Houdini restarts and discovery changes are naturally
  re-read.
- Runtime and Bridge identity/token files remain separate.

### Persistence and service

- Runtime schema version 3 adds the per-Session active Workspace state and its
  monotonically increasing `state_revision`.
- Workspace manifest changes, active-pointer changes, and durable events use
  the accepted cancellation-safe transaction pattern.
- `WorkspaceService` owns identity and health rules and is independent of
  sockets, WebSockets, HOM, and concrete Bridge transport.
- Runtime constructs the Workspace service over the shared ChangeSet
  repository/EventStore path; no duplicate event stream exists.

### Public Runtime protocol

- The four commands accept exact, bounded payloads only.
- Clients cannot upload a manifest, node list, path list, ownership mirrors,
  roles, capabilities, or arbitrary metadata through these commands.
- Runtime server routing is a thin service adapter and does not inspect SQL,
  Bridge DTOs, or Houdini facts directly.

## Review Remediation

The final review cycle hardened the process and real-Houdini acceptance
harness:

- Cross-process Workspace control JSON is published through a same-directory
  temporary file, flushed and `fsync`ed, then atomically replaced. Readers
  cannot observe partially written JSON.
- The process E2E proves stale and identity-conflict switches preserve the
  active pointer and `state_revision` and append no event.
- Runtime subprocess stderr is drained continuously into a bounded buffer.
- Graceful subprocess shutdown is time-bounded.
- A deliberately hanging Runtime/Bridge fixture proves forced process-tree
  cleanup, identity-file cleanup, dead launcher/worker PIDs, and subsequent
  Runtime lock reacquisition.
- The real Houdini smoke uses strict fact reads, public Runtime commands,
  separate Runtime and Bridge identity, event replay, explicit cleanup, and a
  standard Python Runtime subprocess rather than running the WebSocket server
  inside Houdini's `haio` event loop.

The last point avoids known Houdini 21.0.440 `haio` shutdown incompatibilities:
its transport/server integration lacks several interfaces expected by the
Runtime WebSocket server. No private-field workaround was retained in
production or acceptance code.

## Acceptance Evidence

The final accepted code produced:

```text
Runtime process E2E:       9 passed
B2b-3 focused gate:        325 passed
Cross-slice regression:    621 passed
Full offline suite:        2018 passed
uv lock --check:           69 packages, exit 0
compileall:                exit 0
git diff --check:          exit 0
New skip/xfail:            none
```

The real Houdini 21.0.440 acceptance command on Machine B was:

```powershell
& 'D:\houdini\bin\hython.exe' -u tests\runtime\changeset_houdini_smoke.py
```

It exited zero in approximately 7.2 seconds, printed both:

```text
B2B SMOKE OK
SMOKE OK
```

and destroyed its disposable `/obj/eee_task16d_smoke_bound` root without
saving. A non-fatal Qt timer-thread warning was printed after the assertions;
it did not change the zero exit code or cleanup result.

## Promotion Boundary

This acceptance authorizes documenting and pushing `feature/runtime` when the
user explicitly requests it. It does not authorize:

- merging into `main`;
- force-pushing, rebasing, or rewriting accepted commits;
- starting Task 16-E, Task 17, Task 18, or UI work without a new bounded plan;
- weakening Workspace identity, typed ChangeSet, approval, preflight,
  single-FIFO Apply, rollback, receipt, or recovery boundaries.
