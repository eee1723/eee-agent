# Secure HoudiniBridge Read-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved Task 15 read-only HoudiniBridge contract so Runtime can obtain bounded, authenticated scene and selection facts without exposing `hou`, arbitrary Python, or any scene-write capability.

**Architecture:** Keep the existing `eee_agent.bridge`/`houdini_side.start_rpc` path as legacy compatibility code. Add a separate `eee_agent.houdini_bridge` package for strict DTOs, token-authenticated transport, and a bounded client; add a Houdini-side adapter that executes read-only queries through a single main-thread pump. Runtime consumes plain JSON DTOs only.

**Tech Stack:** Python 3.11, Foundation IDs/errors and canonical JSON helpers, a standard-library length-prefixed loopback TCP transport, Houdini 21.0.440/hython for integration, pytest 8.4.1, and standard-library asyncio/threading/queue primitives. The existing rpyc path remains legacy-only and is not reused by the secure bridge.

---

## Scope and ownership

- Claude Code implements only the task named in the current prompt, using model `glm-5.2[1m]`.
- Codex owns this plan, the handoff/status files, file-scope review, RED/GREEN verification, real Houdini acceptance, and promotion to the next task.
- No UI, ChangeSet, approval, workspace persistence, node mutation, HIP load/clear/save/export, HDA installation, shell, `eval`, or `exec` is authorized by this plan.
- Existing CLI, `eee_agent.bridge`, `houdini_side.start_rpc`, and all Runtime v1 files remain unchanged unless a task below explicitly names them.
- Automated tests must remain offline. Real Houdini/hython checks are separate manual/integration commands and must never be hidden behind a production test flag.

## File map

Task 15 creates these focused files:

- `eee_agent/houdini_bridge/__init__.py` — stable exports for the strict client/DTO surface.
- `eee_agent/houdini_bridge/contracts.py` — frozen request, response, error, SceneBinding, and scene-query records; strict validation and canonical JSON.
- `eee_agent/houdini_bridge/auth.py` — Bridge identity/token generation, fingerprint, constant-time validation, fingerprint-only discovery, and atomic `bridge.token` handoff.
- `eee_agent/houdini_bridge/client.py` — authenticated loopback request client; returns plain DTOs and never returns rpyc/HOM proxies.
- `eee_agent/houdini_bridge/queue.py` — bounded FIFO request queue and cancellation/deadline state machine, independent of `hou`.
- `houdini_side/secure_bridge.py` — Houdini-side read-only adapter, main-thread pump, scene epoch callbacks, and loopback server entrypoint.
- `tests/runtime/test_houdini_bridge_contracts.py` — pure-Python RED/GREEN contract tests.
- `tests/runtime/test_houdini_bridge_auth.py` — token/discovery/constant-time behavior tests.
- `tests/runtime/test_houdini_bridge_queue.py` — FIFO, deadline, cancellation, queue-full, shutdown tests.
- `tests/runtime/test_houdini_bridge_client.py` — fake transport tests proving DTO-only client behavior.
- `tests/runtime/test_houdini_bridge_transport.py` — offline loopback server/handshake/request/shutdown integration tests.
- `tests/runtime/houdini_bridge_smoke.py` — explicit real-hython smoke helper; never imported by the offline suite.

Do not modify `eee_agent/bridge/*`, `eee_agent/tools/*`, `eee_agent/app.py`, the Runtime WebSocket protocol, or `houdini_side/chat_panel.py` in Task 15.

## Task 15-A: Strict DTO and error contracts

**Files:** Create `eee_agent/houdini_bridge/__init__.py`, `eee_agent/houdini_bridge/contracts.py`, `tests/runtime/test_houdini_bridge_contracts.py`.

- [ ] **Step 1: Write RED tests for request parsing and canonical JSON**

Add tests that construct a valid `scene.query` request and assert the exact fields:

