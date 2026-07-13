# Houdini 通用 Agent 架构设计

- 状态：已批准
- 日期：2026-07-13
- 范围：总体架构与分阶段交付边界
- 首个能力：严格程序化 SOP 建模
- Agent 框架：LangChain Deep Agents + LangGraph
- Houdini 基线：21.0.440

## 1. 摘要

本项目的目标不是做一个可以随意执行 Houdini Python 的聊天面板，而是做一个在 Houdini 内运行、可恢复、可审计、可扩展的通用 Agent。首个交付能力是高质量、严格程序化、参数化、组件化的 SOP 模型生成，后续再增加材质、Solaris、缓存、渲染等能力。

系统采用本地模块化单体架构：每个 Houdini 实例对应一个独立 Runtime，Runtime 管理会话、运行、模型、持久化、审批、事件流与 capability subgraph；Houdini 内的 dockable Python Panel 只是可关闭、可重连的客户端。Agent 可以读取整个 HIP，但默认只能写入显式标记的 owned workspace。任何既有网络修改都必须形成可预览、可审批、可回滚的 typed ChangeSet，并且只有确定性的 TransactionalExecutor 可以写场景。

建模结果采用硬门禁交付：结构、cook、几何、参数敏感性、语义与 artifact validator 均通过后才允许交付。视觉检查能够触发修复，但不会覆盖确定性失败；没有可用视觉模型时，用户可以按运行跳过视觉 LLM 检查，建模仍可使用。

Phoenix、LangSmith 和任何云部署都不是产品运行依赖。OTel/OpenInference 可发送到本地 Phoenix；只有用户明确标记且清洗后的离线评估才可导出到 LangSmith。

## 2. 当前基线与主要问题

当前代码已经具备以下基础：

- `eee_agent/app.py` 能构建 Deep Agent。
- Houdini 侧已有 RPC 启动、聊天窗口、Phoenix 启动入口和较丰富的运行事件显示。
- 工具层已具备场景读取、节点创建、组件端口、VEX 和程序化建模相关能力。
- `output/table.hip` 已证明组件端口、根参数和参数驱动几何能够工作。
- `eval/` 已有 OBJ 计数和包围盒断言的最小评估脚手架。

当前架构仍属于可工作的原型，主要缺口是：

- `cli.py` 每回合只向 graph 发送新消息，没有持久 checkpointer 和可靠的 `thread_id`，UI 的连续对话外观不等于 Agent 真正保留了多轮状态。
- `houdini_side/chat_panel.py` 同时负责窗口、子进程、协议、状态和渲染，难以继续增加多会话、审批、断线恢复和 artifact 检查。
- 裸 `hrpyc` 默认开放固定端口、无认证，并向远端代理完整 `hou` 模块，不适合作为生产边界。
- Supervisor 和工具缺少严格的读写分离，模型仍可能直接驱动低层场景写操作。
- 当前 DeepSeek 通过 `ChatOpenAI(base_url=...)` 接入，会丢失非标准 `reasoning_content`；DeepSeek V4 工具调用回合要求保留并回传推理块。
- Deep Agents 版本范围过宽，当前版本还会自动加入继承工具的 `general-purpose` subagent，形成未显式声明的能力面。
- 依赖没有 lock，除 `rpyc==4.1.0` 外大多是开放下界，运行行为会随安装时间漂移。
- 现有评估只覆盖少量导出几何事实，没有 graph contract、参数组合、语义、视觉和事务恢复验证。
- 当前桌腿的 perimeter 分布在非正方形桌面上不对称，说明“几何非空”不足以代表高质量程序化结果。
- 环境探测脚本假设 `/d/houdini`，当前 Bash 映射实际为 `/mnt/d/houdini`，会产生错误诊断。

## 3. 目标、非目标与不变量

### 3.1 目标

1. 生成高质量、严格程序化、参数化、组件化的 SOP 模型。
2. 初始覆盖桌、柜、楼梯、建筑构件、机械支架、管路等硬表面和建筑类资产。
3. 提供真实多会话、运行恢复、运行时思考展示、工具与子 Agent 状态、审批和 artifact 检查。
4. 支持主模型和独立视觉模型，并允许按 session 或下一次 run 切换。
5. 让建模成为独立 capability，而不是散落在全局 prompt 或 middleware 中的隐含行为。
6. 允许未来 capability 在同一 workspace 中跨 `/obj`、`/mat`、`/stage` 等 context 工作，同时保持最小写权限。
7. 在本地、离线观测关闭、LangSmith 关闭的情况下仍完整工作。

### 3.2 首个生产范围的非目标

- 有机角色、雕刻、毛发、FX、仿真、材质、灯光和最终渲染不是首个建模 capability 的交付范围。
- 不提供默认全 HIP 写权限。
- 不自动 load、clear、save、重命名或覆盖用户 HIP。
- 不允许模型直接执行任意 Python、shell、HOM 或未声明的文件写入。
- 不以微服务、远程 LangGraph Server 或 LangSmith Deployment 作为首个本地产品架构。
- 不在首个版本实现多用户协作、远程 Houdini 农场或组织级 RBAC。

### 3.3 不可破坏的不变量

- 实际 Houdini graph 是场景事实源；spec 是可追踪、可校验、可重建的设计计划。
- Capability 表达领域功能；middleware 只处理重试、限额、上下文、脱敏、遥测等横切关注点。
- Supervisor 和规划 subagent 没有低层 Houdini 写工具。
- TransactionalExecutor 是唯一场景写入口。
- 每个场景副作用都有 typed effect、`change_id`、权限判断、幂等语义和审计记录。
- 确定性硬门禁失败时不交付结果，视觉评分和总分不能抵消失败。
- 用户明确跳过视觉检查时不阻断建模；跳过只对当前 run 生效。
- 模型、prompt、spec、validator、QualityProfile、Houdini 和依赖版本都进入冻结的 run snapshot。

