# 节点生命周期管理与任务图谱设计

- 状态：设计稿（待实现）
- 日期：2026-07-24
- 范围：commit 时自动布局 + 自动设置 display/render flag + 场景注释；任务图谱（记录每一步创建的节点及用途）；`cleanup_nodes` 废弃节点清理工具；面板极简任务视图
- 来源需求：用户提出四个问题：①agent 建完节点树不排版；②不把最终节点设为 display flag；③试错产生的废弃节点不清理；④希望有"任务图谱"记录每步建了哪些节点、作用是什么，每步执行完更新，帮助 agent 理解任务状态，并支撑清理与注释
- 相关文档：`docs/handoffs/2026-07-24-sandbox-verify-commit-handoff.md`、`docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md`、`CLAUDE.md`

## 1. 摘要

当前主流程是 sandbox → verify → commit：`scratch_build` 在 `/obj/eee_scratch_<run>` 沙盒内建节点，`scratch_commit` 过 gates 后提升进正式场景。代码核实确认三个缺口：

1. 全库无任何 `setPosition` / `moveToGoodPosition` 调用——节点全靠 Houdini 默认落位。
2. 执行器只**读** display flag（`houdini_side/changeset_executor.py` 的 `_scratch_output_node`），从不**设置**。
3. 无单节点删除操作；唯一清理是整个沙盒销毁（`scratch_destroy`）。scratch 提交节点刻意不带 ownership mirror（`scratch.py` 头部注释），事后无法追溯归属与用途。

本设计（用户已选定的方案 A）：

- **commit 时确定性收尾**：布局、display flag、注释由执行器在 `scratch_commit` 提升阶段自动完成，best-effort，不依赖 LLM 记得调用工具，离线 fake scene 可测。
- **任务图谱**：runtime SQLite 新增两张表，记录每一步的 purpose 与涉及节点；`scratch_build` 增加必填 `purpose` 参数；每步落库后把压缩摘要注入 LLM 上下文。
- **清理靠工具**：新增 `cleanup_nodes` 两阶段工具（先建议、后执行），以图谱做归属判断 + 场景实况拓扑做安全性判断。
- **面板极简版**：只读文本区块显示当前步骤清单。

**用户已确认的决策：**
- 触发方式 = 混合：布局 + display flag 在 commit 时自动；清理由 agent 通过工具显式执行。
- 图谱用途 = 全部四项：注入 LLM 上下文、写入 Houdini 场景注释、面板可视化、支撑清理工具。
- 面板 = 本轮只做极简只读文本视图，图形化后续单独一轮。
- 架构 = 方案 A（commit 时确定性收尾 + runtime SQLite 任务图谱）。不采用 events 表派生（方案 B）与复用 knowledge 图存储（方案 C）。

## 2. 背景与现有基线（已通过代码核实）

### 2.1 写入路径

`scratch_build` 工具（`eee_agent/modeling/scratch_coordinator.py:335`）→ `ScratchCoordinator.build` → `ScratchProvider.scratch_exec` 协议 → `BridgeChangeSetProvider.scratch_exec`（`eee_agent/houdini_bridge/changeset_provider.py:214`）→ `BridgeClient.scratch_exec`（`eee_agent/houdini_bridge/client.py:808`）→ socket → `SecureBridgeServer._serve` 分发（`houdini_side/secure_bridge.py:851`）→ `ChangeSetExecutor.scratch_exec`（`houdini_side/changeset_executor.py:2031`）。

scratch op 词表（`eee_agent/houdini_bridge/scratch.py:88`）：`{"create_node", "set_parm", "connect"}`，在单个 `hou.undos.group` 内执行。

### 2.2 工具注册

- 只读工具：`eee_agent/runtime/agent_tools.py`，`build_read_only_tools()`（:411）。
- 建模工具：`scratch_build` / `scratch_commit`（`scratch_coordinator.py:335,440`）。
- 图组装：`eee_agent/runtime/agent_runner.py:348-356`，上下文经冻结 dataclass `RuntimeToolContext`（`eee_agent/runtime/agent_context.py:54-67`）注入。
- 新增桥接能力的全链路：DTO（`scratch.py`）→ client 方法 → provider 方法 → `secure_bridge.py:_serve` 分发臂 + capability 常量 → 执行器方法。

### 2.3 状态与存储

- runtime SQLite（`eee_agent/runtime/migrations.py`）已有表：sessions、runs、events、workspaces、changesets、approvals、change_receipts、session_workspace_state、artifacts。
- `agent_runner.py` 已逐步发出 `tool.started` / `tool.completed` 事件（流水账，不含 op 级语义）。
- `ScratchCoordinator` 目前显式"无状态"（`scratch_coordinator.py:115-121` 注释）。

