# Secure HoudiniBridge Read-Only Contract

- Date: 2026-07-15
- Status: proposed for review
- Parent milestone: Runtime v1 (`feature/runtime`)
- Scope: Task 15 read-only bridge foundation only

## 1. Goal and non-goals

This milestone creates the smallest safe bridge contract needed to observe a
Houdini scene and the user's current node selection. It is a foundation for a
future panel and typed ChangeSet workflow; it is not a scene-editing API.

Included:

- loopback-only Bridge transport with a token separate from the Runtime token;
- versioned request/response DTOs with request IDs, deadlines, and scene epochs;
- a Houdini main-thread execution queue for all HOM reads;
- `SceneBinding` facts: Houdini instance ID, scene epoch, HIP path or `unsaved`,
  and observed revision;
- a read-only `scene.query` operation returning selected nodes and bounded node
  facts;
- explicit errors for authentication, protocol, deadline, stale scene, queue,
  and Houdini read failures;
- pure-Python contract tests and real hython/Houdini read-only integration tests.

Not included:

- `ChangeSet` preview or apply;
- node/parameter/connection creation, deletion, or mutation;
- HIP load, clear, save, export, HDA installation, shell, `eval`, or `exec`;
- Workspace creation or ownership writes;
- UI implementation;
- arbitrary `hou` proxy exposure or remote module/function calls;
- automatic scene backup or rollback.

## 2. Boundary and ownership

The Runtime process owns session/run/event persistence and may request a read
from the Bridge. The Houdini process owns all `hou` access and the scene epoch.
The bridge adapter is the only transport boundary. Neither Runtime nor the
agent graph imports `hou` or receives a live HOM object. Task 15 uses a
standard-library length-prefixed JSON stream on loopback with a first-frame
Bridge-token handshake; the existing unauthenticated rpyc server remains
legacy-only and is not reused by the secure bridge.

The existing unrestricted `hrpyc` path remains a migration-only legacy path. It
must not be selected by Runtime or any new panel code. Existing CLI behavior is
preserved until a later milestone explicitly replaces it.

## 3. DTOs

All DTOs are strict JSON objects. Duplicate keys, unknown keys, non-finite
numbers, non-string keys, cycles, and payloads over 1 MiB UTF-8 are rejected.
Encoding is compact UTF-8 with sorted keys and no NaN/Infinity.

### 3.1 Request

```json
{
  "protocol": "eee.bridge/1",
  "kind": "request",
  "request_id": "req_...",
  "operation": "scene.query",
  "deadline_ms": 5000,
  "scene_epoch": 12,
  "payload": {
    "include_selection": true,
    "node_paths": ["/obj/geo1"],
    "include_geometry_stats": true
  }
}
```

Rules:

- `request_id` is a Foundation request ID and is unique for the live bridge
  connection; retries reuse the same ID as an idempotency key.
- `deadline_ms` is an exact integer in `1..30000`; the bridge never extends it.
- `scene_epoch` is required for scene queries. `None` is not a wildcard.
- `scene.query` allows only the three payload fields shown above. Selection is
  read from Houdini at execution time, not supplied by the caller.
- `node_paths` is optional, contains at most 128 absolute Houdini node paths,
  and never grants access beyond read-only inspection.

### 3.2 Success response

```json
{
  "protocol": "eee.bridge/1",
  "kind": "response",
  "request_id": "req_...",
  "ok": true,
  "result": {
    "instance_id": "hou_...",
    "scene_epoch": 12,
    "hip_path": "C:/project/scene.hip",
    "observed_revision": "sha256:...",
    "selected_nodes": [
      {
        "path": "/obj/geo1",
        "node_type": "geo",
        "parent_path": "/obj",
        "display_name": "geo1",
        "is_locked": false,
        "geometry_stats": {"points": 8, "primitives": 6}
      }
    ],
    "nodes": []
  }
}
```

`selected_nodes` is always present and reflects `hou.selectedNodes()` at the
single main-thread read. `nodes` contains only explicitly requested paths.
Missing nodes are reported as structured per-node facts, not silently replaced
with a different path. `hip_path` is the canonical path or the literal
`"unsaved"`; Save/Save As does not increment the scene epoch.

### 3.3 Error response

```json
{
  "protocol": "eee.bridge/1",
  "kind": "response",
  "request_id": "req_...",
  "ok": false,
  "error": {
    "code": "bridge.stale_scene",
    "category": "stale_scene",
    "message_for_user": "The Houdini scene changed; refresh before continuing.",
    "retryable": true,
    "technical_detail_ref": "err_..."
  }
}
```

Required codes for this milestone:

- `bridge.invalid_request`
- `bridge.unauthorized`
- `bridge.incompatible_version`
- `bridge.deadline_exceeded`
- `bridge.queue_full`
- `bridge.cancelled`
- `bridge.stale_scene`
- `bridge.houdini_read_failed`
- `bridge.not_available`
- `bridge.internal_failure`

No error contains a token, a live HOM object, arbitrary traceback text, or a
filesystem path outside the bounded facts intentionally returned by the DTO.

## 4. SceneBinding and revision

`SceneBinding` is an immutable read result:

