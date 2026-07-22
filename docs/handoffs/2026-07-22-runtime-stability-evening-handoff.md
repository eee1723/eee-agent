# Runtime Stability Evening Handoff

Date: 2026-07-22
Owner on pause: Codex
Working directory: `E:/eee-agent/.worktrees/runtime`
Branch: `feature/a-stability`
Remote: `origin` (`https://github.com/eee1723/eee-agent.git`)

## Pause point

The implementation is intentionally paused after the Stage A strict-contract
cleanup. The current code commit is:

```text
b40096b fix: narrow strict modeling contract types
```

This commit contains the reviewed A-5M5 fix for the exception-direction
regression in `modeling/contracts.py` and its focused regression tests. The
next commit in this pause sequence contains this handoff document.

The branch has not been merged, rebased, or pushed to `main`. No Stage B or
Stage C implementation has started.

## What is complete

The following Stage A work is committed and independently reviewed:

1. `61c509d` — close the Runtime database in the leaking test and make
   unhandled worker-thread warnings fatal;
2. `5a824a1` — record the initial Stage A quality-gate findings;
3. `596cf72` — normalize terminal Run failures for the panel;
4. `df648e0` — use one terminal-result selector for history and live rendering;
5. `8843869` — render structured failure evidence in Run Inspector;
6. `26c0f4d` — restore the repository Ruff gate;
7. `91ad23f` — narrow strict JSON/DTO contract types;
8. `b79d4fc` — extract the shared ChangeSet codec;
9. `bba1964` — narrow Bridge wire/envelope types;
10. `e70d1a5` — narrow ChangeSet Repository event payload types;
11. `b40096b` — narrow Modeling DTO/JSON contracts and restore exception
    direction for enum, schema-version, identifier, qualified-node, code, and
    digest fields.

Product-level Stage A outcomes already present:

- Failed Runs no longer become empty Assistant cards;
- history replay and live terminal settling share the same selector;
- Completed, Cancelled, and Failed terminal states are explicit;
- Run Inspector exposes bounded failure code, user-facing message, and
  retryability;
- ChangeSet decoder logic is shared between Repository and Bridge;
- Repository, Bridge, Modeling, Runtime, and JSON contract boundaries have
  substantially narrower types;
- no new `Any`, `cast`, `type: ignore`, or `# noqa` was introduced by the
  reviewed A-5M5 code;
- `modeling/contracts.py` now has zero local `type: ignore` comments and zero
  direct mypy errors.

## Fresh verification at the pause point

The following commands were run on the A-5M5 working tree immediately before
the code commit:

```text
uv run --frozen --extra eval pytest -q tests/modeling
95 passed in 2.88s

uv run --frozen --extra eval pytest -q \
  tests/runtime/test_changeset_codec.py \
  tests/runtime/test_houdini_bridge_contracts.py \
  tests/modeling/test_bootstrap.py \
  tests/modeling/test_framing.py
163 passed in 0.40s

uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision --no-error-summary
177 errors in 26 files

uv run --frozen ruff check eee_agent/modeling/contracts.py tests/modeling/test_contracts.py
All checks passed!

uv run --frozen ruff check .
All checks passed!

uv run --frozen python -m compileall -q eee_agent houdini_side tests
exit 0

git diff --check
exit 0
```

The full Runtime suite was independently green immediately before A-5M4:
`2034 passed`. It must be rerun on the final Stage A tip before Stage A is
accepted; the full repository suite, `uv lock --check`, and zero-error mypy
have not yet been established after A-5M5.

## Current quality debt

The current complete mypy command still exits nonzero:

```text
177 errors in 26 files
```

The exact current distribution is:

| Count | File or group |
| ---: | --- |
| 26 | `eee_agent/runtime/service.py` |
| 23 | `eee_agent/providers/openai.py` |
| 21 | `eee_agent/app.py` |
| 20 | `eee_agent/providers/anthropic.py` |
| 14 | `eee_agent/panel/runtime_state.py` |
| 9 | `eee_agent/modeling/compiler.py` |
| 9 | `eee_agent/runtime/lock.py` |
| 8 | `eee_agent/changesets/service.py` |
| 6 | `eee_agent/runtime/server.py` |
| 4 | `eee_agent/houdini_bridge/client.py` |
| 4 | `eee_agent/providers/deepseek_v4.py` |
| 4 | `eee_agent/houdini_bridge/capture.py` |
| 4 | `eee_agent/runtime/events.py` |
| 4 | `eee_agent/context_store.py` |
| 3 | `eee_agent/runtime/runs.py` |
| 2 | `eee_agent/knowledge/service.py` |
| 2 | `eee_agent/tool_error_trace.py` |
| 2 | `eee_agent/tracing.py` |
| 2 | `eee_agent/runtime/agent_runner.py` |
| 2 | `eee_agent/runtime/artifacts.py` |
| 2 | `eee_agent/houdini_bridge/sensitivity.py` |
| 2 | `eee_agent/houdini_bridge/__init__.py` |
| 1 | `eee_agent/runtime/checkpoints.py` |
| 1 | `eee_agent/panel/client_state.py` |
| 1 | `eee_agent/modeling/validation.py` |
| 1 | `eee_agent/modeling/proposal.py` |

