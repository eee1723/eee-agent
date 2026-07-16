# Task 18-D Empty-scene Workspace Bootstrap Plan

**Design:** `docs/superpowers/specs/2026-07-17-task18-d-empty-scene-bootstrap-design.md`

## Slice D1 - contracts and pure compiler

- Add exact frozen bootstrap context/result contracts under
  `eee_agent/modeling`.
- Add deterministic bootstrap compiler without Runtime, database, Bridge,
  LangChain, or Houdini imports.
- Extend the verified catalog with object-level `geo` facts only after hython
  introspection.
- Test strict parsing, stable IDs/digests, operation order, policy mode, risk,
  stale/invalid catalog, and budgets.

## Slice D2 - durable finalization

- Add repository/service primitive that derives and atomically persists the
  initial Workspace only after a successful bootstrap receipt.
- Bind the bootstrap metadata to exact change/session/run/workspace/scene
  identity.
- Make duplicate completion idempotent and conflicting completion fail closed.
- Test transaction rollback, concurrent completion, restart, and event order.

## Slice D3 - Runtime proposal context

- When no active Workspace exists but SceneBinding is healthy, build a trusted
  bootstrap proposal context rather than returning no context.
- Generate Workspace ID and root name outside model input.
- Keep normal existing-Workspace compilation unchanged.
- Return the same bounded proposal summary; do not expose bootstrap internals.

## Slice D4 - hython

- Add a dedicated disposable `tests/modeling/bootstrap_houdini_smoke.py`.
- Verify fresh scene, exact `/obj` parent, owned metadata, node graph, cook,
  receipt, manifest facts, stale refusal, rollback cleanup, and idempotency.

## Commands

```powershell
uv run --frozen --extra eval pytest tests/modeling tests/runtime -q
uv run --frozen --extra eval pytest -q
& 'C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe' -u tests\modeling\bootstrap_houdini_smoke.py
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

