# Task 18-A Strict Modeling Foundation Handoff - 2026-07-16

## Current state

- Branch: `feature/runtime`
- Task 16-E: accepted
- Task 17-A: accepted by real Houdini testing
- Task 17-B: accepted by real Houdini testing
- Task 18 design/plan: `d37fba1`
- Task 18-A implementation: `09ea256`
- Focused modeling + Task 16 contract/policy gate: 237 passed
- Full offline suite: 2132 passed, 1 skipped
- `uv lock --check`: passed, 69 packages
- compileall: passed
- `git diff --check`: passed
- No Houdini or live LLM test was required for this pure-Python slice

## What Task 18-A adds

`eee_agent.modeling.contracts` provides strict, frozen, versioned DTOs for:

- `ModelingBrief` with units, axes, bounded semantic constraints, and a
  canonical digest;
- `ProceduralSpec`, `ComponentSpec`, `NodeSpec`, `ParmAssignment`, and
  `InputBinding` with exact qualified logical references, explicit component
  dependencies, duplicate rejection, and hidden-dependency rejection;
- `QualityProfile` with the exact deterministic validator order and a hard
  maximum of two repairs per stage; and
- `RepairBudget`, `RepairAttempt`, and bounded `RepairTicket` records.

Strict JSON entry points reject duplicate keys, unknown fields, invalid UTF-8,
oversized input, non-finite values, wrong primitive types, and mutable aliasing.
The modeling contracts import no Houdini, rpyc, Runtime service, database,
LangChain, or LangGraph code.

`eee_agent.modeling.compiler` provides a developer-owned `NodeCatalog` and
`compile_procedural_spec()`. The catalog, not the model, owns node types,
parameter defaults/shapes, input bounds, output bounds, and child-container
capability. The compiler derives stable executor node IDs, CreateNode,
SetParm, and ConnectInput operations, catalog-derived expected-old values,
scene/workspace/node preconditions, exact postconditions, risk summaries,
affected nodes, and an empty transaction checkpoint plan. It then evaluates
the existing Task 16 policy engine and returns only an allowed typed
`ChangeSet`.

The compiler rejects catalog/source/parameter/path injection, graph cycles,
undeclared dependencies, stale Workspace/binding facts, operation/condition
budget overflow, and unsupported node/parameter/input facts. It has no
Runtime, Bridge, Houdini, model, Apply, staging, validator execution, or
artifact side effect.

## Verification evidence

```text
uv run --frozen --extra eval pytest tests/modeling tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
237 passed

uv run --frozen --extra eval pytest -q
2132 passed, 1 skipped

uv lock --check
passed, 69 packages

uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
passed

git diff --check
passed
```

The only skipped test is the pre-existing optional WSL/Windows environment
probe. No dependency or lockfile changed.

## Security and scope review

- No model-facing tool was added.
- No public Runtime command or WebSocket payload was added.
- No raw ChangeSet/operation JSON is accepted from model input.
- No expected-old value, permission mode, precondition, postcondition, risk,
  checkpoint, or stable executor node ID is model-controlled.
- No `hou`, `rpyc`, legacy bridge, shell, filesystem, eval, exec, or dynamic
  code path is imported or called.
- Existing Task 16 policy, approval, preflight, Apply, receipt, recovery, and
  single-FIFO boundaries are unchanged.

## Next bounded slice

Task 18-B may add one structured, model-facing proposal capability. It must:

1. parse only the accepted Brief/Spec contracts;
2. use a Houdini-verified production NodeCatalog and the current trusted
   Workspace;
3. call the compiler in process;
4. call `RuntimeService.propose_changeset_trusted` only after policy allows;
5. return bounded proposal metadata, never operations or parameter values; and
6. remain offline-testable with an injected catalog/provider.

Do not start staging, approval-to-Apply orchestration, Houdini catalog
introspection, Wrangle/Python source, validators, capture, vision, or Task 19
as a side effect of this handoff.

