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

**Status:** Planned. No new production-code scope is approved until this gate is completed or a narrowly scoped defect is isolated.

**Owner split:** Codex/user performs the manual smoke and delivery actions. Claude Code may implement only a follow-up fix prompt naming the exact files and regression tests supplied by Codex.

**Inputs:** Accepted Runtime tips `724a8fb`, `3b69532`, `c8e6af1`, Codex status commit, and the Runtime plan's manual acceptance procedures.

### 14.1 Push and clean restore

- Push the five local commits currently ahead of `origin/feature/runtime` on `feature/runtime`; never force-push and never merge to `main` in this gate.
- On the target computer, clone/fetch `origin/feature/runtime`, rebuild Python 3.11 dependencies with `uv sync --frozen --extra eval --python 3.11`, and do not transfer `.venv`, SQLite files, tokens, discovery files, logs, or `.env`.
- Verify `uv lock --check`, `uv run --extra eval pytest -q`, `uv run python -m compileall -q eee_agent tests`, `git diff --check`, and a clean worktree. The expected current baseline is at least 1111 passed with no failures; machine-dependent environment probes may be skipped only if no new skip is introduced.

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
- If a check fails, create exactly one follow-up defect task with: RED command, exact failure, authorized files, regression test, and acceptance commands. Do not begin Task 15 until that fix is accepted.

## Task 15: Secure HoudiniBridge contract and transport

**Dependency:** Task 14 accepted, or an explicit decision to proceed with manual smoke pending.

**Purpose:** Replace the legacy unrestricted bridge path with a typed, loopback-only, main-thread-aware read/write boundary. This task is specification-first because the Runtime spec explicitly deferred it.

**First deliverable:** Codex-approved design addendum covering DTO schemas, bridge token separation, request IDs, capability names, scene epoch, queue semantics, timeout/cancellation, error taxonomy, and rollback behavior. No implementation starts before that addendum is reviewed.

**Implementation slices (each a separate Claude prompt/commit):**

1. Restricted DTOs and compatibility parser: typed `scene.query`, `workspace.inspect`, `change_set.preview`, `validate`, `capture`, and `cancel`; reject unknown fields, stale scene epoch, wrong token, and oversized payloads.
2. Houdini main-thread queue: bounded request queue, deterministic request completion, cancellation boundaries, and no direct background-thread `hou` calls.
3. SceneBinding and WorkspaceManifest: scene epoch increments on load/clear, owned-root identity is mirrored in userData, and external nodes are read-only by policy.
4. Bridge lifecycle and recovery: separate Bridge token, authenticated loopback transport, bounded shutdown, stale-request cleanup, and process-restart diagnostics.

**Acceptance gate:** pure Python contract tests, hython/Houdini integration tests, unauthorized/wrong-epoch tests, cancellation and queue-order tests, and proof that legacy unrestricted entry points are not used by Runtime by default.

## Task 16: Policy, typed ChangeSet, approval, and transactional execution

**Dependency:** Task 15 DTOs, SceneBinding, and WorkspaceManifest accepted.

- Add immutable `ChangeSet`, precondition, approval, receipt, and validation contracts in a new milestone-specific spec.
- Implement policy modes `OwnedWorkspace`, `ScopedPatch`, and `ProjectChange`; default-deny external nodes and all untyped effects.
- Implement deterministic preview → approval → precondition re-read → apply → reconcile → receipt flow. Stale name/type/ownership/revision/scene-epoch data must fail closed.
- Make repeated receipt application idempotent and ensure partial Houdini transactions report recovery state rather than claiming success.

**Acceptance gate:** duplicate-apply, stale-change, denied-permission, scene-change, rollback, and process-restart tests; no Runtime event may report durable success before reconciliation facts are persisted.

## Task 17: Docked `.pypanel` client

**Dependency:** Runtime protocol plus Tasks 15–16 stable enough to expose session/workspace/run state.

- Keep the panel a client only: it must not import the agent graph, open SQLite, or own Runtime/checkpoint lifetimes.
- Implement reconnect with `last_seq`, snapshot fallback, session/run inspectors, active-run close choices (continue/stop/cancel), approval display, and actionable structured errors.
- Preserve the existing panel as a rollback path until the new client reaches parity for the accepted Runtime commands.

**Acceptance gate:** Houdini UI smoke, disconnect/reconnect without loss or duplicate, Runtime restart recovery, approval stale-state display, and no mutation from a read-only inspection session.

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

`Task 14 delivery gate → Task 15 Bridge → Task 16 ChangeSet/Policy → Task 17 Panel → Task 18 Modeling → Task 19 Capture/Vision/Eval`.

Each task receives its own approved file list and Claude prompt. Codex promotes a task only after independent verification and a clean focused commit. Deferred Runtime commands remain structured capability errors until their task is accepted; no speculative schema or compatibility shim is added early.
