# 实施计划：四项独立问题修复（B → C → A → D）

调查阶段已完成并经证据验证。每个问题的根因和修复点都已锁定到具体文件行号。四项彼此独立，按"功能止血 → UI 改善 → 新功能"顺序推进，每项完成后单独提交。

---

## 阶段 0：共享前置 — schema 迁移到 v6（D 用）

D 要持久化 todos，`runs` 表需要加 `todos_json` 列。

**文件**：`eee_agent/runtime/migrations.py`
- `SCHEMA_VERSION = 5` → `6`
- 新增 `MIGRATION_V6_SQL`：`ALTER TABLE runs ADD COLUMN todos_json TEXT NULL`（additive，不破坏 v1-v5）
- 加入 `MIGRATIONS` 元组

---

## 阶段 B：执行链路（apply 错误不再静默 + prompt 修复）

### B-1：让 apply 失败原因进入 receipt（不再被吞）

**根因**：`houdini_side/changeset_executor.py:1382-1442` 捕获了 `write_error`，但在 1428-1442 构造失败 receipt 时**完全丢弃**。`ChangeReceipt` 数据结构（`eee_agent/changesets/contracts.py:1436`）本身没有 error 字段。结果：用户审批后 apply 失败，`applied_op_ids=[]`，没有任何错误信息传到 LLM，LLM 只能瞎猜"似乎仅部分应用"。

**改动**：
1. `eee_agent/changesets/contracts.py:1436-1448` `ChangeReceipt` 加两个可选字段：
   - `error_code: str | None = None`（短代码，如 `houdini.create_failed`、`precondition.unmet`）
   - `error_message: str | None = None`（≤500 字符，结构化文本）
   - `__post_init__` 加 `_require_optional_bounded_text` 校验
   - `to_dict()` 加这两个 key（仅 non-None 时输出）
   - **schema_version 不动**（仍是 1，DB 的 `payload_json` 是 JSON 文本字段，不需要 migration）
2. `houdini_side/changeset_executor.py:1418-1442` 失败分支构造 receipt 时传入从 `write_error` 提取的 `error_code`/`error_message`：
   - 新增 helper `_classify_exception(exc) -> tuple[str, str]`，把常见异常（`hou.OperationFailed`、`HoudiniAdapterError` 等）映射到短 code
   - 既给 write_error 分支，也给 postcondition 失败/reconciliation crash 分支使用
3. `eee_agent/runtime/service.py:1539-1540` `_build_modeling_context` 的 `except Exception: return None` 改为捕获后通过 `logging.exception` 记录再返回 None（不再完全静默）。

**测试**：
- `tests/changesets/test_contracts.py` 加 `test_change_receipt_error_fields_round_trip`
- `tests/houdini_side/test_changeset_executor.py` 加 `test_apply_failure_records_error_code`（mock createNode 抛 `hou.OperationFailed`，断言 receipt 的 `error_code` 非 None）
- 新增 pytest 临时目录 fixture，模拟当前 `ses_65fefe0a` 场景重放

### B-2：让错误信息从 receipt 流到事件流、再到 LLM

**改动**：
1. `eee_agent/changesets/service.py` 在 `changeset.rolled_back` / `changeset.applied` 事件的 payload 里带上 receipt 的 `error_code`/`error_message`（非 None 时）
2. `eee_agent/panel/runtime_state.py` `apply_event` 处理这两类事件时把 error 累积进 run 的某个新字段或 activity detail
3. `houdini_side/runtime_panel/inspector.py` 在 RUN tab 渲染时显示这个 error（阶段 A 会重写整个 inspector，这里只是过渡）

### B-3：system_prompt 补 propose_modeling + 错误反馈说明

**文件**：`eee_agent/system_prompt.py:13-50`

**改动**：在 `BASE_PROMPT` 里：
1. 把工具清单（line 19）从 `scene_status, query_scene, inspect_workspace, geometry_stats, and work_status` 改为包含 `propose_modeling`，并说明这是**唯一**的建模写入路径
2. 在 `READ-ONLY WORKFLOW` 里加一条 `PROPOSE MODELING` 步骤，说明调用 `propose_modeling` 的时机
3. 在 `EVIDENCE AND FAILURE HANDLING` 里加一条：审批后的 apply 结果会以 `changeset.applied` / `changeset.rolled_back` 形式返回，**必须读 receipt 的 error_code/error_message**，不要凭"根容器存在"推断 apply 成功

**测试**：`tests/runtime/test_system_prompt.py`（如不存在则新建）断言 prompt 文本包含 `propose_modeling` 和 "rolled_back"。

---

## 阶段 C：时间线历史渲染（前端单文件）