`modeling/contracts.py` is no longer in the error list. Do not report Stage A
as complete until this entire command exits 0 and the Stage A acceptance
evidence is committed.

## Next implementation order

When work resumes, continue only on `feature/a-stability` and keep each task
bounded and independently reviewable:

1. **A-5M6 — Runtime low-level contract cluster**: first inspect and type the
   smaller `runtime/events.py`, `runtime/runs.py`, `runtime/artifacts.py`,
   `runtime/checkpoints.py`, `runtime/agent_runner.py`, `runtime/lock.py`, and
   `runtime/server.py` boundaries. Do not mix this with the large service
   orchestration file unless a dependency requires it.
2. **A-5M7 — `runtime/service.py`**: handle its 26 errors as a separate task;
   preserve event payload contracts, async task ownership, provider boundaries,
   and lifecycle behavior.
3. **A-5M8 — Provider contracts**: split OpenAI/Anthropic/DeepSeek if needed;
   no provider behavior or fallback policy changes merely to satisfy mypy.
4. **A-5M9 — `app.py` and application assembly**.
5. **A-5M10 — Panel state/client state**.
6. **A-5M11 — Modeling compiler/validation/proposal consumers**.
7. **A-5M12 — ChangeSet Service, Bridge tail, context/tracing/knowledge tail**.
8. Run the final Stage A full gates and create the Stage A acceptance report.

The next task prompt must include the exact current error count and must not
use `Any`, broad casts, new ignore comments, or mypy configuration changes.
If a runtime behavior contract changes while narrowing types, stop and add a
differential regression test before proceeding.

## Stage A final gate

Before moving to Stage B, run on the final committed A tip:

```powershell
uv lock --check
uv run --frozen ruff check .
uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision
python -m compileall -q eee_agent houdini_side tests
uv run --frozen --extra eval pytest -q
git diff --check main...HEAD
git status --short --branch
```

The acceptance document must record exact counts, skipped tests, warnings,
versions, commit, and unresolved findings. It must not contain secrets, raw
provider output, prompts, or raw tracebacks.

## Stage B and Stage C remain untouched

Stage B has seven planned tasks and requires accepted Stage A first:

- provider-neutral Vision configuration and real-provider adapter;
- explicit `FAILED` versus `UNAVAILABLE` semantics;
- offline/HFS/disposable hython gates;
- interactive Houdini panel checklist;
- real Vision + Artifact journey;
- release-candidate readiness decision.

Stage C has nine planned tasks and also has not started:

- durable source audit;
- strict `DecisionSummary`/delivery contracts;
- trusted parameter guide;
- Delivery Repository and migration;
- Runtime aggregation and replay;
- structured delivery Panel UI;
- fail-isolated observability core;
- optional Phoenix adapter and real export smoke;
- final Task 19-C acceptance.

Do not create `feature/b-release-acceptance` or `feature/c-task19c-delivery`
until the previous stage is accepted and the Runtime worktree is clean.

## External findings and gates

- RN-007: WSL invocation of `scripts/env_probe.sh` produced an
  `Exec format error`; reproduce through the actual Windows/Claude hook before
  changing the script.
- RN-008: GLM 5.2 gateway authentication is restored, but prior requests were
  rejected by gateway overload. Keep the exact `glm-5.2[1m]` model when
  delegating; do not substitute `k3` or start duplicate workers.
- RN-005/RN-006: Production Vision provider path and FAILED/UNAVAILABLE
  distinction require Stage B real-provider evidence.
- RN-011: Phoenix dependencies are not currently a supported locked feature;
  resolve only in Stage C with explicit optional dependency and real smoke.

## Resume checklist

From a new session at home:

```powershell
Set-Location E:/eee-agent/.worktrees/runtime
git fetch origin
git switch feature/a-stability
git pull --ff-only origin feature/a-stability
git status --short --branch
Get-Content docs/handoffs/2026-07-22-runtime-stability-evening-handoff.md
```

Expected after pull:

- branch `feature/a-stability`;
- clean worktree;
- HEAD at the code commit plus this handoff commit;
- no active Stage B/C branch;
- `.worktrees/runtime` path preserved because the installed Houdini package
  points to it.

Do not merge, push to `main`, create a release tag, remove the Runtime
worktree, or delete the local archive without a separate explicit decision.