### 2.4 约束与不变量（来自 CLAUDE.md 与既有代码）

- 单 FIFO 主线程队列；无跨边界裸 HOM/RPC；loopback + token 认证；协议固定 `eee.bridge/1`，新面为追加 capability。
- 变更在单个 `hou.undos.group`（标签 `"EEE Agent - ..."`）；终态清理在 `hou.undos.disabler()` 下。
- DTO 严格性：frozen slotted dataclass、精确字段集、canonical JSON、有界字符串。
- 离线测试注入 hou-free fake scene，执行器模块级不 import `hou`。
- 只读 inspector 测试把 `setDisplayFlag`/`setRenderFlag` 列为禁止变更（`tests/runtime/test_workspace_bridge_inspector.py:347-348`）——该约束针对只读 inspector 工具；本设计在 commit 写路径设置 flag，需澄清该白名单的适用范围注释。

## 3. 设计

### 3.1 任务图谱存储（新模块 `eee_agent/runtime/task_graph.py`）

`migrations.py` 新增一版迁移，建两张表：

**`task_steps`**：一行 = 一次工具调用步骤。

| 列 | 说明 |
|---|---|
| `id` | INTEGER PK |
| `run_id` | 所属 run |
| `seq` | run 内步骤序号 |
| `tool` | `scratch_build` / `scratch_commit` / `cleanup_nodes` |
| `purpose` | LLM 提供的步骤用途（≤200 字符） |
| `status` | `open`（沙盒中）/ `committed`（已提交）/ `deleted`（其全部节点已被清理） |
| `created_at` | 时间戳 |

**`task_nodes`**：一行 = 一个 agent 创建/编辑过的节点。

| 列 | 说明 |
|---|---|
| `id` | INTEGER PK |
| `run_id` | 所属 run |
| `step_id` | FK → task_steps |
| `node_path` | 创建时路径（沙盒内） |
| `committed_path` | 提交后路径，未提交为 NULL |
| `node_type` | 节点类型 |
| `note` | 节点级用途，可空 |
| `status` | `sandbox` / `committed` / `deleted` |

关键取舍：**不存连线边**。节点拓扑以 Houdini 场景实况为准（清理时经桥接只读查询现查），图谱只存场景答不上来的：用途、步骤归属、生命周期状态。图谱因此永远不会与场景脱节。

模块职责：store（增/查/状态迁移）+ 摘要渲染（供上下文注入与面板共用）。遵循现有 repository 模式（参考 `runtime/sessions.py`、`changesets/repository.py`）。

### 3.2 `scratch_build` 语义扩展 + 上下文注入

- 工具参数新增**必填** `purpose: str`（≤200 字符）；`create_node` op 新增可选 `note: str`（≤200 字符）。`scratch.py` DTO 相应扩展，沿用 frozen dataclass + canonical JSON 风格。
- `ScratchCoordinator` 改为持有 task store 句柄：`scratch_exec` 成功后写入 step + 节点（状态 `sandbox`）；`scratch_commit` 成功后更新 step 状态为 `committed`、节点填 `committed_path` 并置 `committed`。仅做记录，不做判断。
- `agent_runner` 组装 LLM 上下文时注入压缩任务摘要，形如：

```
Task state (run abc123):
1. [committed] 创建四条桌腿 (leg1..leg4: tube, xform)
2. [sandbox] 桌面建模 (tabletop: box1, blast1)
Current output: /obj/geo1/tabletop/blast1
```

- 截断策略：仅最近 10 步 + 未清理节点计数，整体 ≤2KB（沿用工具输出 byte-cap 风格）。

### 3.3 commit 时自动收尾（执行器侧）

在 `scratch_commit` 提升节点的现有 undo group（`changeset_executor.py:2266-2286` 区域）内追加三步，**全部 best-effort**：单项失败收集为 warning 写入 commit 结果，不让布局问题毁掉一次合格提交：

1. **布局**：仅对本次提交的节点集合做拓扑分层布局。按输入连线计算深度（无输入=第 0 列），列 = 深度、同列按行排列，固定列宽/行高常量，`setPosition` 落位；整体平移到节点默认位置的左上角锚点。**不动该集合之外的任何节点**。纯 Python 计算，fake scene 可测。
2. **display flag**：用现有 `_scratch_output_node` 解析输出节点，`setDisplayFlag(True)`；SOP 上下文同时 `setRenderFlag(True)`。
3. **注释**：`scratch_commit` 负载新增可选 `annotations: {节点名: 注释}`（canonical JSON、值 ≤500 字符）。执行器对每个提交节点 `setComment()` 并打开注释显示 flag。注释内容由 runtime 从 task store 填充（节点 note，缺省回退步骤 purpose）。

