# Runtime Post-Acceptance And Product Milestones Plan

> **For agentic workers:** Each implementation task is executed by Claude Code with the explicit `glm-5.2[1m]` model. Codex owns this plan, task status, diff review, independent verification, and push/merge decisions. Use one task prompt and one focused commit at a time.

**Goal:** Close Runtime v1 delivery safely, then extend the product through a restricted Houdini bridge, a reconnecting docked client, deterministic modeling capabilities, and artifact/vision evaluation without weakening the Runtime contracts.

**Architecture:** Runtime v1 remains the durable local control plane: application state and events stay in `app.sqlite`, LangGraph checkpoints stay in `checkpoints.sqlite`, and all clients use the authenticated loopback protocol. The next milestones add typed capability boundaries around that plane; no feature may reintroduce arbitrary Houdini Python, shell access, implicit general-purpose subagents, or an unreviewed second persistence path.

**Tech Stack:** Python 3.11, existing Runtime contracts, `websockets`, `aiosqlite`, LangGraph `AsyncSqliteSaver`, Houdini 21/rpyc 4.1.0, PySide6 `.pypanel`, pytest, and offline contract/integration tests.

---

## Operating rules

- Claude Code performs only the concrete implementation named in the current Codex prompt.
- Codex updates this plan and the handoff, reviews the authorized diff, reproduces reported RED evidence when practical, and runs the acceptance commands before advancing the task.
- Do not modify the approved Runtime spec to make a failing implementation fit.
- Automated tests remain offline. Live GLM-5.2 and Houdini checks are explicitly manual and are recorded separately from pytest evidence.
- Never add arbitrary `run_houdini_python`, `eval`, `exec`, shell, filesystem, HDA-install, HIP-load/clear/save, or write-tool access to the Runtime agent.
- A task cannot advance on a green report alone: the worktree, changed-file scope, focused tests, full suite, lock check, compileall, and diff check must be independently verified.
- A GLM gateway `529` is an external blocker; retry the same task later and do not switch models or create duplicate workers.

## Task 14: Runtime v1 external acceptance and delivery gate

**Status:** In progress. The accepted branch was pushed and the 2026-07-15
target-computer restore at `5915720` passed; manual GLM/Houdini acceptance is
still pending.

**Owner split:** Codex/user performs the manual smoke and delivery actions. Claude Code may implement only a follow-up fix prompt naming the exact files and regression tests supplied by Codex.

**Inputs:** Accepted Runtime tips `724a8fb`, `3b69532`, `c8e6af1`, Codex status commit, and the Runtime plan's manual acceptance procedures.

### 14.1 Push and clean restore

- Push the five local commits currently ahead of `origin/feature/runtime` on `feature/runtime`; this sub-step is complete at `9293174`. Never force-push and never merge to `main` in this gate.
- On the target computer, clone/fetch `origin/feature/runtime`, rebuild Python 3.11 dependencies with `uv sync --frozen --extra eval --python 3.11`, and do not transfer `.venv`, SQLite files, tokens, discovery files, logs, or `.env`.
- Verify `uv lock --check`, `uv run --extra eval pytest -q`, `uv run python -m compileall -q eee_agent houdini_side tests`, `git diff --check`, and a clean worktree. The expected current baseline is at least 1388 passed with no failures; machine-dependent environment probes may be skipped only if no new skip is introduced.

Restore evidence recorded 2026-07-15 (Asia/Shanghai): fast-forwarded
`feature/runtime` from `e036da2` to `5915720`; `uv sync --frozen --extra eval
--python 3.11` and `uv lock --check` resolved/checked 69 packages; the complete
offline suite reported `1387 passed, 1 skipped` (1388 collected, with only the
pre-existing optional WSL probe skipped); compileall and `git diff --check`
exited zero; the worktree was clean before Task 16 planning edits. The machine
has Houdini 21.0.440 at `C:\Program Files\Side Effects Software\Houdini
21.0.440` and hython reports Python 3.11.7.

### 14.2 GLM-5.2 read-only continuity smoke

