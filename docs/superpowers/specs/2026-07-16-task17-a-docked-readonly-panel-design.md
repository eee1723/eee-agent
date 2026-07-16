# Task 17-A Docked Read-Only Panel Design

- Date: 2026-07-16
- Status: approved for bounded implementation
- Parent milestone: Runtime v1 on `feature/runtime`
- Scope: docked read-only Houdini client and selection inspector only

## 1. Goal

Deliver the first production-facing Houdini client for the accepted Runtime and
Secure HoudiniBridge:

- a real dockable `.pypanel`;
- authenticated Runtime discovery and WebSocket connection;
- reconnect with an in-memory `last_seq` cursor per Session;
- snapshot fallback through the accepted Runtime protocol;
- a read-only selection inspector backed by typed `scene.query`;
- visible Runtime, Bridge, HIP, scene-epoch, and selected-node facts.

The panel is an observer. It does not own the Runtime, persistence, agent graph,
or any Houdini write capability.

## 2. Non-goals

Task 17-A does not add:

- Chat, Run start/stop, Session mutation, Workspace mutation, approval, reject,
  ChangeSet preview, Apply, receipt actions, or artifact actions;
- a new Runtime command or event type;
- direct SQLite/checkpoint access;
- a fallback to legacy rpyc, `hrpyc`, generated Python, `eval`, `exec`, shell,
  filesystem browsing, HIP load/clear/save, export, or HDA installation;
- automatic Runtime startup or ownership by the panel;
- token logging, token display, or token persistence outside the accepted
  `runtime.token` and `bridge.token` handoff files;
- replacement or deletion of `houdini_side/chat_panel.py`.

Task 17-B remains responsible for interactive Runs, ChangeSet preview,
approval/rejection, and receipt rendering.

## 3. Ownership and process boundaries

```text
Houdini process
  Python Panel QWidget
    Runtime Qt WebSocket client ------> Runtime loopback WebSocket
    selection query worker -----------> Secure Bridge loopback TCP

Runtime process
  owns app.sqlite, checkpoints.sqlite, Sessions, Runs, Events

Secure Bridge host in Houdini
  owns the listener and one main-thread FIFO
  owns all hou reads
```

The panel may read only the accepted identity handoff files in the shared
Runtime state directory. It never opens either SQLite database.

The Runtime and Bridge tokens remain separate. Runtime credentials authenticate
only Runtime WebSocket requests. Bridge credentials authenticate only the
framed Bridge hello. No token is placed in a URL, command line, environment
variable, UI label, exception message, or telemetry payload.

## 4. Implementation slices

### 17-A1: client contracts and docked Runtime shell

- Add a pure-Python panel client-state module with strict Runtime handoff
  parsing, token-fingerprint verification, command construction, Session
  selection, and monotonic cursor tracking.
- Add a `.pypanel` interface and a Houdini-side QWidget using
  `PySide6.QtWebSockets.QWebSocket`.
- Connect only to `127.0.0.1` from `runtime.json`, with the bearer token in the
  Authorization header.
- On connect: `runtime.ping`, `session.list`, then subscribe to the preferred
  active Session with the remembered cursor.
- On reconnect: reread discovery/token, reconnect to the current Runtime
  identity, and subscribe with the stored `last_seq`.
- Treat `session.snapshot` as the server-provided recovery state. Never invent
  missing Events.

### 17-A2: Secure Bridge host and selection inspector

- Add an idempotent Houdini-owned Secure Bridge host.
- Bind the accepted `BridgeServer` to `127.0.0.1` on an ephemeral port.
- Run transport I/O on a background asyncio thread.
- Pump the accepted `MainThreadReadQueue` from a Houdini/Qt main-thread timer.
- Keep one FIFO for `scene.query`, Workspace, and ChangeSet operations.
- The panel first obtains a current binding through the accepted nullable-epoch
  `workspace.inspect` read, then issues typed `scene.query` with that exact
  scene epoch.
- Each selection refresh uses a short-lived authenticated Bridge client.
- A stale epoch triggers one fresh binding/query cycle; it never weakens to an
  unbound scene query.

### 17-A3: acceptance and packaging

- Install the `.pypanel` through the existing Houdini package path.
- Add menu actions for the Runtime panel and Secure Bridge while preserving the
  legacy panel/RPC actions.
- Verify selection parity, geometry facts, reconnect, snapshot fallback, token
  secrecy, and zero scene mutation in Houdini 21.0.440.

## 5. Runtime client contract

The panel consumes only existing `eee.runtime/1` commands:

- `runtime.ping`
- `session.list`
- `session.subscribe`
- `session.snapshot` only if explicit recovery is needed

Task 17-A does not send Session mutation, Run, Workspace, approval, or reject
commands.

The client keeps:

```text
preferred_session_id: str | None
last_seq_by_session: dict[session_id, non-negative int]
runtime_process_nonce: str | None
connection_generation: non-negative int
```

Rules:

1. A persisted event advances a cursor only when `seq` is an exact positive
   integer and greater than the remembered value.
2. Duplicate or older events are ignored for cursor advancement.
3. A control event with `seq=null` does not change a cursor.
4. Switching Sessions does not discard another Session's cursor.
5. Runtime identity rotation causes a new authenticated connection but does not
   clear cursors.
6. An incompatible major protocol is shown as a terminal compatibility error;
   retry continues only after discovery changes.
7. Unknown `eee.runtime/1` event types may be ignored without disconnecting.