## 4. 总体架构

采用模块化单体 Runtime + capability subgraph：

```text
Houdini 21
  ├─ Dockable Python Panel
  │    └─ Runtime WebSocket Client
  └─ Restricted HoudiniBridge
       ├─ scene query
       ├─ typed ChangeSet apply
       ├─ validation
       └─ capture

Local Runtime Process
  ├─ Session / Run Service
  ├─ LangGraph Runtime Graph
  │    ├─ Deep Agent Supervisor
  │    └─ Capability Registry
  │         └─ Modeling Capability Subgraph
  ├─ Provider Registry / Router
  ├─ Policy / Approval Service
  ├─ Event Store / Artifact Store
  ├─ Recovery Coordinator
  └─ Optional OTel exporters
```

建议的代码边界：

```text
eee_agent/
  core/
    ids.py
    errors.py
    events.py
    artifacts.py
    schemas.py
  runtime/
    service.py
    graph.py
    sessions.py
    runs.py
    event_store.py
    recovery.py
    protocol/
  capabilities/
    registry.py
    modeling/
      graph.py
      state.py
      contracts.py
      planners/
      compiler/
      validators/
      quality_profiles/
  integrations/
    houdini/
      contracts.py
      scene_index.py
      workspace.py
      policy.py
      changeset.py
      executor.py
      capture.py
  providers/
    contracts.py
    registry.py
    router.py
    events.py
    structured.py
    contract_tests.py
    deepseek_v4.py
    anthropic.py
    openai.py
  observability/
    otel.py
    phoenix.py
    langsmith_export.py

houdini_side/
  bridge/
  panel/
    panel.py
    controller.py
    runtime_client.py
    models.py
    widgets/
    renderers/
    styles.py
```

依赖方向固定为：capability 依赖 `core` 与抽象 service；provider、Houdini、SQLite、Qt、Phoenix 和 LangSmith 都属于外层 adapter。Capability 不导入 Qt、`hou` 或具体模型 SDK，Panel 不导入 Agent graph，Supervisor 不导入 Executor 实现。

## 5. Agent 编排与 Capability

顶层 Runtime graph 由项目自身控制，不把整个产品生命周期交给一个自由 Agent。Deep Agent Supervisor 负责理解请求、必要澄清、任务分类、capability 选择和最终汇总。它只能使用场景读取、任务路由、受限研究和 capability dispatch 工具。

Modeling capability 是编译后的 LangGraph subgraph。复杂的 Architecture Planner、Component Designer 和 Design Auditor 可以使用 Deep Agent subagent，但 subagent 只接收完成任务所需的 scene summary、skill 和 schema，不继承场景写工具。

Deep Agents 当前会自动增加 `general-purpose` subagent。实现必须使用当前版本提供的显式开关关闭它，或用同名、受限工具集和明确输出 schema 的 subagent 覆盖。依赖升级时要用 contract test 验证默认 middleware、默认 subagent 和工具继承没有改变。

项目使用 Deep Agents 原生 skills 做渐进式上下文披露，不再把全部 Houdini 知识拼进一个全局 system prompt。Core skill 只包含全局运行约束；建模 skill 按 Architecture Planner、Component Designer、Compiler/Auditor 的职责显式分配。Skill 只能提供说明和模板，不能增加工具、扩大 permission 或绕过 Policy。每个 Run snapshot 记录实际加载 skill 的路径、版本和内容 hash。

首个本地生产版本不向 Agent 暴露 host `LocalShellBackend` 或任意 filesystem backend。Scratch state 使用 thread-scoped backend；持久 artifact 只能通过项目自己的 ArtifactService 访问，从而复用同一套路径白名单、hash、retention 和审计规则。

建模主流程：

```text
Task Router
  -> Scene Context Builder
  -> Modeling Brief Builder
  -> Architecture Planner
  -> Component Designers (可并行，只读)
  -> Procedural Spec Validator
  -> Design Auditor
  -> Compiler
  -> Transactional Executor (staging)
  -> Quality Pipeline
  -> Commit | Repair | Rollback
```

并行只允许出现在无副作用规划和评估中。每个 Houdini 实例同一时间只有一个 active top-level run，所有 HoudiniBridge 请求串行；这避免多个 Agent 同时改变同一 scene revision。

## 6. 严格程序化建模模型

### 6.1 ModelingBrief

`ModelingBrief` 是用户意图转为可验证工程要求的边界，至少包含：

- 资产族和用途。
- 尺寸、单位、轴向和原点约定。
- 必须组件、可选组件和装配关系。
- 用户可调参数、范围、默认值与预期影响。
- 拓扑、封闭性、对称、厚度、间距、净空等约束。
- 输出、质量 profile 和视觉要求。
- 明确假设与尚未确认但允许采用的默认值。

### 6.2 ProceduralSpec 与 ComponentSpec

`ProceduralSpec` 描述根参数、组件 DAG、共享约束、输出、质量 profile 和编译目标。每个 `ComponentSpec` 包含稳定 ID、职责、输入/输出 port、anchor、参数依赖、几何策略、局部不变量和允许的实现节点类型。

