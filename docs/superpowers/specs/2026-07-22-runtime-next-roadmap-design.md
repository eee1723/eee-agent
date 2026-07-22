# Runtime Next Roadmap Design

Date: 2026-07-22
Baseline: `origin/main` at `5a6880b`
Delivery order: A (stability) -> B (release acceptance) -> C (Task 19-C)

## Purpose

This document defines how the next three delivery stages are sequenced, how
worktrees are governed, how Claude Code delegates implementation to GLM 5.2,
and how newly discovered defects or architectural weaknesses enter the work.
The three stages remain separate specifications because they have different
acceptance evidence and must be independently reviewable and reversible.

The project must not optimize for completing a stale checklist. The governing
objective is a trustworthy Houdini product: evidence found in current code,
tests, real provider runs, or the Houdini UI outranks assumptions written in an
older plan.

## Confirmed baseline

The UI work completed on 2026-07-21 is present on `main` and on
`origin/feature/ui-tech-redesign`, which point to the same commit. The merged
work includes:

- the SessionTitleDialog-derived graphite/iron/cyan/amber visual language;
- conversation history replay and streamed assistant/thinking cards;
- structured Run, Workspace, Artifact, and Vision inspector surfaces;
- Apply error propagation into receipts and panel state;
- Deep Agents Todo event capture, persistence, and TodoList rendering;
- compatibility with Runtime snapshots that predate the `todos` field.

No uncommitted UI source exists in the registered worktrees. The local ZCode
plan describes the commits already reachable from `main`; the two Kimi debug
archives contain diagnostic manifests/state rather than source or visual
assets.

The current baseline also has three verified defects or gaps:

1. Ruff rejects an unused `pytest` import in the new history behavior test.
2. A Todo repository test opens an aiosqlite database without closing it,
   leaving a worker thread to call a closed event loop.
3. A failed Run carries `failure_json.message_for_user` through Runtime and
   panel state, but the conversation renders an empty Assistant card when no
   model text was produced.

## Workspace topology

The steady-state topology contains exactly two registered worktrees:

```text
E:/eee-agent
  branch: main
  purpose: stable integration baseline; no direct feature development

E:/eee-agent/.worktrees/runtime
  branch: one active stage branch
  purpose: Claude/GLM implementation, Houdini package target, stage validation
```

The physical `.worktrees/runtime` path remains stable because the installed
Houdini package currently resolves `EEE_PATH` to it. The branch inside that
worktree changes sequentially:

```text
feature/a-stability
feature/b-release-acceptance
feature/c-task19c-delivery
```

Only one stage branch is active at a time. A later stage starts from the locally
accepted result of the previous stage. Work does not run in parallel across
these branches because the Runtime, panel, migrations, package deployment, and
Houdini process are shared state.

## Lossless workspace cleanup

Before implementation, local historical material is preserved outside the
repository:

```text
E:/eee-agent-local-archive/2026-07-22/
  pre-migration-main.patch
  local-zcode-plan.md
  legacy-uv.lock
  kimi-debug-session_-20260720-064438.zip
  kimi-debug-session_-20260720-080203.zip
```

Commit `bc351c0` receives the local tag
`archive/pre-migration-main-2026-07-22` before the obsolete wip branch is
removed. The root worktree then fast-forwards `main` to `origin/main`. The
clean, temporary `main-review-20260722` worktree is removed after its design and
plan commits are reviewed and local `main` fast-forwards through those accepted
documentation commits. The local `feature/runtime` branch is removable only
after the fixed Runtime worktree has switched to the A branch and Git proves
that `feature/runtime` is an ancestor of `main`.

Remote branch deletion, pushing, pull requests, and release tags are outside
this cleanup. They require a later explicit decision.

The ignored `.env` and `.venv` in the Runtime worktree are retained. Their
contents are never copied into documentation, model prompts, logs, or commits.

## Stage boundaries

### Stage A: stability and failure visibility

Stage A restores a warning-free quality baseline, makes every failed Run
visible to the user, and adds regression contracts for the Runtime-to-widget
chain. It does not redesign the panel or change trusted write boundaries.

Accepted Stage A becomes the input to Stage B.

### Stage B: release acceptance

Stage B proves the accepted product in Houdini 21.0.440 and through a real
Vision provider. It combines automated disposable-hython evidence with an
explicit interactive GUI checklist. It does not weaken validation or create a
release tag automatically.