**根因**：`houdini_side/runtime_panel/main_window.py:218-307`
1. `_on_session:218-234` 每次 session 切换 / 首次激活都 `conversation.clear_items()` + 只塞一张 "Switched to session X" 通知
2. `_maybe_render_output:276-307` 只把 `selected_run`（active 或 `_run_order[-1]`）的 output 渲染成一张 assistant 卡
3. **历史 runs（含 `user_input` + `final_response`）在 `snapshot["runs"]` 里，但前端从不迭代**

**改动**（只动 `main_window.py`）：
1. 新增 method `_replay_history(snapshot)`：从 `snapshot["runs"]`（已经按时间顺序）迭代，对每个 run append `view_models.user_message(run["user_input"])` + `view_models.assistant_message(run["final_response"])`。跳过 active run（它会走流式路径）。
2. `_on_session:218-234`：切 session 后调用 `_replay_history`。但 `_on_session` 只拿到 `session_id/title/cursor`，没有 snapshot —— 所以改为：设置一个 `self._history_needs_replay = True` 标记，在下次 `_on_snapshot` 时如果标记为真则 `_replay_history(snapshot)` 然后清标记。
3. 首次连接场景（`_current_session_id == ""`）：也在下次 `_on_snapshot` 时 replay。
4. 保留流式路径不变：active run 仍走 `update_streaming` → `replace_last_assistant`。

**关键边界**：
- 不能每次 snapshot 都 replay（snapshot 频繁到达），必须只在 session 切换或首次加载时 replay 一次
- replay 必须跳过 active run，避免和流式 card 重复
- MAX_MESSAGES = 200 限制仍然适用（`append_bounded` 已经处理）

**测试**：`tests/runtime_panel/test_main_window_history.py`（新建）
- `test_session_switch_replays_history_from_snapshot_runs`
- `test_replay_skips_active_run_to_avoid_duplicate_with_stream`
- `test_replay_not_run_on_every_snapshot`

---

## 阶段 A：Run / Workspace / Artifacts 真 UI

**根因**：`houdini_side/runtime_panel/inspector.py` 整个文件三个 tab 都是 `QPlainTextEdit` + `"\n".join(rows)`。

**改动**（重写 `inspector.py`，保留对外接口）：
1. **保留信号** `createWorkspaceRequested`、`inspectWorkspaceRequested` 和方法签名 `set_run_snapshot`、`set_workspace_facts`、`render_artifacts`、`render_visions`
2. **RUN tab 重构为 `QFrame#Card` 容器**：
   - Header：status badge（`QLabel`，背景用 `STATUS_OK/WARN/ERROR` 根据 status 着色）+ run_id 短码（`DimLabel`）
   - 时间区：started / finished / duration（三栏 `ProminentLabel` + `DimLabel`）
   - 模型区：展开/折叠的 `ThinkingBlock`，内部 `QVBoxLayout` 显示 dependencies 表（`QTableWidget`）和 environment（platform/python/eee_agent 版本），不再 str() dict
   - 活动区：activity 步骤列表（每个 `tool.started`/`tool.completed` 一行），不再只显示最后一条
   - 错误区（B-1 给的字段）：若 `error_code` 非 None，显示红色卡片
3. **WORKSPACE tab**：`QTableWidget`（key/value 两列）替换 `setPlainText`，Create workspace / Inspect 按钮保留
4. **ARTIFACTS tab**：`QTreeWidget`，artifacts 和 vision evaluations 分组，每项状态用 `STATUS_OK/WARN/ERROR` 着色
5. **layout**：每个 tab 改为 `QScrollArea` 包内容 widget，支持长内容滚动

**关键约束**（沿用项目既有风格）：
- 不引入新的 Qt 组件库（项目只用 PySide6 + 自家 theme）
- 所有颜色用 `theme.py` token，不在 inspector.py 出现 hex literal
- 新增的 `MessageItem` 子类型 / widget 放在 `view_models.py` 保持 Qt-free 可测试
- 新 widget 的 objectName 用 theme.py 已有的 `Card`/`ProminentLabel`/`DimLabel`/`ThinkingBlock`/`ThinkingToggle` 钩子

**测试**：
- `tests/runtime_panel/test_inspector_run_view.py`（Qt 可用时跑，否则用 view_models 层单测）
- 重点测：dict 不被 str()、status badge 颜色映射、error 区在 error_code=None 时不显示

---

## 阶段 D：Todo 数据流 + UI

**好消息**：deepagents 的 `TodoListMiddleware` **已经启用**（`harness.py` 没排除），工具 `write_todos` 已经可用，LLM 已经能调用。问题只是 `agent_runner.py` 把流里的 todos 数据丢弃了。

**改动**：

### D-1：后端捕获 todos 并发事件

**文件**：`eee_agent/runtime/agent_runner.py:198-208`

`updates` 分支当前只读 `upd.get("messages")`，忽略 `todos` key。改为：
```python
todos = upd.get("todos")
if isinstance(todos, list):
    yield RunnerEvent(
        event_type="todos.updated",
        payload={"todos": _normalize_todos(todos)},
        retention_class=RetentionClass.OPERATIONAL,  # 流式更新，不持久
    )
```
加 `_normalize_todos` helper 校验 `[{content, status}]` 结构。

