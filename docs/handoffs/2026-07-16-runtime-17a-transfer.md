# Runtime Task 17-A Accepted Handoff - 2026-07-16

## Current state

- Branch: `feature/runtime`
- Task 16-E: accepted
- Task 17-A: accepted
- Accepted chain: `931ac1c`, `ffa069f`, `6685b72`
- Full offline baseline: 2068 passed, 1 skipped
- Real Houdini 21.0.440 dock/restart/selection/zero-mutation gate: passed
- Task 17-B interactive Run/approval UI: ready for a separate bounded design

Do not rewrite the accepted Task 16-E or Task 17-A commits, merge `main`, push,
or weaken the trusted Workspace, typed ChangeSet, approval, preflight,
transactional Apply, receipt, recovery, loopback-auth, or single-FIFO
boundaries.

## Accepted product behavior

The EEE Runtime Python Panel:

- authenticates to Runtime through its independent discovery and token files;
- reconnects and resumes each Session from its monotonic cursor;
- recovers retention gaps from `session.snapshot.snapshot_seq`;
- starts or reuses Houdini's loopback-only authenticated Secure Bridge;
- performs a nullable-epoch binding read followed by exact bound
  `scene.query`;
- renders Runtime and Bridge state, HIP, instance, epoch, revision, selection
  count, node path/type/lock state, and bounded geometry facts;
- remains a read-only client with no direct HOM, SQLite, agent graph, legacy
  rpyc, approval, Apply, or scene-write path.

Both Bridge transport and selection refresh use isolated stdlib selector loops
in worker threads so Houdini's process-wide main-thread-only `haio` policy is
not consulted.

## Acceptance evidence

```text
Panel tests:                          31 passed
Task 17-A2 plan gate:                123 passed
Panel/Bridge/server focused gate:    347 passed
Full offline suite:                  2068 passed, 1 skipped
Houdini bundled-Python haio probes:  passed
Real docked Houdini acceptance:      passed
```

The real user test covered zero/one/multiple selections, geometry and epoch
facts, Runtime restart reconnect, panel close/reopen, Bridge survival, and
scene/filesystem zero mutation. The user reported the complete checklist as
“全部通过”.

## Next boundary

Task 17-B may add interactive Runs, ChangeSet preview, approval/rejection
state, stale-precondition display, trusted Apply/receipt rendering, and
active-run close decisions. It must first receive a dedicated design and plan.

It must not expose a legacy unrestricted write route, bypass the existing
typed ChangeSet/approval/preflight/Apply/recovery pipeline, put bearer tokens
in UI state, or move Houdini operations outside the accepted single FIFO.
