# Task 18-C Runtime Proposal Integration Plan

**Status:** Implemented locally; offline acceptance passed. Awaiting the
manual Houdini gate recorded in
`docs/handoffs/2026-07-16-modeling-18c-transfer.md`.

**Goal:** Wire the bounded modeling proposal tool into an opt-in Runtime
AgentRunner and add the verified Houdini 21.0.440 minimal catalog.

**Design:** `docs/superpowers/specs/2026-07-16-task18-c-runtime-proposal-integration-design.md`

## Authorized files

- Create `eee_agent/modeling/catalog.py`
- Update `eee_agent/modeling/__init__.py`
- Update `eee_agent/app.py`
- Update `eee_agent/runtime/agent_runner.py`
- Update `eee_agent/runtime/service.py`
- Update `eee_agent/runtime/__main__.py`
- Update relevant Runtime/tool tests
- Create focused catalog/context/runtime integration tests

Do not add Apply commands, panel controls, raw operation upload, VEX/source
execution, staging validators, capture, vision, or new dependencies.

## Acceptance commands

```powershell
uv run --frozen --extra eval pytest tests/modeling tests/runtime/test_agent_runner.py tests/runtime/test_read_only_agent.py tests/runtime/test_service.py tests/runtime/test_runtime_cli.py -q
uv run --frozen --extra eval pytest -q
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
uv lock --check
git diff --check
```

Real Houdini acceptance is required only after the offline gate and uses the
manual steps recorded in the handoff.