组件必须通过真实连线和类型化端口组合，不能仅靠名称宣称“组件化”。端口至少区分 geometry、anchor、parameter/reference 和 metadata；编译器验证类型、方向、基数和循环依赖。根参数必须通过表达式或显式映射驱动组件，不允许存在展示在 UI 但对结果无影响的假参数。

Wrangle 或 Python SOP 可以是 owned workspace 中的局部实现节点，但不能成为吞掉整个资产架构的单个不透明脚本。其输入、输出、参数、源码、hash 和预期属性必须进入 spec 和 artifact；源码需要静态检查、cook 检查和参数采样。

### 6.3 Spec 与真实 Graph 对账

Compiler 先把 spec 编译为 typed ChangeSet，Executor 在 staging 应用，随后 Reconciler 从 Houdini 重新读取节点、参数、连接、flags、userData 和输出事实。验证器只信任读取结果，不信任 Compiler 自报成功。实际 graph 与 spec 不一致时，必须修复或失败，不能仅更新 spec 掩盖场景偏差。

每个修复阶段默认最多两次。RepairTicket 指向具体 validator、证据、失败参数样本和最近可重放边界；禁止无上限的 reset/rebuild 循环。

## 7. Session、HIP 与 Workspace

### 7.1 Session 与 Run

Session 是多轮对话、HIP 绑定和 active workspace 的持久容器；Run 是一次用户回合。一个 Session 可包含多个 Run，但一个 Houdini 实例只有一个 active top-level Run。Panel 关闭不会删除 Session 或终止 Runtime。

Panel 在有 active Run 时关闭，必须给出三个动作：

1. 继续后台并关闭。
2. 中止任务并关闭。
3. 取消关闭。

无 active Run 时正常关闭，不弹出额外提示。

### 7.2 SceneBinding

```text
SceneBinding
- houdini_instance_id
- scene_epoch
- canonical_hip_path | unsaved
- workspace_id
- observed_revision
```

Houdini `AfterLoad` 或 `AfterClear` 使 `scene_epoch` 递增。Active Run 随即停止写入并进入 `BlockedSceneChanged`。已经完成的规划可以作为参考保留，但恢复前必须重新读取场景、重新验证 spec 和生成新 ChangeSet。

Save 或 Save As 只更新当前显示路径，不改变内存场景 epoch。未保存场景在 UI 显示 `Untitled · Ephemeral`；进程结束后不能假定可以仅凭旧路径恢复。

### 7.3 WorkspaceManifest

Workspace 是逻辑所有权集合，不是固定的单个 `/obj` 节点。Manifest 保存在应用 DB，并在 owned 根节点的持久 userData 中镜像最小身份：

```text
eee.workspace_id
eee.node_id
eee.capability
eee.role
eee.schema_version
eee.created_by_run
```

路径只用于定位；`eee.node_id` 用于 rename/move 后重识别。一个 workspace 可以拥有 `/obj`、`/mat`、`/stage` 等多个根，但这些根的父 context 和其他节点仍是只读。通用任务可以读取整个 HIP，并在多个 owned root 中写入；它不会因为被分类为“通用任务”而获得全 HIP 写权限。

Scene Read 只表示本地工具可以查询 HIP。Scene Indexer 先生成任务相关摘要，再把最少必要信息送给模型；完整 HIP 内容、脚本、路径和属性不会自动上传。

## 8. 权限、ChangeSet 与事务

### 8.1 权限级别

- `SceneRead`：读取和索引整个 HIP，不产生场景副作用。
- `OwnedWorkspace`：创建、修改、连接或删除同一 `workspace_id` 下的节点；外部节点只读。
- `ScopedPatch`：用户明确选择既有节点集合，每个 ChangeSet 预览并批准，权限不向上下游扩散。
- `ProjectChange`：允许在 HIP 其他位置提出有限、可枚举的 ChangeSet，但仍逐次审批，不提供永久全局写开关。

Policy Engine 按 effect 判定而非工具名判定，例如 `scene.read`、`node.create`、`parm.set`、`wire.connect`、`node.delete`、`code.install`、`file.write`。Capability 必须声明可能产生的 effect。

首个生产范围硬拒绝：自动 load/clear/save HIP、自动解锁或修改 HDA definition、安装 HDA、任意 shell、任意 Python `eval/exec`、artifact/export 白名单之外的文件写入。未来能力必须通过新增 typed effect、Policy 和审批界面扩展，不能重新暴露通用 `run_houdini_python`。

### 8.2 ChangeSet

```text
ChangeSet
- change_id / run_id
- scene_binding / workspace_id
- base_revision
- required_permission
- typed_operations[]
- affected_nodes[]
- read_dependencies[]
- preconditions[]
- expected_postconditions[]
- risk_summary
- checkpoint_plan
- inverse_plan
```

审批界面显示创建、参数修改、源码变化、重连、删除、外部副作用和下游影响。批准后 Executor 再次读取 preconditions；任何参数、连线、名称、类型、ownership 或 revision 变化都会使 ChangeSet 成为 `StaleChangeSet`，必须重新规划。

### 8.3 TransactionalExecutor

执行顺序：

1. 在 staging 构建或准备定向 before snapshot。
2. 冻结 SceneBinding、权限、revision 和 preconditions。
3. 通过 Houdini 主事件循环串行执行短事务。
4. 使用 `hou.undos.group("EEE Agent · <change>")` 形成一个用户可识别的 Undo 项。
5. 执行 typed operations，不执行模型提供的任意 HOM 代码。
6. 立即校验 postconditions、cook 和关键 graph hash。
7. 成功写 ChangeReceipt；失败按 inverse plan 恢复并再次验证。