```text
SceneBinding(
    houdini_instance_id: str,
    scene_epoch: int,
    canonical_hip_path: str | None,
    observed_revision: str,
)
```

- `scene_epoch` starts at `1` for the current scene and increments on Houdini
  load/clear events observed by the bridge.
- The bridge must install load/clear callbacks without changing the scene.
- A request with an older epoch fails with `bridge.stale_scene`; the bridge
  never guesses that the request can still be safe.
- `observed_revision` is a deterministic read hash over the bounded scene facts
  returned by the query. It is evidence only; it is not an apply permission.
- If the callback API is unavailable, the bridge reports
  `bridge.internal_failure` and does not pretend to track epochs.

## 5. Main-thread queue and lifecycle

- Every HOM read is a short item submitted to a bounded FIFO queue.
- A single Houdini main-thread pump drains the queue; worker threads never call
  `hou` directly.
- Queue admission checks the request deadline and cancellation token.
- On timeout or cancellation, the result is discarded if it has not started;
  an already-running read is allowed to finish and its result is not sent to a
  cancelled caller.
- Queue shutdown rejects new work, completes queued requests with
  `bridge.not_available`, drains bounded in-flight work, and releases the
  transport without leaving a background thread holding a HOM reference.
- The bridge binds only to `127.0.0.1` on an ephemeral/configured port and
  writes only a fingerprinted discovery record; full Bridge tokens never enter
  logs, SQLite, or events.

## 6. Token handoff and local identity files

The discovery record is intentionally insufficient to authenticate a client:
it contains the Bridge fingerprint, not the bearer token. The Houdini-side
process therefore publishes the full Bridge token through a separate
same-directory `bridge.token` handoff file. This is a local process handoff,
not a second discovery channel and not a Runtime credential.

- `bridge.token` contains exactly the UTF-8 token text plus one final newline;
  it contains no JSON, metadata, Runtime token, or diagnostic text.
- The token file is created with an atomic temporary-file-and-`os.replace`
  sequence. A failed publication must leave neither a new discovery record nor
  a listener that clients could mistake for a usable bridge.
- On POSIX the final file is mode `0600`; on Windows the implementation must
  use the current-user, machine-local state directory and document the
  equivalent owner-only protection available on that platform. Tests must
  prove that the full token is absent from discovery JSON, reprs, logs, and
  exception text.
- The client reads the token only from this explicit file handoff, verifies its
  fingerprint against discovery before opening a request session, and keeps
  the bearer only in memory for the hello frame. It must not fall back to an
  environment variable, command-line argument, Runtime token, or SQLite row.
- Startup publishes the token file before discovery; shutdown removes both
  identity files after the listener has stopped. Removal is idempotent and
  stale files from a previous process are replaced or removed before a new
  identity is advertised.
- Runtime and Bridge token files remain independent. A Runtime token must fail
  Bridge authentication and a Bridge token must fail Runtime authentication.

## 7. Security and compatibility invariants

- Runtime and Bridge bearer tokens are generated, stored, and rotated
  independently.
- Constant-time token comparison is required.
- The bridge never returns `hou.Node`, `hou.Geometry`, module objects, Python
  source, or arbitrary attribute values.
- The read-only agent tool allowlist remains unchanged; this bridge does not
  add a write tool or a general-purpose subagent.
- Existing CLI selftest/prompt/stdio paths remain untouched by Task 15.

## 8. Test contract

Pure Python tests must prove:

- strict parsing, duplicate-key rejection, size/number/key limits, and compact
  canonical serialization;
- token separation, constant-time validation, wrong-token and wrong-version
  failures; atomic `bridge.token` write/read/cleanup, fingerprint mismatch,
  permission/failure handling, and proof that the token never enters discovery;
- request-id retry behavior and deadline/cancellation mapping;
- stale epoch, queue-full, unavailable, and bounded error mappings;
- no HOM object or arbitrary function is serializable through the DTO;
- Runtime imports do not import `hou` or the legacy unrestricted bridge.

Real Houdini/hython tests must prove:

- the bridge binds loopback-only and exposes the expected discovery data;
- selecting a node changes `selected_nodes` after a query and does not mutate
  the scene;
- node type/path and bounded geometry facts round-trip correctly;
- load/clear increments `scene_epoch`, while Save/Save As does not;
- an old epoch is rejected before any scene read beyond the binding check;
- concurrent requests execute in FIFO order on the main-thread pump;
- cancellation/deadline does not leave a queue item or HOM reference behind;
- bridge shutdown removes local identity files and releases its port.
- a failed token/discovery publication leaves no usable listener or partial
  identity file, and a restarted bridge can replace stale files safely.

Acceptance requires the focused contract/integration suites, the full offline
Runtime suite, `uv lock --check`, compileall, diff check, and a manual Houdini
selection smoke. No ChangeSet or UI test is part of this task.

## 9. Promotion rule

Task 15 is accepted only when the read-only bridge behavior is independently
verified and the authorized diff contains no scene-write path. Task 17-A may
then consume `scene.query` to build the selection inspector. Task 16 remains a
separate milestone for any ChangeSet, approval, or write behavior.
