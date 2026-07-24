# 全链路代码评审 — 2026-07-24

范围:`eee_agent/` 全包、`houdini_side/`、`panel`/`python_panels`、`skills/`、`docs/`、`tests/`、`eval/`、配置文件。
基线:测试套件实测 `3452 passed, 11 skipped`(11 个 skip 均为 opt-in 的 Houdini HFS 知识库契约测试),无 broken import。

发现按优先级分组。每条标注 位置 / 类别 / 严重度 / 建议。

---

## P0 — 用户可见的实际 bug

### 1. 面板 "Rebuild KB" 按钮是死的(客户端校验器拒绝自己发的命令)
- `eee_agent/panel/client_state.py:43-63` `_PANEL_COMMANDS` 缺少 `knowledge.rebuild`,但面板会发送它:`houdini_side/runtime_panel/client.py:695` → `build_command` 抛 `PanelClientError`,被当成连接错误吞掉,命令永远到不了服务端。服务端完整支持(`runtime/protocol.py:24`、`runtime/server.py:476-489`),按钮存在(`runtime_panel/inspector.py:364`),系统提示词还推荐用户点它(`system_prompt.py:42-44`)。
- 类别:矛盾 · 高 · 修复:`_PANEL_COMMANDS` 加 `knowledge.rebuild` + `_validate_panel_payload` 分支(`{}` 或 `{"hfs": str}`),补面板客户端测试。

### 2. Houdini 侧 `technical_detail_ref` 被静默丢弃
- `houdini_side/secure_bridge.py:495-511, 1381-1384`:`_QueuedError.__slots__` 没有 `technical_detail_ref`,`_run_on_queue` 构造时丢弃该字段。而 `changeset_executor.py:870-879` `_policy_denied` 故意把策略拒绝码塞进 `technical_detail_ref` —— 客户端永远收不到。
- 类别:矛盾 · 高 · 修复:`_QueuedError` 增加该字段并透传到 `_error_envelope`(它本来就支持)。

### 3. PNG 捕获文件校验无大小上限(先改名后报错)
- `houdini_side/changeset_executor.py:1026-1047` `_verify_capture_file` 无界流式读 PNG;64 MiB 上限只在 agent 侧 `CaptureResult.__post_init__`(`houdini_bridge/capture.py:70,447`)才生效。超大渲染会先被 atomic rename 就位,然后才报一个含糊的 `bridge.internal_failure`。
- 类别:协议错位 · 中 · 修复:在 `_verify_capture_file` 内 enforce `size <= _MAX_PNG_BYTES`(rename 之前失败)。

### 4. `scratch.py` 方向检查静默丢弃未知 key
- `eee_agent/houdini_bridge/scratch.py:571-587` `_require_orientation_checks` 白名单过滤未知 key 而不报错,违背本模块自己的 "exact field sets" 严格性(docstring 27-29 行)。笔误的 key(如 `expected_axe`)被接受并忽略,Houdini 侧 gate 就在缺检查的情况下运行。
- 类别:矛盾 · 中 · 修复:未知 key 直接拒绝。

---

## P1 — 系统性漂移:guardrails 与知识管线还停留在 sandbox 之前的工具时代

### 5. `loop_guard` 完全失效,且升级指令让 agent 调用已删除的工具
- `eee_agent/loop_guard.py:37-40` `MUTATIVE = {"delete_node","create_node","set_vex","set_parms","set_expression","cook_node","connect_nodes"}` —— 这些工具在现有 allowlist(`runtime/agent_tools.py:351-361` + `scratch_build`/`scratch_commit`)里一个都不存在,guard 永远不会触发。升级文案还说 "call save_hip"(`loop_guard.py:107,112`)—— 已删除。
- 类别:矛盾/死代码 · 高 · 修复:`MUTATIVE`/`_sig` 改为按 `scratch_build`/`scratch_commit` 的 op kinds/targets 签名;更新指令文案。