Executor 不通过盲目调用“Undo 最后一项”做自动回滚，因为 undo 栈可能包含 Houdini 内部事件或用户操作。自动回滚使用自己的 snapshot 和 inverse operations；Houdini Undo 留给用户。回滚验证失败时冻结全部写权限并进入 `CriticalRecoveryRequired`。

Scoped Patch 默认使用受影响节点、参数、连线和外部引用的定向 checkpoint。无法证明可无损恢复的 ProjectChange 必须在审批中额外获得“创建 HIP backup copy”许可；拒绝备份则不执行。备份只能走显式分支，绝不静默保存、加载或改名当前 HIP。

### 8.4 Restricted HoudiniBridge

产品边界不保留裸 `hrpyc`：

- 只监听 loopback，使用随机端口和进程级 token。
- Runtime token 与 HoudiniBridge token 分离，日志只保存 token hash。
- 只接受版本化 DTO：scene query、spec inspect、apply ChangeSet、validate、capture、cancel。
- 不返回 `hou` proxy，不允许远端模块导入或任意函数调用。
- 所有 HOM 调用排队；写操作通过 Houdini 主事件循环回调串行执行。
- 每个请求携带 idempotency key、deadline 和 scene epoch。

当前 `hrpyc` 只能在迁移期通过显式 legacy feature flag 保留，并且不得被新 Runtime 或新 Panel 默认使用。

## 9. Runtime、持久化与恢复

### 9.1 Runtime 生命周期

每个 Houdini 实例对应一个本地 Runtime。Runtime 独立于 Panel 存活，通过 localhost WebSocket + token 服务 Panel。Runtime 不要求 Phoenix、LangSmith 或云服务在线。

应用数据目录位于 `%LOCALAPPDATA%\EEEAgent`：

```text
config/models.toml
state/app.sqlite
state/checkpoints.sqlite
artifacts/<session>/<run>/...
logs/...
```

### 9.2 两类持久化

LangGraph `AsyncSqliteSaver` 保存执行 checkpoint，`thread_id = session_id`。每个用户回合有独立 `run_id`，但延续同一 session thread。

应用 SQLite 保存 `SessionRecord`、`RunRecord`、`WorkspaceRecord`、`EventRecord`、`ApprovalRecord`、`ChangeSetRecord`、`ChangeReceipt`、`ValidationRecord`、`ArtifactRecord` 和 `ModelSnapshot`。大型 spec、源码、图片、原始模型响应和 snapshot 进入内容寻址 artifact store；数据库和 graph state 只保存 ref、hash、media type 和 schema version。

两套 SQLite 不假设跨库原子事务。外部副作用采用 `change_id` 幂等协议：

1. App DB 写 `PendingChange` 和 checkpoint ref。
2. HoudiniBridge 使用同一 `change_id` 应用，并留下可查询的 ownership/receipt 标记。
3. App DB 写 `Applied` receipt。
4. Graph node 返回成功并进入下一 checkpoint。

RecoveryCoordinator 在崩溃后查询实际 scene：完全未应用则安全重试；postconditions 满足则补 receipt；部分应用则回滚；不能证明状态时进入 Critical Recovery。

### 9.3 AgentState

```text
AgentState
- session_id / run_id
- scene_binding / workspace_ref
- capability_route
- normalized_messages
- scene_snapshot_ref
- modeling_brief_ref
- procedural_spec_ref
- current_change_id
- validation_summary
- artifact_refs
- repair_budget
- approval_state
- visual_policy
- model_snapshot
- decision_summary
- run_status / failure
```

运行 checkpoint 可保留完成当前 provider tool loop 所需的精确 provider block。Run 结束时执行 `NormalizeSessionContext`，下一回合只使用用户消息、最终答案、Scene Facts、Tool Summary 和 DecisionSummary；完整 reasoning 不进入长期对话上下文。

为支持运行中断线重放，`ReasoningDelta` 和 provider block 可以在 active Run 期间进入短期 EventRecord/checkpoint。Run 到达终态后，Runtime 写入 normalized checkpoint，并将这些短期事件压缩为 DecisionSummary；超过 recovery grace period 后删除旧 operational checkpoint 和 reasoning event。只有用户显式启用 full trace/debug retention 时，完整推理才作为受保留策略约束的 trace artifact 继续存在。

### 9.4 Run 状态机

```text
Created
  -> PreparingContext
  -> Planning
  -> ValidatingSpec
  -> AwaitingApproval? / AwaitingVisualPolicy?
  -> Staging
  -> Applying
  -> ValidatingResult
  -> Repairing -> Planning|Staging
  -> Finalizing
  -> Completed
```

横向状态：

```text
StopRequested -> Stopping -> Cancelled
SceneChanged -> BlockedSceneChanged
RetryableError -> Retrying
FatalError -> Failed
RollbackFailure -> CriticalRecoveryRequired
```

审批和视觉选择使用 LangGraph interrupt，可跨 Panel 关闭和 Runtime 重启等待。普通 Stop 在模型流、规划节点和参数采样批次之间生效；已经开始的短 Houdini transaction 必须先完成或回滚。Force Stop 取消模型流和未派发 RPC；已进入 Houdini 的请求仍保持 `Stopping`，直到 scene reconciliation 完成。

### 9.5 错误模型

```text
AgentError
- code
- category
- message_for_user
- technical_detail_ref
- retryable
- requires_user_action
- scene_may_have_changed
- suggested_actions[]
- cause_chain[]
```

