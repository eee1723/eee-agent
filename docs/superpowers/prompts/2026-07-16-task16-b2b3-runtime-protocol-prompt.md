# Task 16-B2b-3 Implementation Prompt

Use Claude Code with explicit model `glm-5.2[1m]`. Execute only Runtime
protocol/server/service/CLI/process wiring and B2b acceptance smoke coverage.
Do not start Task 16-E, Task 17, Task 18, UI, or unrelated cleanup.

## Prerequisites

Codex must identify independently accepted B2b-1 and B2b-2 commits in the
worker launch message. If either exact commit is absent, not in the current
ancestry, or not yet accepted, stop before editing.

## Required reading

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
3. `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`,
   Tasks 6-8 and final gate
4. Accepted B2b-1/B2b-2 implementation and tests
5. Existing Runtime protocol/server/service/CLI/process test patterns

Preserve every Codex-owned uncommitted document. Do not edit, stage, restore,
clean, or commit `docs/`.

## Authorized files

- Modify `eee_agent/runtime/protocol.py`
- Modify `eee_agent/runtime/server.py`
- Modify `eee_agent/runtime/service.py`
- Modify `eee_agent/runtime/__main__.py`
- Modify `eee_agent/runtime/__init__.py` only if a stable record export is
  concretely required
- Modify `tests/runtime/test_protocol.py`
- Modify `tests/runtime/test_server.py`
- Modify `tests/runtime/test_service.py`
- Modify `tests/runtime/test_runtime_cli.py`
- Modify `tests/runtime/test_runtime_e2e.py`
- Modify `tests/runtime/runtime_process_fixture.py`
- Modify `tests/runtime/changeset_houdini_smoke.py`

No other production/test file, dependency, lock, executor, agent, UI, or
documentation file is authorized without a concrete blocker and Codex
approval.

## Exact public commands

Move only `workspace.create`, `workspace.bind`, `workspace.switch`, and
`workspace.inspect` from deferred to active Runtime commands. Keep the exact
five-field `eee.runtime/1` envelope and exact payloads from design section 9.
Validate `ses_<32 lowercase hex>`, `ws_<32 lowercase hex>`, lowercase
SHA-256, nullable exact `int >= 1`, and nullable workspace IDs. Bool is not an
integer. Reject every extra/missing field and every manifest, node list, path
list, mirror, capability, role, or arbitrary metadata upload.

## Service and production wiring

- Add one `WorkspaceService` over the accepted repository/EventStore.
- Runtime methods delegate create/bind/switch/inspect and serialize bounded
  `to_dict()` records.
- Notify each returned committed event after the repository transaction. No-op
  or failure sends no notification; replay still sees every committed event.
- Production CLI passes `BridgeWorkspaceFactProvider(paths.state_dir)`.
  Runtime and Bridge tokens remain distinct. The provider opens a short-lived
  discovered connection per request and owns no background task or shutdown
  resource.
- Offline tests inject a fake provider. Missing provider fails closed for
  create/bind/switch and inspect may return `BridgeUnavailable`.
- Preserve startup/shutdown ordering, ChangeSet approval wiring, server error
  isolation, session snapshot compatibility, subscriber backpressure, and
  callback semantics.

## Process and Houdini acceptance

Use the public WebSocket path for process tests. Prove committed event delivery
and replay, restart active-state recovery, exact idempotency, bind refresh,
switch CAS/stale preservation, Session isolation, offline inspect, provider and
commit failure with no half-state, and no snapshot schema drift.

Extend the existing disposable Houdini smoke. Create marked fixtures through
the accepted executor or isolated setup, then exercise real Bridge selection /
manifest inspection and Runtime create/bind/switch/inspect. Ordinary, partial,
duplicate, or stale identity must fail closed. Fingerprint the scene before and
after every B2b operation and prove B2b performs zero Houdini writes. Never save
the HIP.

## Test-first requirements

Begin with genuine RED tests for payload activation/validation, service
delegation/event notification, CLI provider construction, public process flow,
replay/restart/concurrency/failure, and smoke behavior. Do not weaken or delete
accepted assertions and do not introduce skip/xfail.

## Verification

```powershell
uv run --extra eval pytest tests/runtime/test_protocol.py tests/runtime/test_server.py tests/runtime/test_service.py tests/runtime/test_runtime_cli.py tests/runtime/test_runtime_e2e.py tests/runtime/test_workspace_service.py tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_repository.py tests/runtime/test_changeset_service.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
```

The real Houdini 21.0.440 `hython` smoke is a separate mandatory acceptance
gate run after Codex reviews the implementation commit. No new skip/xfail is
allowed.

## Commit and handoff

Stage only authorized implementation/test files and commit once:

```text
feat: expose trusted workspace lifecycle
```

Return commit/parent, exact files, RED/GREEN and full-suite counts, protocol
rejection evidence, post-commit/replay evidence, restart/CAS evidence, no-write
fingerprints, and concerns. Do not push, merge, amend, clean docs, run unrelated
work, or begin Task 16-E.