### 6. 四个 skills 教的是已删除的原始写工具,且被强制打入知识库
- `skills/parametric-building/SKILL.md:13,17,33,46-47`、`procedural-components/SKILL.md:27-41`、`sop-cookbook/SKILL.md:9,35`、`vex-patterns/SKILL.md:63` 引用了 `scene_reset`/`create_node`/`set_parms`/`set_vex`/`save_hip` 等已删工具。而这四个文件被 `knowledge/sources.py:33-38` `REQUIRED_SKILL_PATHS` 强制打入 KB cache,提示词让 agent 建模前必查(`system_prompt.py:39-41`),同时 `system_prompt.py:116-118` 又承认 "The old procedural-building skill files describe deleted raw-write tools" —— 自相矛盾。更糟:当前 `scratch_build` catalog 没有 `blast`/`attribwrangle`/`scatter`,`set_parm` 只收字面量(不收表达式),所以 vex-patterns 和 `ch()` 驱动的工作流根本执行不了。
- 类别:矛盾/过时文档 · 高 · 修复:按 `scratch_build`/`scratch_commit` 真实能力重写或隔离四个 SKILL.md,再重建 KB。

### 7. `context_trim.READBACK_TOOLS` 半过时
- `eee_agent/context_trim.py:38-41`:`anchor_graph`、`describe_node_type`、`hou_status`、`validate_geometry` 已不存在(6 项死 4 项);当前可重推导的回读工具 `scene_status`/`query_scene`/`inspect_workspace` 反而不在裁剪列表。
- 类别:漂移 · 中 · 修复:换成当前回读工具名。

### 8. ContextSeek 种子课程教已删工具
- `eee_agent/context_store.py:69-82` 三条 lesson 描述 `set_vex`/`cook_node` 循环,会在 `EEE_CONTEXTSEEK=true` 时注入 agent 记忆。
- 类别:过时 · 中 · 修复:按 scratch 工作流重写。

---

## P2 — 严格 JSON helper 漂移(其他开发者已发现的问题,确认并扩大)

### 9. 同一套 strict-JSON 校验 helper 有 8 份拷贝,消息/结构已漂移
- 拷贝位置:`houdini_bridge/contracts.py:88-123`(权威)、`runtime/protocol.py:111-122`、`changesets/repository.py:283-303`、`houdini_bridge/workspaces.py:110-135`(与权威同包!)、`modeling/contracts.py:1153-1186`、`vision/provider.py:21-33`、`panel/client_state.py:70-96`、`houdini_side/secure_bridge.py:416-426`。
- 漂移点:dup-key 消息四种("duplicate object key"/"duplicate key"/"duplicate response field"/"duplicate JSON key");`workspaces.py` 单 try 把 UTF-8/JSON 错误合并为 "must be strict JSON" vs contracts 分开报;大小上限不一(256 KiB / 1 MiB / 无);dict 构建 vs set 两种实现。commit `1360dd2` 只去重了 `houdini_bridge/changesets.py` + `client.py`(M10 做了一半)。
- 类别:重复漂移 · 高 · 修复:提取共享模块(如 `eee_agent/core/strict_json.py`,参数化 label/大小上限/错误类型),删除全部本地拷贝;`workspaces.py` 只保留 `_bounded_text`。

### 10. 两个 "canonical JSON loader" 对重复 key 标准不一
- `runtime/models.py:178` `canonical_json_loads` = 裸 `json.loads`,不拒重复 key;`changesets/repository.py:296` `_loads_canonical` 同样声称 "parse canonical JSON" 但拒绝重复 key。持久化的 changeset 与持久化的 event/run 被不同标准对待,且无文档说明。
- 类别:矛盾 · 高 · 修复:让 `canonical_json_loads` 也拒重复 key(与严格的 `canonical_json_dumps` 配对),repository 改为 import 它。

### 11. 校验原语/正则大面积重复
- `_require_exact_int/_bool/_mapping`、`_require_identifier` 同时存在于 `modeling/contracts.py:83-127` 和 `changesets/contracts.py:117-145`;`_SHA256_RE` 出现 8 处;ID 正则(`ses_`/`run_`/`chg_`/`ws_`)在 `panel/client_state.py:38-42`、`panel/runtime_state.py:53-57`、`runtime/server.py:44-47` 三处重复,尽管 `core/ids.py` 是权威;`_MAX_PARM_VALUE_BYTES` 两处;`_require_exact_list` 消息漂移("must be a list" vs "must be an exact list");请求信封 `_REQUEST_FIELDS` 三处逐字重复;`_MAX_DEADLINE_MS` 上限在三处重声明 + 三个 provider 用裸字面量 `30_000`。
- 类别:重复漂移 · 中 · 修复:收敛到 `core/ids` 与一个 contracts-common 模块;常量一律 import 不复制。

