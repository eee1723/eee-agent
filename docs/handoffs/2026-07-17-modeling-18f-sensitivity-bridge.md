# Task 18-F Production Sensitivity Bridge Handoff - 2026-07-17

## Result

Parameter-sensitivity sampling moved from deterministic validation/disposable
smoke into a typed, main-thread Bridge operation with guaranteed exact
restoration and fail-closed evidence. Offline implementation is accepted
locally; real-hython wire verification is deferred until a Houdini license is
available on this machine (the license daemon was intermittently unavailable
during this slice).

## Implementation

- New additive `sensitivity.v1` capability and `sensitivity.sample` typed
  operation in `eee_agent/houdini_bridge/sensitivity.py`: frozen, bounded,
  canonical-JSON `SensitivitySampleTarget`/`SensitivitySampleRequest`/
  `SensitivitySampleResult`/`SensitivitySampleResponse` DTOs with strict
  parsers, reusing the `changesets.py` envelope and strictness helpers. Only
  absolute node paths, stable node ids, parameter names, and literal numeric
  sample values cross the wire — never expressions, source, or callables.
- `BridgeClient.sample_sensitivity()` gates on the advertised capability and
  fails closed with `bridge.capability_unavailable` without sending a frame;
  malformed/mismatched responses abort the connection exactly like
  `changeset.apply`.
- `BridgeChangeSetProvider.sample_sensitivity()` builds the request from the
  applied ChangeSet (affected node paths, scene epoch) and calls with
  `may_have_changed=True`.
- `BridgeServer` advertises `sensitivity.v1` by default, dispatches
  `sensitivity.sample` on the single shared main-thread FIFO, and gates on
  capability and write-freeze state BEFORE any HOM access.
- `ChangeSetExecutor.sample_sensitivity()` resolves every target and reads
  every original value before the first write (zero writes on stale scene,
  unresolvable target, or missing parameter); from the first write on it
  never raises out of the write phase: each sample writes, force-cooks, and
  captures bounded `SceneQueryResult` evidence, then restores every written
  parameter to its exact original value in reverse order with read-back
  verification. An unverifiable restore freezes writes and fails closed with
  `sensitivity.restore_failed`; cook failures and aborted captures are
  classified, never guessed.
- `derive_sensitivity_sample_plan()` samples only parameters the compiled
  ChangeSet itself sets via `SetParm` that the trusted catalog defines as
  safe literal numeric parameters, capped by the quality profile and the
  16-sample bound; the plan is empty (stage stays Unavailable) when the
  profile disables `ParameterSensitivity` or no eligible parameter exists.
- `RuntimeService._validate_applied_changeset` feeds bridge evidence into
  `validate_parameter_sensitivity()` and resolves the ParameterSensitivity
  stage from Unavailable to a final result; any bridge/sampling failure keeps
  the fail-closed durable `modeling.validation_unavailable` event and never
  replays an already durable Apply. `validate_scene_query()` accepts optional
  sensitivity evidence and resolves the stage in the same step.

## Evidence

- `uv run --frozen --extra eval pytest -q` -> `2212 passed`
- New focused tests: 38 (contracts/client, executor fault-injection, wire
  round-trip) + 2 Runtime integration tests covering the resolved
  ParameterSensitivity event and the unavailable fail-closed path
- Required cases from the roadmap covered: interruption, cook-failure,
  restore-failure, stale-scene, restart (no replay of uncertain state), and
  evidence (digest contents, bounded sizes, frozen DTOs)
- `uv lock --check` -> passed
- compileall -> passed
- `git diff --check` -> passed
- Real-hython (Houdini 21.0.440) after the executor/server changes:
  `tests/runtime/houdini_bridge_smoke.py` -> `20 checks passed, 0 failed`;
  `tests/runtime/changeset_houdini_smoke.py` -> SMOKE OK;
  `tests/modeling/bootstrap_houdini_smoke.py` -> BOOTSTRAP SMOKE PASS.
  A dedicated real-hou wire smoke for `sensitivity.sample` itself remains a
  follow-up; the op is covered offline at the contracts, executor
  fault-injection, and loopback wire layers.

## Next slice

Task 19-A content-addressed artifact evidence and capture, or further 18-G
catalog batches in small hython-verified steps.
