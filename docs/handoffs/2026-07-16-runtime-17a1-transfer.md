# Runtime Task 17-A1 Handoff - 2026-07-16

## Current State

- Branch: `feature/runtime`
- Task 16-E: accepted in the preceding focused local commit
- Task 17-A design/plan: added and bounded
- Task 17-A1: implemented and locally accepted
- Task 17-A2 Secure Bridge host/selection inspector: not started
- Task 17-A3 live Houdini UI acceptance: not started
- Task 17-B interactive Run/approval UI: not started

Do not rewrite or discard the Task 16-E or Task 17-A1 commits. Do not push,
merge `main`, or begin approval/Apply UI as cleanup.

## What 17-A1 Adds

- `eee_agent.panel.client_state`
  - strict `runtime.json` and `runtime.token` parsing;
  - exact loopback/port/token-file/protocol checks;
  - SHA-256 token-fingerprint verification;
  - repr-hidden Runtime bearer token;
  - canonical read-only Runtime command builder;
  - compatible response/event parsing with duplicate-key rejection;
  - deterministic active-Session selection;
  - monotonic in-memory `last_seq` cursors per Session.
- `houdini_side.runtime_panel`
  - Houdini-native `PySide6.QtWebSockets.QWebSocket` client;
  - bearer auth through the Authorization header;
  - bounded reconnect delays;
  - stale-socket signal suppression;
  - `runtime.ping`, `session.list`, and `session.subscribe` only;
  - no SQLite, graph, rpyc, Workspace mutation, Run start, approval, or Apply.
- `python_panels/EEEAgentRuntime.pypanel`
  - one menu-visible `eee_agent_runtime` Python Panel interface;
  - dockable/floating Houdini panel creation;
  - panel destruction closes only the client, not Runtime or Bridge.
- Task 17-A design and executable slice plan.

The visual direction uses a compact control-plane observer and an epoch-rail
structure. Slice 17-A1 displays Runtime/Session/cursor lineage; scene epoch and
selected-node facts land in 17-A2.

## Acceptance Evidence

```text
Task 17 panel tests:        18 passed
Panel/protocol/server gate: 219 passed
Full offline suite:         2055 passed, 1 skipped
uv lock --check:            69 packages, exit 0
compileall:                 exit 0
git diff --check:           exit 0
```

The only skip remains the existing optional WSL environment probe. No new skip
or xfail was introduced.

Local Houdini facts verified before implementation:

- Houdini 21.0.440 is installed at
  `C:\Program Files\Side Effects Software\Houdini 21.0.440`;
- Houdini's PySide6 is 6.5.3;
- `PySide6.QtWebSockets.QWebSocket` and
  `QWebSocketProtocol.VersionLatest` are present;
- `hou.paneTabType.PythonPanel` and `.pypanel` `onCreateInterface` are present;
- the installed QtWebSockets type stub confirms the constructor and
  `errorOccurred` signal used by the client.

A headless hython `.pypanel` load was not accepted as evidence because this
machine's current Houdini license environment terminated the process before the
interface query. The real docked UI smoke therefore remains explicitly in
17-A3.

## Security Boundary

17-A1 does not:

- open `app.sqlite` or `checkpoints.sqlite`;
- import the agent graph;
- start or stop Runtime;
- import legacy rpyc/hrpyc;
- send `run.start`, Workspace mutations, approval/reject, or Apply;
- expose or log either token;
- modify Houdini scene state;
- remove or replace `houdini_side/chat_panel.py`.

## Resume Procedure

```powershell
git status --short --branch
git diff --check
uv lock --check
uv run --frozen --extra eval pytest -q
```

Then implement only slice 17-A2 from
`docs/superpowers/plans/2026-07-16-task17-a-docked-readonly-panel.md`:

1. add an idempotent Houdini-owned Secure Bridge host;
2. run transport I/O on one background asyncio thread;
3. pump the accepted single FIFO on the Houdini main thread;
4. obtain a nullable-epoch binding through typed `workspace.inspect`;
5. issue typed `scene.query` with the exact returned epoch;
6. render bounded selection/geometry facts;
7. preserve the client-only, no-write boundary.

Do not begin 17-A3 live acceptance until 17-A2 offline gates pass. Do not begin
17-B without a separate bounded decision.
