# Wave C（C2–C7）完整实施与测试清单

制定日期：2026-07-27。执行分支：`feature/html-to-houdini-pipeline`（worktree
`.worktrees/html-to-houdini`，本文档也在该 worktree 内）。本清单面向执行 agent：
每项给出目标、设计决定（已定，勿擅自更改）、实现清单（文件级）、测试清单、
出口标准。证据纪律遵循 `2026-07-26-hython-agent-chain-roadmap.md` §6：
offline / hython / real provider / GUI 分层标注，未跑的层写 `not_run`，
禁止折叠成上一层的通过。

关联文档：

- 总 roadmap：`docs/superpowers/plans/2026-07-26-hython-agent-chain-roadmap.md`（C 波次定义在 :332-361）
- 当前状态入口：`docs/handoffs/2026-07-27-cross-machine-development-handoff.md`
- C1（typed 表达式）已完成，是本波次表达式能力的地基。

## 0. 全局约定（C2–C5 共用，先读再动手）

### 0.1 组件结构约定（C2 落地，C3–C6 依赖）

- 沙箱内每个组件一个 SOP `subnet`：`wheel`、`frame`、`cockpit`……
- 组件内部：各自的生成/整形链，链尾可选局部 OUT null。
- 顶层：一个 `merge`（命名 `assembled`）汇总各 subnet 输出 + 一个最终
  `null`（命名 `OUT`），**OUT 是顶层唯一 sink**。`_scratch_output_node`
  （`houdini_side/changeset_executor.py:2616`）不改逻辑，依赖此约定。
- 嵌套寻址：ops 内用沙箱相对 ref（`wheel/tube1`）；`create_node` 的
  `node_name` 永远扁平，嵌套一律经 `parent` 表达。

### 0.2 参数载体约定（C3/C4 落地，C5 依赖）

design-intent 参数是**组件 subnet 的公开 spare parameters**，不是隐藏的
几何节点或控制 box：

- `declare_parm` 只允许在 `subnet` 或 sandbox `geo` 容器上增加有界浮点参数；
  参数名、label、unit、default、min、max 都经过 DTO 校验。
- 公开参数直接显示在组件 subnet 的参数面板中，例如 `wheel_width`、
  `frame_width`；manifest 的 `binding` 指向 `wheel/wheel_width`。
- derived 维度 = 几何节点 parm 上的 C1 `expr`，通过相对 ref
  （`../wheel_width`）引用所属 subnet 的公开参数；executor 渲染为绝对路径
  `ch()`。
- 不再创建或要求用户查看 `<component>_ctrl` 节点；顶层仍保持唯一 `OUT` sink。
- constant 维度 = 直接烘焙的字面量。

### 0.3 参数清单（Parameter Manifest）schema（C3 定义 DTO，C4 扩展，C5/C7 消费）

挂在 `ScratchCommitRequest` 上的可选字段 `parameters`：

```json
{
  "name": "seat_width",
  "tab": "Seat",
  "classification": "design_intent | derived | constant",
  "binding": {"node": "seat", "parm": "seat_width"},
  "default": 0.45,
  "min": 0.35,
  "max": 0.60,
  "depends_on": ["seat_width"],
  "unit": "m | deg | count"
}
```

校验规则（DTO 层 fail-closed）：

- `name` 匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，全清单唯一；`tab` 同字符集，缺省 `"Main"`。
- `design_intent`：必须有 `min/default/max` 且 `min <= default <= max`（全部有限数）。
- `derived`：必须有非空 `depends_on`，且每个名字都在清单内；`min/max` 可空。
- `constant`：只有 `default`，禁止 `min/max/depends_on`。
- `binding.node` 走 `_require_node_ref`（沙箱相对，禁 `..`/空段/绝对路径），
  `binding.parm` 走现有 parm 名校验。
- 整个清单序列化后 ≤ 8 KB，条目 ≤ 64。

持久化（不改 DB schema）：commit 时 executor 把清单 JSON 作为**容器根节点的
node comment** 写入（hou `setComment`， bounded）；coordinator 在
`ScratchCommitResult.receipt` 里回显。C5 扫描 harness 从 .hip 场景里读
comment 还原清单；C7 从扫描报告 + 案例 YAML 取数。**不新增 SQLite 表、
不做 migration。**