类别包括 `Validation`、`Permission`、`StaleScene`、`ProviderContract`、`ProviderTransient`、`HoudiniCook`、`HoudiniBridge`、`Artifact`、`Protocol`、`InternalInvariant` 和 `CriticalRecovery`。Traceback 保存为 artifact，UI 主要显示可行动说明与证据链接。

## 10. Runtime 协议

协议使用版本化 WebSocket envelope：

```json
{
  "protocol": "eee.runtime/1",
  "kind": "event",
  "event_id": "evt_...",
  "session_id": "ses_...",
  "run_id": "run_...",
  "seq": 184,
  "timestamp": "2026-07-13T12:00:00+08:00",
  "type": "validation.updated",
  "payload": {}
}
```

命令至少包括：

- session list/create/rename/archive/delete/fork。
- workspace create/bind/switch/inspect。
- run start/stop/force-stop。
- ChangeSet approve/reject。
- visual policy resolve。
- artifact reveal/open/copy reference。
- event replay 和 current snapshot。

事件至少包括：

- run state、reasoning delta、text delta。
- todo、subagent、tool 和 usage。
- ChangeSet、approval 和 permission。
- validation、visual review 和 artifact。
- connection、warning 和 structured error。

`seq` 在每个 session 内严格单调。Panel 重连时提交 `last_seq`；Runtime 重放 EventRecord。超过保留窗口时先发送 `session.snapshot`，再继续增量。未知次版本事件允许忽略并记录；主版本不兼容则拒绝连接并显示升级要求。

## 11. Houdini Panel UI

Panel 改为真正 dockable 的 `.pypanel`。宽屏为三栏，窄屏将左右栏变为 drawer：

```text
Session Sidebar | Conversation + Run Activity | Inspector
```

顶部 Context Bar 始终显示：

```text
HIP > Session > Workspace
Capability | Permission | Primary Model | Vision Model
Runtime | HoudiniBridge | Run State
```

### 11.1 Session Sidebar

- 新建、重命名、归档、删除、fork。
- 显示绑定 HIP、workspace、最后运行状态和时间。
- 清楚标记当前 active session 和后台运行。

### 11.2 Conversation 与 Run Activity

对话消息不混入内部事件。Run Activity 结构化显示：

- 可折叠 live reasoning。
- Todo 与阶段进度。
- 子 Agent 的名称、目标、状态和结构化结果。
- 工具的输入摘要、状态、时长、错误和 artifact。
- ChangeSet、审批、验证、修复和 usage。

可用的完整 reasoning 在运行时实时展示；历史默认只保留 DecisionSummary。UI 不伪造 provider 没有提供的思考内容。

Composer 支持多行输入、明确的 Send/Stop/Stopping 状态；严格模式首版固定开启，不提供制造弱交付语义的假开关。

### 11.3 Inspector

Inspector 包含 `Run`、`Workspace`、`Validation`、`Artifacts`：

- Run：阶段、模型快照、token、时长、retry 和 checkpoint。
- Workspace：所有 owned roots、外部只读依赖、permission scope、scene revision 和 ChangeReceipt。
- Validation：硬门禁、逐参数样本矩阵、阈值来源、证据和 repair 对比。
- Artifacts：图片、prompt、normalized request、raw response、report、源码、spec 和 trace。

审批使用独立 drawer，显示精确 diff、risk、scope、checkpoint 和 approve/reject。视觉检查展示的必须是与发送给模型完全相同 hash 的图片字节。

## 12. Provider 与模型配置

### 12.1 抽象边界

Deep Agents、capability 和 UI 不直接导入 `ChatOpenAI`、`ChatAnthropic` 或供应商 SDK，只消费：

- `ResolvedModel`。
- LangChain `BaseChatModel`。
- 统一 structured output helper。
- 统一 normalized provider events。

配置分三层：

```text
ProviderConnection
- id / provider / transport / base_url
- secret_ref / timeout / retry

ModelProfile
- id / connection / exact model
- thinking / output settings
- declared capabilities

RoleBinding
- primary
- vision
```

配置保存在 `%LOCALAPPDATA%\EEEAgent\config\models.toml`。密钥只通过环境变量或 Windows Credential Manager 的 secret ref 读取，不进入 TOML、SQLite、trace 或 artifact。

### 12.2 DeepSeek V4

默认主模型通过 DeepSeek 官方 Anthropic 兼容 endpoint `https://api.deepseek.com/anthropic` 接入，由 `DeepSeekV4ProviderAdapter` 包装 `ChatAnthropic`。只接受精确模型名 `deepseek-v4-pro` 或 `deepseek-v4-flash`，拒绝已弃用 alias 和静默 fallback。

严格建模默认 `deepseek-v4-pro`、thinking enabled、effort max；通用任务默认 high。DeepSeek V4 不声明图像输入能力，视觉请求路由到独立 vision binding。

不使用当前 `ChatOpenAI(base_url=DeepSeek)` 路径，因为其兼容层会丢弃 `reasoning_content`。也不在 contract test 通过前假设 `langchain-deepseek` 能正确完成 thinking + tool result replay。

### 12.3 能力验证

模型状态为 `Unverified`、`Verified` 或 `Invalid`。验证至少覆盖：

- 普通 chat。
- streaming。
- thinking block。
- tool call、tool result 和第二次响应。
- structured output。
- usage、取消和实际返回模型名。

验证缓存键包含 provider、endpoint、model、adapter 和依赖版本。Run 启动时冻结 ModelSnapshot；Settings 修改只影响下一 Run。Vision 可独立切换。Primary 不能在一个未结束的 tool loop 中热切换；需要从确定性 checkpoint 重启。