### 3.4 `cleanup_nodes` 工具（两阶段，安全优先）

- **阶段一（建议）**：`cleanup_nodes()` 不带 paths 调用 → runtime 取图谱中本会话 `status != deleted` 的节点，经桥接只读查询做拓扑分析（不在当前输出链路、无下游引用、未连线），返回候选清单及原因，不删任何东西。
- **阶段二（执行）**：`cleanup_nodes(paths=[...])` → 逐个校验：在图谱中、属本会话、仍不在输出链路 → 桥接删除。沙盒内节点走新增 `delete_node` op（加入 `scratch.py` `_OP_KINDS`）；已提交节点走执行器新方法 `delete_nodes`，路径白名单由 runtime 下发，Houdini 侧不自行决定删什么。删除在 `"EEE Agent - cleanup"` undo group 内，可撤销。成功后图谱状态置 `deleted`。
- 桥接新增 capability（如 `scratch.v2` 或独立 `cleanup.v1`），握手时宣告，沿用追加式 capability 惯例。

### 3.5 面板极简视图

- 新增只读工具 `task_graph_status`（LLM 亦可调用），返回当前 run 的步骤 JSON：seq、purpose、节点数、status。
- 面板（`houdini_side/runtime_panel/`）加一个纯文本只读区块，轮询同一 provider 函数显示步骤清单。无图形化、无交互编辑。

### 3.6 错误处理

- commit 收尾（布局/flag/注释）best-effort：失败 → warning 列表进 commit 结果，提交本身不回滚。
- `cleanup_nodes` fail-closed：任一校验失败 → 该节点跳过并在结果中说明原因；不部分静默。
- task store 写入失败不阻断建模流程：记录 warning 事件，图谱降级为"本 run 不可用"，上下文注入跳过摘要。

## 4. 测试

- **离线（fake scene）**：
  - 执行器：布局坐标确定性（分层、锚点、不影响集合外节点）、display/render flag、注释写入、best-effort warning 路径、`delete_nodes` 白名单与 undo group。
  - DTO：`scratch.py` 新增字段（purpose/note/annotations）严格性、canonical JSON、超长截断/拒绝。
  - store：迁移幂等、step/node 写入与状态迁移、摘要渲染与 10 步/2KB 截断。
  - coordinator：build/commit 成功后落库、store 失败降级。
  - `cleanup_nodes` 两阶段：候选生成（mock 拓扑查询）、校验拒绝（非本会话/在输出链路/已删除）。
- **hython smoke**（手动运行，沿用既有 smoke 模式）：建若干节点 → commit → 断言位置非默认且分层、display flag 已置、comment 已写；再建孤儿节点 → `cleanup_nodes` 建议命中 → 执行删除。
- 全量离线 gate 保持绿（当前基线 3458 passed, 11 skipped）。

## 5. 非目标（本轮不做）

- 面板图形化图谱视图（节点-边图渲染、交互编辑）。
- 跨 run / 跨会话的任务图谱查询与长期演化分析。
- 图谱存连线边（拓扑以场景实况为准）。
- 对 legacy typed-changeset 路径（`node.create` 等三效应）做同样收尾——该路径已由 WorkspaceManifest 覆盖归属，不在本轮范围。
- 布局算法的美化迭代（避免交叉线、子网对齐等）——本轮只做确定性分层。

## 6. 文档与收尾

- 更新 `CLAUDE.md`：新工具、新 capability、迁移版本、inspector 白名单适用范围澄清。
- 完成后写 handoff 文档（`docs/handoffs/`）。
- 若实现中模块名/函数名与本设计稿有出入，以代码为准并在本文档补"实现注记"。
## 7. Implementation notes

1. Cleanup uses a dedicated `scratch.v2` capability with `scratch.delete` and
   `scratch.topology`; topology remains read-only and deletion is allowlisted by
   the Runtime task graph.
2. `purpose` is required on the scratch wire DTO; annotations are bounded
   sorted pairs and commit results include bounded `warnings`.
3. Commit finalization uses deterministic layered layout anchored to the
   pre-finalization block and is best-effort.
4. Cleanup first suggests leaf/missing candidates and executes only an explicit
   second call after fresh topology validation.
5. Task-store writes degrade for the current run on the first persistence
   failure; modeling remains available.
6. Task summaries are injected through async middleware and omitted when no
   graph exists or the store is unavailable.
7. The panel task graph is intentionally a plain read-only text block.
8. Hython acceptance found deferred SOP display/render flag observation after
   commit return; this remains an open follow-up rather than an assumed pass.
