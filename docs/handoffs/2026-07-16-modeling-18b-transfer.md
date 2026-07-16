# Task 18-B Proposal Seam Handoff - 2026-07-16

## Current state

- Branch: `feature/runtime`
- Task 18-A: accepted at `09ea256`
- Task 18-B pure proposal seam: accepted locally at implementation commit
  `23379df`
- Focused modeling/Runtime regression gate: 99 passed
- Full offline suite: 2140 passed, 1 skipped
- `uv lock --check`: passed, 69 packages
- compileall: passed
- `git diff --check`: passed
- No live Runtime/Houdini test is claimed

## What this slice adds

`eee_agent.modeling.proposal` adds:

- `ModelingProposalContext`, which carries trusted Session/Run, Workspace,
  SceneBinding, QualityProfile, developer-owned NodeCatalog, clock/ID seams,
  and one proposal callback;
- `ModelingProposalCoordinator`, which parses strict Brief/Spec data, compiles
  through Task 18-A, re-evaluates the accepted policy, calls the callback once,
  and returns only a bounded `ModelingProposalSummary`;
- bounded `ModelingProposalError` codes; and
- `propose_modeling`, a LangChain adapter that receives only Brief/Spec dicts
  plus injected `ToolRuntime` context.

The adapter returns only proposal ID/digest, AwaitingApproval state, operation
count, effect names, affected-path count, and approval-required status. It
never returns operations, parameter values, expected-old facts, paths, raw
ChangeSet JSON, or Apply authority.

The proposal exports are lazy. Importing `eee_agent.modeling.contracts` or the
compiler does not import LangChain, Runtime, database, Houdini, or rpyc.

## Deliberately deferred

This is a seam, not active Runtime integration. The existing Runtime
AgentRunner and exact read-only seven-tool registry are unchanged. No Runtime
context schema, Workspace/SceneBinding provider wiring, production Houdini
catalog, WebSocket command, panel proposal surface, approval-to-Apply
transition, or real pending-approval test has been added.

The next bounded slice must wire the context into Runtime only after a
Houdini-21.0.440-verified catalog exists and must preserve the existing
read-only tools as a separately auditable capability set.

## Verification evidence

```text
uv run --frozen --extra eval pytest tests/modeling tests/runtime/test_agent_runner.py tests/runtime/test_read_only_agent.py tests/runtime/test_changeset_service.py -q
99 passed

uv run --frozen --extra eval pytest -q
2140 passed, 1 skipped

uv lock --check
passed, 69 packages

uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
passed

git diff --check
passed
```

The only skipped test is the pre-existing optional WSL/Windows environment
probe.
