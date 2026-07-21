# Task 18-A Strict Modeling Foundation Plan

**Status:** Accepted locally at `09ea256` after focused 237-test and full
2132-test offline gates. Task 18-B is not started.

**Goal:** Add strict model-facing modeling contracts and a deterministic,
catalog-gated compiler that produces an existing trusted typed ChangeSet
without exposing write authority.

**Design:** `docs/superpowers/specs/2026-07-16-task18-strict-modeling-foundation-design.md`

## Authorized files

- Create `eee_agent/modeling/__init__.py`
- Create `eee_agent/modeling/contracts.py`
- Create `eee_agent/modeling/compiler.py`
- Create `tests/modeling/__init__.py`
- Create `tests/modeling/test_contracts.py`
- Create `tests/modeling/test_compiler.py`
- Update project status documentation after acceptance

No Runtime service, server, panel, ChangeSet contract/policy, Bridge, Houdini
adapter, legacy tool, provider, dependency, or lockfile change is authorized.

## Step 1: Contract RED tests

Add tests for:

- strict Brief/Spec JSON round trips and canonical digests;
- exact field sets and duplicate-key rejection;
- schema versions, exact primitive types, finite values, bounds, and fresh
  output trees;
- axis rules, component DAGs, qualified node references, and duplicates;
- exact QualityProfile validator order and two-repair maximum; and
- RepairBudget/Ticket bounds and monotonic attempt accounting.

Run:

```powershell
uv run --frozen --extra eval pytest tests/modeling/test_contracts.py -q
```

Expected RED: `eee_agent.modeling` does not exist.

## Step 2: Implement strict contracts

Create frozen, slotted DTOs and strict JSON entry points. Reuse the accepted
Runtime canonical JSON implementation. Do not import ChangeSet, Runtime
service, `hou`, `rpyc`, LangChain, or legacy tools in `contracts.py`.

Run the Step 1 command to green.

## Step 3: Compiler RED tests

Add deterministic compiler tests for:

- exact Brief/Profile/Workspace/Scene binding;
- catalog allowlists and exact parameter shapes;
- component and node dependency ordering/cycle rejection;
- hidden dependency rejection;
- deterministic stable node/op IDs;
- exact derived operations, expected-old defaults, pre/postconditions, risk,
  affected nodes, and empty checkpoint plan;
- Task 16 policy acceptance;
- input/output bounds; and
- operation/path/expression/source injection denial.

Run:

```powershell
uv run --frozen --extra eval pytest tests/modeling/test_compiler.py -q
```

Expected RED: compiler/catalog types do not exist.

## Step 4: Implement deterministic compiler

Create the trusted catalog contracts, compile error/result types, graph
validation, deterministic ordering, ChangeSet construction, and existing
policy check. The compiler accepts trusted IDs/time/binding/manifest as
arguments; the model does not mint them.

Run:

```powershell
uv run --frozen --extra eval pytest tests/modeling -q
```

## Step 5: Boundary and regression gate

Run:

```powershell
uv run --frozen --extra eval pytest tests/modeling tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --frozen --extra eval pytest -q
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Inspect the modeling package AST/imports and assert it contains no `hou`,
`rpyc`, legacy bridge, Runtime service, database, LangChain, shell, filesystem,
or dynamic execution import.

## Step 6: Review and status

Review the exact diff, update the Task 18 roadmap/handoff with measured test
evidence, and commit Task 18-A as one focused implementation commit plus a
status/acceptance documentation commit.

Do not start Task 18-B or request real Houdini testing as a side effect.