### D-2：todos 持久化（让 LLM 在后续 run 看到上一轮的 todo）

**文件**：
- `eee_agent/runtime/runs.py` `RunRepository.create_and_acquire` 和 `transition`：读写 `todos_json` 列
- `eee_agent/runtime/models.py:224-248` `RunRecord` 加 `todos: tuple[dict, ...] = ()` 字段
- `eee_agent/runtime/events.py` `snapshot_data` 在 run dict 里输出 todos
- `_run` 启动新 run 时，从上一个 run 的 todos 继承初始 todos（deepagents 的 `PlanningState.todos`）

### D-3：前端捕获并渲染

**文件**：
1. `eee_agent/panel/runtime_state.py:419-456` `apply_event` 加分支：`event_type == "todos.updated"` 时更新 `self._todos` 列表
2. `snapshot()` 输出 `todos` 字段
3. `houdini_side/runtime_panel/view_models.py` 新增 `TodoList` widget 模型 + `todo_list_card(todos)` factory
4. `houdini_side/runtime_panel/conversation.py`（或新文件 `todo_panel.py`）加 `TodoListView` widget：水平/垂直条状的 todo 进度条，每项显示 `□/◉/✓ pending/in_progress/completed`
5. `main_window.py` 在 conversation 上方或 inspector 加一个 todo 显示区，订阅 snapshot.todos

**测试**：
- `tests/runtime/test_agent_runner.py` 加 `test_todos_updated_event_emitted_from_updates_mode`
- `tests/panel/test_runtime_state.py` 加 `test_todos_updated_event_updates_snapshot_todos`
- `tests/runtime_panel/test_todo_view.py`（view_models 层）

---

## 执行顺序与提交策略

每阶段一次独立 commit，方便回滚和 code review：

1. **阶段 0**（schema 迁移） → commit `chore: bump schema to v6, add runs.todos_json`
2. **阶段 B-1**（receipt error 字段） → commit `fix: capture apply write_error into ChangeReceipt instead of dropping it`
3. **阶段 B-2**（事件流透传 error） → commit `fix: surface apply error_code/message through changeset events`
4. **阶段 B-3**（system prompt） → commit `fix: document propose_modeling + rolled_back receipt reading in BASE_PROMPT`
5. **阶段 C**（时间线历史） → commit `fix: replay session run history from snapshot instead of showing only the last reply`
6. **阶段 A**（inspector 真 UI） → commit `feat: structured Run/Workspace/Artifacts inspector replacing key:value text dump`
7. **阶段 D-1/D-2/D-3**（todo 全链路） → 拆 2-3 个 commit：`feat(runtime): emit todos.updated events from deepagents stream` / `feat(persistence): carry run todos across runs` / `feat(ui): TodoList widget showing live task progress`

每个 commit 都跑现有测试套件（`pytest tests/`）确认没破坏。

---

## 风险与未决事项

1. **B-1 的 receipt error 字段是可选字段**，不破坏既有客户端解析。但如果 `eee_agent/changesets/contracts.py` 的 `_validate_receipt_state` 有严格状态校验，需要确认 RolledBack+error 的组合不被拒。这点在阶段 B-1 开始时会先读这个 validator 再动。
2. **阶段 A 的 widget 重写量较大**（~300 行 inspector + 测试），如果时间紧可以降级为：保留 `QPlainTextEdit` 但用 `json.dumps(..., indent=2)` 格式化 model_snapshot，最少代码止血。但既然要做就一次做对。
3. **阶段 D-2 的"从上一 run 继承 todos"** 需要确认 deepagents `PlanningState` 怎么接受初始 todos —— 这部分需要在实现时读 `langchain/agents/middleware/todo.py` 确认入口，可能需要往 graph 初始 state 注入。如果复杂，D-2 可以先跳过，只做 D-1 + D-3（每轮 run 的 todo 仍然实时显示，只是不跨 run 持久）。

---

## 完成后的预期效果

- **B**：下次用户审批一个会失败的 changeset 时，LLM 能看到 `error_code: houdini.create_failed` / `error_message: "Node type 'copytopoints::2.0' not found in context geo"`（或类似），并给出准确诊断和重新提案，而不是误描述"似乎仅部分应用"。
- **C**：切 session / 重连后，时间线显示完整历史对话（用户问 + 助答成对），而不是只有最新一条。
- **A**：右侧 Run 页面是结构化卡片（status badge、模型信息表、活动步骤列表），不再是 `key: value` 文本堆。
- **D**：实时显示 agent 的 todo 进度（□/◉/✓），用户能看到 agent 当前在哪一步。

---

按此顺序执行。如有任何阶段你想跳过/调整/重排，告诉我。