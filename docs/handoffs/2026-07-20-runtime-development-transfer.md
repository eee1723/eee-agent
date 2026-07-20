# Runtime development transfer

Date: 2026-07-20
Workspace: `E:\eee-agent\.worktrees\runtime`
Branch: `feature/runtime`
Reviewed code checkpoint: `525e727315d9055cd27e751474f109c8486e9366`

This is the entry document for the next developer. Read the approved design
and execution plan first:

- `docs/superpowers/specs/2026-07-17-runtime-mvp-development-design.md`
- `docs/superpowers/plans/2026-07-17-runtime-mvp-development-plan.md`

## Repository state

- The worktree is clean at the reviewed checkpoint, before this handoff commit.
- `feature/runtime` is **40 commits ahead** of `origin/feature/runtime`; none of
  the new Runtime/MVP/Vision commits have been pushed.
- Local branches retained: `main`, `wip/pre-migration-main`, `feature/runtime`.
- Remote branches retained: `origin/main`, `origin/wip/pre-migration-main`,
  `origin/feature/runtime`.
- Obsolete local/remote `feature/foundation` and
  `feature/houdini-knowledge-graph` branches and worktrees were already removed.
- Preserve the annotated tags:
  `runtime-pre-mvp-2026-07-17`, `foundation-final-2026-07-17`,
  `knowledge-graph-final-2026-07-17`, and
  `runtime-pre-knowledge-integration-2026-07-17`.
- No Runtime release-candidate tag exists. Do not tag or push until the open P1
  and manual GUI/provider gates below are closed.

## Completed milestones

| Stage | State | Main result |
| --- | --- | --- |
| S0 | complete | Baseline frozen and tagged; risks/handoff recorded. |
| S1 | complete | Secure `RuntimeToolContext`, bounded read-only tools and lazy imports. |
| S2 | complete | Legacy raw-write Bridge/tools/panel/RPC paths and dependencies removed. |
| S3 | complete | Artifact pending/available/eviction/failure/reconcile lifecycle is durable. |
| S4 | complete | Real Houdini 21.0.440 sensitivity smoke passed 25 checks. |
| S5 | complete | Read-only Knowledge cache integrated with strict status/snapshot metadata. |
| S6 | complete | Input/DTO hardening and Windows frozen/static/HFS CI workflow added. |
| S7 | offline complete | Deterministic MVP acceptance plus strict provider evidence harness. |
| S8 | offline complete | Vision wired into the production post-Apply flow; real provider journey is an S9 gate. |
| S9 | incomplete | GUI/manual provider/release gates remain open. |

Key recent commits:

- `424e9e4` CI frozen/static/HFS gates.
- `93eb2d6` + `6bbe64b` + `e132a1a` MVP acceptance and hardening.
- `74e7c73` through `1674173` strict Vision contracts/router/evaluation.
- `e1f8f0a` durable `vision.evaluation_completed` service API.
- `525e727` strict provider evidence, contradictory Vision evidence rejection,
  Vision CI lint/type coverage, and commit-range whitespace checking.

## Verification evidence

Latest complete repository run before `525e727`:

```text
uv run --frozen --extra eval pytest -q
2852 passed, 11 skipped in 165.26s
```

The 11 normal-gate skips are the explicit Houdini Knowledge contract. It was
run separately:

```text
EEE_RUN_HOUDINI_KB_TESTS=true
tests/knowledge/test_hfs_contract.py: 11 passed in 15.13s
```

Fresh verification after `525e727`:

```text
MVP + Vision focused suites: 30 passed in 3.69s
Ruff Runtime + Vision: passed
Mypy selected Runtime boundaries + all Vision: passed
compileall: passed
uv lock --check: passed (90 packages)
git diff --check: passed
```

Additional GUI/runtime automation:

```text
tests/panel: 86 passed
panel package + Runtime process restart/replay: 15 passed
```

The next developer must rerun the full frozen suite after `525e727`; the latest
complete full-suite evidence predates that commit, although its focused/static
gates pass.

## Resolved blocker

### P1 (closed 2026-07-20): Vision is wired into the production Apply flow

The wiring change sits in the working tree on top of `1e52d7a` (service,
`__main__`, new delivery tests and these doc updates); commit it as its own
change before any tag or push.

The production wiring from the original six-step order is implemented:

1. `RuntimeService.__init__()` and `RuntimeService.open()` accept an optional
   typed `vision_provider` seam and always construct a `VisionRouter` over the
   service `ArtifactStore`.
2. `eee_agent/runtime/__main__.py` injects the seam via `_vision_provider()`
   (currently `None` until the real-provider gate; no provider SDK or
   credential is imported into Houdini-side modules).
