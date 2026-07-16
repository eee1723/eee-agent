# Autonomous Modeling Development Roadmap - 2026-07-17

## Objective

Continue from Task 18-C without requiring interactive Houdini verification.
Use offline tests and disposable Houdini 21.0.440 `hython` processes as the
primary acceptance evidence. Defer only visual/docking/IME checks that truly
require the user to operate Houdini GUI.

The product target is conversation-first modeling. Workspace IDs, manifest
revisions, SceneBinding details, raw operations, and recovery records remain
available in an advanced Inspector but are not normal user workflow.

## Non-negotiable boundaries

- The model never receives `create_node`, `connect_nodes`, `set_parms`, raw
  Apply, database, Bridge, filesystem, shell, or source-execution authority.
- Model output is strict Brief/Spec data. A developer-owned compiler emits the
  only executable typed ChangeSet.
- Every write requires an exact persisted approval bound to ChangeSet digest,
  Houdini instance, and scene epoch.
- Approve may trigger an internal trusted Apply, but no public raw Apply
  command or panel operation payload is introduced.
- Houdini writes stay on the accepted single FIFO/main-thread transaction.
- Stale facts, partial writes, unavailable Bridge, validator failures, and
  repair exhaustion fail closed and retain evidence.
- Existing Task 16 recovery/idempotency and Task 17 reconnect/IME behavior may
  not be weakened.

## Autonomous acceptance levels

### Level A - offline

- strict DTO/parser/compiler/policy/repository/service tests;
- fake-Bridge transaction, stale, rollback, restart, idempotency, and event
  ordering tests;
- fake-agent and Runtime protocol tests;
- full `pytest -q`, lock, compileall, and diff checks.

### Level B - disposable hython

- fresh unsaved Houdini scene per smoke;
- exact 21.0.440 type/default introspection before catalog expansion;
- graph create/wire/parm/cook/geometry/rollback/reconcile checks;
- no saved HIP, no external file writes, and cleanup in `finally`;
- Runtime stays in standard Python while HOM stays inside hython/Bridge.

### Level C - deferred real GUI

- dock layout, responsive drawers, focus, Chinese IME, visual hierarchy;
- mouse approval flow and screenshot/artifact viewer parity;
- final user-journey acceptance.

Level C does not block independent implementation of Levels A and B.

## Work sequence

### Task 18-D - Empty-scene Workspace bootstrap

Goal: compile the first modeling request when no Workspace exists.

- Add a frozen `WorkspaceBootstrapContext` carrying trusted Session/Run,
  SceneBinding, generated Workspace ID, allowed parent `/obj`, root name, and
  catalog/profile facts.
- Extend compilation with a separate bootstrap entry point. It creates one
  owned `geo` root followed by the requested SOP graph in a single typed
  ChangeSet.
- Use `ProjectChange` only for the initial external-parent create; all created
  nodes carry the same new Workspace ID and `created_by_run` mirror.
- Top-level ChangeSet `workspace_id` remains `None` until the first transaction
  succeeds. No provisional manifest is persisted before receipt success.
- After a successful receipt, derive exact created node facts and atomically
  create/activate the WorkspaceManifest. Failure leaves no active Workspace.
- Add offline compiler/policy/service/repository tests and a fresh-scene hython
  bootstrap smoke.

### Task 18-E - Approval-to-Apply orchestration

Goal: one user Approve action performs the existing trusted Apply flow.

- Keep `changeset.approve` as the bounded public decision command.
- After approval commits and notifications are durable, start/join the
  existing in-process `apply_changeset_trusted()` task.
- Return only a bounded decision/apply summary; stream durable Applying,
  Applied, RolledBack, or CriticalRecovery events to the panel.
- Rejection never calls Apply. Expired/stale approval never calls Apply.
- Client disconnect/cancellation does not cancel an accepted Apply.
- Restart recovery queries receipt/facts and never replays an uncertain write.
- Bootstrap Apply finalizes the Workspace; normal Apply refreshes Workspace
  facts only after successful reconciliation.

### Task 18-F - Deterministic validation and repair budget

Goal: prove delivered geometry instead of trusting a successful tool call.

- Implement validators in locked order: SpecContract, Graph, Cook, Geometry,
  ParameterSensitivity, Semantic, Artifact.
- Store a bounded immutable ValidationReport with evidence digests.
- A failed hard validator cannot be overridden by vision or natural language.
- Generate strict RepairTicket values from validator evidence.
- Permit at most two repair attempts per stage. Each repair is a new typed
  ChangeSet and approval decision; updating only the Spec cannot hide failure.
- Add replay/restart tests and deterministic Golden Case fixtures.

### Task 18-G - Catalog expansion and Golden Cases

Goal: support useful procedural models while keeping execution auditable.

- Expand in small verified batches, starting with safe SOPs needed for
  box/transform/merge/null, then primitive/extrude/copy/sweep/boolean groups.
- Verify exact Houdini type name, category, parameter tuple shape, default,
  input count, output count, and cook behavior with hython before cataloging.
- Keep file/source/expression/Python/VEX parameters absent until a separately
  versioned source policy is accepted.
- Golden Cases cover simple prop, repeated assembly, parameter sensitivity,
  failed cook, stale scene, rollback, and deterministic recompile.

### Task 18-H - Product UI convergence

Goal: replace the current control-plane workflow with a normal product flow.

- Main surface: conversation composer, Run activity, preview, result.
- Context bar: HIP, Session, Workspace health, Runtime/Bridge, Run state.
- Approval appears as a drawer only when required.
- Workspace create/bind ID/revision fields move to Advanced Inspector.
- Empty scene bootstraps automatically from the first approved request.
- Approve triggers internal Apply; there is no second Apply button.
- Scene, Workspace, receipt, raw IDs, and recovery controls remain available
  for diagnostics.
- Implement state reducers and nonvisual tests independently; defer only final
  Houdini GUI acceptance.

### Task 19-A - Capture and artifact foundation

- Content-addressed artifact metadata and bounded retention.
- Deterministic framing preflight with at most two adjustments.
- Screenshot bytes shown to the user must hash-identically match bytes sent to
  a vision model.
- Capture failure is explicit and cannot masquerade as visual success.

### Task 19-B - Vision routing and evaluation

- Explicit Vision Router and provider capability resolution.
- User-visible skip/waiver records when vision is unavailable.
- Parsed reports are schema-validated; raw provider responses are redacted and
  stored only as artifacts.
- Vision cannot override deterministic validator failures.

### Task 19-C - Delivery and observability

- Final DecisionSummary, ValidationReport, ChangeReceipt, artifacts, parameter
  guide, and recovery evidence.
- Phoenix/LangSmith remain optional outer adapters; local correctness never
  depends on either service.

## Overnight priority and stopping rule

1. Commit this roadmap and executable Task 18-D/18-E plans.
2. Implement Task 18-D pure contracts/compiler/policy tests.
3. Implement bootstrap persistence/orchestration and fake-Bridge tests.
4. Run disposable hython bootstrap smoke.
5. If green, implement Task 18-E approval-to-Apply orchestration.
6. If time remains, begin Task 18-F contracts/validator core; do not start UI
   reshaping before bootstrap/Apply are stable.

Continue autonomously while a safe, bounded next step exists. Stop only for:

- a repeated external license/provider failure that prevents all meaningful
  progress;
- a required product choice that changes permission or destructive behavior;
- a real GUI-only acceptance item after all offline/hython work is complete.

Every completed slice receives its own focused commit, review result, handoff,
and exact measured test evidence. Do not push or merge.