```python
request = BridgeRequest.from_dict({
    "protocol": "eee.bridge/1",
    "kind": "request",
    "request_id": "req_0123456789abcdef0123456789abcdef",
    "operation": "scene.query",
    "deadline_ms": 5000,
    "scene_epoch": 1,
    "payload": {"include_selection": True, "node_paths": [], "include_geometry_stats": True},
})
assert request.operation is BridgeOperation.SCENE_QUERY
assert request.scene_epoch == 1
assert request.to_json() == '{"deadline_ms":5000,"kind":"request","operation":"scene.query","payload":{"include_geometry_stats":true,"include_selection":true,"node_paths":[]},"protocol":"eee.bridge/1","request_id":"req_0123456789abcdef0123456789abcdef","scene_epoch":1}'
```

Parametrize rejection for missing/extra fields, wrong exact types (`bool` is not an integer), duplicate JSON keys at any depth, non-finite numbers, non-string keys, cycles, payloads over 1 MiB UTF-8, wrong protocol, malformed request IDs, deadline outside `1..30000`, missing scene epoch, and `scene.query` payload fields outside the approved three.

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_contracts.py -q
```

Expected: collection fails because `eee_agent.houdini_bridge.contracts` does not exist.

- [ ] **Step 3: Implement frozen DTOs and strict serializers**

Implement exact public types:

```python
class BridgeOperation(StrEnum):
    SCENE_QUERY = "scene.query"

@dataclass(frozen=True, slots=True)
class SceneBinding:
    instance_id: str
    scene_epoch: int
    hip_path: str | None
    observed_revision: str

@dataclass(frozen=True, slots=True)
class BridgeRequest:
    request_id: str
    operation: BridgeOperation
    deadline_ms: int
    scene_epoch: int
    payload: Mapping[str, object]

@dataclass(frozen=True, slots=True)
class SelectedNode:
    path: str
    node_type: str
    parent_path: str
    display_name: str
    is_locked: bool
    geometry_stats: Mapping[str, object] | None

@dataclass(frozen=True, slots=True)
class SceneQueryResult:
    binding: SceneBinding
    selected_nodes: tuple[SelectedNode, ...]
    nodes: tuple[SelectedNode, ...]

@dataclass(frozen=True, slots=True)
class BridgeError:
    code: str
    category: str
    message_for_user: str
    retryable: bool
    technical_detail_ref: str | None

@dataclass(frozen=True, slots=True)
class BridgeResponse:
    request_id: str
    result: SceneQueryResult | None
    error: BridgeError | None
```

Reuse `eee_agent.runtime.models.freeze_json`, `thaw_json`, and `canonical_json_dumps` only for strict JSON values; do not import `hou`. Convert all mutable inputs into frozen snapshots before returning. Map validation failures to `AgentError` codes in the `bridge.*` namespace and preserve exact compact sorted JSON output.

- [ ] **Step 4: Verify DTO GREEN and core regression**

Run:

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_models.py tests/core/test_errors.py -q
```

Expected: all selected tests pass and existing model/error behavior remains unchanged.

- [ ] **Step 5: Commit Task 15-A**

```powershell
git add eee_agent/houdini_bridge tests/runtime/test_houdini_bridge_contracts.py
git commit -m "feat: define read-only Houdini bridge DTOs"
```

## Task 15-B: Bridge identity and authenticated client

**Files:** Create `eee_agent/houdini_bridge/auth.py`, `eee_agent/houdini_bridge/client.py`, `tests/runtime/test_houdini_bridge_auth.py`, `tests/runtime/test_houdini_bridge_client.py`.

- [ ] **Step 1: Write RED tests for identity and client boundaries**

Test that identity generation uses at least 32 random bytes, discovery contains host/port/protocol/fingerprint but not the full token, token comparison is constant-time, malformed credentials fail, and Runtime/Bridge tokens are independent. Test the client with a fake transport that returns a response DTO and assert the caller receives `SceneQueryResult`, never a proxy or mutable transport object.

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_auth.py tests/runtime/test_houdini_bridge_client.py -q
```

Expected: collection fails because the new auth/client modules do not exist.

- [ ] **Step 3: Implement identity and client**

Use `secrets.token_urlsafe(32)`, SHA-256 fingerprinting, `hmac.compare_digest`, and atomic discovery publication. The client must enforce `127.0.0.1`, perform a first-frame token handshake over the framed loopback TCP transport, apply the request deadline, reject incompatible responses, and close the transport on cancellation. It must parse response JSON through `BridgeResponse` and return frozen plain DTOs only.

- [ ] **Step 4: Verify GREEN**

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_auth.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_auth.py tests/runtime/test_protocol.py -q
```

