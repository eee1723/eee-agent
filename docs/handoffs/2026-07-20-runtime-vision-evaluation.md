# Runtime Vision and Evaluation handoff

Date: 2026-07-20
Branch: `feature/runtime`

> Handoff status: core contracts/router/event API are implemented, but the
> router is not yet invoked by the production post-Apply flow. See
> `2026-07-20-runtime-development-transfer.md`; do not mark Task 8 complete.

## Delivered

- Strict frozen Vision DTOs reject unknown fields, unsupported media, absolute
  or escaping paths, oversized text/list/image budgets, non-finite confidence,
  and advisory decisions that override deterministic validation failure.
- `VisionRouter` resolves only an available `ArtifactRef` from `ArtifactStore`.
  It never reads a Bridge source path. The canonical file must resolve below
  the managed artifact root and be a regular file.
- Artifact reads are limited to the provider budget plus one byte, then checked
  against the stored size and SHA-256 before the exact bytes are sent to the
  provider.
- Provider unavailable, API-key missing, timeout, malformed/schema-invalid
  response, waiver, missing/changed artifact and deterministic failure paths
  produce bounded fail-closed outcomes.
- `DeliveryEvaluation` contains bounded brief/spec, ChangeSet digest, approval,
  receipt, validation evidence, artifact refs/status, Knowledge manifest,
  Vision status/report, final decision and recovery evidence.
- `RuntimeService.record_vision_evaluation()` persists the record as a durable
  `vision.evaluation_completed` event, committed before subscriber delivery and
  recoverable through event replay.

## Verification

- Vision contracts/router/service focused suite: **20 passed**.
- Full frozen repository gate: **2852 passed, 11 skipped** in 165.26s.
- Explicit HFS Knowledge contract: **11 passed** in 15.13s.
- Panel state/package suite: **86 passed**.
- Panel package plus Runtime process restart/replay suite: **15 passed**.
- Ruff, focused Mypy, compileall, `uv lock --check`, and `git diff --check`
  passed.
- After the final evidence/CI hardening commit, the combined MVP + Vision
  focused suite is **30 passed**; the full frozen suite must be rerun.

## Gate status

- `offline-ready`: ready.
- `real-Houdini-ready`: ready for the existing typed Bridge/HFS smokes.
- `real-provider-tested`: not run; the Task 7 adapter trust boundary still
  requires approved credentials and a real provider journey.
- `vision-contracts-ready`: ready.
- `vision-provider-tested`: not run; tests use an injected deterministic fake.
- `GUI-accepted`: pending interactive Houdini validation.

## Houdini package deployment

The current worktree package was installed with Houdini 21.0.440 hython:

```text
package: C:\Users\EEE\Documents\houdini21.0\packages\eee_agent.json
project root: E:/eee-agent/.worktrees/runtime
panel: E:/eee-agent/.worktrees/runtime/python_panels/EEEAgentRuntime.pypanel
```

At deployment time a running Houdini window (`untitled.hip`) predated the
package update. It was not closed automatically because its save state is not
known. Restart Houdini before treating any manual GUI result as current.

## Remaining risks

- Provider execution failures currently use `VisionStatus.UNAVAILABLE` with a
  specific `vision.provider_failed` reason. The unused `FAILED` status should
  be selected only if the product needs a distinct UI state.
- The Houdini panel currently records the durable Vision event in the generic
  event/replay stream but has no dedicated normalized Vision report view.
- `VisionRouter` is not yet called from the production post-Apply
  validation/capture path; this is the current P1 implementation blocker.
- GUI acceptance, real provider evidence, release tag and pushing the Runtime
  branch remain intentionally incomplete.
