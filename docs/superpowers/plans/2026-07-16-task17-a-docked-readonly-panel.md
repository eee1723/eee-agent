# Task 17-A Docked Read-Only Panel Plan

## Boundary

Implement only
`docs/superpowers/specs/2026-07-16-task17-a-docked-readonly-panel-design.md`.

Do not add a public Apply command, Run composer, Session mutation UI, Workspace
mutation UI, approval/reject UI, artifact actions, SQLite access, legacy rpyc
fallback, arbitrary execution, dependency, or merge to `main`.

Preserve `houdini_side/chat_panel.py` and the legacy menu path as rollback.

## Slice 17-A1: Client State and Runtime Shell

Authorized production files:

- `eee_agent/panel/__init__.py` (new)
- `eee_agent/panel/client_state.py` (new)
- `houdini_side/runtime_panel.py` (new)
- `python_panels/EEEAgentRuntime.pypanel` (new)

Authorized tests:

- `tests/panel/test_client_state.py` (new)
- `tests/panel/test_panel_package.py` (new)

RED:

- malformed, non-loopback, wrong-protocol, bad-port, wrong-token-file, and
  fingerprint-mismatch Runtime handoffs fail closed;
- token never appears in repr or bounded errors;
- Session choice and per-Session cursor rules are deterministic;
- compatible responses/events parse, duplicate keys and incompatible protocol
  fail;
- panel source imports no SQLite, agent graph, rpyc, or write executor;
- `.pypanel` defines one menu-visible interface and returns the Runtime widget.

GREEN:

- implement the pure client-state contract;
- implement QWebSocket discovery/auth/reconnect/list/subscribe shell;
- render Runtime status, chosen Session, and cursor/epoch-rail placeholder;
- preserve all old panel files and menu actions.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/panel -q
uv run --frozen python -m compileall -q eee_agent/panel houdini_side/runtime_panel.py
```

## Slice 17-A2: Secure Bridge Host and Selection Inspector

Authorized production files:

- `houdini_side/secure_bridge_host.py` (new)
- `houdini_side/runtime_panel.py`
- `MainMenuCommon.xml`
- `houdini_side/install_menu.py`
- `houdini_side/README_INSTALL.md`

Authorized tests:

- `tests/panel/test_secure_bridge_host.py` (new)
- focused Bridge lifecycle/transport tests if a regression requires them

RED:

- host is loopback-only and idempotent;
- one background asyncio transport thread and one accepted main-thread FIFO;
- identity publishes only after bind and cleans up on failure/stop;
- panel obtains a nullable-epoch binding, then sends exact typed `scene.query`;
- no selection and stale-scene states are bounded;
- panel close does not stop Bridge or Runtime.

GREEN:

- implement the host lifecycle around accepted `BridgeServer`;
- pump the queue from the Houdini main thread;
- implement short-lived selection query worker;
- render exact binding, node, and geometry facts;
- add explicit menu actions without removing legacy RPC/chat actions.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/panel tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_houdini_bridge_queue.py -q
uv run --frozen python -m compileall -q eee_agent houdini_side tests
```

## Slice 17-A3: Cross-Slice Acceptance

Run:

```powershell
uv lock --check
uv run --frozen --extra eval pytest -q
uv run --frozen python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

On detected Houdini 21.0.440:

- install/reload the `.pypanel`;
- start Secure Bridge and Runtime with an isolated Runtime home;
- verify authenticated Runtime status and Session subscription;
- select zero, one, and multiple nodes and verify exact parity;
- verify scene epoch/revision and bounded geometry facts;
- restart Runtime and prove reconnect without duplicate/lost event cursor;
- close/reopen the panel and prove Runtime/Bridge remain alive;
- compare before/after scene and filesystem facts for zero mutation.

After acceptance, update README, CLAUDE, roadmap, review result, and a new Task
17-A handoff with exact evidence. Do not push or merge without a separate user
decision.
