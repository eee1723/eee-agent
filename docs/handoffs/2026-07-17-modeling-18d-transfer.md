# Task 18-D Empty-scene Bootstrap Handoff - 2026-07-17

## Result

Task 18-D offline and disposable-hython implementation is accepted locally;
interactive Houdini UI verification remains deferred by user request.

## Implementation

- `WorkspaceBootstrapContext` and deterministic
  `compile_bootstrap_procedural_spec()` create a verified `geo` root under
  `/obj` followed by catalog-gated SOP nodes.
- Bootstrap ChangeSets use `ProjectChange`, no provisional WorkspaceManifest,
  exact SceneBinding preconditions, external-parent checkpoint coverage, and
  bounded risk summary.
- `derive_bootstrap_manifest()` converts only a fully applied reconciled receipt
  into exact owned node facts.
- `ChangeSetRepository.complete_bootstrap_apply()` atomically commits receipt,
  terminal ChangeSet state, Workspace row, active Workspace state, and durable
  events; identical completion is idempotent.
- Runtime modeling context now chooses bootstrap mode when the Session has no
  Workspace. Trusted profile, catalog, root identity, Session, Run, and scene
  facts remain outside model input.
- Proposal coordinator injects trusted brief/profile/root bindings into strict
  Spec parsing; model cannot spoof them or invent scene IDs.

## Evidence

- `uv run --frozen --extra eval pytest -q` -> `2162 passed, 1 skipped`
- modeling focused gate -> `52` compiler/proposal/bootstrap/validation tests plus
  persistence/context coverage
- `uv lock --check` -> passed
- compileall -> passed
- `git diff --check` -> passed
- Houdini 21.0.440 hython bootstrap smoke -> passed:
  `Applied`, ownership metadata, non-empty Cook, manifest derivation,
  `AlreadyApplied` replay, and forced `RolledBack` cleanup.

## Next slice

Task 18-E wires user approval to the existing internal Apply task while keeping
raw Apply and write tools out of the model and public Runtime command surface.