### 0.4 命名与边界

- 所有新 wire 字段都是可选的：旧扁平构建路径行为逐字节不变（回归靠现有
  3549 离线测试 + 既有 smoke 证明）。
- op 上限 `_MAX_OPS_PER_CALL = 64` 不变；清单条目独立计数。
- 禁止在 DTO/executor 里引入任意字符串表达式、文件路径、Python/VEX 执行
  （沿用 C1 的安全边界）。

---

## 1. C2 — 组件化组织（subnet 分组）

目标（roadmap :339）：wheel/frame/cockpit/drivetrain 等组件放进 SOP subnet。
方案已定：**SOP subnet + 沙箱相对嵌套寻址**（不做 netbox 方案）。

### 1.1 实现清单

**A. Wire/DTO — `eee_agent/houdini_bridge/scratch.py`**

- [ ] `set_parm.node_name`、`connect.node_name`、`delete_node.node_name` 的
  校验从 `_require_node_name`(:119）换成 `_require_node_ref`(:179)
  （允许 `/`，沿用 traversal 拒绝：`..`、空段、反斜杠、绝对路径）。
- [ ] `create_node.node_name` 保持 `_require_node_name` 不变。
- [ ] `ScratchCommitRequest.annotations`(:843-884）的键从 `_require_node_name`
  放宽为 `_require_node_ref`。

**B. Executor — `houdini_side/changeset_executor.py`**

- [ ] 新增 helper `_resolve_scratch_node(container_path, node_index, ref)`：
  先 `node_index.get(ref)`，再 `hou.node(f"{container_path}/{ref}")`。
- [ ] `scratch_exec`(:2216-2276）四个 op 分支统一改走 helper —— 修掉
  `parent` 跨调用静默回落容器根（:2218）和嵌套节点跨调用不可达
  （:2227/2249/2255/2267）两个既有缺陷。
- [ ] `create_node` 注册 node_index 用相对路径键：
  `key = f"{op.parent}/{op.node_name}" if op.parent else op.node_name`；
  无 parent 时键不变（兼容扁平用法）。
- [ ] `_finalize_commit`(:2666)：布局递归一层 —— 直接子节点是 subnet 的，
  对其 children 再跑 `_layered_layout`；annotations 匹配从 basename 改为
  相对路径（`node.path()` 去掉 `container.path() + "/"` 前缀）。
- [ ] `_scratch_output_node`(:2616)：不改逻辑；docstring 补一句组件结构下
  "顶层 OUT 是唯一 sink" 的预期。

**C. Coordinator — `eee_agent/modeling/scratch_coordinator.py`**

- [ ] `_record_build`(:244-254)：记录路径改为
  `sandbox_root + (op.parent + "/" if op.parent else "") + op.node_name`。
  subnet 容器本身也是 `create_node`，自动入库 → cleanup allowlist 天然包含它。
- [ ] `_commit_annotations`(:262-282)：键改为沙箱相对路径（`node.node_path`
  去掉 `sandbox_root + "/"` 前缀），消除跨 subnet 同名覆盖。
- [ ] `_cleanup_execute` 沙箱分支（:376-415）：`delete_node` op 传沙箱相对
  ref 而非 basename；删除 "nested sandbox deletion is unsupported" 跳过分支；
  保留现有 containment/traversal 校验；删除目标按路径深度降序排序
  （叶子先于 subnet 容器，避免父先删导致子 not found 噪音）。
- [ ] `scratch_build` docstring(:624-642)：补组件用法（subnet + parent +
  嵌套 ref），并修正 "literal values only — no expressions" 的过时表述
  （C1 已支持 `expr`，docstring 与能力不一致是已知陈旧点）。

**D. Skill — `skills/procedural-components/SKILL.md`、`skills/sop-cookbook/SKILL.md`**

- [ ] 新增组件结构约定段落（见 §0.1），替换/修订 "flat named chains per part"
  段落（procedural-components :16-28、sop-cookbook :97-104）。
- [ ] 不新增 skill 文件；若新增必须同步
  `eee_agent/knowledge/sources.py:33-39` 的 `REQUIRED_SKILL_PATHS` 穷举白名单。

### 1.2 测试清单

离线（`uv run --frozen --extra eval pytest -q`，基线 3549 passed / 11 skipped，
只允许新增）：

- [ ] `tests/runtime/test_scratch_bridge.py`：DTO 校验 ——
  set_parm/connect/delete_node 接受 `wheel/tube1`；拒绝 `../escape`、
  `/abs/path`、空段（`a//b`）、反斜杠；annotations 键接受嵌套 ref、拒绝 traversal。
- [ ] coordinator 测试（新增或扩展既有文件）：
  - `_record_build` 记录嵌套路径（带 parent 的 create_node）；
  - `_commit_annotations` 按相对路径键控，两个 subnet 内同名节点不互相覆盖；
  - `_cleanup_execute` 嵌套沙箱目标真正删除且深度降序（该分支目前无测试，
    顺带补上）；嵌套 committed 目标走全路径 allowlist 删除。
- [ ] `tests/runtime/test_task_graph.py`：嵌套路径 record → mark_committed
  （suffix 重写，`/obj/eee_scratch_run_1/wheel/tube1` →
  `/obj/bike1/wheel/tube1`）→ mark_deleted 全链路。
- [ ] executor-fake 测试：`_resolve_scratch_node` 跨调用解析 parent/ref；
  `_finalize_commit` 嵌套 annotations 按相对路径匹配。

真机 hython（本机 Houdini 21.0.440，`D:\houdini`）：

- [ ] 新增 `tests/runtime/component_houdini_smoke.py`：两个组件 subnet
  （各 2–3 个 SOP + 内部连线）+ 顶层 merge/OUT → commit → 断言：
  committed 嵌套路径存在；`scene_topology` 返回嵌套边；display flag 在 OUT 上；
  结构 gate 通过；cleanup 删掉嵌套叶子和空 subnet。打印 `COMPONENT SMOKE OK`。
- [ ] 回归：`tests/runtime/task_graph_houdini_smoke.py`、scratch bridge
  journey 既有 smoke 全过（证明扁平路径行为不变）。

### 1.3 出口标准

离线 gate 全绿（计数 ≥ 基线）；`COMPONENT SMOKE OK`；既有 smoke 回归通过；
真实 Provider 层标 `not_run`（留 C 波次末统一验收）。

---

## 2. C3 — 参数分类（design intent / derived / constant）

目标（roadmap :340）：每个维度归类为 design intent（暴露为参数）/
derived（表达式派生）/ constant（烘焙）。产出物 = §0.3 的 Parameter
Manifest DTO + ctrl 节点约定 + skill 教学。

### 2.1 实现清单

**A. Manifest DTO — `eee_agent/houdini_bridge/scratch.py`**

- [ ] 新增 `ScratchParmDeclaration`（frozen dataclass）按 §0.3 schema 与校验
  规则；`ScratchCommitRequest` 新增可选字段
  `parameters: tuple[ScratchParmDeclaration, ...] = ()`，`__post_init__`
  做清单级校验（名称唯一、≤64 条、序列化 ≤8 KB）。
- [ ] `parse_scratch_commit_request`(:1734 附近）同步解析。

**B. Coordinator — `eee_agent/modeling/scratch_coordinator.py`**

- [ ] `scratch_commit` 工具签名新增可选 `parameters` 入参，透传到
  `ScratchCommitRequest`；工具 docstring 给出三类分类的定义和 ctrl 约定指针。
- [ ] 轻量交叉校验（best-effort，不新增 fail 路径以外的行为）：manifest 的
  `binding.node` 必须是本 run task graph 里记录过的沙箱相对路径；找不到时
  拒绝 commit 并给出 bounded reason（参数绑到不存在的节点是建模错误，
  应当 fail-closed）。

**C. Executor — `houdini_side/changeset_executor.py`**

- [ ] `_finalize_commit`：当 `request.parameters` 非空时，把清单 JSON
  （bounded ≤8 KB）写入容器根节点 comment（`container.setComment(...)`），
  保持既有 per-node annotations 行为不变。

**D. Skill 修订（C1 之后的措辞对齐，一次做完）**

以下四个 skill 仍写 "literal-only / no expressions"，与 C1 已提交的事实矛盾，
本次全部修订（只改措辞段落，不新增 skill 文件）：

- [ ] `skills/procedural-components/SKILL.md:8-14`、:30-38：
  "you are the expression engine" 改为 C1 `expr` + 三类分类教学。
- [ ] `skills/sop-cookbook/SKILL.md:13-16`、:106-111：Golden rules 更新。
- [ ] `skills/vex-patterns/SKILL.md:64-68`：`ch()` "NOT expressible" 改为
  typed `expr` AST 的说明（相对 ref、白名单函数、深度/节点上限）。
- [ ] `skills/parametric-building/SKILL.md:17-19`：同步。
- [ ] `skills/procedural-modeling/SKILL.md` Stage 6(:82-107)：扩写为正式的
  分类约定 —— 三类定义、ctrl 载体（§0.2）、依赖模式（tracker /
  proportional / offset）如何落成 `depends_on`。
- [ ] `skills/html-to-houdini/SKILL.md:73-78`："channel references are a
  future compiler feature" 已过时，改为指向 C1 expr 与 manifest。

### 2.2 测试清单

离线：

- [ ] DTO 测试：三类分类各自的合法样本通过；design_intent 缺 min/max 拒绝、
  `min > default` 拒绝；derived 空 depends_on 拒绝、depends_on 指向未声明
  名字拒绝；constant 带 min/max 拒绝；重名拒绝；65 条拒绝；超限序列化拒绝。
- [ ] coordinator 测试：parameters 透传到 provider 调用；binding.node 未在
  task graph 记录时 commit 拒绝并带 bounded reason。
- [ ] executor-fake：非空 parameters 时容器 comment 被写入 bounded JSON。
- [ ] KB 重建测试（若存在 skill 指纹断言）随 skill 文本修改同步更新。

真机 hython：

- [ ] 扩展 `component_houdini_smoke.py`（或独立 smoke）：构建带 ctrl 节点 +
  derived expr（`../seat_ctrl/sizex`）的组件 → 带 parameters 的 commit →
  断言容器 comment 含清单 JSON、derived parm 上是 Hscript 表达式、
  改 ctrl 值 re-cook 后几何 bbox 跟随变化（表达式链路真机验证）。

### 2.3 出口标准

清单 DTO fail-closed 校验全覆盖；skill 文本与 C1 能力一致；真机 smoke 证明
"改 ctrl → derived 跟随"。real provider 层 `not_run`。

---

## 3. C4 — 参数 Tab、范围与依赖关系

目标（roadmap :341）：在 C3 清单上补 tab 分组、min/default/max 范围、
依赖边的自动核对。设计已定：tab/range 是 manifest 元数据（§0.3 已含），
**不碰 Houdini parm template**（catalog ops 无法安全编辑参数界面，明确不做）。

### 3.1 实现清单

**A. 依赖核对 — `eee_agent/modeling/scratch_coordinator.py`**

- [ ] commit 时对 derived 条目做依赖一致性核对：从本 run 的 build ops
  （coordinator 侧保留 typed_ops 的 expr AST）收集所有 `ref` 路径，核对
  每个 derived 声明的 `depends_on` 至少在一条 expr ref 中解析到对应
  design-intent 条目的 binding；缺失时 commit 拒绝（fail-closed，
  bounded reason 列出缺失边）。需要 coordinator 在 build 时把 expr ref
  摘要存进内存（run 级，不进 DB）。
- [ ] 环检测：depends_on 图上 DFS，发现环拒绝（derived 互相引用成环会让
  扫描结果不可复现）。

**B. Tab/范围的消费出口 — commit receipt**

- [ ] `ScratchCommitResult.receipt` 增加 `parameter_tabs` 摘要：
  `{tab: [names]}` 与每个 design-intent 参数的范围，供 journey/evidence
  落盘和后续面板展示（面板改动本身不做，留 Stage C）。

### 3.2 测试清单

离线：

- [ ] 依赖核对：expr ref 与 depends_on 匹配通过；声明了 depends_on 但无任何
  expr 引用 → 拒绝；expr 引用了 ctrl 但 manifest 未声明 → 拒绝（反向核对）；
  环（a→b→a）拒绝。
- [ ] receipt 摘要：`parameter_tabs` 分组正确、范围原样携带。

真机 hython：

- [ ] 复用 C3 smoke 场景，断言 commit receipt 的 `parameter_tabs` 内容。

### 3.3 出口标准

依赖边双向核对 + 环检测全绿；receipt 带 tab 摘要。real provider 层 `not_run`。

---

## 4. C5 — min/default/max 参数扫描

目标（roadmap :342）：每个 design-intent 参数按 min/default/max 三档重新
cook + 几何断言 + 截图。形态已定：**独立确定性 hython harness**（不经过
LLM、不经过 bridge —— 直接 `hou` 操作场景副本，与 fault_injection smoke
同形态），读容器 comment 里的 manifest 驱动扫描。

### 4.1 实现清单

- [ ] 新增 `tests/runtime/param_scan_houdini_smoke.py`：
  1. 构建（或加载）带 manifest 的已 commit 资产；
  2. 从容器 comment 还原 manifest；
  3. 对每个 design-intent 参数 × {min, default, max}：设 binding parm →
     force cook OUT → 读几何 stats → 跑 `eval/geometry_assertions.py`
     的 `evaluate`（范围随档位缩放的 envelope）+
     `mesh_component_count`（断连检测，与 default 档一致）；
  4. 每档一张截图：临时相机 + flipbook ROP，参考
     `changeset_executor.py:2106` 的 capture 实现，在 smoke 内自带最小
     helper（不重构 executor）；
  5. 每档结束恢复 default；失败档输出 B6 结构化报告
     `{fault, check, location, expected, actual, hint}`（复用
     `fault_injection_houdini_smoke.py:150-158` 的 `make_report` 约定，
     location = 参数名@档位）；
  6. evidence JSON（≤16 KB）：`{asset, parameters: [{name, tiers: [{tier,
     value, ok, issues, screenshot}]}], cook_errors}`，打印
     `PARAM SCAN OK`。
- [ ] 档位 envelope 换算：案例 YAML 提供 default envelope（见 C6 schema），
  min/max 档按参数对 bbox 的影响方向缩放 —— 简化规则：bbox 每维允许
  `[default_env * (1 - scan_tol), default_env * (1 + scan_tol)]`，
  `scan_tol` 默认取 `(max - min) / default` 的上限 0.5；换算逻辑放进
  smoke 顶部纯函数，离线可单测。
- [ ] cook error 即失败（hard gate），断连（component count 变化）即失败。

### 4.2 测试清单

离线：

- [ ] envelope 换算纯函数单测（缩放、上限截断、退化 default=0 拒绝）。

真机 hython：

- [ ] 对 C3 smoke 的资产跑 `PARAM SCAN OK`：≥1 个 design-intent 参数 ×
  3 档，全部无 cook error、无断连、截图非空 PNG。
- [ ] 负样本：人为把一个参数的 max 设为会断连的值，断言该档被捕获并产出
  B6 报告（扫描 harness 自身可信度）。

### 4.3 出口标准

正样本全档通过；负样本被定位；截图落盘且非空。real provider 层 `not_run`。

---

## 5. C6 — 案例库扩充

目标（roadmap :343-348）：椅子、书桌、货架、自行车、参数化小屋五个案例
可回归。现状：椅子/书桌/货架只以 `_CASES` 存在于
`tests/runtime/html_session_journey.py:115-185`（含 envelope 表，可直接移植）；
自行车有 `eval/cases/bicycle.yaml`（静态 expect，未参数化）；
小屋有 `eval/cases/house.yaml`（2 个静态案例）。

### 5.1 实现清单

**A. 案例 schema 扩展 — `eval/cases/*.yaml` + `eval/run_eval.py` 解析**

在现有字段（`name/prompt/export_path/expect`）上追加可选字段，旧字段语义不变：

```yaml
- name: chair
  prompt: "..."
  export_path: output/chair_eval.obj
  expect: {min_verts: 24, bbox_min: [...], bbox_max: [...]}   # 现有
  parts:                               # 可选 → evaluate_parts
    seat: {min_verts: 8, bbox_min: [...], bbox_max: [...]}
    legs: {required: true, min_verts: 8}
  color: {rgb: [0.6, 0.4, 0.2], tol: 0.05}   # 可选 → evaluate_color
  parameters:                          # 可选 → C5 扫描输入（期望 manifest）
    - {name: seat_width, tab: Seat, min: 0.35, default: 0.45, max: 0.60}
  scan_expect: {mesh_components: 1, scan_tol: 0.5}   # 可选 → C5 档位不变量
  tags: [l4-journey, parametric]       # 可选 → 案例选择
```

- [ ] `eval/run_eval.py` 的 `load_cases` 解析新字段（宽松解析、缺省为空），
  `--dry` 路径对新字段做 schema 校验（不评估几何）。
- [ ] `--case` 选择支持 tag 前缀（`--case tag:parametric`）。

**B. 五个案例**

- [ ] `eval/cases/chair.yaml`、`desk.yaml`、`shelf.yaml`：移植 journey
  `_CASES` 的 envelope/尺寸/parts 文本为 YAML expect + parts +
  parameters + scan_expect；prompt 复用 journey brief 的核心句。
- [ ] `bicycle.yaml`：补 parameters（wheel_radius、frame_length、
  seat_height 等 design-intent）与 parts（wheel_f/wheel_r/frame/saddle）。
- [ ] `eval/cases/parametric_hut.yaml`（新）：小屋 = 墙身 box + 屋顶
  （polyextrude 或 grid+xform）+ 门洞（boolean2）；envelope 自定并记录
  推导依据；parameters（width/depth/wall_height/roof_pitch）。
- [ ] `html_session_journey.py` 的 `_CASES` 改为从 `eval/cases/` 读
  （单一事实源），journey 专有字段（`target_name`、brief 文本）留在 journey，
  以 case name 关联。若耦合成本高于收益，允许保留两份但必须在 YAML 里
  注明镜像关系 —— 执行时先评估，选低成本方案并记录理由。

**C. 回归入口**

- [ ] 每个案例一条可重复执行路径：L4 journey（真实 Provider，椅/桌/架已有）
  或确定性 hython 构建脚本（自行车/小屋，fault_injection 同形态，
  不用 LLM）；案例 YAML 标注 `regression: journey | deterministic`。

### 5.2 测试清单

- [ ] `--dry` 全案例 schema 校验通过；非法新字段样本被拒。
- [ ] 确定性案例的 hython 构建脚本通过各自 expect/parts 断言。
- [ ] 每个带 parameters 的案例：`PARAM SCAN OK`（接 C5 harness）。
- [ ] 椅/桌/架 journey 用移植后的 YAML 重跑一次（真实 Provider 层 ——
  需要本机凭证；无凭证时标 `not_run`，不得用离线结果顶替）。

### 5.3 出口标准

5 个案例 YAML 入库且 schema 校验通过；每个案例有标注的回归路径；
至少椅/桌/架三案例在真实 Houdini 上完成 journey + 扫描（Provider 层按
凭证可用性如实标注）。

---

## 6. C7 — eval 指标扩展

目标（roadmap :349-354）：`eval/run_eval.py` 记录翻译成功率、参数化覆盖率、
几何断言通过率、平均修复轮数、端到端耗时、Provider 成本。现状：
`eval/run_eval.py` 只有 `--dry` stdout 输出，无 JSON 报告；最佳模板是
`eval/knowledge/run_eval.py`（`CaseResult` dataclass、`summarize()`、
`percentile()`、单一 JSON payload）。

### 6.1 指标定义与数据源（已定，勿自行发明口径）

| 指标 | 定义 | 数据源 |
|---|---|---|
| translation_success_rate | 案例端到端通过数 / 案例总数 | journey evidence JSON（`status`/`commit_committed`/`final_geometry_ok`）或确定性脚本退出码 |
| param_coverage | 有 manifest 且 design-intent 参数全部带 min/default/max 的案例数 / 应参数化案例数（tag=parametric） | 案例 YAML + commit receipt / 场景 comment |
| geometry_assertion_pass_rate | 通过的几何断言数 / 断言总数（final + 每档扫描 + parts/color 逐条计） | 扫描 evidence + journey evidence |
| avg_repair_rounds | 每 run 首次 `verify_geometry` ok 之前 ok=false 的次数，按案例取均值 | run 事件流中的 verify_geometry 结果（journey 产生的 runtime SQLite events；读取先例：`html_session_journey.py:525-549` 只读查询 app.sqlite） |
| e2e_duration_s | `runs.finished_at - started_at` | runtime SQLite `runs` 表（`eee_agent/runtime/models.py:208-216`） |
| provider_tokens / est_cost | input/output/cache_read token 合计；成本 = token × 单价表 | 事件流 `model.usage_updated`（`eee_agent/providers/events.py:32-44`）；单价表放 eval 配置文件（不入库、不含密钥），无单价时只报 token |

### 6.2 实现清单

- [ ] `eval/run_eval.py` 新增 `--report <dir>` 模式：扫描指定目录下的
  journey/扫描 evidence JSON +（可选）runtime SQLite，聚合 §6.1 指标，
  输出单一 JSON `{metrics, cases: [...]}` 到 stdout 和
  `<dir>/eval_report.json`；结构上复用 `eval/knowledge/run_eval.py` 的
  `CaseResult`/`summarize()`/`percentile()` 模式。
- [ ] 无 evidence 的指标写 `null` 并在 `notes` 标注 `not_run`（证据纪律）。
- [ ] SQLite 只读打开（`mode=ro`），找不到文件时降级为 `not_run`，不报错退出。
- [ ] 版本间可比：报告头带 `git_sha`（`git rev-parse HEAD`，失败时 null）、
  生成时间、evidence 来源清单。

### 6.3 测试清单

- [ ] 离线单测：构造样例 evidence/SQLite fixture，六指标各自计算正确；
  缺 evidence → `null` + `not_run`；SQLite 缺失 → 降级不崩。
- [ ] 对 C5/C6 真实产出跑 `--report`，人工核对一个案例的指标与 evidence
  原文一致。

### 6.4 出口标准

报告 JSON 可供版本间对比（字段稳定）；所有取数路径有降级；offline 层全绿。

---

## 7. 执行顺序与依赖

```text
C2（subnet + 嵌套寻址）──► C3（manifest DTO + ctrl 约定 + skill 对齐）
                              │
                              ▼
                          C4（依赖核对 + tab 摘要）
                              │
                              ▼
                          C5（扫描 harness）
                              │
                              ▼
                          C6（五案例，可与 C7 的离线部分并行）
                              │
                              ▼
                          C7（指标聚合，依赖 C5/C6 的 evidence 样例）
```

- 每项完成 = 实现清单全勾 + 测试清单全绿 + 证据按 roadmap §6 记录到
  handoff（commit SHA、离线计数、hython 版本、smoke 名与退出码、
  bounded evidence、节点增删数、分层通过状态）。
- 全部完成后跑真实 Provider 三连验收（需凭证）并更新 handoff 的 Wave C 段
  与 roadmap 复选框。
- 推送/合并不在本清单范围（仍阻塞于人工 Houdini UI 检查，由用户执行）。

## 8. 明确不做

- 不编辑 Houdini parm template / 真正的参数界面 Tab（结构化 ops 无法安全
  表达；tab 是 manifest 元数据）。
- 不改 `task_nodes` schema、不做新 migration。
- 不做 per-component subagents、raw network_mode、异步 job protocol
  （roadmap 明确后置）。
- 不在 DTO/executor 引入任意字符串表达式或文件路径（C1 安全边界）。
- 不删除 legacy `propose_modeling`/compiler/proposal 旧入口（roadmap 要求
  主链路稳定后再评估）。