### 12. freeze/thaw 在 core 与 runtime 各一份且已漂移
- `core/events.py:31-81` vs `runtime/models.py:106-157`(models.py:101 注释自认 "Mirrors eee_agent.core.events")。漂移:`active.remove` vs `active.discard`;错误消息不同。
- 类别:重复漂移 · 中 · 修复:core 导出、runtime import。

### 13. SHA-256-of-canonical-JSON 实现 4 份
- `changesets/contracts.py:167`、`changesets/repository.py:306`、`changesets/service.py:916`、`houdini_bridge/workspaces.py:333`。
- 类别:重复 · 低 · 修复:一个 `canonical_digest(value)` helper。

---

## P3 — 矛盾/未对齐的常量和契约

### 14. Vision 超时默认值 `30.0` 写死在 4 处
- `config.py:101`、`runtime/__main__.py:190`、`runtime/service.py:473,652`、`vision/router.py:119`;`0.1..120.0` 边界也两处。
- 类别:漂移 · 中 · 修复:`config.py` 定义 `DEFAULT_VISION_TIMEOUT_SECONDS`,各处 import。

### 15. LLM/vision 默认模型名在 `config.py` 内部重复
- `config.py:45-49` vs `config.py:83-86`,同一字面量两组 dict,升级模型要改两处。
- 类别:漂移 · 中 · 修复:一个 `_DEFAULT_MODELS`,vision 取子集。

### 16. 终态 run status 定义 3 份
- `runtime/runs.py:36-41`、`runtime/events.py:24-28`、`runtime/service.py:170-172`。
- 类别:重复 · 中 · 修复:在 `models.py` 的 `RunStatus` 旁定义唯一 `TERMINAL_RUN_STATUSES`,其余派生。

### 17. 结构化错误实例重复
- `internal.runtime_failure` AgentError:`runtime/service.py:177-181` vs `runtime/server.py:59-63` 逐字重复;`runtime.interrupted`:`runtime/runs.py:56-62` vs `runtime/service.py:185-189`,仅靠注释保持一致;`_invalid_envelope`:`protocol.py:79-84` vs `server.py:66-73`。
- 类别:重复 · 中 · 修复:各定义一次、互相 import,并加相等性测试。

### 18. 面板是协议的第二份独立实现,无漂移防护
- `panel/client_state.py:22-23` 重声明 `PROTOCOL`/`MAX_MESSAGE_BYTES`;`_PANEL_COMMANDS` 手工维护 `runtime/protocol.py:21` `COMMAND_TYPES` 的子集,无任何 import 或断言保证一致(F1 就是这个结构问题的直接后果)。`server.py:44-47` 同样重声明 ID 正则而不用 `core/ids.require_id`。
- 类别:架构 · 中 · 修复:面板 import 协议常量;加测试断言 `_PANEL_COMMANDS ⊆ COMMAND_TYPES`;中期抽 `runtime/wire.py` 供 Qt-free 客户端复用。

### 19. Bridge 错误 category 是无校验的自由字符串
- `houdini_bridge/contracts.py:469,477` `BridgeError.category` 只查非空;生产方写裸字面量(如 `read_only_provider.py:277` `category="stale_scene"`),与 `core/errors.ErrorCategory` 无任何绑定,可静默漂移。`workspaces.py:86-103` `WorkspaceInspectError` 又是第三个 bespoke 错误 DTO。
- 类别:改进 · 中 · 修复:BridgeError 构造边界校验 category 属于 ErrorCategory 值集。

### 20. Provider 适配器对同类配置错误风格不一
- `deepseek_v4.py:75-83` 抛结构化 `AgentException(PROVIDER_CONTRACT)`;`anthropic.py:28`、`openai.py:25` 对等价错误抛裸 `ValueError`。
- 类别:不一致 · 中 · 修复:anthropic/openai 也用 `_configuration_error` helper。

