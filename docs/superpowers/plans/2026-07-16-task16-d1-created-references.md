# Task 16-D1 Executable Plan

## Accepted base and finish line

- Branch: `feature/runtime`
- Accepted Task 16-D implementation: `3435f4b`
- Accepted documentation tip: `2556b8a`
- Goal: safely support references to nodes created earlier in the same ordered
  ChangeSet, then independently pass offline and Houdini 21.0.440 gates.
- Stop after D1. Do not start B2b, 16-E, UI work, push, or merge.

Codex owns this plan, the design supplement, prompt, checklist, review result,
scope approval, final tests, and commits. Claude `glm-5.2[1m]` owns RED/GREEN
implementation only and must not edit, stage, restore, clean, or commit Codex
documentation.

## Authorized implementation files

- `eee_agent/changesets/contracts.py`
- `eee_agent/changesets/policy.py`
- `houdini_side/changeset_executor.py`
- `eee_agent/houdini_bridge/changesets.py` only if strict decoding needs a
  compatibility-preserving adjustment
- `tests/runtime/test_changeset_contracts.py`
- `tests/runtime/test_changeset_policy.py`
- `tests/runtime/test_changeset_executor.py`
- `tests/runtime/test_changeset_bridge_preflight.py`
- `tests/runtime/test_changeset_bridge_transport.py`
- `tests/runtime/changeset_houdini_smoke.py`

No other production/test file is authorized without a concrete D1 blocker and
Codex approval.

## Step 1: Contract dependency graph, RED then GREEN

Add failing tests for create-then-set, created connect target/source, created
parent, and a multi-level legal chain. Add construction failures for forward
references, create-parent cycles, created ID with wrong path/type/workspace,
and created path with missing/wrong ID/type/workspace.

Implement a deterministic operation-order validator. A reference colliding by
created ID or derived path must exactly equal its producer's derived `NodeRef`
and the producer must already have been visited. Preserve duplicate create
ID/path checks and canonical serialization/digest behavior.

Gate:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py -q
```

## Step 2: Permission policy, RED then GREEN

Prove `OwnedWorkspace` accepts exact earlier-created targets, sources, and
parents absent from the manifest, while contradictory created references and
ordinary non-manifest write targets remain denied. Prove `ScopedPatch` still
denies create and ProjectChange behavior is unchanged.

Adjust ownership evaluation only for exact derived created references. Keep
affected-node, risk-summary, lock, ambiguity, external-touch, and manifest
checks intact.

Gate:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_policy.py -q
```

## Step 3: Preflight and derivation, RED then GREEN

Prove preflight checks every create target's global stable-ID/path absence but
does not demand existence or read parm/wire state for transaction-created
nodes. Prove existing-node stale facts still reject before writes.

Derive identity, old-value, and checkpoint requirements in operation order.
Exclude facts that cannot exist before the transaction, including state
produced by an earlier operation, without weakening initial facts for existing
nodes.

Gate:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_executor.py -q
```

## Step 4: JIT transactional enforcement, RED then GREEN

Implement exact `expected_old_value` and `expected_old_source` comparisons
immediately before their writes. Immediately before child creation, and before
any later operation using a created node, revalidate its full created identity
and six ownership mirrors, including schema and creating run.

Add failure tests where a prior op creates or mutates state and a later JIT
check fails. Assert the failing op makes no mutation, earlier writes roll back
in strict reverse order, applied-op evidence is truthful, and rollback failure
still yields Partial/CriticalRecovery plus write freeze.

Prove operation order for:

```text
create parent -> create child -> set child parm -> connect created endpoints
```

Also rerun cancellation-after-first-write, idempotent replay, receipt query,
postcondition reconciliation, and bounded cache tests.

## Step 5: Transport and real Houdini smoke

Preserve the exact schema-v1 wire shape and explicit apply/receipt dispatch.
Only adjust decoding tests if contract validation exposes a strict parse-time
failure. Extend the disposable smoke with the D1 chain, replay, receipt query,
and cleanup. Save nothing.

## Codex independent acceptance

Claude stops after reporting changed files and test output. Codex inspects the
entire diff, checks every item in the D1 review checklist, reproduces findings,
and sends blocking findings back to the same Claude conversation for fixes.

Final commands:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check 2556b8a
git status --short --branch
```

Only the existing optional WSL probe may skip. Then run the detected Houdini
21.0.440 `hython` smoke in a fresh process and record its exact command/result.

After all gates pass, Codex creates separate implementation and documentation
commits. Do not push or merge.
