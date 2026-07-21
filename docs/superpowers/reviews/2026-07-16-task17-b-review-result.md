# Task 17-B Acceptance Result

- Review date: 2026-07-16 (Asia/Shanghai)
- Design: `cc96acc`
- Implementation: `13e0782`
- Runtime/panel corrections: `423a0e4`, `15b6c00`, `73c6214`,
  `7d8d552`, `5174378`
- Result: **Accepted**

## Delivered boundary

Task 17-B turns the accepted read-only Houdini panel into a bounded Runtime
control plane. It creates and selects Sessions, starts and cooperatively stops
Runs, reconstructs bounded Run state and output after reconnect, lists bounded
durable ChangeSet summaries, and sends exact approval or rejection decisions.

The panel still does not own Runtime, the graph, checkpoints, SQLite, or
Houdini mutation. It exposes no public Apply command, raw operation JSON,
parameter values, automatic approval, legacy panel-side rpyc fallback, or Task
18 compiler behavior.

## Automated evidence

- Focused panel/server gate: **174 passed**
- Full offline suite: **2106 passed, 1 skipped**
- `uv lock --check`: passed, 69 packages
- compileall and `git diff --check`: passed
- The only skip is the existing optional WSL/Windows environment probe.

## Real Houdini evidence

The complete gate ran with Houdini 21.0.440 and the local Runtime on
`127.0.0.1:8112`.

- Session creation, selection, remembered/default selection, and panel reopen
  passed. Both the Session title and Run Request fields accepted Chinese IME
  input without losing focus or treating candidate-confirmation Enter as an
  action.
- A read-only Run called only `hou_status` and reported the connected Houdini
  version, `untitled.hip`, and 24 fps in Chinese. It completed without scene,
  export, or repository mutation.
- Closing and reopening the panel recovered a Session with more than 300
  persisted events without ONLINE/OFFLINE flashing, duplicate output, or
  disabled controls.
- Runtime restart recovery passed with PID `39348`, process nonce
  `S1A3-yWq62Qh89PRNu0iww`, and start time
  `2026-07-16T14:41:30.606303+00:00`. The prior terminal Run and output
  remained available after reconnect.
- Cooperative Stop passed in the Chinese Session `钱钱钱`
  (`ses_336b418713ad481e9c7d7c6e2c436899`). Its latest two Runs converged to
  `Cancelled`; the final one is
  `run_fb84a6c998be4c0e81c5fab6cabc49a4`, with snapshot boundary 1156 and no
  active Run.
- APPROVALS showed `00` / no pending approval and no Apply button. Runtime
  inspection found zero ChangeSets in every test Session, as expected before
  Task 18.
- Scene selection refresh and epoch/node facts still matched Houdini, and the
  user observed no scene or repository mutation attributable to the panel.

The user reported the final checklist as “全部通过”. Task 17-B is accepted.
Task 18 has not started and requires its own bounded design and plan.