### 21. `untitled` sentinel 疑似永远不匹配(需真机验证)
- `houdini_side/secure_bridge.py:103` `_HIP_UNSAVED_SENTINEL = "untitled"`;未保存场景的 `hou.hipFile.name()` 通常是 `untitled.hip` 甚至完整临时路径,274 行的相等比较可能永不命中,导致 `SceneBinding.hip_path` 携带假路径而非 `None`。
- 类别:疑似协议错位 · 中 · 修复:按 basename stem 匹配;在真实 Houdini 上验证。

---

## P4 — 死代码

| # | 位置 | 说明 | 建议 |
|---|---|---|---|
| 22 | `houdini_bridge/changesets.py:273-279, 296-343` | 6 个解码 wrapper 无任何调用点(`_decode_dt`/`_decode_condition`/`_decode_risk`/`_decode_parm_snapshot`/`_decode_wire_snapshot`/`_decode_checkpoint`),注释 "kept so existing call sites are unchanged" 已半过时;其 imports 仅为它们服务 | 删除 6 个函数及专用 imports,或把注释明确指向唯一引用它们的测试 |
| 23 | `providers/events.py:34,47,52` | `ToolCallCompleted`/`ModelCompleted`/`ModelFailed` 全代码库从未构造 | 删除并收缩 union |
| 24 | `providers/contracts.py` | `ModelRole`/`VerificationStatus`/`RoleBindings`/`VerificationCheck`/`ModelVerification` 无生产消费者(只有测试),却公开 re-export | 删除或标 experimental |
| 25 | `workflow_middleware.py` + `app.py:82-87` | `is_enabled()` 硬编码 `False`、`_status_block()` 恒返回 `""` —— 墓碑模块仍挂在装配链上 | 删模块 + app.py 块(已有测试 pin 它 disabled) |
| 26 | `modeling/proposal.py` + `runtime/service.py:1811-1847` | 退役的 `propose_modeling` 仍每次 modeling run 构造 context/coordinator,`modeling/__init__.py:111-128` 仍公开导出,并拖着一串只被测试用的编译/repair 函数 | 决定复活或删除;删则 proposal.py/service seam/exports 一起下 |
| 27 | `system_prompt.py:97-112` | `_strip_frontmatter`/`_read` 失去唯一调用者,`os`/`repo_root` imports 仅为 `_read` 服务 | 删除 |
| 28 | `houdini_bridge/workspaces.py:401,434` | `scene_may_have_changed` 参数形同虚设:`__post_init__` 无条件拒绝 `True` | 删参数,或注明是刻意的 fail-closed wire 字段 |
| 29 | `runtime/service.py:129-134` | `_LegacyRunner` 协议只为注入测试 double 存在 | 让测试 double 遵从 `AgentRunner`,删并行协议 |
| 30 | `secure_bridge.py:888-889` | `request_id = obj.get("request_id")` 连续写两遍 | 删一行 |
| 31 | `houdini_bridge/read_only_provider.py:54` | `_STALE_RETRY_CODES` 含该 provider 永远不会触发的 `bridge.binding_mismatch` | 删除或注释说明 |
| 32 | `vision/contracts.py:247`、`knowledge/service.py:171+` | `RedactedRawResponseRef` 只在测试构造;`KnowledgeService.neighbors` 无 Runtime 出口 | 裁剪或接线 |

---

## P5 — 过时文档/注释(代码内)