3. After a successful ArtifactStore capture and the durable deterministic
   validation event, `RuntimeService._record_delivery_evaluation()` builds a
   `VisionRequest` from the registered `ArtifactRef` (never the Bridge source
   path), invokes `VisionRouter`, and persists a semantically consistent
   `DeliveryEvaluation` with `record_vision_evaluation()`.
4. Advisory/provider failure is recorded as bounded unavailable evidence and
   never replays, undoes, or fails the durable Apply.
5. `tests/runtime/test_vision_delivery.py` proves capture -> exact
   ArtifactStore bytes -> Vision -> durable replayed
   `vision.evaluation_completed`, plus deterministic failure precedence,
   provider failure isolation, and no event without a registered capture.
6. Deterministic failure precedence holds: a failed deterministic report can
   never become `accepted=true`.

Verification after the wiring:

```text
Full frozen suite: 2859 passed, 11 skipped in 155.06s
Vision focused (contracts/router/delivery): 26 passed
Affected Apply/MVP/e2e/panel suites: 165 passed
Ruff Runtime + Vision: passed
Mypy selected Runtime boundaries + all Vision: passed
compileall / uv lock --check / git diff --check: passed
```

Remaining open gates are unchanged and all belong to S9: real provider
journey, dedicated Vision panel rendering, the interactive GUI checklist, the
RC tag and the push.

## Recently closed audit findings

- A no-op provider command can no longer claim success. The adapter must write
  <=16 KiB strict JSON to `EEE_RUNTIME_MVP_EVIDENCE_PATH` with exact proposal,
  approval, receipt, validation, artifact, replay and cleanup evidence.
- `DeliveryEvaluation` now rejects completed-without-report,
  unavailable-with-report, and accepted decisions that contradict an advisory
  failure or deterministic failure.
- CI now lints/types `eee_agent/vision` and checks whitespace across the actual
  PR/push commit range rather than only an already-clean checkout.

## Manual gates and local Houdini state

The package is installed at:

```text
C:\Users\EEE\Documents\houdini21.0\packages\eee_agent.json
EEE_PATH=E:/eee-agent/.worktrees/runtime
```

`hython` resolves the current `EEEAgentRuntime.pypanel` and
`houdini_side/runtime_panel.py`. A visible Houdini FX 21.0.440 process was
started as PID `20772` with window title `untitled.hip - Houdini FX 21.0.440`.
Do not close it without checking for unsaved work.

Still required:

- Real provider journey using approved credentials and the strict evidence
  sidecar. Current status: `not run`.
- ~~Dedicated panel rendering for normalized Vision status/report~~ Done
  2026-07-20: `parse_vision_event()` validates the durable
  `vision.evaluation_completed` payload into a bounded summary and the
  ARTIFACTS tab renders a dedicated VISION EVALUATIONS section (status,
  decision, advisory result, bounded summary). Visual layout acceptance stays
  part of the interactive GUI checklist below.
- Interactive Houdini GUI checklist: narrow/docked layout, focus, Chinese IME,
  review/approval mouse flow, reconnect/restart, artifacts, recovery, and full
  MODEL -> REVIEW -> Approve and build -> RESULT journey.
- Screenshots/checklist evidence must remain machine-local; do not commit
  secrets, prompts or unredacted provider output.

## Residual P2 risks

- `VisionStatus.FAILED` is not used; provider execution failures currently use
  `UNAVAILABLE` plus `vision.provider_failed`.
- Production Secure Bridge disconnect is not yet proven to automatically call
  `MainThreadReadQueue.cancel(request_id)`; S4 proves the public cancellation
  seam but not full socket-disconnect binding.
- Artifact panel summaries may show duplicate lifecycle/captured rows; failed
  canonical artifact bytes can remain for later cleanup.
- The local `pip-audit` command reached its correct exported dependency input
  but PyPI advisory lookup failed once due a local SSL EOF. Validate it in CI.

## Next command sequence

```powershell
Set-Location E:\eee-agent\.worktrees\runtime
git status --short --branch
git log -12 --oneline
uv run --frozen --extra eval pytest -q
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
uv run --frozen --extra eval pytest -q tests/knowledge/test_hfs_contract.py
Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
uv run --frozen --group dev ruff check eee_agent/runtime eee_agent/vision
uv run --frozen --group dev mypy --follow-imports=skip --ignore-missing-imports eee_agent/runtime/agent_context.py eee_agent/runtime/knowledge.py eee_agent/runtime/agent_tools.py eee_agent/vision
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Only after the production Vision wiring, real provider gate, manual GUI gate,
fresh full suite and final code review are all approved should the next
developer create/push the Runtime RC tag or push `feature/runtime`.