- [ ] **Step 5: Commit Task 15-B**

```powershell
git add eee_agent/houdini_bridge/auth.py eee_agent/houdini_bridge/client.py tests/runtime/test_houdini_bridge_auth.py tests/runtime/test_houdini_bridge_client.py
git commit -m "feat: authenticate read-only Houdini bridge client"
```

## Task 15-C: Main-thread queue and Houdini-side scene query

**Files:** Create `eee_agent/houdini_bridge/queue.py`, `houdini_side/secure_bridge.py`, `tests/runtime/test_houdini_bridge_queue.py`.

- [ ] **Step 1: Write RED queue and fake-Houdini tests**

Cover FIFO execution, queue capacity, deadline-before-start, cancellation-before-start, shutdown rejection, one in-flight read, and exception mapping. Use a fake HOM adapter with methods `binding()`, `selected_nodes()`, `node_info(path)`, and `geometry_stats(path)`; never import `hou` in the offline tests.

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_queue.py -q
```

Expected: collection fails because the queue module does not exist.

- [ ] **Step 3: Implement the bounded queue**

Use a single owner task/thread and an explicit request state (`queued`, `running`, `completed`, `cancelled`, `expired`). Admission checks exact deadline and capacity. Cancellation removes queued work or marks running work as discard-result; it never interrupts a HOM call mid-operation. Shutdown rejects new work, drains bounded work, and leaves no worker alive.

- [ ] **Step 4: Implement Houdini-side read-only adapter**

In `houdini_side/secure_bridge.py`, import `hou` only inside the Houdini process. Implement `scene.query` from one main-thread snapshot: read `hou.applicationVersionString()`, `hou.hipFile.name()`, `hou.selectedNodes()`, requested `hou.node(path)`, and bounded geometry counts. Install load/clear callbacks that increment `scene_epoch`; Save/Save As only updates `hip_path`. Reject all operation names except `scene.query`; never expose the underlying rpyc service or arbitrary call dispatch.

- [ ] **Step 5: Verify queue GREEN and compile**

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_queue.py -q
uv run python -m compileall -q eee_agent houdini_side tests
```

- [ ] **Step 6: Commit Task 15-C**

```powershell
git add eee_agent/houdini_bridge/queue.py houdini_side/secure_bridge.py tests/runtime/test_houdini_bridge_queue.py
git commit -m "feat: add main-thread read-only Houdini bridge queue"
```

## Task 15-D: Real transport, token handoff, and integration acceptance (accepted)

**Files:** Modify `eee_agent/houdini_bridge/auth.py`, `eee_agent/houdini_bridge/client.py`, and `houdini_side/secure_bridge.py`; extend the focused auth/client tests; create `tests/runtime/test_houdini_bridge_transport.py` and `tests/runtime/houdini_bridge_smoke.py`; modify only the Task 15 section of `README.md` and `CLAUDE.md` if installation instructions are required.

- [x] **Step 1: Write RED token-handoff and offline loopback tests**

Add RED coverage for the approved local identity handoff before adding a
listener. The tests must cover atomic write/read of `bridge.token`, exact
token-file contents, POSIX owner-only permissions where supported, replacement
of stale files, idempotent cleanup, fingerprint verification, and publication
failure leaving no partial token/discovery file. Assert that discovery JSON,
reprs, logs, and error text never contain the full token.

Use the real `BridgeClient` and a real loopback `asyncio.start_server` with a fake injected scene adapter to test hello authentication, wrong-token rejection, one `scene.query` request/response, request-id matching, frame limits, stale epoch mapping, and clean server/client shutdown. These tests must not import `hou` and must not call the real Houdini process.

