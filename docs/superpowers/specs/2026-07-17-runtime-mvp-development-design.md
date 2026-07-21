# Runtime MVP 安全交付与集成设计

**日期：** 2026-07-17
**状态：** 已获用户确认，待拆解为实施计划
**基线：** \`feature/runtime @ 69b3fd2\`
**目标：** 依照依赖关系，把 EEE Agent 从当前 Runtime/建模原型推进到可安全交付的 Runtime MVP，并完成 Knowledge Graph 集成、真实 Houdini 边界验证和正式入口收口。

## 1. 决策与范围

用户确认的产品优先级是：先做可安全交付的 Runtime MVP，再扩展 Vision/Evaluation 和最终 GUI。

已确认的关键决策：

- 旧 \`Open Agent Panel\`、旧 \`cli prompt/stdio\` 和 raw Houdini write tool 路径彻底下线，不作为正式产品入口。
- Runtime Agent 的只读查询必须迁移到 Secure Bridge；不能只保留名称上的 \`read_only_tools\` allowlist，而继续依赖旧未认证 rpyc。
- Knowledge Graph 纳入第一版安全 Runtime MVP，不延迟到之后的独立版本。
- ArtifactStore 必须先修复跨 SQLite/文件系统的一致性问题，再接入 Vision。
- sensitivity Bridge 必须有专门的真实 Houdini wire smoke，offline/fake/loopback 测试不能单独作为 production-ready 证据。
- \`feature/runtime\` 是当前集成主线；不从旧 \`main\` 或根目录 \`wip/pre-migration-main\` 重新开发。
- 分支清理在已打 tag、合并完成、联合测试通过之后进行；本地和远端已验收分支均可清理，tag 和 handoff 必须保留。

本设计不包含：

- 新增任意未验证的 Houdini SOP catalog 能力；
- 放宽模型权限；
- 让 Vision 覆盖 deterministic validator 失败；
- 在 GUI 验收前宣称 production-ready；
- 在未通过联合测试前删除 Knowledge Graph 唯一回滚分支。

## 2. 当前状态和主要问题

当前 Runtime 已包含 typed ChangeSet、审批、preflight、transactional Apply、receipt、rollback、restart recovery、modeling compiler、validation、Golden Cases、capture/artifact foundation。最新 Runtime 离线套件为 2317 passed；真实 Houdini capture、Bridge、changeset、bootstrap 和 Golden Case smoke 已通过。

Knowledge Graph 在独立 \`feature/houdini-knowledge-graph\` 分支完成，离线 831 passed，真实 HFS contract 11 passed，但尚未集成 Runtime。

必须优先关闭的已知问题：

1. \`ArtifactStore\` 在 SQLite transaction 中删除旧文件；若后续事务失败并 rollback，metadata 行恢复而文件已删除，产生可复现的“行存在、文件缺失”状态。
2. Runtime 的 \`read_only_tools()\` 仍来自旧 \`eee_agent/tools/*\`，底层导入旧 \`eee_agent.bridge.hou_client\`；因此新 Runtime 查询路径仍依赖未认证 legacy rpyc。
3. sensitivity \`sample\` 尚无专门真实 Houdini wire smoke。
4. Runtime 和 Knowledge Graph 分支存在 \`.gitignore\`、\`CLAUDE.md\`、\`README.md\`、\`eee_agent/tools/registry.py\`、\`pyproject.toml\` 等合并冲突。
5. 文档记录的测试数量、当前阶段和 Golden Case 数量落后于实际代码。

## 3. 目标架构

### 3.1 正式 Agent 写入路径

正式路径固定为：

\`\`\`text
用户描述模型
  -> Runtime Session/Run
  -> Secure read-only scene/workspace/knowledge query
  -> strict Modeling Brief/Spec
  -> developer-owned deterministic compiler
  -> typed ChangeSet + digest
  -> exact persisted approval
  -> internal trusted Apply
  -> Secure Bridge single FIFO/main-thread transaction
  -> receipt
  -> Cook/Geometry/Sensitivity/Semantic/Artifact validation
  -> Run result, evidence and recovery state
\`\`\`

模型不得直接获得：

- \`create_node\`、\`delete_node\`、\`connect_nodes\`、\`set_parms\`、\`set_vex\`；
- raw Apply；
- \`hou\` 或 rpyc proxy；
- Secure Bridge client、socket、SQLite、filesystem 或 shell；
- 任意 source execution。

### 3.2 Secure read-only 查询边界

Runtime Agent 的查询工具必须经过 typed Secure Bridge DTO 或本地受限 Knowledge Capability：

\`\`\`text
Agent tool
  -> bounded adapter
  -> typed DTO
  -> Secure Bridge / KnowledgeService
\`\`\`

工具返回值必须是有大小上限的普通 JSON/dict，不得泄漏 live HOM object、rpyc proxy、绝对本地路径或底层连接对象。

### 3.3 Knowledge Graph 边界

Knowledge Graph 是共享、可重建的本地 cache，不是 Run artifact。Runtime 负责：

- 启动检查 \`missing/stale/corrupt/schema_mismatch/ready\`；
- 发出 bounded status event，但不因 cache 不可用而阻塞 Runtime 启动；
- 将 \`kb_manifest_sha256\`、\`kb_schema_version\`、\`houdini_build\` 固化到 Run snapshot；
- 通过 Restricted Research Capability 提供 bounded search/body 查询；
- 保持 live \`describe_node_type()\` 为实际可创建节点和参数的最终权威。

模型不得直接访问 knowledge SQLite、HFS 文档目录或任意 document body。

### 3.4 Artifact 状态机

Artifact 注册和清理必须显式表达跨介质状态：

\`\`\`text
staged -> verified -> pending -> available
                         \\-> failed
available -> pending_eviction -> evicted
available -> missing
\`\`\`

不可回滚的文件操作不能被伪装成可回滚的 SQLite transaction。Runtime 重启时必须能够收敛 pending/orphan/missing 状态。

## 4. 开发阶段与依赖关系

阶段不得跳过；每阶段完成后必须提交实现、测试、handoff、门禁输出和 clean worktree。

\`\`\`text
S0 基线冻结
  -> S1 Secure read-only 迁移与 legacy 下线
  -> S2 Artifact 一致性修复
  -> S3 sensitivity 真实 Houdini wire smoke
  -> S4 Knowledge Graph Runtime 集成
  -> S5 Runtime MVP 联合验收
  -> S6 Vision/Evaluation
  -> S7 GUI 验收与发布清理
\`\`\`

### S0：基线冻结

输入：\`feature/runtime @ 69b3fd2\`。
输出：基线 tag、当前测试证据、当前 handoff、分支状态记录。

门禁：

- Runtime \`pytest -q\`；
- \`uv lock --check\`；
- \`compileall\`；
- \`git diff --check\`；
- worktree clean；
- \`.env\`、Runtime DB、cache、artifact 不在 Git；
- 记录 Runtime 2317 passed 和现有 Houdini smoke 结果。

### S1：Secure read-only 迁移与 legacy 下线

顺序必须是“先迁移安全查询，再删 legacy”，不能先删除旧工具导致 Runtime Agent 失去查询能力。

工作内容：

1. 新建 Secure read-only Agent tool adapter；
2. 将 Runtime \`AgentRunner\` 从旧 \`eee_agent/tools/registry.py\` 切换到新 adapter；
3. 让 scene/workspace/geometry 查询通过 Secure Bridge typed DTO；
4. 将 Knowledge 查询预留为同一 read-only capability 面；
5. 移除 \`build_agent()\` 隐式加载 \`all_tools()\` 的正式入口；
6. 删除 Houdini menu 的 \`Open Agent Panel\` 和 \`Start RPC Bridge Only\`；
7. 删除旧 \`cli prompt/stdio\` 产品入口；
8. 删除或移入 archive 的 \`start_rpc.py\`、\`chat_panel.py\`、\`launch.py\` 和旧 raw tool chain；
9. 保证任何正式入口都不能导入 \`eee_agent.bridge\`；
10. 增加静态导入、工具 allowlist、menu、CLI 和 zero-write 测试。

完成标准：

- Runtime Agent 不导入 \`eee_agent.bridge\`；
- Runtime Agent 不启动 legacy rpyc；
- Agent graph 中不存在 raw write tool；
- Secure read-only query 可通过真实 Bridge；
- Scene fingerprint 在 Agent 查询前后相同；
- legacy 产品入口和 raw write path 不可触达。

### S2：Artifact 一致性修复

工作内容：

1. 将 Artifact 元数据、文件放置、eviction 和 cleanup 设计成可恢复状态机；
2. 不在 SQLite rollback 可能发生的窗口内执行不可追踪文件删除；
3. 为 pending/evicted/missing/orphan 增加 durable 状态或 journal；
4. Runtime 启动时执行 artifact reconciliation；
5. 让 session cleanup 失败可重试；
6. Panel/后续 Vision 能区分 available、evicted、missing、failed；
7. 保持 ArtifactStore 的 hash/size/type verification；
8. 保证 panel/vision 读取同一份已校验字节。

必须新增的故障测试：

- retention 后 DB rollback；
- DB commit failure；
- rename 后进程失败；
- commit 后进程失败；
- duplicate artifact id/path；
- missing file after event replay；
- orphan file after restart；
- cleanup failure and retry；
- global/session retention 同时触发。

完成标准：

- 不存在“metadata 行存在但文件已丢失”的假成功状态；
- cleanup 失败可重试；
- restart 后状态可收敛；
- Artifact failure 不会伪装成视觉成功；
- 全 Runtime 套件保持通过。

### S3：Sensitivity 真实 Houdini wire smoke

新增独立真实 hython smoke，验证：

- \`sensitivity.v1\` capability；
- strict request/response；
- scene epoch/stale；
- target/parm preflight zero-write；
- sample write、force cook、bounded evidence；
- reverse-order exact restore；
- restore read-back；
- cook/interruption/restore failure；
- write freeze；
- disconnect/restart/no-replay；
- 最终 scene fingerprint 不变。

只有 offline、executor fault injection、loopback 和真实 Houdini wire smoke 全部通过，才能把 sensitivity 标记为 production-ready。

### S4：Knowledge Graph Runtime 集成

工作内容：

1. 为 Runtime 和 Knowledge Graph 最终 commit 创建 tag；
2. 将 Knowledge Graph 合并到 \`feature/runtime\`；
3. 手动解决 \`.gitignore\`、\`CLAUDE.md\`、\`README.md\`、\`registry.py\`、\`pyproject.toml\`、\`test_env_probe.py\` 冲突；
4. 将两个 Knowledge tools 加入 Secure read-only allowlist；
5. 将共享 cache path 纳入 \`RuntimePaths\`；
6. 增加 startup KB status event；
7. 增加 Run snapshot 的 KB manifest/schema/build；
8. 建立 Restricted Research Capability；
9. 禁止 Executor 直接读取文档 body；
10. 增加 Knowledge cache missing/stale/corrupt/schema mismatch 测试；
11. 运行 Runtime + Knowledge 联合测试和真实 HFS contract。

完成标准：

- Knowledge 查询正式进入 Runtime Agent；
- cache 不可用不阻塞 Runtime 启动；
- Run 可追溯 Knowledge 版本；
- body/path/query 都有 bounded contract；
- live catalog authority 不被 Knowledge 查询取代；
- 联合测试通过。

### S5：Runtime MVP 联合验收

使用真实 provider 执行至少一轮完整流程：

\`\`\`text
empty scene -> read-only query -> strict proposal -> exact approval
-> typed Apply -> receipt -> validation -> artifact -> event replay
\`\`\`

至少覆盖：

- 空场景 bootstrap；
- 已有 Workspace 增量建模；
- approve/reject/expired；
- stale scene；
- Bridge unavailable；
- cook/geometry/semantic failure；
- repair ticket/exhaustion；
- restart/recovery/no-replay；
- artifact capture failure；
- Knowledge cache unavailable/stale。

MVP 不能标记 production-ready，除非：

- 全离线套件通过；
- 真实 HFS contract 通过；
- Secure Bridge smoke 通过；
- sensitivity wire smoke 通过；
- capture smoke 通过；
- Golden Cases 通过；
- 真实 LLM end-to-end 通过；
- recovery 通过；
- 无 legacy import/入口/raw write tool；
- 文档和 CI 同步。

### S6：Vision/Evaluation

Vision Router 必须是 advisory-only：

- provider capability resolution；
- unavailable/waiver evidence；
- schema-validated normalized report；
- raw response redacted artifact；
- 输入来自 ArtifactStore 同一份 hash-verified bytes；
- 不得覆盖 deterministic validator 失败。

Evaluation 输出必须包含：

- brief/spec；
- ChangeSet digest；
- approval；
- receipt；
- ValidationReport；
- ArtifactRef；
- Knowledge manifest；
- Vision status/report；
- final decision/recovery evidence。

### S7：GUI 验收与发布清理

正式 UI 流程是：

\`\`\`text
MODEL -> REVIEW -> Approve and build -> Apply/validate -> Result/recovery
\`\`\`

Details/Inspector 中保留高级诊断信息，不再暴露第二个公开 Apply 按钮。

真实 Houdini GUI 必须由用户验收：

- dock/narrow layout；
- focus/Enter/Chinese IME；
- approval drawer/mouse；
- reconnect/restart；
- artifact list/details；
- error/recovery；
- empty-scene complete journey；
- no accidental scene mutation。

## 5. 分支和远端清理策略

分支清理必须在 S4 联合测试通过以后执行：

1. 确认两个 worktree clean；
2. 为 \`feature/foundation\`、\`feature/houdini-knowledge-graph\` 和 Runtime integration base 创建 tag；
3. 合并 Knowledge Graph 到 \`feature/runtime\`；
4. 运行联合测试、lock、compile、diff 和 HFS smoke；
5. 更新 handoff/README/CLAUDE；
6. push 集成 commit；
7. 删除本地已合并分支；
8. 删除远端 \`origin/feature/foundation\`；
9. 删除远端 \`origin/feature/houdini-knowledge-graph\`；
10. 保留 tags、handoff 和 \`feature/runtime\`。

以下分支暂不删除：

- \`main\`；
- \`wip/pre-migration-main\`；
- \`feature/runtime\`。

远端删除前必须检查：

\`\`\`powershell
git branch -r --merged feature/runtime
git log --oneline origin/<branch>..feature/runtime
git tag --contains <branch-head>
\`\`\`

远端删除是独立的发布清理动作，不得和代码合并混在同一个不可审计 commit 中。

## 6. 全局质量门禁

每个阶段和最终发布都必须运行：

\`\`\`powershell
uv run --frozen --extra eval pytest -q
uv lock --check
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
git diff --check
\`\`\`

需要补充 Windows CI：

- frozen dependency install；
- Runtime suite；
- Knowledge suite；
- compileall；
- lock check；
- diff check；
- lint/type/security checks；
- 可选 Houdini HFS contract/smoke。

任何阶段出现以下情况都必须停止推进并修复：

- raw write tool 回到 Agent graph；
- Runtime 导入 legacy bridge；
- stale/uncertain Apply 被重放；
- Artifact metadata 与文件状态不一致；
- deterministic validation 被 Vision 覆盖；
- Knowledge cache 版本无法进入 Run snapshot；
- GUI/真实 LLM 结果被离线测试替代性宣称。

## 7. 非目标和后续事项

本 MVP 不包含：

- per-component subagents B2；
- 不受 catalog 约束的 source/Python/VEX execution；
- arbitrary Houdini document execution；
- 自动将 main 重写成 Runtime 主线；
- 在没有用户 GUI 验收的情况下自动宣称产品发布。

完成 S7 后，再单独评估：

- B2 per-component subagents；
- richer asset-level Golden Cases；
- Vision provider 扩展；
- Knowledge semantic/embedding search；
- Runtime 模块拆分和性能优化。