| # | 位置 | 问题 |
|---|---|---|
| 33 | `houdini_bridge/__init__.py:1-9` | 还说 "read-only bridge … future authenticated transport (15-B)…" —— 三者都已实现,bridge 已能写 |
| 34 | `secure_bridge.py:387-397, 514, 743-755` | banner/类 docstring 还说 "Only scene.query … never mutated";实际 dispatch 10 个操作含写操作;`_serve` docstring 漏列 3 个 `scratch.*` |
| 35 | `houdini_side/changeset_executor.py:1-30` | 模块 docstring 标题 "Read-only … performs no mutation",但模块内的 `ChangeSetExecutor` 全是写操作 |
| 36 | `houdini_bridge/scratch.py:605-607` | docstring 说 promotion 在单个 `hou.undos.group` 内原子回滚;executor 明确文档化 undos.group 不是事务,实为 promote 前拒绝 + 日志式名称恢复 |
| 37 | `houdini_bridge/changesets.py:1079-1081` | 说 `scene_epoch` 不 gate;实际 executor 对 epoch 不匹配 fail-closed |
| 38 | `houdini_bridge/capture.py:15-16` | 说写 `<name>.png.tmp` 再 rename;实际是 sibling temp dir `.tmp_<artifact_id>/` + `os.replace` |
| 39 | `runtime/agent_runner.py:27` | 注释引用已删除的 CLI preview 行为;真正的第二份 600 字符截断在 `panel/runtime_state.py:632`(硬编码) |
| 40 | `harness.py:1-6` | docstring 说 Foundation "keeps the existing tool surface for compatibility",与 `app.py:1-7` "intentionally not a compatibility fallback" 矛盾 |
| 41 | `config.py:123` | 引用 CLAUDE.md 的 "Known limitation" 一节 —— 不存在 |
| 42 | `panel/client_state.py:5`、`knowledge/api.py:4` | 引用 Task 17-A / "Stage 6" 等历史标签 |
| 43 | `houdini_side/secure_bridge.py:337-343` vs `workspace_inspector.py` | `_read_is_locked` 吞所有异常返回 `False` vs inspector 严格校验 —— 同一数据两种严格度;6 个 `eee.*` mirror key 常量在两处定义不同名字 |

---

## P6 — 文档基线与入口文档漂移

- `CLAUDE.md:18-19`、`SETUP.md:124-125`、`README.md:30`:测试基线写的 3446 passed / 12 skipped,实测 3452 / 11。
- `README.md:218,223-224`、`SETUP.md:235-236`:仍把 "交互式 GUI checklist" 和 "Vision real-provider journey" 列为未过的 gate,但 Stage B 验收(`9fc058b`,已合入 main)记录 B-07/B-08 均 PASS。当前真正剩下的 gate 是 real-Houdini sandbox smoke(见最新 handoff)。
- `docs/handoffs/2026-07-23-runtime-stage-b-pause-handoff.md:31`:自称 "current entry",已被同日及 07-24 handoff 取代;应加 superseded 横幅。其余 24 份历史 handoff 与代码一致,无需动。
- `README.md:91,226`:layout 漏 `eval/knowledge/`(50 条 golden query 的 KB 评估器);"Eval framework ⏳ scaffold" 低估现状。
- `eval/run_eval.py:6`、`eval/cases/house.yaml:3`:推迟理由引用早已完成的 "Runtime MVP S6";`--dry` 在 fresh clone 上 0/2(output/ 被 gitignore,无 house.obj/tower.obj fixture)。
- `.env.example`:缺一组有代码消费者的高级开关(`EEE_RUNTIME_HOME`、`EEE_TRACING_PROJECT`、`EEE_TRIM_READBACKS`、`EEE_LOOP_*`、`EEE_COMPACT_TOOL`、`EEE_WORKFLOW_STATUS`、`EEE_HFS` 等)—— 均为可选,建议加 commented "advanced tunables" 一节。`scripts/env_probe.sh:46` 的 `EEE_PROBE_ENV_FILE` 无文档。
- `docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md:128,152`:引用不存在的 `python -m eee_agent.knowledge.build_kb`(实为 `knowledge/build.py`)。
- `tests/test_cli_stream_events.py`:文件名误导,实际断言的是 legacy CLI 模式已删除。

---

## 已验证无问题(不需要动)

- Bridge 线协议两端一致:帧格式、`MAX_MESSAGE_BYTES`、hello/ack、10 个 operation 的 dispatch 与能力门、`PROTOCOL`/deadline 常量单值。
- Manifest 校验链:`_decode_manifest` → `codec.decode_workspace_manifest` → `WorkspaceManifest.__post_init__` 单一权威,无重复校验逻辑残留。
- Executor 操作覆盖面:agent 侧契约字段(含 `expected_old_value` 等)与 Houdini 侧 `_execute_op` 精确匹配;6 种 condition 全部实现。
- workspace inspector → `WorkspaceInspectResult.build` 的 canonical order/revision hash 与 agent 侧 `from_dict` 复核一致。
- `python_panels/EEEAgentRuntime.pypanel` 不是平行实现,是 36 行注册 stub(小瑕疵:`MainMenuCommon.xml:21` 用 `import runtime_panel` 而 pypanel 用 `from houdini_side import runtime_panel`,同进程两个模块对象,建议统一)。
- 面板↔服务端事件词表、run 状态机、changeset 命令 payload 校验全部匹配;除 `knowledge.rebuild` 外的命令子集是有意为之。
- `pyproject.toml`:11 个运行时依赖全部被使用,无未声明 import;ruff/CI/依赖基线测试健康。
- 安装脚本、`MainMenuCommon.xml` 菜单映射、`memory/AGENTS.md`、Python/Houdini 版本声明全部与代码一致。