Run:

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_transport.py -q
```

Expected RED: the token-file API and server entrypoint do not yet exist.

- [x] **Step 2: Implement the token handoff and framed loopback server**

Add explicit auth helpers for the `bridge.token` file. The client must be able
to load the token together with discovery, verify the advertised fingerprint,
and construct its in-memory identity without accepting a Runtime token or an
environment/CLI fallback. Keep the full token out of `BridgeIdentity.__repr__`,
discovery payloads, diagnostics, and exceptions.

Add a server lifecycle to `houdini_side/secure_bridge.py` that binds only to `127.0.0.1`, publishes Bridge discovery using `write_bridge_discovery`, authenticates the first hello frame with `validate_bridge_token`, parses requests with `parse_request`, submits the operation to `MainThreadReadQueue`, and serializes `BridgeResponse` results/errors. The server must never dispatch arbitrary names or return HOM objects. The Houdini process must drive `queue.pump_one()` from its main-thread event callback; the network loop may run independently only for transport I/O.

Startup must publish `bridge.token` atomically before discovery. If either
identity publication step fails, the listener must be closed and no discovery
record or partial token file may remain. Shutdown is ordered: stop accepting
requests, drain/reject bounded work, close sockets, then remove both identity
files; `close()` is idempotent and does not save/clear/mutate the HIP.

The server must reject wrong protocol/kind/token, duplicate keys, invalid frames, stale epochs, unknown operations, oversized frames, and requests after shutdown.

- [x] **Step 3: Verify offline transport GREEN**

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_transport.py -q
uv run --extra eval pytest tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_houdini_bridge_auth.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_auth.py tests/runtime/test_protocol.py -q
```

- [x] **Step 4: Write the explicit smoke helper**

The helper must read `bridge.token` from the same state directory as the
discovery record, verify the discovery fingerprint, connect using the framed
TCP client, perform the token handshake, issue `scene.query`, print only
bounded DTO facts, select a known test node through the Houdini UI/manual
script, issue a second query, and assert the selected path appears. It must
never print the token or place it in a command line, environment variable,
log, or exception; it must never create/delete/connect/set parameters/save/
export or call the legacy `eee_agent.bridge` client.

- [x] **Step 5: Run offline RED/GREEN regression before Houdini**

```powershell
uv run --extra eval pytest tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_houdini_bridge_auth.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
```

Expected: all Task 15 tests pass before starting Houdini.

- [x] **Step 6: Run the real hython smoke**

Start Houdini 21.0.440, load the bridge script from the repository, and run:

```powershell
& "D:\houdini\bin\hython.exe" tests/runtime/houdini_bridge_smoke.py --state-dir "$env:TEMP\eee-bridge-smoke" --host 127.0.0.1 --port 18811
```

The smoke must prove loopback binding, token authentication, selection parity, node/type facts, geometry bounds, load/clear epoch increment, Save/Save As epoch stability, stale-epoch rejection, FIFO ordering, cancellation, and clean shutdown. Record the before/after scene fingerprint and assert no mutation.

- [x] **Step 7: Run full acceptance**

```powershell
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

Expected: zero failures, no new skips/xfailed tests, lock/compile/diff checks exit 0, and only authorized Task 15 files changed.

- [x] **Step 8: Commit Task 15-D**

```powershell
git add eee_agent/houdini_bridge/auth.py eee_agent/houdini_bridge/client.py houdini_side/secure_bridge.py tests/runtime/test_houdini_bridge_auth.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/houdini_bridge_smoke.py README.md CLAUDE.md
git commit -m "feat: expose authenticated read-only Houdini scene query"
```

## Task 15-D acceptance record

Codex independently accepted implementation `fcdee32` plus lifecycle fix
`bfc00f3`. Evidence: token/auth/client/transport slice 122 passed; the full
Task 15 focused slice passed 370; the complete offline suite passed 1388;
`uv lock --check`, compileall, and `git diff --check` passed; and the real
`D:\\houdini\\bin\\hython.exe` smoke passed 20 checks with no scene mutation.
The lifecycle fix specifically proves a publication failure leaves
`listener.is_serving() == False`, removes both identity files, and preserves
the original publication exception even when cleanup raises.

## Promotion gate

Task 15 is accepted only after all four focused suites, the full offline suite,
compileall, lock check, diff check, and the real Houdini selection smoke pass.
The accepted output is a plain `SceneQueryResult`; no UI or ChangeSet code may
depend on legacy `eee_agent.bridge`. Task 17-A may start only after this gate;
Task 16 remains the sole owner of future write/approval behavior.