The automatic Session choice is deterministic:

1. retain the preferred Session if it is still active;
2. otherwise choose the active Session with the latest `updated_at`;
3. break ties by `session_id`;
4. if no active Session exists, remain connected with no subscription.

## 6. Bridge selection contract

The panel never calls HOM directly for inspected facts. It consumes the exact
Bridge DTOs:

1. `workspace.inspect(mode="selection", scene_epoch=null)` to obtain the current
   `SceneBinding`;
2. `scene.query(include_selection=true, node_paths=[],
   include_geometry_stats=true)` with the returned exact `scene_epoch`.

The displayed facts are limited to:

- HIP path / `unsaved`;
- Bridge instance identifier, shortened for display only;
- scene epoch;
- observed revision, shortened for display only;
- selected node path, type, parent, display name, lock state;
- bounded geometry statistics returned by the DTO.

No selection is adopted into a Workspace by inspection. Selection remains
context, never authority or write permission.

## 7. Lifecycle and threading

### Runtime WebSocket

`QWebSocket` lives on the Houdini UI thread. Signals update the QWidget on the
same thread. Reconnect uses a single-shot timer with bounded exponential delays:

```text
250 ms, 500 ms, 1 s, 2 s, 5 s, then 5 s
```

Only one live socket and one reconnect timer exist per panel instance.

### Bridge host

The Secure Bridge listener runs in one daemon transport thread with one asyncio
event loop. The `MainThreadReadQueue` is pumped only by a Qt timer on the
Houdini main thread. Shutdown:

1. stops accepting;
2. rejects queued work;
3. lets an already-running typed operation finish;
4. closes writers;
5. removes Bridge identity files;
6. removes scene callbacks;
7. stops the transport loop/thread.

The panel is not the owner of that lifecycle. Closing a panel never stops the
Bridge or Runtime.

## 8. Visual direction

Subject: a Houdini technical artist inspecting the live control plane and the
exact scene selection. The panel's single job in 17-A is to answer: “What
Runtime and scene am I looking at right now?”

Palette:

- Graphite `#17191D`: base
- Slate `#20242A`: raised surfaces
- Iron `#343A43`: dividers and inactive tracks
- Houdini amber `#FF7A1A`: current/active state
- Survey cyan `#63C7C9`: read-only observed facts
- Signal red `#E45B55`: failures

Type:

- restrained display: Bahnschrift SemiCondensed
- body: Segoe UI
- identifiers/data: Consolas

Layout:

```text
┌ EEE / LIVE SCENE ───────── Runtime ●  Bridge ● ┐
│ HIP / Session                                     │
├ epoch rail: instance ── epoch ── revision ───────┤
│ SELECTED 02                              Refresh │
│ /obj/geo1     geo       points 8 / prims 6      │
│ /obj/camera1  cam       no geometry              │
└ bounded status/error direction ──────────────────┘
```

Signature element: the epoch rail. It turns scene identity into a compact
lineage (`instance -> epoch -> revision`) instead of presenting three unrelated
badges. Amber marks the current epoch; cyan marks facts observed under it.

Self-critique: a generic chat/sidebar layout was rejected because 17-A is an
inspector, not a conversation surface. Decorative cards and broad gradients
were removed. The single visual risk is the epoch rail; everything else stays
quiet and Houdini-native.

## 9. Failure behavior

- Runtime absent: “Runtime is not running” and scheduled reconnect.
- Runtime identity malformed: “Runtime identity could not be verified.”
- Runtime unauthorized: authentication error; token is never shown.
- Bridge absent: Runtime status remains usable; selection area says the Secure
  Bridge is unavailable.
- No Session: connected state with “No active Session.”
- No selection: valid empty state, not an error.
- Stale scene: retry one bound query; if it remains stale, show “Scene changed;
  refresh again.”
- Slow consumer: reconnect and resubscribe from the remembered cursor.
- Unknown compatible event: ignore and retain connection.
- Malformed/incompatible envelope: close that connection and show a bounded
  protocol error.

## 10. Test and acceptance contract

Offline tests prove:

- strict Runtime handoff parsing and fingerprint verification;
- loopback-only host/valid port/token filename requirements;
- token exclusion from repr/errors;
- deterministic Session choice;
- monotonic per-Session cursor behavior;
- command/envelope strictness and duplicate-key rejection;
- `.pypanel` XML shape and rollback-panel preservation;
- Runtime panel source does not import SQLite, the agent graph, legacy rpyc, or
  any write executor;
- Bridge host uses the accepted `BridgeServer` and one
  `MainThreadReadQueue`.

Houdini 21.0.440 acceptance proves:

1. the interface appears as a Python Panel and docks normally;
2. Runtime authentication and ping succeed;
3. disconnect/restart reconnects and resubscribes from `last_seq`;
4. snapshot fallback produces a coherent view;
5. selected path/type match Houdini selection;
6. scene epoch and bounded geometry facts display;
7. before/after node, parameter, wire, HIP, HDA, and filesystem facts are
   unchanged;
8. closing/reopening the panel does not stop Runtime or Bridge;
9. old `chat_panel.py` remains usable as the rollback path.

## 11. Promotion rule

17-A is accepted only after all three slices pass their gates and the real
Houdini selection smoke records zero mutation. Task 17-B starts only in a new
bounded design/plan. No 17-A cleanup may add an Apply button or public
`changeset.apply` Runtime command.
