# Hython 分层验证与 Agent 全链路开发计划

- 日期：2026-07-26
- 状态：已核对，执行中
- 核对结论：2026-07-26 代码、环境与测试基线逐项核对通过
- 当前分支：`feature/html-to-houdini-pipeline`
- 目标：先用真实 Houdini/hython 独立验证确定性建模能力，再通过
  Runtime + Secure Bridge + 真实 Provider 验证 Agent 主链路，最后完成
  HTML 草图、参数化、案例库和产品体验闭环。

## 1. 结论

可以并且应该采用以下分层验证，而不是直接用 LLM 端到端运行来定位所有问题：

```text
离线 pytest
    ↓
hython 直接验证（真实 hou，零 LLM、零 Runtime）
    ↓
Secure Bridge 确定性验证（真实 hou + Runtime 侧 provider，零 LLM）
    ↓
真实 Agent scratch 链路（真实 Provider + Runtime + Bridge + Houdini）
    ↓
HTML 草图两轮会话链路（草图审核 → 批准 → 构建/验证/提交）
    ↓
参数化案例库与产品验收
```

每一层只在上一层通过后运行。这样出现失败时可以明确归因到：

- Houdini 节点/参数/cook/gate；
- Bridge 协议、能力发现或主线程队列；
- Runtime 上下文和工具注册；
- LLM 工具选择与提示词；
- HTML 草图审核和跨 Run 会话连续性；
- 参数表达式与资产设计质量。

编号约定：本文的 `Wave A/B/C/D` 是实施波次；`HTML-P0`～`HTML-P6`
专指 `docs/handoffs/html-to-houdini-dev-plan.md` 中的流水线阶段。两者不是
同一套编号。

本机已检测到可用安装：

```text
C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe
```

## 2. 当前基线与关键发现

### 2.1 已具备

- 离线完整测试基线：`3509 passed, 12 skipped`。
- `scratch_build`、`scratch_commit`、`scratch.destroy` 已进入生产路径。
- Secure Bridge、真实 hython worker、真实 Provider 验收框架已有可复用基础。
- 已有真实 Houdini smoke：
  - `tests/runtime/houdini_bridge_smoke.py`
  - `tests/runtime/changeset_houdini_smoke.py`
  - `tests/modeling/bootstrap_houdini_smoke.py`
  - `tests/modeling/golden_cases_houdini_smoke.py`
  - `tests/runtime/capture_houdini_smoke.py`
  - `tests/runtime/sensitivity_houdini_smoke.py`
- HTML→Houdini 工作区已包含 `render_sketch`、`verify_geometry`、两个新 skill、
  catalog 扩展和自行车 eval 案例。

### 2.2 必须先解决的验收漂移

`tests/runtime/provider_journey.py` 的 `_BRIEF` 已要求模型调用
`scratch_build`/`scratch_commit`，但 `_run_journey()` 仍按旧主链路查找：

```text
AwaitingApproval → approve_changeset → approval.approved
→ modeling.validation_completed → modeling.artifact_captured
```

新 scratch 主链路会直接通过 `scratch_commit` 提升节点，并不保证产生旧式
`AwaitingApproval` ChangeSet。因此当前 provider journey 不能作为新主链路的
有效验收证据。必须先改为 scratch-native 证据模型。

### 2.3 当前验证门缺口

catalog 构建目前不能可靠创建 `component_id` 和 `edini_world_axis` primitive
属性；`scratch_verify.py` 在属性不存在时会跳过部分检查。因此 bake 和
orientation 可能“空通过”。在修复前，不能把“四道硬 gate 通过”当作完整的
方向验证证据。

## 3. 验收层级

## L0 — 离线确定性测试

用途：每次提交的最低门槛，不需要 Houdini、网络或 Provider。

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

通过标准：

- 0 failed；
- skip 仅允许显式 HFS/hython/WSL/Provider 条件项；
- `python -m eee_agent.cli versions` 正常；
- 工作区无意外生成的受跟踪运行时文件。

## L1 — hython 独立建模验证

用途：只验证真实 `hou`、SOP 类型、参数、连接、cook、几何统计和 commit
gate，不启动 Runtime，不调用 LLM。

新增入口：

```text
tests/runtime/scratch_houdini_smoke.py
```

注意：`scratch_houdini_smoke.py` 是用 hython 启动的真实 Houdini 验收入口；
`houdini_side/scratch_verify.py` 是被生产 commit 路径调用的 gate 实现模块。
二者职责不同，前者验证后者及其外围链路，不复制 gate 逻辑。

