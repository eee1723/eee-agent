# EEE Agent Foundation Migration Handoff

## Purpose

This document is the repository-owned context for moving development to another
computer. Chat history is useful but is not the source of truth. Resume from the
approved architecture specification, the implementation plan, this handoff, and
the Git history listed below.

Generated: 2026-07-13 (Asia/Shanghai)

## Repository State

- Repository: `https://github.com/eee1723/eee-agent.git`
- Main workspace: `E:\eee-agent`
- Foundation worktree: `E:\eee-agent\.worktrees\foundation`
- Foundation branch: `feature/foundation`
- Foundation implementation HEAD before this handoff commit:
  `76b1af9 test: align provider contract specification`
- Foundation worktree status at handoff preparation: clean
- Main branch HEAD: `357bb0b chore: ignore local worktrees`
- Main is two commits ahead of `origin/main`.

Migration push completed and was verified from the old computer:

- `origin/main` -> `357bb0be005ffeb1118a5601811ce6b693a3aa52`
- `origin/feature/foundation` initially included this handoff at `fb32319`; read
  the current remote branch tip for the later migration-status update commit.
- `origin/wip/pre-migration-main` ->
  `bc351c00ada6a0e15ff4ffdc41e2f19ebcfc4fe5`
- Both the main/WIP workspace and Foundation worktree were clean after pushing.

## Sources Of Truth

Read these files in order before changing code:

1. `docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`
2. `docs/superpowers/plans/2026-07-13-foundation.md`
3. `docs/handoffs/2026-07-13-foundation-migration.md`

The approved architecture specification is the product-level source of truth.
The Foundation plan deliberately implements only the dependency, core-contract,
provider, event-normalization, and explicit Deep Agents harness foundations.

## Product Direction And Approved Decisions

- Build a general Houdini agent on LangChain Deep Agents.
- Strict procedural and parametric modeling is the first capability middleware,
  not the final boundary of the product.
- Later capabilities must plug into a stable general runtime instead of growing
  a modeling-specific monolith.
- The agent may read the whole HIP. Write access must later flow through a typed,
  restricted Houdini bridge and explicit ChangeSets.
- A task that is not a modeling task is treated as a general task. Workspaces are
  capability scopes over the HIP, not isolated replacement HIP files.
- The UI must clearly display the active workspace, multiple sessions, runtime
  events, reasoning output, tools, artifacts, approvals, and task status.
- Closing the UI while a task is active must prompt whether to abort the task.
- Local tracing remains the primary development path. LangSmith is an optional
  export/integration path, not a replacement for the domain event store.
- Capture must use clear, readable, non-misleading views, avoid viewport grids and
  distracting overlays, and avoid angles that can mislead a vision model.
- A dedicated vision model can be configured when the primary model is not
  multimodal. Routing must select it only when needed.
- If neither the primary model nor a configured vision model can inspect images,
  ask whether to skip visual verification. Skipping must not block modeling.
- Captured images, the vision prompt, and the model's visual interpretation must
  be artifacts that can be opened quickly from the UI.
- Reasoning and visible text are separate runtime event types. Provider-specific
  thinking blocks must survive tool-call replay when required by the provider.
- Deep Agents' implicit `general-purpose` subagent and `task` tool are disabled in
  Foundation until a restricted general capability is designed explicitly.

## Foundation Progress

### Completed And Approved

Task 1, reproducible dependencies:

- Commit: `6acc3ff build: lock foundation dependencies`
- Exact Python 3.11 application dependencies, pytest extra, and `uv.lock`.
- Dependency tests passed 10/10 and `uv lock --check` passed.

Task 2, environment probe:

- Commits: `cd5bc03`, `7df939d`, `de9784a`, `fdd0589`, `9e0afa4`.
- Tracked read-only/non-fatal WSL and Git Bash probe.
- Correct `/mnt/d/houdini` before `/d/houdini` discovery.
- WSL-to-Windows Python environment handling, status-only dotenv parsing,
  secret-safe `bash -x`, no bytecode writes, and interop-aware tests.
- Final Task 2 suite passed 12/12.

Task 3, typed IDs:

- Commit: `e9d5468 feat: add typed foundation ids`
- Session, Run, Event, Workspace, Change, and Artifact ID contracts.
- Specification and code-quality reviews approved.

Task 4, structured errors and artifact references:

