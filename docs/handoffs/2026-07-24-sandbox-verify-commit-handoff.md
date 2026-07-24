# Sandbox + Verify + Commit (Pi model) Handoff

Date: 2026-07-24
Owner: continued from the Pi-model pivot plan
Branch: `feature/sandbox-verify-commit` (branched from `main` at `5a6880b`)
Prior source of truth: `docs/handoffs/2026-07-23-stage-b-pass-stage-c-handoff.md`

## What landed

The agent's modeling workflow pivoted from the legacy blind-whole-spec-at-once
model (`propose_modeling` → Brief/Spec → typed ChangeSet → approval → Apply)
to an **iterative sandbox + verify + commit** model (the "Pi model"). The agent
now builds one node at a time in an isolated sandbox container, observes the
cooked result, and promotes verified geometry into the real scene only after
four hard quality gates pass.

The change-set / ownership / recovery internals are **retained** as the commit
persistence seam — this was a deliberate "don't throw out the stronger
transactional kernel" decision. The agent-facing Brief/Spec/Compiler layer is
retained as a module but `propose_modeling` is **retired from the agent graph**.

### Three new bridge operations (all gated on the `scratch.v1` capability)

1. **`scratch.exec`** (Phase 1) — build/extend a reserved
   `/obj/eee_scratch_<sandbox_id>` geo container with structured ops
   (create_node / set_parm / connect). No ownership mirrors (sandbox nodes
   never pollute the real workspace node-id index). Returns cooked geometry
   stats + cook errors. Preserved on failure by default so the agent can retry.
2. **`scratch.commit`** (Phase 2) — promote a verified sandbox into the real
   scene through four hard gates, wrapped in one `hou.undos.group` (atomic
   rollback on partial failure — stronger than Pi's bare rename). On refusal
   the sandbox is preserved.
3. **`scratch.destroy`** (Phase 3) — best-effort cleanup of one run-scoped
   sandbox container. **Bypasses the write-freeze gate** (cleanup must run even
   after an uncertain recovery, otherwise a crashed run leaks its sandbox
   forever).

### Four hard verify gates (`houdini_side/scratch_verify.py`)

Ported from Pi (`Edini/python3.11libs/edini`) and adapted to run inside the
Houdini process as part of `scratch.commit`:

- **bake** — every prim with a `@component_id` must carry a non-zero
  `@edini_world_axis` (the deterministic construction axis).
- **structure** — refuses monolithic assets (≥3 components all from a single
  Python SOP with no modular assembly nodes).
- **orientation** — compares baked world axes against declared expected axes
  using pure-Python PCA math; PCA is warning-only (the bake is authoritative).
- **health** — orphan_points / open_curves are hard failures; degenerate /
  nonmanifold / open_boundary / coincident are advisory.

The pure-Python math (`eee_agent/modeling/orientation_math.py`: covariance,
Jacobi eigendecomposition, quaternion axis comparison) is hou-free and
independently tested.

### New modules

| Module | Role |
|---|---|
| `eee_agent/houdini_bridge/scratch.py` | DTOs for exec/commit/destroy (strict, frozen, canonical JSON) |
| `eee_agent/modeling/scratch_coordinator.py` | Agent-facing coordinator + `scratch_build`/`scratch_commit` tools |
| `eee_agent/modeling/orientation_math.py` | Pure-Python PCA + axis-comparison math (ported from Pi) |
| `houdini_side/scratch_verify.py` | The four verify gates (ported from Pi, hou-free module) |

### Touched production modules

- `eee_agent/runtime/agent_context.py` — added `scratch` slot to
  `RuntimeToolContext`.
- `eee_agent/runtime/agent_runner.py` — registers `scratch_build` +
  `scratch_commit` on modeling runs; `propose_modeling` retired.
- `eee_agent/runtime/service.py` — `_build_scratch_context` (run-scoped
  sandbox_id), `_scratch_bridge_provider` injection, `_sandbox_id_from_run`
  helper, and `_cleanup_scratch_sandbox` hook in `_run_guarded`'s finally
  (covers completed/failed/cancelled).
- `eee_agent/system_prompt.py` — Pi-style iterative workflow
  (UNDERSTAND → KNOWLEDGE → BUILD → OBSERVE → VERIFY → COMMIT) replacing the
  old PLAN/PROPOSE/REVIEW.
- `eee_agent/houdini_bridge/client.py` — `scratch_exec` / `scratch_commit` /
  `scratch_destroy` client methods.
- `eee_agent/houdini_bridge/changeset_provider.py` — provider methods for all
  three ops.
- `houdini_side/changeset_executor.py` — `scratch_exec` / `scratch_commit` /
  `scratch_destroy` executor methods + commit receipt builder.
- `houdini_side/secure_bridge.py` — dispatch + `_serve_scratch_*` handlers for
  all three ops.

## Verification

Full offline gate: **3446 passed, 12 skipped** (the 12 skips need a local
Houdini 21.0.440 HFS + hython or WSL and are unrelated to this change).

New test coverage (~200 tests):
- `tests/modeling/test_orientation_math.py` — 27 pure-math tests.
- `tests/runtime/test_scratch_verify.py` — 29 gate tests (each gate's
  pass/refuse path against fake-hou geometry).
- `tests/runtime/test_scratch_bridge.py` — 108 tests (exec/commit/destroy
  DTO round-trips, client flows, coordinator behavior, tool context gating,
  service cleanup hook).

## Key design decisions (why this shape)

- **Structured single-op mode first, raw `exec` deferred.** create/set/connect
  are sub-second and fit the existing 30s synchronous bridge, so an async job
  protocol is not needed yet. network_mode (raw Python exec) is deferred until
  structured ops prove insufficient.
- **Sandbox nodes carry no ownership mirror.** This prevents polluting a real
  workspace's node-id index — a load-bearing constraint.
- **`scratch.exec` / `scratch.commit` gate on `write_frozen`; `scratch.destroy`
  does not.** Cleanup must run even after an uncertain recovery.
- **Commit wrapped in one undo group.** Stronger than Pi (which has no undo
  layer around commit) — a gate failure rolls back atomically.
- **ChangeSet/ownership/recovery kernel retained.** It is the commit
  persistence seam and the cross-restart recovery path (Pi lacks this).
- **`propose_modeling` retired from the graph, module retained.** The Brief/
  Spec/Compiler layer is kept pending the new workflow stabilizing, per the
  plan's risk note ("avoid deleting valuable things before the workflow is
  validated").

## Open / next

- **Real-Houdini end-to-end smoke** of the full scratch_build → observe →
  scratch_commit cycle against a live Houdini 21.0.440 (the offline tests use
  fake-hou; a real cook is the remaining acceptance gate).
- **Full deletion of the retained propose_modeling / compiler / proposal
  modules** once the sandbox workflow is validated in real use (Phase 3
  deferred the type-relocation work — `NodeCatalog`/`WorkspaceBootstrapContext`
  live in `compiler.py` and are reused by `catalog.py` / `service.py`).
- **Deferred `network_mode` (raw Python exec) + async job protocol** — only if
  structured single-op mode proves insufficient for real modeling tasks.
