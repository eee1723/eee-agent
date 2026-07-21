# Runtime MVP 安全交付与集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `feature/runtime` 上完成 Secure read-only Runtime、Artifact 一致性修复、真实 sensitivity 验证、Knowledge Graph 集成、Runtime MVP、Vision/Evaluation 和最终 GUI/发布门禁。

## 2026-07-20 progress snapshot

- S0-S6 complete: baseline, Secure Runtime/read-only tools, legacy write-path
  removal, Artifact lifecycle, real Houdini sensitivity, Knowledge integration,
  boundary hardening and CI gates.
- S7 offline acceptance complete. Real-provider execution is still `not run`;
  the runner now requires a strict bounded evidence sidecar.
- S8 offline complete: Vision contracts/router/evaluation are wired into the
  production post-Apply flow; captured artifacts flow as exact ArtifactStore
  bytes through `VisionRouter` into a durable `vision.evaluation_completed`
  delivery record. Real-provider execution is still `not run`.
- S9 incomplete: the interactive Houdini GUI checklist, real-provider
  evidence, release tag and push remain pending. Dedicated Vision panel
  presentation landed 2026-07-20 (bounded parse + ARTIFACTS tab section).
- Current transfer and exact verification evidence:
  `docs/handoffs/2026-07-20-runtime-development-transfer.md`.

**Architecture:** Runtime Agent 只通过受限的 Secure read-only tool adapter 查询 Houdini，通过严格 Brief/Spec 和确定性 compiler 产生 typed ChangeSet；所有写入必须经过 exact approval、single FIFO/main-thread Apply、receipt 和 deterministic validation。Knowledge Graph 作为共享、可重建、可追溯的只读 capability 集成到 Runtime；ArtifactStore 使用可恢复状态机协调 SQLite 元数据与文件系统，避免跨介质 rollback 不一致。

**Tech Stack:** Python 3.11、uv frozen lock、LangChain/Deep Agents、LangGraph、aiosqlite/SQLite、WebSocket Runtime、Houdini 21.0.440 hython、Secure Bridge typed JSON DTO、pytest。

---

## 执行规则

- 开发基线固定为 `feature/runtime @ 742c910`（设计文档提交之后）。
- 每个任务从前一任务的验收 commit 创建独立分支；不从旧 `main` 或根目录 `wip/pre-migration-main` 重新开发。
- 任务必须按顺序执行；S1 未完成前不删除 legacy bridge，S2 未完成前不接 Vision，S3 未完成前不合并 Knowledge Graph。
- 每个任务使用 TDD 顺序：先写失败测试，再实现，再运行 focused/full tests，最后提交。
- 每个任务结束必须运行：
  ```powershell
  uv lock --check
  uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
  git diff --check
  ```
- 不得提交 `.env`、token、Runtime DB、Knowledge cache、artifact、Houdini preference 或临时 smoke 目录。
- 任何 raw write tool、legacy bridge import、uncertain Apply replay 或 Artifact metadata/file 不一致都立即停止当前任务。

## 文件职责地图

### S1 Secure read-only 迁移和 legacy 下线

- Create: `eee_agent/runtime/agent_context.py` — Runtime Agent 的非模型上下文和 provider Protocol。
- Create: `eee_agent/runtime/agent_tools.py` — 只读 Agent tool factory；禁止导入 `eee_agent.bridge`。
- Modify: `eee_agent/runtime/agent_runner.py` — 使用新 tool factory 和统一 context schema。
- Modify: `eee_agent/runtime/service.py` — 注入 read-only provider，给每个 Run 创建 RuntimeToolContext。
- Modify: `eee_agent/modeling/proposal.py` — 从 RuntimeToolContext 取得 modeling context。
- Modify: `eee_agent/app.py` — 禁止 `build_agent()` 隐式加载 `all_tools()`。
- Modify: `MainMenuCommon.xml`、`houdini_side/README_INSTALL.md`、`README.md`、`SETUP.md` — 删除正式 legacy 入口和旧运行说明。
- Delete/archive after migration: `eee_agent/bridge/`、legacy `eee_agent/tools/*`、`houdini_side/start_rpc.py`、`chat_panel.py`、`launch.py`。
- Test: `tests/runtime/test_agent_tools.py`、`tests/runtime/test_agent_runner.py`、`tests/runtime/test_read_only_agent.py`、`tests/runtime/test_runtime_e2e.py`、`tests/runtime/houdini_bridge_smoke.py`。

### S2 Artifact 一致性

- Modify: `eee_agent/runtime/migrations.py` — 增加 artifact lifecycle/reconciliation schema。
- Modify: `eee_agent/runtime/artifacts.py` — 实现可恢复状态机和提交后清理。
- Modify: `eee_agent/runtime/service.py` — 启动 reconciliation、session cleanup retry、bounded event。
- Modify: `eee_agent/panel/runtime_state.py`、`houdini_side/runtime_panel.py` — 显示 available/evicted/missing/failed。
- Test: `tests/runtime/test_artifact_store.py`、`tests/runtime/test_service.py`、`tests/runtime/test_runtime_e2e.py`、`tests/runtime/capture_houdini_smoke.py`。

### S3 Sensitivity wire smoke

- Create: `tests/runtime/sensitivity_houdini_smoke.py` — 独立真实 hython/secure bridge smoke。
- Modify only when the smoke reveals a contract gap: `eee_agent/houdini_bridge/changeset_provider.py`、`eee_agent/houdini_bridge/client.py`、`houdini_side/secure_bridge.py`、`houdini_side/changeset_executor.py`。
- Test: `tests/runtime/test_sensitivity_bridge.py`、`test_sensitivity_bridge_transport.py`、`test_sensitivity_executor.py`。