---

## 建议的落地顺序

1. **P0 四条**(面板按钮、technical_detail_ref、PNG 上限、orientation key 严格化)—— 都是小改动、直接修用户可见或安全相关行为。
2. **P1 三条**(loop_guard、skills、context_trim/context_store)—— 让 guardrails 和 KB 与 scratch 工具时代对齐;skills 改完需要 KB rebuild。
3. **P2 strict-JSON 收敛**(共享模块 + canonical_json_loads 加 dup-key 拒绝)—— 一次性消除最大漂移源,附带解决 #11-13 的大部分。
4. **P3 常量/错误收敛**,每条都配一个防漂移测试(面板命令子集断言、interrupted error 相等断言等)。
5. **P4 死代码删除** + **P5/P6 文档刷新**,可合并成一次 "neat-freak" 收尾。

---

## 处理结果（2026-07-24 修复轮）

### 已修复

- **P0-1** 面板 `_PANEL_COMMANDS` 加入 `knowledge.rebuild` + payload 校验分支 + 回归测试（`tests/panel/test_client_state.py`）。
- **P0-2** `_QueuedError` 增加 `technical_detail_ref` 并透传到全部 10 个 dispatch 点 + 回归测试。
- **P0-3** `_verify_capture_file` 在 atomic rename 前强制 `_MAX_PNG_BYTES`（常量从 `capture.py` 导入，不再两处定义）+ 回归测试。
- **P0-4** `_require_orientation_checks` 拒绝未知 key（白名单改为封闭集合）+ 回归测试。
- **P1-5** `loop_guard` 重定向到 `scratch_build`/`scratch_commit`（按 op 序列与 commit 目标签名），删除 `save_hip` 指令文案。
- **P1-6** 四个 `skills/*/SKILL.md` 全部按 catalog + scratch 工作流重写；`system_prompt.py` 旧注释更新。
- **P1-7** `context_trim.READBACK_TOOLS` 换成现行回读工具（scene_status/query_scene/inspect_workspace/geometry_stats/work_status）。
- **P1-8** `context_store` 种子课程重写为 scratch 工作流，marker 版本化（v2）使重种子生效。
- **P2-9** 新建 `eee_agent/core/strict_json.py` 单一权威；8 份拷贝全部改为 import（contracts 保留私有别名兼容）。
- **P2-10** `canonical_json_loads` 现在拒绝重复 key；repository `_loads_canonical` 委托给它。
- **P2-12** freeze/thaw 收敛：`core/events.py` 为唯一实现，`runtime/models.py` 改为别名。
- **P2-13** `canonical_digest()` 加入 `runtime/models.py`，3 处 digest 实现收敛（`_sha256_hex(text)` 签名不同，保留）。
- **P3-14/15** vision 超时与默认模型表收敛到 `config.py`（`DEFAULT_VISION_TIMEOUT_SECONDS` 等 + `_DEFAULT_MODELS`）。
- **P3-16** `TERMINAL_RUN_STATUSES` 单一定义于 `runtime/models.py`，runs/events/service 派生。
- **P3-17** `_INTERRUPTED_ERROR`（runs 拥有，service import）、`_RUNTIME_FAILURE_ERROR`（service 拥有，server import）、`_invalid_envelope`（protocol 拥有，server import）全部单一化。
- **P3-18** 面板 `PROTOCOL`/`MAX_MESSAGE_BYTES` 改为从 `runtime.protocol` import。
- **P3-20** anthropic/openai adapter 改用与 deepseek 相同的结构化 `AgentException(PROVIDER_CONTRACT)`。
- **P3-21** `_hip_path` 未保存场景按 basename stem 匹配 `untitled`/`untitled.hip`（等值比较疑似永不命中；真机验证仍待 real-Houdini smoke）。
- **P3 附加** graceful timeout 常量、deadline 上下界（`MIN/MAX_DEADLINE_MS` 从 contracts 导出，10 处收敛）、`_MAX_RESULT_BYTES`→`_MAX_TOOL_RESULT_BYTES` 改名。
- **P4-22** 删除 `houdini_bridge/changesets.py` 6 个无调用点 decoder 及专用 imports（`_decode_owned/_decode_noderef/_decode_operation` 因 codec 对等测试保留）。
- **P4-23** 删除 `ToolCallCompleted`/`ModelCompleted`/`ModelFailed` 死事件。
- **P4-24** 删除 `ModelRole`/`VerificationStatus`/`RoleBindings`/`VerificationCheck`/`ModelVerification` 及对应测试（验证特性落地前无消费者）。
- **P4-25** 删除 `workflow_middleware.py` 墓碑模块 + `app.py` 装配块；测试改为断言模块不存在。
- **P4-27** 删除 `system_prompt.py` 的 `_strip_frontmatter`/`_read` 死 helper。
- **P4-28** `WorkspaceInspectResult.build()` 删除恒为 False 的 `scene_may_have_changed` 参数（wire 字段保留，fail-closed 校验保留）。
- **P4-30** 删除 secure_bridge 重复的 `request_id` 赋值行。
- **P5-33..42** 全部过时 docstring/注释已改（包级、BridgeServer、_serve、changeset_executor、scratch commit、ReceiptRequest、capture、agent_runner、harness、config、panel client_state、knowledge api）。
- **P5 附加** `TOOL_RESULT_PREVIEW_CHARS` 移到 `runtime/models.py`，agent_runner 与 panel runtime_state 共享；`scratch_verify.py` 的 attribwrangle 拒绝提示改为说明 catalog 无属性写节点；`_canonical_dumps` 委托 `canonical_json_dumps`。
- **P6** 入口文档 gate 状态修正（B-07/B-08 已 PASS）、pause handoff 加 superseded 横幅、eval 文档与布局补全、`.env.example` 增加高级开关节、`EEE_PROBE_ENV_FILE` 文档化、`tests/test_cli_stream_events.py` 改名 `test_cli_removed_modes.py`、`knowledge/service.py` `_SHA256_RE` 锚定、`EMPTY_SESSION_TITLE` 面板侧镜像常量 + 防漂移测试。
- **防漂移测试**新增 `tests/runtime/test_shared_authorities.py`（终态/结构化错误/面板协议常量与命令子集/会话标题）。

