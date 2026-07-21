# Task 18-B Modeling Proposal Seam Design

- Date: 2026-07-16
- Scope: Pure proposal orchestration and bounded tool adapter
- Status: Approved for local implementation

## Goal

Provide one narrow seam from validated model intent to the already accepted
trusted `ChangeSetService.propose()` call. The seam may compile and persist a
proposal, but it cannot approve, Apply, inspect SQLite directly, call Houdini,
or return raw operations/parameter values to the model.

## Inputs and authority

The seam receives a trusted `ModelingProposalContext` containing:

- exact Session and Run IDs;
- the current trusted WorkspaceManifest;
- the current SceneBinding;
- a developer-owned NodeCatalog;
- the QualityProfile;
- an async callback to the Runtime trusted proposal method; and
- an injected UTC clock and ChangeSet ID factory.

The model supplies only strict JSON object values for ModelingBrief and
ProceduralSpec. The context supplies all identity, scene, workspace, catalog,
permission, operation, and approval facts.

## Flow

1. Parse exact Brief/Spec objects with the Task 18-A strict contracts.
2. Verify Session/Run IDs and Spec brief/profile bindings.
3. Compile with `compile_procedural_spec()`.
4. Call the trusted proposal callback with the compiled ChangeSet and the
   compiler's already-verified PolicyDecision.
5. Return a bounded `ModelingProposalSummary` containing only:
   `change_id`, digest, workspace ID, operation count, effect names, affected
   path count, approval-required flag, and `AwaitingApproval` state.

The callback is invoked exactly once per successful proposal. A callback
failure does not cause a second proposal or Apply attempt.

## Tool adapter

`propose_modeling` is a LangChain tool only at the adapter boundary. Its
signature contains `brief`, `spec`, and injected `ToolRuntime`; the latter
provides the trusted `ModelingToolContext`. Missing or malformed context fails
closed with a bounded error object. The tool never accepts a ChangeSet,
operation, path, expected-old value, permission mode, approval decision, or
Apply request.

The existing Runtime AgentRunner and exact read-only seven-tool registry remain
unchanged in this slice. Runtime graph injection is a later integration slice
that requires its own context schema, Workspace/SceneBinding lifecycle, and
real pending-approval test.

## Errors

All expected failures are `ModelingProposalError` codes:

- `modeling.proposal_context_invalid`;
- `modeling.proposal_input_invalid`;
- `modeling.proposal_compile_failed`; and
- `modeling.proposal_persist_failed`.

Messages are bounded and contain no raw model payload, traceback, token, HOM
object, or filesystem content. The adapter returns only a bounded
`{"ok": false, "code": ...}` object to the model.

## Acceptance

- strict input and context identity tests;
- exact-once trusted callback test;
- bounded summary projection test;
- compiler/policy denial propagation tests;
- adapter missing-context/malformed-input tests; and
- import scan proving no Runtime service, database, Houdini, rpyc, filesystem,
  shell, or dynamic execution enters the pure proposal coordinator.

No live Runtime/Houdini test is claimed until the later graph-injection slice
has a production catalog and active Workspace path.