- Start `uv run python -m eee_agent.runtime serve` with the configured GLM credentials and an isolated `EEE_RUNTIME_HOME`.
- Connect over loopback WebSocket using the discovery port and bearer token; create one Session and start a pure-text read-only Run.
- Record the ordered event types, reasoning/text/usage payloads, terminal state, final response, and Run ID. Assert no write-tool name appears.
- Restart Runtime, reconnect, start a follow-up Run in the same Session, and record evidence that the checkpoint-backed conversation is available through `thread_id == session_id`.
- If the provider is unavailable or returns HTTP 529, record the external failure without changing code or weakening offline acceptance.

### 14.3 Houdini read-only smoke

- Run `scripts/env_probe.sh` (or the Windows equivalent) first and confirm the local Houdini 21.0.440 path and bundled rpyc version.
- Start the existing RPC bridge and Runtime. Capture a before snapshot containing HIP path, Houdini version, node inventory, selected read-only geometry facts, and a filesystem hash of the inspected fixture.
- Ask Runtime only for HIP/version/node/geometry inspection. Record the tool names and arguments observed by the bridge.
- Capture the same facts after the run and assert no node creation/deletion/connection/parameter mutation, HIP save/export, HDA change, or file write occurred.
- Stop both processes and confirm Runtime identity files and locks are cleaned up.

### 14.4 Evidence and decision

- Append dated evidence and any external-service failure to the handoff; do not call a failed external smoke a code failure without a reproducible local symptom.
- If all checks pass, mark Task 14 `Complete, Codex accepted`, then decide separately whether to merge `feature/runtime` into `main`.
- If a check fails, create exactly one follow-up defect task with: RED command, exact failure, authorized files, regression test, and acceptance commands. Do not begin Task 15 implementation until that fix is accepted or Codex explicitly records the external-smoke exception and authorizes the read-only foundation slice.

## Task 15: Secure HoudiniBridge contract and transport

**Dependency:** Task 14 accepted, or an explicit decision to proceed with manual smoke pending.

**Current state:** Task 15-A (strict DTO and error contracts) is Codex-accepted
at `4c22d45`, and Task 15-B (Bridge identity and authenticated client) is
Codex-accepted at `379a5c8`. Task 15-B delivered 33 auth tests, 34 client
tests, a 257-test focused regression slice, and a fresh full offline suite of
1275 passed. Task 15-C (main-thread queue and Houdini-side scene query) is
Codex-accepted at `c717e60` (implementation `3d4687f` plus the cross-thread
Future-resolution fix); its final focused regression passed 311 tests and the
fresh full offline suite passed 1329 tests. Task 15-D (token handoff, real
transport, and integration acceptance) is Codex-accepted at `bfc00f3` on top
of implementation `fcdee32`; its final focused regression passed 370 tests,
the full offline suite passed 1388 tests, and the real Houdini smoke passed 20
checks. The read-only design and executable breakdown are recorded in
`docs/superpowers/specs/2026-07-15-secure-houdini-bridge-readonly-design.md`
and `docs/superpowers/plans/2026-07-15-secure-houdini-bridge-readonly.md`.

**Purpose:** Replace the legacy unrestricted bridge path with a typed, loopback-only, main-thread-aware read-only boundary. This task is specification-first because the Runtime spec explicitly deferred it; write effects remain in Task 16.

**First deliverable:** Codex-approved design addendum covering DTO schemas, bridge token separation, request IDs, capability names, scene epoch, queue semantics, timeout/cancellation, error taxonomy, and rollback behavior. No implementation starts before that addendum is reviewed.

**Implementation slices (each a separate Claude prompt/commit):**

1. Restricted DTOs and compatibility parser: typed `scene.query` (including the current Houdini selection); reject unknown fields, stale scene epoch, wrong token, and oversized payloads.
2. Houdini main-thread queue: bounded request queue, deterministic request completion, cancellation boundaries, and no direct background-thread `hou` calls.
3. SceneBinding: scene epoch increments on load/clear, HIP identity is bounded, and all returned node facts are read-only.
4. Bridge lifecycle and recovery: separate Bridge token delivered through an
   atomic `bridge.token` handoff file, authenticated loopback transport,
   bounded shutdown, stale-request cleanup, and process-restart diagnostics.

