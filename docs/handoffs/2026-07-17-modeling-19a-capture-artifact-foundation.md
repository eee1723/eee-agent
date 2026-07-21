# Task 19-A Capture and Artifact Foundation Handoff - 2026-07-17

## Result

Task 19-A first slice (capture + artifact foundation) is implemented and
accepted locally: typed `capture.v1` Bridge operation, content-addressed
artifact store with bounded retention, deterministic framing preflight with
at most two adjustments, hash-verified byte identity, explicit fail-closed
capture failures, and a read-only panel Artifacts section. Vision routing
(Task 19-B) is out of scope; `artifact.reveal/open/copy_reference` stay
deferred.

## Implementation

- `eee_agent/houdini_bridge/capture.py`: `capture.v1` capability +
  `capture.capture` typed op. Frozen, bounded, canonical-JSON DTOs (request:
  node paths, scene epoch, Runtime-owned absolute target dir, artifact id,
  deterministic render/framing settings; result: content-addressed reference
  fields + bounded framing report). Image bytes never cross the wire.
- `eee_agent/modeling/framing.py`: pure deterministic framing (bbox → camera
  placement, margin evaluation, ≤2 fixed-order adjustments). No hou, no
  randomness, no wall clock; bit-identical output for identical input.
- `houdini_side/changeset_executor.py::capture`: stale-epoch precheck;
  force-cook evidence nodes; world-space union bbox (SOP `geometry()` lifted
  by the parent object transform; object-level nodes resolved through
  `displayNode()` + `worldTransform()` — `ObjNode` has no `geometry()`);
  owned temp camera + Vulkan flipbook ROP scope (1280x960 PNG, 8x AA, fixed
  headlight, no grid/materials/textures) journaled immediately and destroyed
  in `finally` semantics; render into a sibling temp directory so the picture
  path keeps its `.png` extension (the flipbook picks the format from the
  extension; `.png.tmp` silently rendered a PIC file — found by real-hython
  smoke); PNG magic + streamed sha256 verified before atomic rename; every
  failure classified (`capture.invalid/target_unavailable/cook_failed/
  no_geometry/framing_failed/render_unavailable/render_failed/cleanup_failed`),
  never a guessed success.
- `eee_agent/runtime/artifacts.py` `ArtifactStore`: SQLite `artifacts` table
  (schema v4) + filesystem bytes under `artifacts/<session>/<run>/`;
  registration re-hashes the delivered file and rejects hash/size mismatch
  (file deleted, fail closed); bounded retention (per-session 32 artifacts /
  64 MiB, global 256 / 512 MiB) with deterministic oldest-first eviction of
  file + row that never touches the event log; `redacted` column carries the
  19-B redaction hook shape; `delete_session` removes artifact files with a
  structured cleanup error.
- `eee_agent/runtime/service.py`: after a durable successful Apply, capture
  runs only when the profile enables the Artifact validator and the provider
  exposes the typed op; success resolves `ValidatorKind.ARTIFACT` via
  `validate_artifact_capture` and emits durable `modeling.artifact_captured`
  (ArtifactRef fields, never bytes); any failure emits durable
  `modeling.capture_failed` with a structured code plus a Failed Artifact
  result — capture failure never masquerades as visual success and never
  replays the durable Apply.
- Panel: read-only Artifacts section in the Details surface fed by durable
  events; strict parsing + bounded summaries live in
  `eee_agent/panel/runtime_state.py` (`parse_artifact_event`,
  `append_artifact_summary`, `_ARTIFACT_REFRESH_EVENTS`).
- Byte identity: one stored file is the single source of truth; its sha256
  is computed once at capture and re-verified by the Runtime before
  registration; the panel and any later vision input read the same bytes.

## Evidence

- `uv run --frozen --extra eval pytest -q` -> `2317 passed`
- `uv lock --check`, compileall, `git diff --check` -> passed
- Real Houdini 21.0.440 hython smoke `tests/runtime/capture_houdini_smoke.py`
  -> `SMOKE OK: 21 checks passed, 0 failed` (byte-identity sha256, 1280x960
  PNG IHDR, framing band, scope cleanup, scene not mutated, stale epoch
  fail-closed with zero writes)
- Executor world-space bbox fix covered by two new fake-hou tests
  (object-level displayNode + world transform; SOP lifted by parent
  transform); PIC-format regression covered by the temp-directory scheme.

## Next slice

Task 19-B vision routing and evaluation (advisory only, never overrides
deterministic validator failures) and Task 19-C delivery/observability;
Task 18-G richer asset-level Golden Cases in small hython-verified catalog
batches; Task 18-H final GUI pass stays deferred to real-Houdini acceptance.