### 12.4 标准事件

Provider adapter 统一输出：

```text
ReasoningDelta
TextDelta
ToolCallStarted
ToolCallArgumentsDelta
ToolCallCompleted
UsageUpdated
ModelCompleted
ModelFailed
```

这使 Panel、trace 和持久化不需要理解供应商私有 streaming chunk。

## 13. 截图与视觉检查

### 13.1 截图后端

Houdini 21 的主截图后端使用 Vulkan `Flipbook` ROP，不依赖用户当前 desktop、当前 viewport 或已弃用的 OpenGL ROP。TransactionalExecutor 在 owned temp scope 创建相机与 ROP，完成 capture 后清理。

固定输出：

- 1280x960 PNG，8x AA。
- smooth shaded。
- 中性固定灯光。
- 关闭材质、纹理、`Cd`、透明、雾、bloom 和 wireframe。
- 不显示 grid、handle、HUD、选择高亮或其他 guide。
- Flipbook 透明 Alpha 在 artifact pipeline 中合成到稳定中性纯色背景。

每个 spec 明确 up/front axis。标准视图包括：

- 固定 front-right-above 三分之四透视，50mm，只用于上下文。
- front、back、left、right、top 正交视图。
- bottom 按资产适用性启用。

不使用夸张广角和特殊倾斜角度。640x480 preflight 最多调整 framing 两次，要求有效、非空、四周至少 6% margin、最长轴占画面约 72%-84%、中心偏移不超过 3%。最终视觉模型收到的图片不做二次裁切；UI overlay 只在查看器中显示且不会回传模型。

### 13.2 Vision Router

模型注册表提供 `primary` 和可选 `vision`，`vision_route = auto | primary | dedicated`。只有 capability verification 证明 primary 支持图像时，`auto` 才能选择 primary；否则使用 dedicated vision。

如果没有配置 vision 且 primary 不支持多模态，UI 必须让用户选择：

1. 配置视觉模型。
2. 跳过本次视觉检查。
3. 取消运行。

跳过只对当前 Run 有效，下一 Run 重新询问。CLI 必须显式传 `visual_policy=skip`，不能静默跳过。跳过视觉 LLM 后仍生成标准截图；所有确定性 validator 仍必须通过，最终状态显示 `Completed · Visual review skipped by user`。截图后端本身不可用时也必须单独、显式豁免。

### 13.3 Visual Artifacts

每个 attempt 保存：

```text
capture_manifest.json
images/*.png
prompt.md
request.normalized.json
response.raw.json
report.json
metadata.json
```

`Validation > Visual Review` 提供原图画廊、全分辨率查看、findings、prompt、raw response、parsed report、metadata、模型、usage、时长、hash 和多 attempt 对比。用户可快速打开 artifact、所在目录或复制引用。

Visual report 能生成 RepairTicket，但视觉通过不能覆盖确定性失败。首个版本由确定性门禁保留最终 veto。

## 14. 质量门禁与评估

采用“硬门禁 + 趋势评分”，不使用单一加权总分决定交付。每个 Run 冻结带版本号的 `QualityProfile`，按资产族定义阈值、适用规则、参数采样、允许拓扑、语义约束和视觉 rubric。

### 14.1 Validator Pipeline

1. `SpecContractValidator`：schema、组件 DAG、端口、稳定 ID、单位、范围、轴向和参数连接。
2. `GraphValidator`：实际节点、ownership、连接、flags、悬空节点、硬锁、越权引用和外部依赖。
3. `CookValidator`：强制 cook 后读取错误和警告；错误阻断，未进入版本化 allowlist 的警告也阻断。
4. `GeometryValidator`：空几何、非有限值、包围盒、未使用点、零面积、退化、重叠、非流形、法线和自相交。复杂检查在 staging 临时诊断分支中运行，读取结果后清理。
5. `ParameterSensitivityValidator`：默认值、上下界、边界附近值和受控 pairwise；验证 declared effect、单调关系、组件数量、接触、净空、无效参数和串扰。
6. `SemanticValidator`：按资产族检查支撑、对称、间距、厚度、锚点、装配和尺寸容差。
7. `ArtifactValidator`：workspace manifest、ChangeSet、run snapshot、截图、prompt、原始响应、hash 和 replay 信息。用户跳过视觉 LLM 时必须存在 `SkippedByUser` 决策记录；截图后端被单独豁免时必须存在 capture waiver。Validator 验证真实 artifact 或显式 waiver，不能把两者都缺失当成通过。
8. `VisualValidator`：可修复、可按 Run 跳过，不推翻确定性失败。

Parameter sampling 有固定预算，防止组合爆炸。失败生成精确 RepairTicket，每阶段最多两次修复；耗尽预算则回滚 staging，不交付半成品。

### 14.2 Golden Cases

Golden Case 采用不变量优先，而不是脆弱的完整 mesh hash 或逐像素截图：

- 保存 brief、scene fixture、QualityProfile、预期组件/参数行为、容差、禁止结果和参数样本。
- 对 schema、graph contract、稳定事实和关键 artifact hash 做精确比较。
- 参考 mesh/image 用于人工和视觉回归，不单独决定通过。
- 初始覆盖桌、柜、楼梯或立面模块、机械支架、管路，以及空输入、极限尺寸、非法 spec、cook 失败和视觉跳过。
- 真实运行只有用户明确“提升为 Golden Case”才进入本地候选集；清洗和人工复核后才能导出。

### 14.3 测试层级

