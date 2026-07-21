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

- `feature/runtime` was pushed to `origin` on 2026-07-20 after the P1 Vision
  wiring closed, so development can continue from another machine. The push
  deliberately preceded the manual GUI/provider gates; those gates below
  remain open and are still required before any release.
- Local branches retained: `main`, `wip/pre-migration-main`, `feature/runtime`.
- Remote branches retained: `origin/main`, `origin/wip/pre-migration-main`,
  `origin/feature/runtime`.
- Obsolete local/remote `feature/foundation` and
  `feature/houdini-knowledge-graph` branches and worktrees were already removed.
- Preserve the annotated tags:
  `runtime-pre-mvp-2026-07-17`, `foundation-final-2026-07-17`,
  `knowledge-graph-final-2026-07-17`, and
  `runtime-pre-knowledge-integration-2026-07-17`.
- No Runtime release-candidate tag exists. Do not create the RC tag until the
  manual GUI/provider gates below are closed.

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
| S9 | partial | Real provider journey passed 2026-07-20 (see below); interactive GUI checklist and RC tag remain open. |

Key recent commits:

- `424e9e4` CI frozen/static/HFS gates.
- `93eb2d6` + `6bbe64b` + `e132a1a` MVP acceptance and hardening.
- `74e7c73` through `1674173` strict Vision contracts/router/evaluation.
- `e1f8f0a` durable `vision.evaluation_completed` service API.
- `525e727` strict provider evidence, contradictory Vision evidence rejection,
  Vision CI lint/type coverage, and commit-range whitespace checking.
- `d13fdc3` advisory Vision wired into the production post-Apply flow.
- `c7f3d0f` bounded Vision evaluation rendering in the Runtime panel.
- `ed3bc1d` queued Bridge reads cancelled on client disconnect.
- `9addf8a` artifact panel lifecycle rows merged into captured rows.

## Verification evidence

Latest complete repository run (at `9addf8a`):

```text
uv run --frozen --extra eval pytest -q
2900 passed, 11 skipped in 161.91s
```

The 11 normal-gate skips are the explicit Houdini Knowledge contract. It was
run separately (at `d13fdc3`):

```text
EEE_RUN_HOUDINI_KB_TESTS=true
tests/knowledge/test_hfs_contract.py: 11 passed in 9.15s
```

Static gates at `9addf8a`:

```text
Ruff Runtime + Vision + panel: passed
Mypy selected Runtime boundaries + all Vision: passed
compileall: passed
uv lock --check: passed (90 packages)
git diff --check: passed
```

Focused suites added on 2026-07-20:

```text
Vision contracts/router/delivery: 26 passed
Affected Apply/MVP/e2e/panel suites: 165 passed
Panel suite incl. Vision parse and artifact dedup: 122 passed
Bridge transport/queue/preflight/executor suites: 344 passed
```

One transient failure of `test_lock.py::test_os_releases_lock_on_subprocess_termination`
was observed once under full-suite load; it passes in isolation and in
subsequent full runs and is a Windows subprocess-timing flake, not a
regression.

Earlier evidence predating `525e727` (for the historical record):

```text
2852 passed, 11 skipped in 165.26s
MVP + Vision focused suites: 30 passed in 3.69s
```

Additional GUI/runtime automation:

```text
tests/panel: 86 passed
panel package + Runtime process restart/replay: 15 passed
```

The full frozen suite has been rerun after `525e727`; the current evidence is
the `9addf8a` run above.

## Resolved blocker

### P1 (closed 2026-07-20): Vision is wired into the production Apply flow

The wiring was committed as `d13fdc3` (service, `__main__`, new delivery
tests and doc updates) and is included in the 2026-07-20 push.

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

Remaining open gates all belong to S9: the real provider journey and the
interactive GUI checklist (the dedicated Vision panel rendering landed in
`c7f3d0f`; the branch was pushed for cross-machine development, while the RC
tag stays gated).

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