- Commits: `c981a81`, `847ff73`, `5c0ba4a`, `2855b77`.
- Deeply immutable, JSON-safe structured errors.
- Canonical POSIX artifact references safe for Windows/Houdini materialization.
- Windows drives, traversal, ADS, invalid characters, control characters,
  reserved device aliases, trailing dots/spaces, bad MIME/schema/size, and invalid
  SHA-256 values are rejected.
- Final Task 4 suite passed 184/184. Full suite then passed 209/209.

Task 5, durable domain events:

- Commits: `176eb86`, `a86835d`, `35e2cd5`.
- Deep immutable JSON snapshots and fresh `to_dict()` output.
- Strict JSON types, finite floats, cycle detection, canonical UTC timestamps,
  strict event types and schema v1, and exact five-field `from_dict()` replay.
- Final event suite passed 83/83, core passed 270/270, full passed 292/292.
- Specification and code-quality reviews approved.

### Implemented, Review Incomplete

Task 6, provider contracts and registry:

- Commits:
  - `ddb8c62 feat: add provider contracts and registry`
  - `5223136 fix: align provider contracts with specification`
  - `76b1af9 test: align provider contract specification`
- Production contracts, registry, environment secret resolver, and package exports
  are implemented.
- The first specification review found nullable annotation, exact-message, and
  exact-test-block differences. `5223136` fixed production differences.
- The second specification review found only literal test-block differences.
  `76b1af9` replaced the test block as requested.
- A final specification re-review was started but its agent connection failed
  before returning a verdict.
- Code-quality review for Task 6 has not started.
- Last full suite before the literal test-only alignment: 296 passed.
- Post-`76b1af9` focused provider contract suite: 4 passed.

Resume Task 6 by doing these steps in order:

1. Re-run independent specification review over `35e2cd5..76b1af9`.
2. Fix and re-review any specification issue before proceeding.
3. Run an independent code-quality review over the approved cumulative range.
4. Return quality findings to the Task 6 implementation worker and re-review.
5. Only after both reviews approve, mark Task 6 complete and begin Task 7.

### Not Started

- Task 7: DeepSeek V4 Anthropic adapter and no-network thinking/tool replay payload.
- Task 8: Anthropic/OpenAI adapters and provider-neutral model factory.
- Task 9: normalized provider events and legacy Panel JSON-lines compatibility.
- Task 10: explicit Deep Agents harness without implicit general-purpose/task.
- Task 11: runtime dependency version report and CLI command.
- Task 12: configuration documentation and final Foundation verification.

Runtime, SQLite, WebSocket, Restricted HoudiniBridge, docked UI, strict modeling
validators, capture/vision, Phoenix/LangSmith integration changes, and evaluation
remain later milestones by design.

## Important Provider Research Already Verified

- DeepSeek V4's Anthropic-compatible endpoint is
  `https://api.deepseek.com/anthropic`.
- Exact intended models are `deepseek-v4-pro` and `deepseek-v4-flash`.
- Reject deprecated aliases `deepseek-chat` and `deepseek-reasoner`; unsupported
  names may silently map to V4 Flash.
- The Anthropic-compatible API supports thinking, effort, tools, tool results, and
  streaming, but does not support image input.
- Thinking/tool replay must retain assistant thinking and tool-use blocks before
  the user tool-result block.
- Installed `langchain-anthropic` payload generation was probed without network;
  it preserved this ordering with `output_version="v1"`.
- Deep Agents 0.6.12 automatically adds a `general-purpose` subagent and `task`
  tool unless disabled through a registered harness profile. The DeepSeek adapter
  uses `ChatAnthropic`, so the relevant provider key includes `anthropic`.

See the implementation plan for exact source links and planned test payloads.

## Environment Baseline

- Python target: `>=3.11,<3.12`.
- `uv`: 0.11.4 at the time of planning.
- Exact direct versions are recorded in `pyproject.toml` and `uv.lock`.
- Houdini baseline: 21.0.440.
- Houdini bundled RPyC and agent RPyC must both be `4.1.0`.

After checkout on the new computer:

```powershell
uv sync --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent tests
bash scripts/env_probe.sh
```

The probe is diagnostic and always exits zero. Read its status lines instead of
using its process exit as proof that Houdini and the RPC bridge are available.

## Main Worktree Changes That Are Not In Foundation

