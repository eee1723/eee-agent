# Runtime Task 17-A2 Handoff - 2026-07-16

## Current State

- Branch: `feature/runtime`
- Task 16-E: accepted in a focused local commit
- Task 17-A1: accepted in local commit `931ac1c`
- Task 17-A2: implemented and offline-accepted in a focused local commit
- Task 17-A3: waiting for the user's real Houdini UI/restart/zero-mutation test
- Task 17-B interactive Run/approval UI: not started

Do not rewrite or discard the Task 16-E, 17-A1, or 17-A2 commits. Do not push,
merge `main`, or add approval/Apply UI as cleanup.

## What 17-A2 Adds

### Houdini-owned Secure Bridge host

`houdini_side/secure_bridge_host.py` now:

- binds exactly `127.0.0.1` on an ephemeral port;
- runs transport I/O on one daemon asyncio thread;
- adopts the accepted `BridgeServer`;
- pumps the accepted `MainThreadReadQueue` only from
  `hou.ui.addEventLoopCallback`;
- processes a bounded batch per Houdini idle callback;
- publishes the independent `bridge.token` and fingerprint-only discovery only
  after bind;
- installs/removes the scene-epoch callback on Houdini's main thread;
- keeps one FIFO for scene, Workspace, preflight, Apply, and receipt operations;
- provides idempotent process-global `start()`, `stop()`, and credential-free
  `status()`;
- remains alive when the panel closes.

### Exact read-only selection query

`query_selection()` uses one short-lived authenticated Bridge client per
refresh:

1. typed `workspace.inspect(mode="selection", scene_epoch=null)` obtains the
   current `SceneBinding`;
2. typed `scene.query` uses that exact non-null scene epoch;
3. instance and epoch identity are cross-checked;
4. one complete two-read retry is allowed after `bridge.stale_scene`;
5. there is no wildcard query, raw HOM, legacy rpyc, or generated Python
   fallback.

### Docked inspector UI

The `.pypanel` now renders:

- independent Runtime and Secure Bridge connection states;
- HIP, selected Runtime Session, and reconnect cursor;
- the signature epoch rail: Houdini instance, scene epoch, and revision;
- exact selection count;
- node path, node type, lock state, points, and primitives;
- bounded empty, stale, offline, auth, and internal-failure states.

Selection network I/O runs on a short-lived daemon worker thread. Houdini facts
still execute only through the Bridge main-thread FIFO.

### Reconnect correction

The panel now recognizes the accepted `session.snapshot` control event and
advances its per-Session cursor from `payload.snapshot_seq`. This closes the
retention-gap case where `seq=null` snapshots could otherwise be requested
again on every reconnect.

### Menu and installation

The existing Houdini package now exposes:

- **Open Runtime Observer**
- **Start Secure Bridge Only**
- **Stop Secure Bridge**

The legacy **Open Agent Panel** and **Start RPC Bridge Only** actions remain
unchanged as rollback paths.

## Automated Acceptance Evidence

```text
Task 17 panel tests:                  29 passed
17-A2 plan gate:                     121 passed
Bridge auth/client/inspector slice:  138 passed
Panel/Bridge/server focused gate:    345 passed
Full offline suite:                  2066 passed, 1 skipped
uv lock --check:                     69 packages, exit 0
compileall:                          exit 0
Runtime CLI help:                    exit 0
XML parse (menu + .pypanel):         exit 0
git diff --check:                    exit 0
```

The only skip remains the existing optional WSL environment probe. No new skip
or xfail was introduced.

The real-composition panel test starts the actual loopback listener,
BridgeServer, token/discovery handoff, and BridgeClient over a deterministic
fake Houdini scene. The main test thread pumps the registered Houdini idle
callback. The result returns the exact selected `/obj/geo1` with 8 points and 6
primitives, then proves both identity files and both Houdini callbacks are
removed on stop.

## Local Houdini/Qt Verification

Against the detected Houdini 21.0.440 installation:

- `secure_bridge_host` imports successfully under Houdini's bundled Python;
- Houdini PySide6 6.5.3 imports after loading the installation's Qt DLL paths;
- `RuntimePanel`, `QHeaderView.ResizeMode`, and
  `QWebSocketProtocol.VersionLatest` resolve against the shipped API;
- an offscreen populated panel rendered successfully at 560 by 680 pixels;
- the preview confirmed the epoch rail, dual status indicators, selection
  count, and four-column node fact table fit the intended narrow layout.

This is supporting evidence only. It does not replace the real docked Houdini
test because headless hython is blocked by the current machine's license
environment.

## Preserved Security Boundary

The panel does not:

- open `app.sqlite` or `checkpoints.sqlite`;
- import or own the agent graph;
- start, stop, or own Runtime;
- call `hou.selectedNodes()` or any HOM method directly;
- import legacy rpyc/hrpyc;
- send `run.start`, Session/Workspace mutation, approval/reject, or Apply;
- display, log, or persist either bearer token;
- stop Secure Bridge when the panel closes.

The Secure Bridge continues to advertise the already accepted typed
`changeset.v1` capability for trusted Runtime Apply. The panel has no route to
that operation.

## Required User Test: Task 17-A3

The next action requires a real Houdini GUI and user observation:

1. install/reload the Houdini package;
2. start Runtime in a terminal;
3. open **EEE Agent > Open Runtime Observer**;
4. dock the panel;
5. verify zero, one, and multiple node selection parity;
6. verify geometry facts and epoch rail;
7. restart Runtime and verify automatic reconnect/cursor continuity;
8. close/reopen the panel and verify Runtime/Secure Bridge stay alive;
9. verify no scene or filesystem mutation.

Exact commands and checkboxes are supplied to the user after the focused
17-A2 commit is created and the worktree is clean.