`hython` resolves the current `EEEAgentRuntime.pypanel` and the
`houdini_side/runtime_panel/` package (the three-pane redesign landed after
this handoff was written; see
`docs/superpowers/plans/2026-07-21-runtime-panel-three-pane.md`). A visible
Houdini FX 21.0.440 process was
started as PID `20772` with window title `untitled.hip - Houdini FX 21.0.440`.
Do not close it without checking for unsaved work.

Still required:

- ~~Real provider journey using approved credentials and the strict evidence
  sidecar~~ Done 2026-07-20 (second machine, `Z:/EEE_Project/EEEProceduralModeling`,
  Houdini 21.0.440 + DeepSeek): harness `tests/runtime/runtime_mvp_provider_e2e.py`
  returned `{"status": "passed", "reason": "provider_journey_completed"}` with
  the new adapter `tests/runtime/provider_journey.py` (+ hython worker
  `provider_journey_houdini_worker.py`). The first real run exposed two
  production gaps, both fixed and covered by offline tests: (1) production
  never passed a `read_only_provider` to `RuntimeService.open()`, so all
  read-only tools were always `bridge.unavailable` — new
  `eee_agent/houdini_bridge/read_only_provider.py` (`BridgeReadOnlyProvider`)
  wired in `__main__.py`; (2) the strict Brief/Spec schema was invisible to
  the model (`propose_modeling` took bare `dict` args) — the tool docstring
  now embeds the exact schema, validated by
  `test_tool_docstring_minimal_skeleton_parses`. The Vision seam remains
  `None` in production; the Vision real-provider journey is still open.
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
- 2026-07-21: the panel view layer was rebuilt as the three-pane
  conversation-centric layout (spec
  `docs/superpowers/specs/2026-07-21-runtime-panel-three-pane-design.md`).
  The interactive GUI checklist must now run against this UI; the checklist
  items are unchanged. Backend startup is automatic (panel spawns
  `python -m eee_agent.runtime serve` when discovery is missing) — verify
  the auto-start path as part of the checklist's reconnect/restart item.
- 2026-07-21 (conversation UX): the panel now streams assistant replies
  token-by-token (`model.text_delta`, already pushed by the backend) and
  renders a collapsible thinking block from `model.reasoning_delta` (new
  panel-side accumulator in `RuntimePanelState._thinking`; OPERATIONAL, not
  replayed from history). Cards branch on `kind` (user accent bubble vs
  assistant surface card). The composer is multi-line with Ctrl+Enter submit
  (bare Enter inserts a newline so IME confirmation stays safe). Sending the
  first prompt with no active Session auto-creates one (placeholder
  "New session"); once its first run completes the backend renames it via a
  best-effort LLM call (`eee_agent/runtime/titles.py` +
  `RuntimeService._maybe_autotitle_session`), and the panel refreshes the
  sidebar in place on the `session.renamed` event. The previous "no reply"
  bug (terminal-only output render) is superseded by live streaming. Full
  offline gate: ~2960 passed, 11 skipped.

## Residual P2 risks

- `VisionStatus.FAILED` is not used; provider execution failures currently use
  `UNAVAILABLE` plus `vision.provider_failed`.
- ~~Production Secure Bridge disconnect is not yet proven to automatically call
  `MainThreadReadQueue.cancel(request_id)`~~ Closed 2026-07-20: every queued
  request now polls the transport EOF through `await_with_signal()` and
  cancels the queued item on disconnect (queued items never run for a dead
  client; a running item's result is discarded for the durable receipt path).
  Proven by `test_client_disconnect_cancels_queued_request` and the healthy
  sequential-traffic regression test in
  `tests/runtime/test_houdini_bridge_transport.py`.
- ~~Artifact panel summaries may show duplicate lifecycle/captured rows~~
  Closed 2026-07-20: `append_artifact_summary()` now merges a lifecycle event
  into the already-listed captured row for the same artifact instead of
  adding a duplicate row. Failed canonical artifact bytes intentionally remain
  for the retryable cleanup path.
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

Only after the real provider gate, the manual GUI gate, a fresh full suite
and a final code review are all approved should the next developer create and
push the Runtime RC tag. (The production Vision wiring is done and
`feature/runtime` was already pushed on 2026-07-20 for cross-machine
development.)