At initial handoff preparation, `E:\eee-agent` had these uncommitted user changes:

```text
 M CLAUDE.md
 M README.md
 M houdini_side/install_menu.py
?? .claude/settings.json
?? scripts/env_probe.sh
```

Tracked diff summary:

```text
3 files changed, 143 insertions(+), 77 deletions(-)
```

Do not discard or overwrite these files. They are not present in the clean
Foundation worktree branch. The tracked Foundation `scripts/env_probe.sh` is a
reviewed successor to the untracked main-worktree script, but the main copy has
not been deleted or modified by Foundation work.

Migration completion update: these five paths were reviewed for common credential
patterns, syntax-checked where applicable, committed without `.env` on branch
`wip/pre-migration-main`, and pushed as commit `bc351c0`. The only key-shaped
string found was the documented placeholder `sk-your-deepseek-key` in the old
probe script. The main worktree is now checked out on the WIP branch and clean.

Before migration, either review and commit these files on a separate WIP branch,
or export a binary Git patch and separately archive the two untracked files. A
normal `git push` does not transfer working-tree changes or stashes.

## Secrets And Machine-Local State

These paths are ignored and are not transferred by Git:

- `.env`
- `.venv/`
- `.worktrees/`

Never commit `.env`. Transfer required credentials through a password manager or
another encrypted channel, then create a new `.env` from `.env.example` on the
new computer. Rebuild `.venv` with `uv sync`; do not copy it between machines.
Recreate worktrees from pushed branches; do not copy `.worktrees` as project data.

Review `.claude/settings.json` before committing or copying it because local tool
settings can contain machine-specific paths or sensitive configuration.

## Recommended Git Migration

On the old computer, after reviewing the main dirty files:

```powershell
git -C E:\eee-agent push origin main
git -C E:\eee-agent\.worktrees\foundation push -u origin feature/foundation
```

If the main dirty files should be preserved in Git, create and push a separate WIP
branch rather than mixing them into Foundation:

```powershell
git -C E:\eee-agent switch -c wip/pre-migration-main
git -C E:\eee-agent add CLAUDE.md README.md houdini_side/install_menu.py scripts/env_probe.sh
# Add .claude/settings.json only after confirming that it contains no secret.
git -C E:\eee-agent commit -m "wip: preserve pre-migration workspace changes"
git -C E:\eee-agent push -u origin wip/pre-migration-main
```

As an offline fallback for committed history, create a Git bundle and copy it by
an encrypted drive or trusted storage:

```powershell
git -C E:\eee-agent bundle create E:\eee-agent-2026-07-13.bundle --all
```

The bundle still excludes uncommitted, untracked, and ignored files.

On the new computer:

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
git -C E:\eee-agent fetch --all --prune
git -C E:\eee-agent worktree add E:\eee-agent\.worktrees\foundation feature/foundation
Set-Location E:\eee-agent\.worktrees\foundation
uv sync --extra eval --python 3.11
```

If `feature/foundation` is not created as a local branch automatically:

```powershell
git -C E:\eee-agent worktree add -b feature/foundation `
  E:\eee-agent\.worktrees\foundation origin/feature/foundation
```

## New-Computer Resume Prompt

Start a new development conversation from the Foundation worktree and use:

```text
Continue EEE Agent development in the current feature/foundation worktree.
First read, in order:
1. docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md
2. docs/superpowers/plans/2026-07-13-foundation.md
3. docs/handoffs/2026-07-13-foundation-migration.md

Do not restart planning or alter the approved architecture. Resume Foundation
Task 6 at the pending final specification re-review for range
35e2cd5..76b1af9. After specification approval, perform independent code-quality
review, fix and re-review any findings, then continue Tasks 7-12 using fresh
implementation, specification-review, and code-quality-review subagents. Preserve
unrelated user changes and use official provider/Houdini APIs rather than guesses.
```

## Migration Completion Checklist

- Both `main` and `feature/foundation` are visible on the remote.
- The handoff commit is visible on `feature/foundation`.
- Main dirty work is either on a pushed WIP branch or separately backed up.
- `.env` credentials are transferred securely and are not in Git history.
- The new machine recreates `.venv` and the Foundation worktree.
- `uv lock --check`, the full suite, compileall, and environment probe run.
- The new conversation reads the three source-of-truth documents before editing.
