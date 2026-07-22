# Runtime Release Acceptance Design

Date: 2026-07-22
Stage: B
Dependency: accepted Stage A

## Goal

Prove the Runtime, Secure Bridge, three-pane Houdini panel, approval/Apply
journey, and advisory Vision path in their real production environments. The
stage produces complete release-candidate evidence but does not create or push
an RC tag automatically.

## Non-goals

- Replacing deterministic validators with visual judgment.
- Giving the model direct Bridge, filesystem, shell, HOM, or Apply authority.
- Persisting screenshots, secrets, prompts, or raw provider responses in Git.
- Treating an offline fake-provider test as real-provider acceptance.
- Redesigning UI during acceptance without a separately triaged defect.

## Preconditions

- Stage A is accepted with zero unexpected warnings.
- `E:/eee-agent` is clean on the accepted local `main`.
- `E:/eee-agent/.worktrees/runtime` is clean on the B branch.
- The Houdini package still resolves the fixed Runtime worktree path.
- The worktree has a matching `uv.lock`, `.venv`, and ignored `.env`.
- Any visible Houdini scene is checked for unsaved work before restart or
  process termination.

## Acceptance layers

### Layer 1: offline and protocol regression

Run the complete offline suite and the focused Runtime, panel, provider,
ChangeSet, artifact, and Vision suites. This proves deterministic contracts
before a slower external system is involved.

### Layer 2: disposable Houdini automation

Use Houdini 21.0.440 and a fresh temporary scene for:

- HFS/Knowledge contracts;
- authenticated Secure Bridge connectivity and bounded DTOs;
- Workspace bootstrap, bind, inspect, and switch;
- ChangeSet preflight, exact approval binding, transactional Apply, receipt,
  rollback, and restart recovery;
- parameter-sensitivity sample and exact restoration;
- deterministic capture and ArtifactStore byte registration.

Tests must clean up temporary nodes and processes, avoid saved HIP output, and
keep HOM inside Houdini/hython. A failed restoration or uncertain write fails
closed and remains visible.

### Layer 3: interactive Houdini panel

The GUI checklist runs against the exact accepted B branch loaded through the
installed package.

#### New Session and visual language

- SessionTitleDialog uses the accepted graphite/iron/cyan/amber language.
- Modal focus, default button, Escape, mouse interaction, and Windows Chinese
  IME behave correctly.
- Auto-created placeholder Sessions and manually titled Sessions coexist
  without duplicate or stale sidebar entries.

#### Three-pane layout

- Wide, medium, narrow, docked, and floating layouts remain readable.
- Sidebar, conversation, Inspector, and approval drawer preserve hierarchy.
- Approval amber remains reserved for authorization, while errors use semantic
  error treatment.

#### Conversation journey

- first prompt without a Session auto-creates and later auto-titles it;
- assistant and thinking streams update in place;
- history survives Session switches and reconnects;
- Completed, Cancelled, and Failed Runs each show an explicit terminal result;
- Chinese IME confirmation never accidentally submits a Run;
- stop and force-stop controls converge to the correct state.

#### Structured Inspector

The Run tab shows structured status, timing, environment, dependencies,
Activity, Apply outcome, TodoList, and failure evidence. Workspace and Artifact
surfaces remain tabular/structured. No raw dictionary or `key:value` wall is
accepted as the primary presentation.

#### Approval and Apply

The user completes MODEL -> REVIEW -> APPROVE -> APPLY -> RESULT through the
panel. Rejection, expiry, stale scene, rollback, and recovery outcomes are
visible and cannot silently imply success.

#### Runtime lifecycle

Verify automatic backend start, stale discovery rejection, reconnect, Runtime
restart, panel close/reopen, and spawned-process reap behavior. A panel must
not kill a Runtime it did not spawn.

### Layer 4: real Vision provider

Production currently injects no Vision provider. Stage B implements a
provider-neutral `VisionProvider` adapter selected through the existing
provider registry only when explicit Vision configuration is present. With no
Vision configuration, production retains the current unavailable path. Provider
SDKs and secrets never enter Houdini-side modules.

The real journey uses exactly the ArtifactStore bytes registered after
deterministic capture. The response is normalized through existing strict
Vision contracts. Raw provider content remains redacted/local; only bounded
normalized evidence becomes durable and user-visible.

Provider failure is advisory:

- it records an explicit unavailable/failed result;
- it does not undo or fail an already durable Apply;
- it cannot turn deterministic failure into acceptance;
- it cannot create an unverified Artifact reference.

The distinction between `VisionStatus.FAILED` and `UNAVAILABLE` must be decided
from actual provider behavior. If there is no stable product distinction, the
unused state is removed rather than retained speculatively.

## Evidence handling

Committed evidence contains commands, versions, counts, normalized statuses,
and redacted identifiers only. Screenshots, provider payloads, credentials,
local paths containing sensitive user data, and unsanitized prompts stay in a
machine-local acceptance directory.

Every manual checklist item records pass, fail, or not-run. A not-run item
cannot be summarized as passed.

## Failure and discovery handling

A real-Houdini or provider failure begins with reproduction and boundary
tracing. It is not patched by guessing. Findings that invalidate the stage are
added to the B plan with evidence. A UI improvement request that does not block
the agreed checklist is recorded for a later product slice.

Repeated external license or gateway failures are reported as external gates.
They do not justify weakening authentication, using fake evidence, or changing
the required provider without approval.

## Release-candidate readiness

Stage B is accepted only when:

- offline and static gates pass freshly;
- disposable Houdini checks pass;
- every interactive GUI item is passed or explicitly rejected by the user;
- the real Vision provider journey passes with normalized evidence;
- the exact code under test is committed and the worktree is clean;
- a final code and evidence review finds no unresolved blocker/high finding.

Tag creation and remote publication are separate authorized actions after this
readiness decision.