### S4 Knowledge Graph integration

- Merge: `feature/houdini-knowledge-graph` into `feature/runtime` after S3.
- Modify: `eee_agent/runtime/paths.py`、`eee_agent/runtime/service.py`、`eee_agent/runtime/models.py`、`eee_agent/runtime/events.py`、`eee_agent/runtime/agent_context.py`、`eee_agent/runtime/agent_tools.py`、`eee_agent/runtime/protocol.py`、`eee_agent/config.py`。
- Modify: `pyproject.toml`、`uv.lock`、`.gitignore`、`CLAUDE.md`、`README.md`、`tests/test_env_probe.py` after resolving conflicts.
- Create: `tests/runtime/test_knowledge_runtime_integration.py`。
- Re-run: all Runtime tests, all Knowledge tests and opt-in HFS contract.

### S5–S7 MVP/Vision/GUI

- Modify: `eee_agent/runtime/service.py`、`eee_agent/runtime/server.py`、`eee_agent/runtime/protocol.py` only for missing MVP event/recovery seams.
- Create: `tests/runtime/test_runtime_mvp_e2e.py` and provider-gated local e2e runner.
- Create: `eee_agent/vision/router.py`、`eee_agent/vision/contracts.py`、`eee_agent/vision/evaluation.py` and their tests.
- Modify: `eee_agent/panel/runtime_state.py`、`houdini_side/runtime_panel.py` for final artifact/vision/result surfaces.
- Modify: CI configuration and synchronized handoffs/docs.
- Delete local/remote merged branches only after S4 joint acceptance and tags.

---

## Task 0: Freeze the Runtime baseline

**Branch:** `feature/runtime-s0-baseline`

**Files:**
- Read only: `docs/superpowers/specs/2026-07-17-runtime-mvp-development-design.md`
- Create: no source file.
- Test: existing full Runtime and Houdini smoke commands.

`- [ ]` **Step 1: Verify the baseline commit and worktree**

Run:
```powershell
git switch feature/runtime
git status --short --branch
git rev-parse HEAD
```

Expected:
```text
HEAD 742c910
worktree clean
```

`- [ ]` **Step 2: Run the baseline offline gate**

Run:
```powershell
uv run --frozen --extra eval pytest -q
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Expected:
```text
2317 passed
all other commands exit 0
```

`- [ ]` **Step 3: Record current Houdini evidence**

Run sequentially with `D:\houdini\bin\hython.exe`:

```powershell
& 'D:\houdini\bin\hython.exe' -u tests\runtime\capture_houdini_smoke.py
& 'D:\houdini\bin\hython.exe' -u tests\runtime\changeset_houdini_smoke.py
& 'D:\houdini\bin\hython.exe' -u tests\modeling\bootstrap_houdini_smoke.py
& 'D:\houdini\bin\hython.exe' -u tests\modeling\golden_cases_houdini_smoke.py
```

Expected:
- capture smoke: 21 checks passed;
- changeset smoke: SMOKE OK;
- bootstrap smoke: BOOTSTRAP SMOKE PASS;
- Golden Cases smoke: GOLDEN CASES PASS.

`- [ ]` **Step 4: Create the rollback tag**

Run:
```powershell
git tag runtime-pre-mvp-2026-07-17 742c910
git show --no-patch runtime-pre-mvp-2026-07-17
```

Expected: tag resolves to `742c910`.

`- [ ]` **Step 5: Commit baseline handoff**

Update `docs/handoffs/2026-07-17-cross-machine-modeling-handoff.md` with the measured 2317 count and current next task, then run `git diff --check`.

Commit:
```powershell
git add docs/handoffs/2026-07-17-cross-machine-modeling-handoff.md
git commit -m "docs: freeze runtime MVP baseline"
```

---

## Task 1: Define the Secure Runtime Agent context and read-only provider

**Branch:** `feature/runtime-secure-readonly`

**Files:**
- Create: `eee_agent/runtime/agent_context.py`
- Create: `eee_agent/runtime/agent_tools.py`
- Modify: `eee_agent/runtime/agent_runner.py`
- Modify: `eee_agent/runtime/service.py`
- Modify: `eee_agent/modeling/proposal.py`
- Test: `tests/runtime/test_agent_tools.py`
- Test: `tests/runtime/test_agent_runner.py`
- Test: `tests/runtime/test_read_only_agent.py`

`- [ ]` **Step 1: Write the context/provider contract tests**

Add tests with these exact names and assertions:

- `test_runtime_context_rejects_missing_readonly_provider`: constructing the context without a provider raises `TypeError`.
- `test_readonly_tools_return_bounded_plain_dicts`: every result is a plain dictionary and body/list fields stay within configured limits.
- `test_readonly_tools_never_import_legacy_bridge`: importing the Runtime tool module does not load `eee_agent.bridge` or `eee_agent.tools`.
- `test_agent_runner_builds_only_secure_tools`: the runner tool list contains only the Runtime read-only factory output plus `propose_modeling` when explicitly enabled.
- `test_proposal_tool_reads_modeling_context_from_runtime_context`: the proposal tool reads the injected modeling context and does not construct a bridge client.
- `test_missing_secure_provider_returns_bridge_unavailable_without_write`: the unavailable provider returns the bounded `bridge.unavailable` error and records no write call.

Use a fake provider whose methods return bounded DTOs:

```python
class FakeReadOnlyProvider:
    async def scene_status(self):
        return {"scene_fingerprint": "test-scene", "node_count": 0}

    async def query_scene(self, node_paths):
        return {"nodes": [{"path": path} for path in node_paths[:8]]}

    async def inspect_workspace(self, workspace_id):
        return {"workspace_id": workspace_id, "status": "ready"}

    async def geometry_stats(self, node_path):
        return {"node_path": node_path, "point_count": 0, "primitive_count": 0}

    async def work_status(self, workspace_id):
        return {"workspace_id": workspace_id, "active": False}