### 有意保留 / 结论修正

- **#19 BridgeError.category** 维持自由字符串：bridge 类别（policy/deadline/stale_scene 等）是有意的 wire 分类，不映射 `ErrorCategory`；强制枚举会误伤合法类别。
- **#26 propose_modeling** 保留：CLAUDE.md 明确记录 "retired from the agent graph (module retained)"，是项目决定；service seam 注释已说明无消费者。
- **#29 _LegacyRunner** 保留为测试注入 seam，docstring 已改写说明其结构性用途。
- **#31 `_STALE_RETRY_CODES` 含 `bridge.binding_mismatch`**：评审结论有误 —— 该 code 由 `read_only_provider.py:281` 自身抛出，重试集合是正确的，未改动。
- **#32** `KnowledgeService.neighbors` / `RedactedRawResponseRef` 保留（低价值公开 API，留给后续接线）。
- **#43** inspector 严格 vs adapter 宽松的 `_read_is_locked` 维持现状（严格度差异是有意的分层）。

### 新发现的 surface gap（超出本评审范围，建议立项）

- scratch catalog **无法创建 prim 属性**（无 attribute-writing 节点），因此 `component_id`/`edini_world_axis` 永不存在，bake 与 orientation gate 对 catalog 构建永远 vacuously pass；`orientation_checks` 对 catalog 构建实际是惰性的。SKILL.md 已如实说明，但这意味着两道硬 gate 目前形同虚设，需要 catalog 扩展或 gate 重设计。