**Acceptance gate:** token-file write/read/cleanup and failure tests; pure
Python contract tests; offline loopback transport tests; hython/Houdini
integration tests; unauthorized/wrong-epoch tests; cancellation and
queue-order tests; and proof that legacy unrestricted entry points are not used
by Runtime by default. The full token must never appear in discovery, logs,
SQLite, events, command lines, or environment variables.

**Task 15-D status:** Accepted after independently verifying the listener
startup failure path. `BridgeServer.serve()` now owns the adopted listener for
the lifecycle, closes and awaits it when token/discovery publication fails,
preserves the original publication exception, and removes both identity files.
`BridgeServer.stop()` is idempotent and closes the listener, queue, writers,
adapter callback, and identity files in a bounded order. No UI, ChangeSet,
approval, or scene-write behavior is included.

## Task 16: Policy, typed ChangeSet, approval, and transactional execution

**Dependency:** Task 15 DTOs, SceneBinding, and WorkspaceManifest accepted.

**Current state:** Design and executable slices are Codex-reviewed in
`docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md` and
`docs/superpowers/plans/2026-07-15-typed-changeset-policy.md`. Task 16-A is
complete and Codex-accepted at `79f281d` after the implementation
chain `6b94cb5`, `9f750ae`, `6b860ff`, `dff573f`, and `79f281d`; all F1-F8
findings are closed. The independent gate passed 187 focused, 423 regression,
and 1574 full offline tests with only the pre-existing optional WSL probe
skipped. Eight historical identity/policy findings are recorded in
`docs/superpowers/reviews/2026-07-15-task16-a-review-result.md`. Task 15 did not
deliver WorkspaceManifest; the accepted Task 16-A contract now supplies that
missing immutable foundation before any write capability is enabled.

Task 16-B is split for auditability: B1 (schema v2, migration checksums, and
typed repository) is Codex-accepted at `54f2989` after implementation
`a3914f5`; B2 (approval service and Runtime protocol) is now unblocked but has
been split into B2a/B2b. B2a is Codex-accepted at `7b3bff8` after
implementation `8fed692` and two approval-integrity follow-ups. Task 16-C is
Codex-accepted at `6050a00` after implementation `86a6bb6` and two
identity-integrity follow-ups; B2b may now be planned against that accepted
provider seam. Task 16-D is Codex-accepted at `3435f4b`. Its independent gate
passed 462 focused and 1821 full offline tests (one existing optional WSL skip),
plus the real Houdini 21.0.440 transactional smoke. The closed findings and
residual boundary are recorded in
`docs/superpowers/reviews/2026-07-16-task16-d-review-result.md`.

Task 16-D1 is Codex-accepted at `39f7346` with status commit `2ee9a2e`. It
closes the residual intra-ChangeSet created-reference boundary: exact earlier
created nodes may be set, connected, or used as create parents with JIT stale
checks and the accepted rollback/freeze semantics. Its independent gate passed
513 focused and 1872 full offline tests (the same optional WSL skip), plus a
real Houdini 21.0.440 created-parent/created-endpoint smoke. B2b was later
accepted through `b2a1b80`; Task 16-E is implemented, locally accepted, and
included in its focused local commit. See
`docs/superpowers/reviews/2026-07-16-task16-e-review-result.md`.

- Add immutable `ChangeSet`, precondition, approval, receipt, and validation contracts in a new milestone-specific spec.
- Implement policy modes `OwnedWorkspace`, `ScopedPatch`, and `ProjectChange`; default-deny external nodes and all untyped effects.
- Implement deterministic preview → approval → precondition re-read → apply → reconcile → receipt flow. Stale name/type/ownership/revision/scene-epoch data must fail closed.
- Make repeated receipt application idempotent and ensure partial Houdini transactions report recovery state rather than claiming success.

**Acceptance gate:** duplicate-apply, stale-change, denied-permission, scene-change, rollback, and process-restart tests; no Runtime event may report durable success before reconciliation facts are persisted.

## Task 17: Docked `.pypanel` client

**Dependency:** Runtime protocol plus the read-only portion of Task 15. The read-only panel slice may start before Task 16; its write/approval slice must wait for Task 16.

