# Runtime Task 17-B Offline Handoff - 2026-07-16

## Current state

- Branch: `feature/runtime`
- Task 16-E: accepted
- Task 17-A: accepted by the user's real Houdini test
- Task 17-B design: `cc96acc`
- Task 17-B implementation: `13e0782`
- Qt WebSocket delivery fix: `423a0e4`
- Focused frame-delivery hotfix gate: 172 passed
- Full offline baseline: 2104 passed, 1 skipped
- Real Houdini Task 17-B gate: pending

Do not rewrite the accepted Task 16-E/17-A commits, `13e0782`, or `423a0e4`,
merge `main`, push, or weaken the trusted Workspace, typed ChangeSet, approval,
preflight, transactional Apply, receipt, recovery, loopback authentication,
or single-main-thread-FIFO boundaries.

## First real-test finding and correction

The first Task 17-B real test reached Runtime and created two Sessions, but the
panel remained unable to select them or enable Start. Runtime protocol
envelopes were serialized as UTF-8 bytes, so the WebSocket library sent binary
frames. Qt connected successfully but the panel listened only to
`textMessageReceived`; consequently every response and event was silently
missed.

`423a0e4` corrects both ends:

- Runtime now sends JSON protocol envelopes as text WebSocket frames;
- the panel also accepts binary JSON frames for compatibility with an
  already-running pre-fix Runtime;
- the New Session dialog now explicitly distinguishes the short Session title
  from the Run request and shows visible creating/waiting feedback.

Using Houdini 21.0.440's bundled PySide6 against the still-running old Runtime,
the corrected full panel observed both existing Sessions, selected the newest,
recovered its snapshot, rendered `READY`, enabled Start, and left Stop disabled.

## What Task 17-B adds

The docked **EEE Runtime** panel is now a bounded interactive control plane:

- create and select active Runtime Sessions;
- start a Run from a bounded prompt;
- request cooperative Stop or confirmed Force Stop;
- rebuild Run status, final output, and recent tool activity from snapshots and
  ordered persisted events;
- reconnect without treating a disconnect or panel close as cancellation;
- list the newest 50 ChangeSets for the selected Session through the new
  read-only `changeset.list` command;
- display bounded permission, risk flags, effect names, affected paths,
  approval expiry/decision, receipt revisions, and critical-recovery evidence;
- approve or reject only the exact displayed `change_id` plus canonical digest;
- preserve the accepted typed Scene selection inspector.

The visual direction remains a narrow graphite Houdini control plane with
`RUN`, `APPROVALS`, and `SCENE` surfaces. Pending authorization alone receives
the amber approval-gate treatment. Populated Run and approval states were
rendered with Houdini 21.0.440's bundled PySide6 at 560 x 760 during offline
review.

## Security boundary

Task 17-B does not add:

- a public `changeset.apply`;
- raw operation JSON or parameter values;
- direct panel access to SQLite, the agent graph, checkpoints, or Runtime
  process lifetime;
- direct scene mutation from the panel;
- a panel-side legacy rpyc fallback;
- automatic approval or automatic replay of Apply;
- Task 18 compiler behavior.

`changeset.list` is scoped by exact Session ID, newest-first, limited to 50,
and read from one consistent repository transaction. Each summary caps effect
names and affected paths at 12 and omits operations, parameters, conditions,
tokens, tracebacks, and Bridge internals. Repository corruption fails closed.

The current Runtime agent's existing read-only Houdini tools still use the
localhost legacy rpyc bridge behind an exact allowlist. Start **Start RPC Bridge
Only** when the manual Run is expected to call those tools. This transport is
not imported or owned by the panel; the panel selection inspector continues to
use the authenticated Secure Bridge.

## Verification evidence

```text
Focused hotfix gate:      172 passed
Full offline suite:       2104 passed, 1 skipped
uv lock --check:          passed, 69 packages
compileall:               passed
git diff --check:         passed
Boundary source scan:     no Apply/workspace-create/SQLite/rpyc/eval/exec path
Live old-Runtime Qt test: 2 Sessions, selected/snapshot/READY/Start enabled
```

The only skipped test is the existing optional WSL/Windows environment probe.
No live LLM or Houdini scene is required by the offline suite.

## Required real Houdini acceptance

1. Restart Houdini so the corrected Python module reloads.
2. Start Runtime from the repository:

   ```powershell
   uv run --frozen --extra eval python -m eee_agent.runtime serve
   ```

3. In Houdini choose **EEE Agent > Start RPC Bridge Only**, then
   **EEE Agent > Open Runtime Control**.
4. Select the existing short `TEST` Session, or create a new Session using only
   a short title such as `17B smoke`. Put the following prompt in **RUN
   REQUEST**, not in the New Session dialog:

   ```text
   只调用 hou_status 检查 Houdini 连接状态，然后用中文简短报告结果。不要调用其他 Houdini 工具，不要创建、修改、保存或导出任何内容。
   ```

   Start the Run. Confirm Planning/tool activity/output/Completed and zero
   scene mutation.
5. Close and reopen the panel. Confirm the Session and terminal Run recover.
6. Restart Runtime. Confirm reconnect and snapshot recovery without duplicate
   output or a false cancellation.
7. Start another text-only Run and request Stop while it is active. Confirm it
   converges through Stop Requested/Stopping to Cancelled. If it completes too
   quickly, repeat with a longer response request.
8. Confirm **APPROVALS** shows `00` / no pending approval and contains no Apply
   button. This is expected until Task 18 creates trusted proposals.
9. Recheck zero/one/multiple Scene selections and the epoch/node facts.
10. Confirm no scene or repository file mutation attributable to the panel.

After the user reports the complete checklist, add a Task 17-B review result
with the exact evidence. Do not start Task 18 as a side effect of acceptance;
it requires its own bounded design and plan.