Accepted Stage B makes the codebase release-candidate-ready. Creating or
pushing an RC tag remains a user decision.

### Stage C: delivery and observability

Stage C implements the final Task 19-C delivery package and optional
observability adapters. Local correctness and user-visible delivery evidence
must remain available when Phoenix and LangSmith are absent or unavailable.

## Continuous discovery and triage

Every implementation and validation task includes a discovery pass. Findings
are recorded with the following fields before any scope decision:

| Field | Meaning |
| --- | --- |
| Evidence | Reproduction, failing command, source path, event trace, or GUI observation |
| Impact | User harm, data/scene risk, security boundary, release risk, or maintainability cost |
| Severity | Blocker, high, medium, or low |
| Root-cause status | Confirmed, single hypothesis under test, or unknown |
| Stage relationship | Blocks current stage, adjacent to current work, or independent |
| Proposed disposition | Fix now, add to a later scoped spec, request architecture decision, or document only |
| Verification | Test or observation that will prove resolution |

Disposition rules:

- A blocker or high-severity defect that invalidates current acceptance is
  added to the current stage after root-cause investigation.
- An adjacent defect may be included when the same component and verification
  boundary are already in scope and the change stays independently testable.
- An independent feature or refactor is added to the relevant later spec, not
  bundled into the current patch.
- A security, authorization, persistence, recovery, or Houdini scene-integrity
  weakness pauses implementation until its architecture is reviewed.
- Three failed fix hypotheses trigger an architecture discussion instead of a
  fourth patch attempt.
- A passing test never overrides contradictory real-Houdini or provider
  evidence. The contradiction becomes a finding and the gate remains open.

The active implementation plan gains a clearly labeled discovered-work item
only after this triage. Original requirements remain traceable; discovered
work is not silently rewritten into them.

## Claude Code and GLM 5.2 execution contract

Codex owns specifications, plans, task prompts, diff review, verification,
commits, branch transitions, and user reporting. Claude Code invokes GLM 5.2
only for the current bounded implementation task.

The target invocation uses headless JSON mode and the exact gateway model name
`glm-5.2[1m]`. Before the first edit, a long-timeout no-tool probe must return a
successful result and a session id. GLM 5.2 is known to respond slowly, so task
timeouts are measured in minutes rather than the previous 120-second probe.

If the gateway times out or returns a transient gateway error:

- keep the same task and working tree unchanged;
- wait and retry the same model with a longer bounded timeout;
- do not substitute the locally configured `k3` model;
- do not start duplicate workers against the same files;
- report a repeated external failure without pretending implementation ran.

Each implementation prompt specifies exact file scope, locked boundaries, the
required failing test, RED and GREEN commands, and forbidden operations. GLM
may read, edit, and run allowlisted tests. It may not push, merge, rebase,
create tags, edit secrets, remove worktrees, or commit. Codex reviews the actual
diff and reruns verification independently before creating a focused local
commit.

Review findings are returned through the same Claude session when possible.
One task must reach acceptance or be explicitly reverted before another task
edits overlapping files.

## Verification cadence

Every task runs its focused RED/GREEN tests. Each accepted commit runs its
affected subsystem suite. Each stage closes with:

```text
uv run --frozen --extra eval pytest -q
uv run --frozen --group dev ruff check eee_agent/runtime eee_agent/vision houdini_side tests/panel
uv run --frozen --group dev mypy --follow-imports=skip --ignore-missing-imports \
  eee_agent/runtime/agent_context.py \
  eee_agent/runtime/knowledge.py \
  eee_agent/runtime/agent_tools.py \
  eee_agent/vision
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Houdini and provider checks remain explicit additional gates; an offline suite
cannot claim they passed.

## Documentation and decision records

The roadmap is implemented through three scoped specifications:

- `2026-07-22-runtime-stability-design.md`
- `2026-07-22-runtime-release-acceptance-design.md`
- `2026-07-22-task19c-delivery-observability-design.md`

Each specification receives its own implementation plan. Discovered work is
added to the appropriate document with evidence and scope rationale before it
is implemented. A live findings register at
`docs/superpowers/reviews/2026-07-22-runtime-next-findings.md` records the
evidence, severity, root-cause status, disposition, owner stage, and resolution
commit for every new issue. Acceptance evidence records exact commands, counts,
skipped gates, and remaining risks.
