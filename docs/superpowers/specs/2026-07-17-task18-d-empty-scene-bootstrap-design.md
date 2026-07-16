# Task 18-D Empty-scene Workspace Bootstrap Design

## Problem

`workspace.create` correctly rejects an ordinary or empty selection because a
WorkspaceManifest can only describe nodes that already carry trusted EEE
identity. The Task 18-C compiler simultaneously requires a healthy existing
Workspace. This creates a bootstrap cycle in a clean scene.

## Chosen flow

The first strict modeling request compiles in bootstrap mode:

```text
trusted SceneBinding + generated workspace_id + strict Brief/Spec
  -> ProjectChange bootstrap ChangeSet
  -> AwaitingApproval
  -> exact user approval
  -> existing typed Bridge transaction
  -> successful reconciled receipt
  -> atomically persist and activate WorkspaceManifest
```

The ChangeSet creates one `geo` object under exact parent `/obj`, then creates
the requested SOP graph beneath it. Every CreateNode carries the same generated
Workspace ID, modeling capability, role, schema mirror, and current Run ID.

## Trust and persistence rules

- Bootstrap is selected by trusted Runtime context, never a model argument.
- `/obj` is the only initial external parent in v1.
- The root name is normalized and bounded by developer code.
- The bootstrap ChangeSet uses `ProjectChange`, has no top-level Workspace
  binding, marks the external parent in risk, and always requires approval.
- No Workspace row or active-workspace state exists before successful Apply.
- Manifest facts are derived from typed CreateNode operations plus reconciled
  postconditions, not from model output or UI selection.
- Manifest creation and its durable event commit atomically after successful
  receipt. Duplicate completion is idempotent.
- RolledBack, Partial, or CriticalRecovery receipts never create a Workspace.

## Compiler shape

Add a separate `compile_bootstrap_procedural_spec()` entry point rather than
making existing Workspace compilation nullable. It accepts an exact frozen
bootstrap context and reuses the same DAG, catalog, parameter-shape, operation
budget, stable-ID, and policy checks.

The generated operation order is:

1. create object-level `geo` root under `/obj`;
2. create SOP nodes in stable topological order;
3. assign cataloged parameters;
4. connect inputs in stable order.

Preconditions include exact SceneBinding and NodeAbsent facts. Postconditions
cover every created identity, parameter assignment, and wire.

## Acceptance

- strict contract and canonical digest tests;
- deterministic compile and policy contradiction tests;
- no provisional Workspace before receipt success;
- applied receipt creates/activates one exact manifest;
- duplicate finalization is idempotent;
- rollback/partial/recovery/stale/Bridge-unavailable paths create no manifest;
- fresh-scene Houdini 21.0.440 hython smoke proves owned metadata, graph, cook,
  receipt, manifest, and zero leftover nodes after forced rollback.