- 纯 Python unit/schema/provider/validator contract tests。
- `hython` graph、cook、parameter 和 artifact integration tests。
- Houdini GUI/RPC/Vulkan Flipbook 端到端测试。
- 显式启用的真实 provider 夜间评估。

确定性结果是交付门禁；cook 时间、节点数、采样通过率、repair 次数、token、延迟和视觉缺陷数作为趋势指标。

## 15. Observability 与 LangSmith 边界

产品自身的 `RunRecord`、`EventRecord`、`ValidationRecord` 和 artifact 是事实源。OTel/OpenInference instrumentation 覆盖：

- run、capability、subagent 和 graph node。
- model、tool、HoudiniBridge 和 ChangeSet。
- validator、capture、vision 和 approval。

Phoenix 是默认的可选本地观测目标。Phoenix 关闭时产品行为不变。Span 默认只记录 ID、hash、版本、状态、时长和指标，不记录完整 reasoning、密钥、整份 HIP、图片字节或未清洗 prompt；本地 debug 需显式 opt-in。

LangSmith 只用于用户显式标记 `eval_export=true` 且完成清洗的离线 dataset/experiment。生产 live trace 不默认发送 LangSmith，Runtime、审批和质量门禁都不依赖 LangSmith。Exporter 将 provider-neutral RunRecord 映射为 LangSmith example/run，不能让 LangSmith SDK 类型进入 core 或 capability。

## 16. 端到端数据流

一次严格建模 Run 的完整数据流：

1. Panel 发送带 session、workspace、visual policy 和 next-run model override 的 `run.start`。
2. Runtime 冻结 SceneBinding、ModelSnapshot、QualityProfile 和协议版本。
3. Scene Indexer 读取整个 HIP，但只产生任务相关 SceneSnapshot artifact。
4. Supervisor 路由到 Modeling capability。
5. Planner 生成 ModelingBrief、ProceduralSpec 和 ComponentSpec；设计 subagent 无写权限。
6. Spec validator 和 auditor 通过后，Compiler 生成 typed ChangeSet。
7. Policy Engine 判定 OwnedWorkspace、ScopedPatch 或 ProjectChange；需要时 graph interrupt 等待审批。
8. Executor 在 staging 中幂等应用，Reconciler 从 Houdini 重读真实 graph。
9. Quality Pipeline 运行确定性采样；失败进入受预算限制的 RepairTicket 循环。
10. Capture pipeline 生成标准视图。Vision Router 选择 primary、dedicated 或用户确认 skip。
11. 全部门禁通过后 commit；否则 rollback。
12. Artifact、ValidationReport、ChangeReceipt 和 DecisionSummary 写入应用存储并流式发送 UI。
13. `NormalizeSessionContext` 清理 provider 私有运行上下文，保留下一回合需要的稳定事实。

## 17. 安全、隐私与版本稳定性

- 所有网络 listener 只绑定 loopback；token 使用高熵随机值并区分 Runtime 与 Bridge。
- 密钥只通过 secret ref 获取，任何 event、error、trace 和 artifact 写入前统一 redaction。
- SceneRead 输出先经过数据最小化。用户路径、源码、参数字符串和图片只有任务需要时才进入模型请求。
- Artifact 按 session/run 分区，保存 hash、media type、producer、retention 和 redaction 状态。
- 每个 schema 带版本；读取旧数据必须通过显式 migration，不能在 model prompt 中临时猜字段。
- `uv.lock` 锁定完整依赖图；`rpyc==4.1.0` 保持与 Houdini 21.0.440 一致，升级 Houdini 时重新做 wire compatibility test。
- Run snapshot 记录 Python、Houdini、Deep Agents、LangChain、LangGraph、provider adapter、prompt、skill、spec、validator 和 profile 版本。

## 18. 分阶段迁移

总体设计分成六个里程碑，每个里程碑单独细化、实施和验收：

### 18.1 Foundation

- 引入 `uv.lock`、pytest 和版本报告。
- 修复环境探测路径。
- 建立 core ID、schema、error、event 和 artifact contract。
- 实现 ProviderRegistry、DeepSeek V4 adapter、normalized events 和离线 provider contract tests。
- 显式固定或关闭 Deep Agents 默认 general-purpose subagent。

### 18.2 Runtime

- 应用 SQLite、AsyncSqliteSaver、Session/Run 和 EventStore。
- WebSocket token、版本协议、streaming 和 event replay。
- 先通过兼容 adapter 跑通 read-only 回合，不立即删除现有 CLI。

### 18.3 Secure Houdini Bridge

- Restricted DTO、主事件循环队列、SceneBinding、WorkspaceManifest。
- Policy、typed ChangeSet、幂等 receipt、最小 TransactionalExecutor 和 recovery。
- 裸 hrpyc 只留在默认关闭的 legacy flag。

### 18.4 Docked UI

- `.pypanel`、多会话、Context Bar、Run Activity、Inspector、审批和关闭提示。
- WebSocket 重连、snapshot 与 event replay。
- 将 `chat_panel.py` 的进程、协议、状态和 renderer 职责逐步迁出。

### 18.5 Strict Modeling Capability

- Brief/Spec/Component/Compiler、staging、Reconciler。
- 全部确定性 validator、repair budget、QualityProfile 和 Golden Cases。
- 修复桌腿 perimeter 分布不对称，并加入非正方形桌参数回归。

### 18.6 Capture、Vision 与 Eval

- Vulkan Flipbook ROP、标准视图和 framing preflight。
- Vision Router、缺失模型提示、按 Run skip、artifact viewer。
- OTel/Phoenix 和显式 LangSmith eval export。