**Current state:** Task 17-A is accepted after its real Houdini
dock/restart/selection/zero-mutation gate. Task 17-B now has its own bounded
design and plan and is implemented/offline-accepted in `13e0782`. The panel
creates/selects Sessions, starts/stops Runs, rebuilds bounded Run output and
activity from snapshots plus ordered events, lists durable bounded ChangeSet
summaries, and sends exact approve/reject decisions. The approval surface never
receives raw operations or parameter values and there is no public Apply
command. The first real test exposed binary Runtime frames being missed by
Qt's text-only signal; `423a0e4` makes new servers send text frames and keeps
binary compatibility in the panel. The focused hotfix gate passes 172 tests
and the full suite passes 2104 tests with the same single optional WSL skip.
Populated Run and approval states were also rendered with Houdini 21.0.440's
bundled PySide6 at a narrow dock size. Real Houdini
Run/reconnect/stop/empty-approval acceptance is the remaining Task 17-B gate.

### 17-A: Read-only selection inspector (earliest Houdini UI test)

- Keep the panel a client only: it must not import the agent graph, open SQLite, or own Runtime/checkpoint lifetimes.
- Implement the dockable shell, Runtime connection/discovery, reconnect with `last_seq`, snapshot fallback, and a selection inspector that requests the typed `scene.query` DTO and displays selected node paths, node types, scene epoch, and read-only geometry facts.
- The first manual UI test is: select a node in Houdini, refresh or receive a selection update, and verify the panel shows the same path/type without changing the scene or writing a file.
- Preserve the existing `chat_panel.py` path as a rollback path until this read-only slice is accepted.

### 17-B: Interactive approval and run UI

- Implemented: Session creation/selection, Run start/stop/force-stop,
  snapshot/event recovery, bounded output/activity, ChangeSet queue/risk
  preview, approve/reject, and receipt/critical-recovery evidence.
- The panel has no Apply button and cannot serialize raw operations. Task 18
  remains responsible for creating trusted typed proposals and invoking the
  existing in-process Apply path.

**Acceptance gate for 17-A:** Houdini UI smoke, reconnect without loss or duplicate, selection path/type parity, scene-epoch display, and no mutation from a read-only inspection session.

**Acceptance gate for 17-B:** real Runtime Run/restart/stop recovery,
empty-approval state before Task 18, Scene inspector regression, and no scene or
repository mutation. Approval/stale/receipt branches are covered offline until
Task 18 can produce trusted proposals.

## Task 18: Strict modeling capability

**Dependency:** Tasks 15–17.

- Define Brief/Spec/Component schemas, compiler output, staging, reconciler, deterministic validators, repair tickets, repair budget, QualityProfile, and Golden Cases.
- Keep compiler output typed and bounded; Wrangle/Python SOP source must be hashed, statically checked, cooked, and parameter-sampled.
- Require structure, cook, geometry, parameter sensitivity, semantic, and artifact validators before delivery; visual review cannot override deterministic failures.

**Acceptance gate:** offline schema/compiler/validator tests plus hython graph/cook/parameter integration tests; two-repair maximum is enforced and a failed reconcile cannot be hidden by updating only the spec.

## Task 19: Capture, vision, and evaluation

**Dependency:** Task 18 artifacts and validation contracts.

- Add deterministic capture/framing preflight and content-addressed artifact metadata.
- Add Vision Router, explicit per-Run skip/waiver records, artifact viewer references, and redacted raw-response storage.
- Add OTel/Phoenix integration only as an outer adapter; LangSmith export remains opt-in and sanitized.

**Acceptance gate:** artifact hash/retention tests, capture failure/waiver tests, vision unavailable/skip tests, redaction tests, and end-to-end quality-gate evidence. No external observability service may be required for local Runtime correctness.

## Milestone order and promotion rule

`Task 14 delivery gate → Task 15 Bridge read-only slice → Task 17-A selection inspector → Task 16 ChangeSet/Policy → Task 17-B approval UI → Task 18 Modeling → Task 19 Capture/Vision/Eval`.

Each task receives its own approved file list and Claude prompt. Codex promotes a task only after independent verification and a clean focused commit. Deferred Runtime commands remain structured capability errors until their task is accepted; no speculative schema or compatibility shim is added early.