```

`- [ ]` **Step 2: Run focused tests and verify they fail**

Run:
```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_agent_tools.py tests/runtime/test_read_only_agent.py
```

Expected: FAIL because `agent_context.py` and `agent_tools.py` do not exist and the runner still uses `eee_agent.tools.registry.read_only_tools`.

`- [ ]` **Step 3: Implement `RuntimeToolContext`**

Create the frozen runtime-only context:

```python
@dataclass(frozen=True, slots=True)
class RuntimeToolContext:
    read_only: ReadOnlyProvider
    modeling: object | None = None
```

Define `ReadOnlyProvider` as a Protocol with only the bounded async methods used by the tools. Do not import `hou`, `rpyc`, `eee_agent.bridge`, SQLite or filesystem clients in this module.

`- [ ]` **Step 4: Implement the secure read-only tool factory**

Create `build_read_only_tools()` in `eee_agent/runtime/agent_tools.py`.

Each tool must:

- accept only model-safe scalar/list/dict arguments;
- obtain `ToolRuntime.context`;
- require exact `RuntimeToolContext`;
- call the injected provider;
- convert provider DTOs to bounded plain dicts;
- return a bounded error object on unavailable/stale/invalid provider state;
- never mutate Houdini;
- never return a live provider/client/proxy.

Keep tool names stable where the Runtime protocol already depends on them, but remove all imports from `eee_agent.tools.*`.

`- [ ]` **Step 5: Update AgentRunner and proposal context**

Modify `build_agent_runner()` so every Runtime graph uses:

```python
context_schema=RuntimeToolContext
tools=build_read_only_tools()
```

When `modeling=True`, append only `propose_modeling`. Modify `propose_modeling()` to require `RuntimeToolContext` and read `context.modeling` as the existing `ModelingToolContext`.

Modify RuntimeService to create a per-session/per-run RuntimeToolContext from the injected Secure read-only provider. If no provider is available, inject a provider that returns a bounded `bridge.unavailable` result; never fall back to legacy rpyc.

`- [ ]` **Step 6: Run focused tests**

Run:
```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_agent_tools.py tests/runtime/test_read_only_agent.py tests/runtime/test_agent_runner.py tests/runtime/test_service.py
```

Expected: all focused tests pass and no test imports `eee_agent.bridge`.

`- [ ]` **Step 7: Commit the secure adapter**

Run:
```powershell
git add eee_agent/runtime/agent_context.py eee_agent/runtime/agent_tools.py eee_agent/runtime/agent_runner.py eee_agent/runtime/service.py eee_agent/modeling/proposal.py tests/runtime/test_agent_tools.py tests/runtime/test_agent_runner.py tests/runtime/test_read_only_agent.py
git commit -m "feat: route runtime agent queries through secure bridge"
```

---

## Task 2: Remove all formal legacy raw-write entry points

**Branch:** `feature/runtime-legacy-off`

**Files:**
- Modify: `eee_agent/app.py`
- Modify/delete: `eee_agent/cli.py`
- Modify: `MainMenuCommon.xml`
- Modify: `houdini_side/README_INSTALL.md`
- Modify: `README.md`
- Modify: `SETUP.md`
- Delete/archive: `eee_agent/bridge/`、legacy `eee_agent/tools/*`、`houdini_side/start_rpc.py`、`chat_panel.py`、`launch.py`
- Test: `tests/runtime/test_legacy_removed.py`
- Modify: `tests/test_harness.py`、`tests/test_cli_stream_events.py`、`tests/runtime/test_read_only_agent.py`

`- [ ]` **Step 1: Inventory all legacy imports and entry points**

Run:
```powershell
rg -n "eee_agent\.bridge|start_rpc|chat_panel|Open Agent Panel|all_tools\(|cli prompt|cli stdio" eee_agent houdini_side tests MainMenuCommon.xml README.md SETUP.md
```

Record every match in the task branch before deleting files. The inventory must include workflow middleware and tests.

`- [ ]` **Step 2: Write removal contract tests**

Create `tests/runtime/test_legacy_removed.py` with tests that:

- assert no formal menu item named `Open Agent Panel`;
- assert no `Start RPC Bridge Only` menu item;
- assert `build_agent()` without explicit tools raises `TypeError`;
- assert Runtime graph tool names contain no raw write names;
- assert importing Runtime modules does not load `eee_agent.bridge`;
- assert CLI parser exposes only the supported Runtime command/version paths;
- assert no Runtime read-only tool imports `eee_agent.bridge`.

`- [ ]` **Step 3: Run tests to verify the removal tests fail**

Run:
```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_legacy_removed.py
```

Expected: FAIL against the currently present menu, CLI and implicit `all_tools()` behavior.

`- [ ]` **Step 4: Remove formal menu and CLI routes**

Delete the two legacy menu actions and update the help text to advertise only Runtime Control. Remove `prompt` and `stdio` from the production CLI parser. Keep `versions` and the Runtime server command. Do not leave a hidden alias that starts legacy rpyc.

`- [ ]` **Step 5: Make agent construction explicit**

Change `build_agent()` so `tools=None` is rejected. Callers must pass an explicit list. Update Runtime callers to use the Secure tool factory. Delete `ALL_TOOLS` and any compatibility function that can reintroduce raw write tools into a graph.

`- [ ]` **Step 6: Delete/archive legacy modules after the new adapter is green**

Delete the old rpyc client/server and raw tool modules. If a historical copy is required for audit, move it outside the importable package under `archive/legacy/` and add a test that no package import path discovers it.

`- [ ]` **Step 7: Run removal and full Runtime tests**

Run:
```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_legacy_removed.py tests/runtime/test_agent_tools.py tests/runtime/test_agent_runner.py
uv run --frozen --extra eval pytest -q
```

Expected:
- removal contract passes;
- full Runtime suite passes;
- no legacy import failure is hidden by test order.

`- [ ]` **Step 8: Verify the real Secure Bridge path**

Run:
```powershell
$state = Join-Path $env:TEMP ("eee-bridge-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $state | Out-Null
try {
    & 'D:\houdini\bin\hython.exe' -u tests\runtime\houdini_bridge_smoke.py --state-dir $state
} finally {
    if (Test-Path -LiteralPath $state) {
        Remove-Item -LiteralPath $state -Recurse -Force
    }
}
```

Expected:
- token handshake succeeds;
- no legacy RPC module is imported;
- scene fingerprint unchanged;
- all 20 existing checks pass.

`- [ ]` **Step 9: Commit legacy removal**

```powershell
git add -A
git commit -m "security: remove legacy raw-write agent path"
```

---

## Task 3: Repair ArtifactStore cross-media consistency

**Branch:** `feature/runtime-artifact-consistency`

**Files:**
- Modify: `eee_agent/runtime/migrations.py`
- Modify: `eee_agent/runtime/artifacts.py`
- Modify: `eee_agent/runtime/service.py`
- Modify: `eee_agent/panel/runtime_state.py`
- Modify: `houdini_side/runtime_panel.py`
- Test: `tests/runtime/test_artifact_store.py`
- Test: `tests/runtime/test_service.py`
- Test: `tests/runtime/test_runtime_e2e.py`
- Test: `tests/runtime/capture_houdini_smoke.py`

`- [ ]` **Step 1: Add failing rollback/recovery tests**

Add tests with these exact names and assertions:

- `test_retention_db_rollback_does_not_restore_missing_file`: an injected post-unlink/pre-commit failure leaves the old row and old file unavailable, with a recoverable state recorded.
- `test_register_commit_failure_leaves_recoverable_pending_record`: a failure after staging persists a `pending` or `failed` row and never emits an availability event.
- `test_restart_reconciles_pending_file_placement`: startup completes a valid staged placement and transitions it to `available`.
- `test_restart_marks_missing_available_file_without_success_event`: startup marks a missing canonical file as `missing` and emits no success event.
- `test_cleanup_failure_is_retryable_after_session_delete`: cleanup increments `cleanup_attempts`, records an error code, and succeeds on a later retry.
- `test_event_replay_distinguishes_evicted_artifact`: replay distinguishes `evicted` from `missing` and never returns either as available bytes.
- `test_duplicate_artifact_path_never_overwrites_available_bytes`: a duplicate registration uses a unique staging path and preserves the existing canonical file.

The first test must inject an exception after the old file is selected for eviction but before the DB transaction commits, then assert that the old row and old file cannot be reported as available.

`- [ ]` **Step 2: Run focused tests and verify current failure**

Run:
```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_artifact_store.py tests/runtime/test_service.py -k "artifact or retention or cleanup"
```

Expected: the rollback/recovery tests fail against the current unlink-inside-transaction implementation.

`- [ ]` **Step 3: Add lifecycle schema**

Add a migration after schema v4 with exact fields:

```sql
artifact_state TEXT NOT NULL DEFAULT 'available'
  CHECK (artifact_state IN ('pending','available','pending_eviction','evicted','missing','failed')),
cleanup_attempts INTEGER NOT NULL DEFAULT 0 CHECK (cleanup_attempts >= 0),
last_error_code TEXT NULL,
updated_at TEXT NOT NULL
```

Keep existing hash/size/path constraints. Add an index for `artifact_state, updated_at`.

`- [ ]` **Step 4: Implement explicit registration states**

Change registration to:

1. hash and size-verify source;
2. place or retain the file in a staging path that cannot collide with an available canonical path;
3. insert a `pending` metadata row and commit;
4. atomically place the file in the canonical path;
5. hash/size re-check the canonical file;
6. transition row to `available` and commit;
7. on any later failure, retain a structured pending/failed row for reconciliation instead of claiming success.

Never overwrite a canonical file belonging to another available artifact.

`- [ ]` **Step 5: Implement commit-safe retention**

Change retention to:

1. select deterministic oldest candidates;
2. transactionally mark candidates `pending_eviction` and commit;
3. unlink files outside the metadata transaction;
4. transactionally mark successful candidates `evicted`;
5. mark failed deletions `failed` with bounded error code and retry count;
6. keep event history intact but make panel/lookup status explicit.

Do not delete a file and then rely on a later SQLite rollback to represent the previous state.

`- [ ]` **Step 6: Implement startup reconciliation**

At RuntimeService open, call an ArtifactStore reconciliation pass that:

- marks available rows with missing files as `missing`;
- completes or rolls back pending placements using hash/size;
- retries bounded pending_eviction cleanup;
- removes unreferenced staging files;
- emits bounded recovery evidence;
- never turns a missing artifact into an available one without hash verification.

`- [ ]` **Step 7: Update panel and event parsing**

Keep ArtifactRef metadata-only, but add explicit state to the panel summary. The panel must render:

```text
AVAILABLE
EVICTED
MISSING
FAILED
```

An event replay containing an evicted/missing artifact must not display it as a valid image.

`- [ ]` **Step 8: Run focused and full tests**

Run:
```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_artifact_store.py tests/runtime/test_service.py tests/runtime/test_runtime_e2e.py
uv run --frozen --extra eval pytest -q
```

Expected: all new failure-injection tests and the full Runtime suite pass.

`- [ ]` **Step 9: Run capture smoke**

Run:
```powershell
& 'D:\houdini\bin\hython.exe' -u tests\runtime\capture_houdini_smoke.py
```

Expected: 21 capture checks pass and the delivered PNG hash still matches the Runtime-registered bytes.

`- [ ]` **Step 10: Commit Artifact consistency**

```powershell
git add eee_agent/runtime/migrations.py eee_agent/runtime/artifacts.py eee_agent/runtime/service.py eee_agent/panel/runtime_state.py houdini_side/runtime_panel.py tests/runtime/test_artifact_store.py tests/runtime/test_service.py tests/runtime/test_runtime_e2e.py tests/runtime/capture_houdini_smoke.py
git commit -m "fix: make artifact retention crash recoverable"
```

---

## Task 4: Add real sensitivity wire smoke

**Branch:** `feature/runtime-sensitivity-hython-smoke`

**Files:**
- Create: `tests/runtime/sensitivity_houdini_smoke.py`
- Test: `tests/runtime/test_sensitivity_bridge.py`
- Test: `tests/runtime/test_sensitivity_bridge_transport.py`
- Test: `tests/runtime/test_sensitivity_executor.py`
- Modify, when a failing smoke assertion identifies a contract gap: `eee_agent/houdini_bridge/changeset_provider.py`、`eee_agent/houdini_bridge/client.py`、`houdini_side/secure_bridge.py`、`houdini_side/changeset_executor.py`。 Preserve the existing typed sensitivity protocol and keep any change limited to that seam。

`- [ ]` **Step 1: Define the smoke scene and expected values**

The smoke must create a disposable owned geo/SOP graph with one catalog-safe numeric parameter, capture the exact initial value and scene epoch, and clean the graph in a `finally` block. It must not save a HIP or write outside its temporary bridge state directory.

`- [ ]` **Step 2: Add the real wire assertions**

The script must assert:

- capability advertisement includes `sensitivity.v1`;
- wrong/stale epoch is rejected before any write;
- valid sample changes geometry evidence;
- exact original parameter value is restored;
- post-restore read-back equals the original value;
- a forced cook failure is classified;
- a restore failure freezes writes;
- an interrupted request does not replay;
- final scene fingerprint equals the initial fingerprint;
- no unowned node or temp file remains.

`- [ ]` **Step 3: Run the new smoke before implementation changes**

Run:
```powershell
& 'D:\houdini\bin\hython.exe' -u tests\runtime\sensitivity_houdini_smoke.py
```

Expected: FAIL only if the missing smoke operation or assertion is not yet wired; record the first failing boundary.

`- [ ]` **Step 4: Implement only the missing wire seam**

Use existing typed `SensitivitySampleRequest/Response`, `BridgeClient.sample_sensitivity()`, `BridgeChangeSetProvider.sample_sensitivity()` and `ChangeSetExecutor.sample_sensitivity()`. Do not add a second sensitivity protocol or a direct HOM path.

`- [ ]` **Step 5: Run focused sensitivity tests**

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_sensitivity_bridge.py tests/runtime/test_sensitivity_bridge_transport.py tests/runtime/test_sensitivity_executor.py
```

Expected: all offline/transport/executor tests pass.

`- [ ]` **Step 6: Run real wire smoke and existing Houdini smokes**

```powershell
& 'D:\houdini\bin\hython.exe' -u tests\runtime\sensitivity_houdini_smoke.py
& 'D:\houdini\bin\hython.exe' -u tests\modeling\bootstrap_houdini_smoke.py
& 'D:\houdini\bin\hython.exe' -u tests\runtime\changeset_houdini_smoke.py
```

Expected: sensitivity smoke passes all checks; existing smokes remain green.

`- [ ]` **Step 7: Commit sensitivity evidence**

```powershell
git add tests/runtime/sensitivity_houdini_smoke.py tests/runtime/test_sensitivity_bridge.py tests/runtime/test_sensitivity_bridge_transport.py tests/runtime/test_sensitivity_executor.py eee_agent houdini_side
git commit -m "test: verify sensitivity bridge against real Houdini"
```

## Task 5: Integrate Knowledge Graph into Runtime

**Branch:** `feature/runtime-knowledge-integration`

**Precondition:** Tasks 1–4 accepted; both source worktrees clean; no legacy Runtime imports.

**Files:**
- Merge: `feature/houdini-knowledge-graph`
- Modify: `eee_agent/runtime/paths.py`
- Modify: `eee_agent/runtime/service.py`
- Modify: `eee_agent/runtime/models.py`
- Modify: `eee_agent/runtime/events.py`
- Modify: `eee_agent/runtime/agent_context.py`
- Modify: `eee_agent/runtime/agent_tools.py`
- Modify: `eee_agent/runtime/protocol.py`
- Modify: `eee_agent/config.py`
- Modify: `pyproject.toml`、`uv.lock`、`.gitignore`
- Modify: `CLAUDE.md`、`README.md`、`tests/test_env_probe.py`
- Create: `tests/runtime/test_knowledge_runtime_integration.py`
- Test: all `tests/knowledge/*` and Runtime suite.

`- [ ]` **Step 1: Tag both source branches before merge**

Run from the repository containing the worktrees:

```powershell
git tag foundation-final-2026-07-17 feature/foundation
git tag knowledge-graph-final-2026-07-17 feature/houdini-knowledge-graph
git tag runtime-pre-knowledge-integration-2026-07-17 feature/runtime
git show --no-patch foundation-final-2026-07-17
git show --no-patch knowledge-graph-final-2026-07-17
git show --no-patch runtime-pre-knowledge-integration-2026-07-17
```

`- [ ]` **Step 2: Merge Knowledge Graph without deleting source branches**

Run:
```powershell
git switch feature/runtime
git merge --no-ff feature/houdini-knowledge-graph -m "merge: integrate Houdini knowledge graph into runtime"
```

Resolve only the documented files. Do not accept the old raw-tool registry from either side.

`- [ ]` **Step 3: Write failing Runtime/Knowledge integration tests**

Create tests with these exact names and assertions:

- `test_runtime_readonly_allowlist_includes_knowledge_tools`: the secure allowlist contains only the two Knowledge read-only tools.
- `test_knowledge_query_never_imports_legacy_bridge`: importing Knowledge tools does not load the legacy bridge or raw-write registry.
- `test_missing_cache_emits_nonblocking_status`: a missing cache emits `missing` and Runtime remains startable.
- `test_stale_cache_emits_status_and_run_snapshot`: a stale cache emits `stale` and persists the same status in the Run snapshot.
- `test_corrupt_cache_does_not_block_runtime_start`: a corrupt cache emits `corrupt` and returns a bounded unavailable result.
- `test_run_snapshot_contains_kb_manifest_schema_and_houdini_build`: snapshot fields are populated and replayable.
- `test_knowledge_body_query_is_bounded_and_logical_path_only`: result count/body bytes are capped and absolute machine paths are rejected.
- `test_live_catalog_remains_authority_for_creatability`: Knowledge text never changes the live node catalog or creatability decision.

`- [ ]` **Step 4: Add RuntimePaths shared cache location**

Add a dedicated shared Knowledge cache path under the user-local Runtime cache root. Keep it distinct from:

- app DB;
- checkpoint DB;
- Run artifacts;
- session cleanup;
- artifact retention.

Do not place the Knowledge SQLite file under a Run/session directory.

`- [ ]` **Step 5: Add startup Knowledge status**

On Runtime open, classify the cache as exactly one of:

```text
missing
stale
corrupt
schema_mismatch
ready
```

Persist/emit a bounded status event. If status is not `ready`, Runtime still starts and the Agent receives an unavailable capability result.

`- [ ]` **Step 6: Add Run snapshot fields**

Persist:

```text
kb_manifest_sha256
kb_schema_version
houdini_build
knowledge_status
```

at Run creation or first Knowledge capability use. Ensure replay exposes the same values.

`- [ ]` **Step 7: Add bounded Knowledge Agent tools**

Expose only `search_houdini_knowledge` and `get_houdini_knowledge` through the Secure read-only tool factory. Enforce:

- max results;
- max body bytes;
- logical POSIX source paths;
- no absolute machine path;
- no SQLite connection;
- no raw document directory;
- explicit unavailable/stale result.

`- [ ]` **Step 8: Run combined tests**

Run:
```powershell
uv run --frozen --extra eval pytest -q
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
uv run --frozen --extra eval pytest -q tests/knowledge/test_hfs_contract.py
Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Expected:
- Runtime and Knowledge tests pass in one checkout;
- HFS contract: 11 passed;
- no legacy import;
- no raw write tool.

`- [ ]` **Step 9: Update integrated handoff**

Record:

- merged commit;
- Runtime test count;
- Knowledge test count;
- HFS count;
- cache path;
- Run snapshot fields;
- unresolved GUI/LLM/Vision work;
- branch cleanup candidates.

`- [ ]` **Step 10: Commit the integration**

```powershell
git add -A
git commit -m "feat: integrate bounded Houdini knowledge into runtime"
```

`- [ ]` **Step 11: Clean merged local and remote branches**

Only after Step 8 passes:

```powershell
git branch --merged feature/runtime
git log --oneline origin/feature/foundation..feature/runtime
git log --oneline origin/feature/houdini-knowledge-graph..feature/runtime
git tag --contains foundation-final-2026-07-17
git tag --contains knowledge-graph-final-2026-07-17
git branch -d feature/foundation
git branch -d feature/houdini-knowledge-graph
git push origin --delete feature/foundation
git push origin --delete feature/houdini-knowledge-graph
```

Do not delete `main`, `wip/pre-migration-main` or `feature/runtime`.

---

## Task 6: Establish CI and synchronized quality gates

**Branch:** `feature/runtime-ci-gates`

**Files:**
- Create: Windows CI workflow under `.github/workflows/`.
- Modify: `pyproject.toml` for lint/type configuration only if the chosen tools are pinned.
- Modify: `README.md` and current handoff with exact CI commands.
- Test: CI itself plus local commands.

`- [ ]` **Step 1: Add a Windows frozen-dependency job**

The job must run:

```powershell
uv sync --frozen --extra eval
uv run --frozen --extra eval pytest -q
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
```

`- [ ]` **Step 2: Add static quality jobs**

Pin and run the selected lint/type/security tools in the lockfile. The job must fail on:

- import of `eee_agent.bridge` from Runtime;
- unused or dead imports;
- unsafe type widening at DTO boundaries;
- dependency/security failure.

`- [ ]` **Step 3: Add optional Houdini HFS job**

When a self-hosted Houdini 21.0.440 runner exists, run:

```powershell
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
uv run --frozen --extra eval pytest -q tests/knowledge/test_hfs_contract.py
& $env:HFS\bin\hython.exe -u tests\runtime\capture_houdini_smoke.py
& $env:HFS\bin\hython.exe -u tests\runtime\sensitivity_houdini_smoke.py
```

The non-Houdini CI job must remain green without HFS.

`- [ ]` **Step 4: Run the same commands locally**

Expected: local and CI commands use identical frozen dependencies and test paths.

`- [ ]` **Step 5: Commit CI gates**

```powershell
git add .github pyproject.toml uv.lock README.md docs/handoffs
git commit -m "ci: enforce runtime MVP quality gates"
```

---

## Task 7: Execute the Runtime MVP end-to-end acceptance

**Branch:** `feature/runtime-mvp-acceptance`

**Files:**
- Create: `tests/runtime/test_runtime_mvp_e2e.py`
- Create: provider-gated local e2e runner under `tests/runtime/`.
- Modify only for missing event/recovery seams: `eee_agent/runtime/service.py`、`eee_agent/runtime/server.py`、`eee_agent/runtime/protocol.py`、`eee_agent/panel/runtime_state.py`。
- Update: `docs/handoffs/2026-07-17-runtime-mvp-acceptance.md`.

`- [ ]` **Step 1: Add deterministic protocol scenarios**

Test these without a real provider:

- empty-scene bootstrap;
- existing Workspace proposal;
- reject;
- expired approval;
- stale scene;
- Bridge unavailable;
- cook failure;
- validation failure;
- repair exhaustion;
- restart/no-replay;
- artifact unavailable;
- Knowledge unavailable/stale.

`- [ ]` **Step 2: Add provider-gated real run**

The runner must:

- require an explicit environment opt-in;
- never print secrets;
- use a disposable Houdini scene;
- persist only bounded event summaries;
- clean its temporary state;
- fail with “not run” rather than claiming pass when provider credentials or HFS are absent.

`- [ ]` **Step 3: Run the full deterministic acceptance**

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_runtime_mvp_e2e.py tests/runtime
```

Expected: all deterministic scenarios pass.

`- [ ]` **Step 4: Run one real provider journey**

Execute the documented provider-gated command. Expected evidence:

- proposal digest;
- approval event;
- Apply/receipt;
- validation report;
- artifact status;
- event replay;
- final scene cleanup.

`- [ ]` **Step 5: Update MVP handoff**

The handoff must state separately:

```text
offline-ready
real-Houdini-ready
real-provider-tested
GUI-accepted
```

Do not collapse unavailable gates into a single success flag.

`- [ ]` **Step 6: Commit MVP acceptance evidence**

```powershell
git add tests/runtime docs/handoffs
git commit -m "test: accept runtime MVP end-to-end boundaries"
```

---

## Task 8: Implement advisory Vision and Evaluation

**Branch:** `feature/runtime-vision-eval`

**Files:**
- Create: `eee_agent/vision/__init__.py`
- Create: `eee_agent/vision/contracts.py`
- Create: `eee_agent/vision/router.py`
- Create: `eee_agent/vision/evaluation.py`
- Modify: `eee_agent/runtime/service.py`
- Modify: `eee_agent/panel/runtime_state.py`
- Modify: `houdini_side/runtime_panel.py`
- Test: `tests/runtime/test_vision_router.py`
- Test: `tests/runtime/test_vision_contracts.py`
- Test: `tests/runtime/test_runtime_e2e.py`
- Test: Knowledge and Golden Case evaluation suites.

`- [ ]` **Step 1: Write strict Vision DTO tests**

Define frozen DTOs for:

- provider capability;
- request;
- unavailable/waiver;
- normalized visual report;
- redacted raw response reference;
- final vision decision.

Reject unknown fields, oversized payloads, invalid media types, absolute local paths and unbounded provider content.

`- [ ]` **Step 2: Write router behavior tests**

Test:

- provider available;
- provider unavailable;
- API key missing;
- timeout;
- malformed response;
- schema-invalid response;
- user waiver;
- artifact missing/evicted;
- deterministic validator failure cannot be overridden.

`- [ ]` **Step 3: Implement Router using ArtifactStore bytes**

The router must resolve the ArtifactRef through ArtifactStore, re-check hash/size, and pass exactly those bytes to the provider. It must not read the Bridge target path directly.

`- [ ]` **Step 4: Persist only bounded evidence**

Store raw provider output only as a redacted artifact reference. Store normalized report and user-visible summary as strict bounded events. Preserve deterministic validator status as the higher-priority decision.

`- [ ]` **Step 5: Add Evaluation delivery record**

Produce a bounded final record containing:

```text
brief/spec
changeset digest
approval
receipt
validation report
artifact refs/status
knowledge manifest
vision status/report
final decision
recovery evidence
```

`- [ ]` **Step 6: Run Vision/Evaluation tests**

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_vision_router.py tests/runtime/test_vision_contracts.py tests/runtime/test_runtime_e2e.py
uv run --frozen --extra eval pytest -q
```

Expected: all deterministic failure-precedence and artifact parity tests pass.

`- [ ]` **Step 7: Commit advisory Vision/Evaluation**

```powershell
git add eee_agent/vision eee_agent/runtime/service.py eee_agent/panel/runtime_state.py houdini_side/runtime_panel.py tests/runtime docs
git commit -m "feat: add advisory vision and delivery evaluation"
```

---

## Task 9: Complete GUI acceptance and release cleanup

**Branch:** `feature/runtime-gui-acceptance`

**Files:**
- Modify: `houdini_side/runtime_panel.py`
- Modify: `eee_agent/panel/runtime_state.py`
- Modify: `python_panels/EEEAgentRuntime.pypanel`
- Modify: `MainMenuCommon.xml`
- Modify: `houdini_side/README_INSTALL.md`
- Update: `README.md`、`CLAUDE.md`、all current handoffs.
- Test: real Houdini GUI manual checklist and all automated suites.

`- [ ]` **Step 1: Verify product-mode UI state reducer**

Automated tests must cover:

- MODEL/REVIEW/APPROVAL/APPLY/RESULT/RECOVERY;
- no second public Apply action;
- Details/Inspector visibility;
- artifact status;
- Knowledge status;
- reconnect;
- restart;
- empty session;
- Chinese IME-safe text editor state.

`- [ ]` **Step 2: Install the current Houdini package**

Run:

```powershell
& 'D:\houdini\bin\hython.exe' houdini_side\install_menu.py
```

Restart Houdini before GUI testing. Do not use the removed legacy menu actions.

`- [ ]` **Step 3: Execute the user GUI checklist**

Manually verify:

- dock and narrow layout;
- focus;
- Enter and Chinese IME;
- approval drawer/mouse;
- reconnect;
- Runtime restart;
- artifact details;
- error/recovery;
- empty-scene build;
- no accidental scene mutation;
- complete MODEL -> REVIEW -> Approve and build -> Result flow.

Record pass/fail/untested with screenshots only in machine-local temporary output.

`- [ ]` **Step 4: Synchronize documentation**

Update every current handoff and README with:

- actual HEAD;
- actual test counts;
- actual Golden Case count;
- sensitivity smoke status;
- Knowledge Graph integration status;
- MVP status by gate;
- GUI status;
- branch cleanup status;
- known limitations.

`- [ ]` **Step 5: Run the final automated gate**

```powershell
uv run --frozen --extra eval pytest -q
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
uv run --frozen --extra eval pytest -q tests/knowledge/test_hfs_contract.py
Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

Expected:

- Runtime and Knowledge full suites pass;
- HFS contract: 11 passed;
- no unexpected untracked files;
- all quality gates exit 0.

`- [ ]` **Step 6: Create release candidate tag**

After GUI and automated gates pass:

```powershell
git tag runtime-mvp-rc-2026-07-17
git push origin feature/runtime
git push origin runtime-mvp-rc-2026-07-17
```

`- [ ]` **Step 7: Clean merged local and remote branches**

Run only after verifying merged commits and tags:

```powershell
git branch --merged feature/runtime
git branch -r --merged feature/runtime
git branch -d feature/foundation feature/houdini-knowledge-graph
git push origin --delete feature/foundation
git push origin --delete feature/houdini-knowledge-graph
```

Keep:

```text
main
wip/pre-migration-main
feature/runtime
runtime-mvp-rc-2026-07-17
foundation-final-2026-07-17
knowledge-graph-final-2026-07-17
runtime-pre-mvp-2026-07-17
```

`- [ ]` **Step 8: Final release handoff**

Create `docs/handoffs/2026-07-17-runtime-mvp-release.md` with:

- release tag;
- exact test outputs;
- HFS smoke outputs;
- real provider status;
- GUI checklist;
- Knowledge manifest/build;
- artifact/recovery evidence;
- known non-goals;
- rollback tags;
- deleted branch names;
- remaining B2/asset/semantic search work.

Commit:

```powershell
git add README.md CLAUDE.md docs/handoffs docs/superpowers/reviews
git commit -m "docs: hand off runtime MVP release candidate"
```

---

## 计划自审清单

- S1 覆盖了 legacy raw-write 下线和 Runtime read-only tool 迁移，避免 read-only allowlist 继续依赖旧 rpyc。
- S2 覆盖了已复现的 ArtifactStore rollback bug、文件/数据库状态机、reconciliation、cleanup retry 和 panel status。
- S3 覆盖了真实 sensitivity wire smoke 及 stale/zero-write/restore/restart。
- S4 覆盖了 Knowledge Graph allowlist、shared cache、startup status、Run snapshot、Restricted Research Capability、联合测试和远端分支删除。
- S5 覆盖了真实 provider 和离线 Runtime MVP gates。
- S6 覆盖了 advisory-only Vision、artifact byte parity、redaction、unavailable evidence 和 deterministic failure precedence。
- S7 覆盖了 GUI、CI、文档、tag、远端 cleanup 和 release handoff。
- 计划没有使用 `TBD`、`TODO` 或未定义的功能名。
- `RuntimeToolContext` 是 S1 定义、S5 使用、S6 不越权的统一上下文接口。
- Knowledge cache 明确不属于 Run artifact；Artifact retention 不会删除 Knowledge cache。
- 远端分支删除仅发生在 S4 联合测试通过和 tag 保留之后。