每个里程碑保留上一条可运行路径，使用 feature flag、schema migration、contract test 和端到端验收。只有新路径达到行为等价、恢复测试和回滚测试通过后才删除对应 legacy 代码，避免长期维护两套产品架构。

本总体 spec 不直接转换成一个数百项的大 implementation plan。用户审阅通过后，第一份 plan 只覆盖 Foundation；Foundation 完成并验收后，再对 Runtime 里程碑进行细化设计和计划。

## 19. 总体验收标准

架构落地后的首个完整建模版本必须满足：

1. 在一个 Houdini 实例中创建多个持久 Session，重启 Panel 后可恢复列表、消息、Run 状态和 event stream。
2. Panel 关闭时 active Run 给出继续、中止和取消关闭选择；继续后 Runtime 不中断。
3. UI 明确显示 HIP、Session、Workspace roots、Capability、Permission、模型和连接状态。
4. Agent 可读取整个 HIP，但未审批时无法修改非 owned 节点；测试证明 raw HOM 写入口不可达。
5. HIP load/clear 后旧 Run 无法继续写入，新操作必须重新绑定和生成 ChangeSet。
6. 同一 ChangeSet 重放不会重复创建节点或重复应用副作用。
7. 事务失败可以按 snapshot/inverse plan 恢复；无法恢复时冻结写入并保留证据。
8. DeepSeek V4 thinking + tool call + tool result 的多回合 payload 通过 adapter contract test。
9. 模型切换只影响下一 Run；视觉模型可独立选择。
10. 无视觉模型时用户可以跳过视觉检查，确定性验证通过后仍能交付并明确标记跳过。
11. 标准截图无 grid、handle、HUD、特殊角度和误导性 framing；UI 可打开原图、prompt、raw response 和 parsed report。
12. 建模输出通过结构、cook、几何、参数敏感性、语义和 artifact 硬门禁；任何阻断失败都不交付。
13. 非正方形桌等 Golden Case 验证组件对称和参数行为，不只验证几何数量。
14. Phoenix、LangSmith 同时关闭时，Session、Run、验证、恢复和 UI 全部正常工作。
15. 所有依赖、schema、模型、prompt、skill、validator 和 QualityProfile 版本可从 Run snapshot 追溯。

## 20. 参考资料

### LangChain、Deep Agents 与 LangGraph

- Deep Agents subagents: <https://docs.langchain.com/oss/python/deepagents/subagents>
- Deep Agents skills: <https://docs.langchain.com/oss/python/deepagents/skills>
- Deep Agents context engineering: <https://docs.langchain.com/oss/python/deepagents/context-engineering>
- Deep Agents backends: <https://docs.langchain.com/oss/python/deepagents/backends>
- Deep Agents production: <https://docs.langchain.com/oss/python/deepagents/going-to-production>
- LangGraph persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- LangGraph interrupts: <https://docs.langchain.com/oss/python/langgraph/interrupts>
- LangGraph streaming: <https://docs.langchain.com/oss/python/langgraph/streaming>
- LangChain messages: <https://docs.langchain.com/oss/python/langchain/messages>
- LangChain custom middleware: <https://docs.langchain.com/oss/python/langchain/middleware/custom>

### DeepSeek 与 Provider

- DeepSeek thinking mode: <https://api-docs.deepseek.com/guides/thinking_mode/>
- DeepSeek tool calls: <https://api-docs.deepseek.com/guides/tool_calls/>
- DeepSeek Anthropic API: <https://api-docs.deepseek.com/guides/anthropic_api/>
- DeepSeek API updates: <https://api-docs.deepseek.com/updates>
- LangChain ChatOpenAI compatibility note: <https://docs.langchain.com/oss/python/integrations/chat/openai>

### Houdini 21

- Python Panel Editor: <https://www.sidefx.com/docs/houdini/ref/windows/pythonpaneleditor.html>
- Houdini RPC: <https://www.sidefx.com/docs/houdini/hom/rpc.html>
- HIP events and backup APIs: <https://www.sidefx.com/docs/houdini/hom/hou/hipFile.html>
- Node user data: <https://www.sidefx.com/docs/houdini/hom/nodeuserdata.html>
- Undo groups: <https://www.sidefx.com/docs/houdini/hom/hou/undos.html>
- UI event callbacks: <https://www.sidefx.com/docs/houdini/hom/hou/ui.html>
- Geometry API: <https://www.sidefx.com/docs/houdini/hom/hou/Geometry.html>
- Node cook errors and warnings: <https://www.sidefx.com/docs/houdini/hom/hou/Node.html>
- Clean SOP: <https://www.sidefx.com/docs/houdini/nodes/sop/clean.html>
- PolyDoctor SOP: <https://www.sidefx.com/docs/houdini/nodes/sop/polydoctor.html>
- Flipbook ROP: <https://www.sidefx.com/docs/houdini/nodes/out/flipbook.html>
- OpenGL ROP deprecation: <https://www.sidefx.com/docs/houdini/nodes/out/opengl.html>

### Observability 与评估

- Phoenix overview: <https://arize.com/docs/phoenix>
- Phoenix datasets: <https://arize.com/docs/phoenix/learn/datasets-and-experiments/datasets-concepts>
- Phoenix experiments: <https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/run-experiments>
- LangSmith evaluation: <https://docs.langchain.com/langsmith/evaluation>
- LangSmith datasets: <https://docs.langchain.com/langsmith/manage-datasets>
- LangSmith OpenTelemetry: <https://docs.langchain.com/langsmith/trace-with-opentelemetry>