运行方式：

```powershell
& 'C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe' `
  -u tests\runtime\scratch_houdini_smoke.py
```

至少覆盖：

1. 在一次性 `/obj/eee_scratch_smoke_*` 下创建节点。
2. 使用 catalog 中的真实节点类型设置参数并连接。
3. 强制 cook，核对点/面数与 bbox。
4. 注入一个非法参数，证明失败可定位且不会误报成功。
5. 注入一个 health/structure 失败，证明 commit 会拒绝并保留 sandbox。
6. 修复后重新 commit，证明节点被提升到目标路径。
7. 目标已存在时 fail-closed。
8. 校验 receipt、最终节点和 sandbox 消失。
9. finally 中删除本次创建的节点；不保存、加载或清空用户 HIP。
10. 记录 before/after 场景指纹，证明只改动测试私有分支。

首批用例：

- 简单件：单个 box/tabletop，允许显式 `skip_structure_check=True`；
- 模块件：桌面 + 四腿或简化自行车组件，不允许跳过 structure；
- gate 反例：缺失/错误 construction axis、开放曲线或孤立点。

通过标准：

- 脚本输出 `SMOKE OK` 且退出码为 0；
- 所有创建物被清理；
- 不依赖 `.env`、Provider 凭据或 Runtime SQLite；
- 失败信息能定位到具体 op/cook/gate。

## L2 — Secure Bridge 确定性链路

用途：验证生产 Bridge 路径，但仍不让 LLM 参与。它用于区分“真实 Houdini
可用”和“Runtime 到 Houdini 的线协议可用”。

新增入口：

```text
tests/runtime/scratch_bridge_houdini_journey.py
```

实现方式：

- 复用 `provider_journey_houdini_worker.py` 的 hython worker 启动模式；
- venv 进程构造生产一致的：
  - `BridgeChangeSetProvider`
  - `BridgeReadOnlyProvider`
  - `ScratchCoordinator`
- 直接调用 coordinator 的 `build()`/`commit()`，不经过模型；
- 通过 Bridge 回读 final path、geometry stats 和 sandbox 清理状态。

覆盖：

- discovery/token/capability；
- `scratch.exec`、`scene.geometry_stats`、`scratch.commit`、`scratch.destroy`；
- stale scene、deadline、写冻结、重复目标和 worker 提前退出；
- commit 成功/拒绝两条路径；
- worker 停止后身份文件清理。

通过标准：

- 生成不含秘密的、≤16 KiB 的 JSON evidence；
- evidence 至少包含 build/cook/geometry/gates/commit/final-query/cleanup；
- 零 LLM、零人工判断。

## L3 — 真实 Agent scratch 主链路

用途：证明模型能通过生产 Runtime 正确选择和调用新工具，而不再验证旧
`propose_modeling` 流程。

先修订：

- `tests/runtime/provider_journey.py`
- `tests/runtime/runtime_mvp_provider_e2e.py`
- `tests/runtime/test_runtime_mvp_e2e.py`
- 对应 handoff/README 验收描述

新的 scratch-native evidence 建议字段：

```text
run_status
scratch_build_seen
scratch_build_ok
geometry_verified
scratch_commit_seen
commit_status
commit_receipt_present
final_path
final_geometry_ok
sandbox_absent
restart_replay_last_seq
scene_cleanup
```

旧字段 `proposal_digest`、`approval_event`、`AwaitingApproval` 不再作为 scratch
主链路的通过条件。旧 ChangeSet 内核仍可保留独立兼容 smoke。

首个 Agent 提示词保持简单、确定：

```text
在 sandbox 中创建一个指定尺寸的桌面 box，观察几何统计，调用
verify_geometry，通过后使用 scratch_commit 提交到 /obj 下；不要使用旧的
propose_modeling。
```

验收必须从持久事件和真实场景双重取证：

- `tool.started/tool.completed` 中出现所需工具；
- 每个工具返回结构合法；
- Runtime Run 终态为 Completed；
- Bridge 回读 final path 存在且几何满足断言；
- sandbox 不存在；
- Runtime 重启后事件可 replay；
- worker 最终清理测试节点。

通过标准：

- 连续运行 3 次至少 3/3 成功；
- 不接受“模型文本声称成功”作为证据；
- 不允许自动回退到 `propose_modeling`；
- 任何一次失败都保留 bounded event/worker log 供定位。

## L4 — HTML 草图两轮 Agent 链路

用途：验证当前 `procedural-modeling` skill 的强制用户审核门和跨 Run 连续性。

这是两轮会话，不能压成一个 Run：

1. Run A：用户提出建模需求。
   - Agent 分析组件和参数候选；
   - 生成 HTML；
   - 调用 `render_sketch`；
   - 返回 PNG 路径并停止等待审核；
   - 不允许提前调用 `scratch_build`。
2. Run B：用户回复“草图通过，继续”。
   - Agent 读取同 Session 上下文；
   - 调用 `scratch_build`；
   - 调用 `verify_geometry`；
   - 调用 `scratch_commit`；
   - 输出参数拆解记录。

通过标准：

- 审核前零 Houdini 写操作；
- PNG 是合法、非空、尺寸受限的图片；
- 第二轮复用第一轮草图和参数意图；
- 最终 Houdini 几何与草图 bbox/组件断言一致；
- 连续 3 个简单案例通过后，才进入复杂自行车验收。

## 4. 开发波次

## Wave A — 建立可信主链路基线（P0，预计 4–7 个开发日）

- [ ] A1. 整理并提交当前 HTML→Houdini 未提交工作区，保持 L0 全绿。
- [ ] A2. 用现有 `catalog_probe_houdini.py` 重新探测新增 node/parms。
- [ ] A3. 修复 bake/orientation 空通过：
  - 先用 hython 探测 Houdini 21 的原生 Attribute Create 路径；
  - 优先加入受限、catalog-gated 的属性写入节点；
  - 若暂不能支持，则当调用者提供 orientation checks 而属性缺失时必须
    fail-closed，不能 reported passed；
  - 添加正例和反例。
- [ ] A4. 新建 L1 `scratch_houdini_smoke.py` 并通过。
- [ ] A5. 新建 L2 deterministic Bridge journey 并通过。
- [ ] A6. 将 provider journey 从旧 ChangeSet evidence 迁移到 scratch-native
  evidence。
- [ ] A7. 用已批准的真实 Provider 连跑 L3 三次并记录有界证据。

Wave A 当前进度：

- [x] A1. HTML→Houdini WIP 已收口并提交（`e108db6`）。
- [x] A2. Houdini 21.0.440 catalog probe 通过。
- [x] A3. bake/orientation 缺属性时 fail-closed（`9bcf5c7`）。
- [x] A4. L1 `scratch_houdini_smoke.py` 通过。
- [ ] A5. L2 deterministic Bridge journey。
- [ ] A6. scratch-native provider evidence/harness。
- [ ] A7. 真实 Provider 连跑三次。

Wave A 出口：

- 离线、hython、Bridge、真实 Agent 四层全部通过；
- `scratch_build → observe/verify → scratch_commit` 成为被证据证明的主链路；
- CLAUDE/README 不再声称尚未执行的 gate 已通过。

## Wave B — 完成 HTML→Houdini 产品链路（P1，预计 6–10 个开发日）

- [ ] B1. Headless Chrome 真机 smoke：
  - 浏览器发现；
  - HTML/PNG 写入；
  - PNG 格式、尺寸、大小限制；
  - CDN 不可用和超时诚实失败；
  - 增加空白/全透明图的最低检测，避免“文件存在即成功”。
- [ ] B2. L4 两轮会话验收：先椅子，再书桌，再货架。
- [ ] B3. 完成 HTML-P1 的 3 次草图稳定性验收。
- [ ] B4. 完成 HTML-P2 的“全新物体零参数名错误”验收。
- [ ] B5. 完成 HTML-P3 的五类故障注入：
  - 断连；
  - 错尺寸；
  - 错 pivot；
  - 漏部件；
  - 错材质/颜色（视觉层）。
- [ ] B6. 为每个失败生成可直接反馈给 Agent 的定位报告。

Wave B 出口：

- 三个非自行车案例能从草图审核走到 Houdini commit；
- 数值断言负责硬正确性，视觉判断只作补充；
- 自行车作为复杂回归，而不是唯一成功样本。

## Wave C — 参数化自动化与案例库（P2，预计 7–12 个开发日）

- [ ] C1. 设计并实现表达式支持，不在 catalog 里直接放开任意字符串：
  - typed expression DTO/AST；
  - 只允许 `ch()`、算术和白名单函数；
  - 相对路径与目标参数校验；
  - 禁止文件路径、Python/VEX 任意执行。
- [ ] C2. 组件化组织：wheel/frame/cockpit/drivetrain 等 subnet/netbox。
- [ ] C3. 参数分类：design intent / derived / constant。
- [ ] C4. 生成参数 Tab、范围和依赖关系。
- [ ] C5. min/default/max 参数扫描，每档重新 cook + 几何断言 + 截图。
- [ ] C6. 案例库扩充：
  - 椅子；
  - 书桌；
  - 货架；
  - 自行车；
  - 参数化小屋。
- [ ] C7. 扩展 `eval/run_eval.py`，记录：
  - 翻译成功率；
  - 参数化覆盖率；
  - 几何断言通过率；
  - 平均修复轮数；
  - 端到端耗时和 Provider 成本。

Wave C 出口：

- 至少 5 个案例可回归；
- 参数 min/default/max 扫描无断连、无 cook error；
- 新案例参数化耗时不超过静态翻译的目标上限；
- eval 输出可用于版本间比较。

## Wave D — 节点生命周期、任务图谱与交付体验（P3）

基础主链路通过后再执行已有计划：

```text
docs/superpowers/plans/2026-07-24-node-lifecycle-task-graph.md
```

范围：

- [ ] task_steps/task_nodes；
- [ ] 每次 build 的 purpose/note；
- [ ] commit 自动布局、display/render flag、节点注释；
- [ ] `cleanup_nodes` 两阶段安全清理；
- [ ] 任务摘要注入 Agent；
- [ ] 面板只读任务视图；
- [ ] 独立 task graph hython smoke。

随后完成 Stage C：

- [ ] Delivery card；
- [ ] Inspector 参数表；
- [ ] 本地、可选、fail-isolated observability；
- [ ] Phoenix 仍保持可选，不成为正确性依赖。

## 5. 尚未完成任务总表

### 发布阻塞

- [ ] 当前 HTML→Houdini 改动尚未提交。
- [ ] scratch 新主链路没有专用真实 hython smoke。
- [ ] bake/orientation gate 存在空通过风险。
- [ ] provider journey 与新 scratch 主链路证据不一致。
- [ ] 新主链路尚未完成真实 Provider + Runtime + Houdini 三连跑。
- [ ] 12 个环境相关 skip 尚未在本机形成一份统一的真机验收记录。

### HTML→Houdini

- [ ] Headless Chrome 真机与空白图检测。
- [ ] HTML-P1 三次草图质量稳定性。
- [ ] HTML-P2 全新物体翻译验收。
- [ ] HTML-P3 五类错误注入。
- [ ] HTML-P4 typed 参数表达式、组件化和联动扫描。
- [ ] HTML-P5 多案例回归库。
- [ ] HTML-P6 两轮会话完整接入和 eval 指标。

### Runtime/产品

- [ ] 节点自动排版与 display/render flag。
- [ ] task graph、上下文摘要与面板展示。
- [ ] `cleanup_nodes`。
- [ ] Delivery/Observability Stage C。
- [ ] 真实 scratch 链路稳定后，评估删除保留的
  `propose_modeling`/compiler/proposal 旧入口；不得提前删除仍被共享的类型。

### 有意后置、当前不阻塞

- [ ] raw `network_mode` 和异步 job protocol：仅在结构化 op 被真实案例证明
  不足时立项。
- [ ] B2 per-component subagents。
- [ ] 图形化任务图谱。
- [ ] Phoenix 默认集成。

## 6. 每次里程碑的证据要求

每个里程碑必须同时保存：

1. commit SHA 和脏工作区状态；
2. 离线测试计数；
3. hython/Houdini 版本；
4. 执行的 smoke 名称和退出码；
5. bounded JSON evidence；
6. 创建/清理的测试节点数量；
7. 失败时的 step/code，不记录密钥或完整 Provider 输出；
8. 对应 handoff 中明确区分：
   - offline passed；
   - hython passed；
   - real provider passed；
   - GUI/manual passed。

任何一层未执行都写 `not_run`，不得折叠成上一层的通过。

## 7. 推荐的立即执行顺序

1. 当前 WIP 收口并跑 catalog hython probe。
2. 先修 gate 空通过，再写 `scratch_houdini_smoke.py`。
3. 写 deterministic Bridge journey。
4. 改写 scratch-native provider evidence/harness。
5. 跑真实 Agent 简单桌面三连验收。
6. 跑 HTML 草图两轮会话的椅子/书桌/货架。
7. 开始 typed 参数表达式和参数扫描。
8. 主链路稳定后再实施任务图谱与 Stage C。
