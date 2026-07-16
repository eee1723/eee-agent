# Task 16-E Independent Review Checklist

## Scope and Authority

- [ ] Changes stay within the authorized plan files.
- [ ] No public `changeset.apply` command or raw operation upload exists.
- [ ] No LLM write tool, arbitrary code, shell, filesystem, HIP/HDA/export, or
      unrestricted Bridge surface was added.
- [ ] Workspace remains context and does not bypass policy/approval/preflight.

## Durable Boundaries

- [ ] Approval consumption, `Approved -> Applying`, and the state event commit
      atomically before Bridge I/O.
- [ ] Receipt, terminal state, state event, and outcome event commit atomically.
- [ ] Broadcast occurs only after commit.
- [ ] Receipt completion is exact-idempotent and conflict-safe.
- [ ] Recovery to `Approved` keeps the approval `Consumed` and explicit retry
      does not consume it again.

## Apply and Cancellation

- [ ] One Apply task exists per `change_id`; concurrent callers cannot duplicate
      a Bridge write.
- [ ] Caller disconnect/cancellation cannot interrupt an accepted write.
- [ ] Shutdown waits through the bounded graceful period and leaves timed-out
      work as recoverable `Applying`.
- [ ] Stop/Force Stop does not interrupt the FIFO transaction after the durable
      write boundary.

## Recovery Truthfulness

- [ ] Recovery checks receipt before facts and never automatically replays.
- [ ] Exact before state returns to `Approved`; exact post state records
      `AlreadyApplied`; neither/ambiguous records `CriticalRecovery`.
- [ ] Instance/epoch drift fails closed.
- [ ] Unprovable create post-state is not reported as success.
- [ ] Transient Bridge absence stays `Applying` and blocks writes without
      permanently fabricating scene corruption.
- [ ] Partial/Critical receipts freeze all further writes.

## Security and Compatibility

- [ ] Production provider uses only loopback discovery and `bridge.token`.
- [ ] Tokens, raw exceptions, full ChangeSets, and parameter values are absent
      from events and errors.
- [ ] Runtime imports neither `hou`, `rpyc`, nor the legacy bridge.
- [ ] Existing Workspace and read-only Runtime behavior remains compatible.
- [ ] Existing WebSocket rejection of `changeset.apply` remains tested.

## Evidence

- [ ] Focused repository/service/provider/Runtime tests pass.
- [ ] Runtime crash/restart process test proves no duplicate effect.
- [ ] Full offline suite passes with no new skip/xfail.
- [ ] `uv lock --check`, compileall, CLI versions/help, and `git diff --check`
      pass.
- [ ] Disposable real-Houdini smoke passes and cleans up without saving.
- [ ] Secret/local-state and changed-file scope scans are clean.
