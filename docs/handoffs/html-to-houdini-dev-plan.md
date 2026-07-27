# Three.js → Houdini 程序化资产流水线:开发计划

版本:v1 · 日期:2026-07-24 · 依据案例:`docs/handoffs/threejs-to-houdini-bicycle-case.md`

## 0. 定位与原则

**目标**:让 agent 稳定地产出**参数化、组件化的 Houdini 资产**,而不是一次性烘焙几何。

**路线**(自行车案例已验证):Three.js 作草图层(LLM 主场:文本原生 + 视觉闭环)→ LLM 当翻译器 → Houdini 资产层。机械转换器不做,做**辅助 LLM 翻译的 skill + 验证工具链**。

**四条原则**(来自通用编程 agent 的反向工程):
1. 对象尽量文本化——草图阶段一切是代码
2. 每一步都有廉价确定的验证——截图、几何断言、node card 预检
3. 知识靠注入不靠记忆——映射表、检查清单、配方写进 skill,不让模型凭印象写参数名
4. 参数化是核心难点——设计意图 vs 派生量的区分是资产质量的分水岭

## 1. 阶段总览

```
P0 基建盘点 ──→ P1 HTML 质量门 ──→ P2 翻译 skill ──→ P3 验证器 ──→ P4 参数化自动化 ──→ P5 案例库 ──→ P6 接入 agent 主循环
                (提示词+截图自修)   (映射表+流程)     (断言+对比)     (组件+参数拆分)        (回归测试)     (eval 驱动)
```

P1–P3 互相独立可并行;P4 依赖 P2;P5 从 P2 完成后持续进行;P6 依赖 P2/P3/P4 全部。

> **2026-07-24 状态更新**:P0 已决策(scratch_build 通道,见 p0-channel-decision.md);
> P1/P2/P3 已以"方案 A"落地为产品形态——编排 skill `skills/procedural-modeling`
> (六阶段流程含 HTML 质量门与对话审核门)+ 翻译参考 skill `skills/html-to-houdini`
> + 有界工具 `render_sketch`(headless Chrome 出图)与 `verify_geometry`(loop 内断言),
> catalog 已扩展(torus/sphere/copyxform + sweep2 tube 参数)。剩余:P4 参数化表达式、
> P5 案例库、P6 全量接入与 eval 指标。

---

## P0 基建盘点(0.5 天)

**目的**:统一执行通道,避免两套工具并存。

- 盘点 fxhoudinimcp MCP 工具集(本项目案例已用:build_network / node card / verify_network / render_viewport)与 `houdini_side/changeset_executor.py` 的能力差
- 决策:翻译产物走哪条通道(MCP 直建 vs changeset 文件),写入 skill 约束
- 确认 headless Chrome 截图命令在目标机器可用(已验证:`--headless=new --screenshot`)

**完成标准**:一页通道决策记录,skill 里只有一条执行路径。

## P1 HTML 草图质量门(1–2 天)

**目的**:草图质量决定全链路上限,把"静态、高细节、结构准确"固化成可复用提示词模板。

- 写 `skills/html-sketch/`(或并入翻译 skill 的前置段):
  - **静态**:禁动画、禁交互逻辑;场景一次性可求值的纯声明
  - **高细节**:材质分组、比例合理、该有倒角有倒角
  - **结构准确**:部件语义命名(`tube_down` 非 `mesh3`);尺寸常量集中文件头部;优先 `tube(p1,p2,r)` 这类可映射辅助函数,不直接散落 BufferGeometry
- 自修循环:生成 → headless 截图 → LLM 看图自评 → 改 → 再截,最多 3 轮
- 定义草图验收 checklist(命名规范、常量集中、材质分组、可映射结构占比)

**完成标准**:同一需求连跑 3 次,草图均通过 checklist;自修循环平均 ≤2 轮收敛。

## P2 html-to-houdini 翻译 skill(3–5 天,核心)

**目的**:把自行车案例的流程固化成 `skills/html-to-houdini/SKILL.md`,让任意 LLM 会话照做即可稳定翻译。

内容骨架:
1. **强制流程**:读草图 → `get_node_card` 验证每个节点类型参数名 → `build_network` 原子构建 → `verify_network` → 截图双端对比 → 几何计数断言
2. **映射表**(种子来自案例,逐案例扩充):
   - tube → line+sweep(tube 模式,cols=截面分段)✅已验证
   - 循环阵列 → copyxform(pivot 显式)✅已验证
   - torus/sphere/box/grid 直译 ✅已验证
   - 待验证:ExtrudeGeometry→curve+polyextrude、LatheGeometry→revolve、CSG→boolean、instancing→copytopoints、噪声形变→mountain/attribnoise
3. **检查清单**:pivot 必须显式(xform/copyxform);Y-up 一致;torus orient;parmTuple 解析组件名;重构用"建新→原位换线→删旧"不断线手法
4. **细分规范**:节点固有分辨率(sweep cols / torus rows·cols)+ 末端 normal SOP cusp 60°,禁用 subdivide 兜底
5. **反模式清单**:不猜参数名、不逐节点 create(用 build_network)、不烘焙可派生的值

**完成标准**:换一个全新物体(如椅子),LLM 仅凭 skill 完成翻译,零参数名错误,双端截图结构一致。

## P3 验证器(2–3 天)

**目的**:给"翻译对了没"一个机器可判定的答案,补编程 agent 的 `pytest` 位。

- 扩展 `eval/geometry_assertions.py`,形成断言库:
  - 包围盒(尺寸/位置容差)、prim/point 计数、对称性、连通性(车架不脱节的机器判定)
  - 部件级断言:按节点路径分组断言,而非只断言整体
- 双端交叉验证:Three.js 截图 vs Houdini 截图喂视觉模型做结构一致性判断(语义层),几何断言管数值层
- 断言失败报告格式化:可直接回灌 agent 循环作为修正上下文

**完成标准**:对自行车 hip 故意注入 5 类错误(断连、错尺寸、错 pivot、漏部件、错颜色),断言全部捕获并给出可定位的报告。

## P4 参数化自动化(4–6 天,最难)

**目的**:把"识别设计意图、组织依赖"从人肉推理变成 skill 引导的半自动流程。

- **组件拆分**:按语义把网络组织进 netbox/subnet(wheel / frame / cockpit / drivetrain…),组件边界从草图的语义命名继承
- **参数分类**:每个尺寸标注为 设计量(暴露为参数)/ 派生量(写成表达式)/ 常量(烘焙);skill 提供分类引导问题和表达式模板(`sqrt(dx²+dy²+dz²)` 追踪类、比例类、偏移类)
- **参数选项卡**:按组件出 tab,挂在 geo 容器上;跨组件引用统一 `ch("../...")` 相对路径
- **联动验证**:自动对参数做 min/max 扫描 + 每档截图 + 断言,生成联动验证报告(自行车案例的人肉验证自动化)

**完成标准**:自行车按组件重组为 4 个 subnet + 分 tab 参数,参数 min/max 扫描零脱节、零报错;新案例参数化耗时 < 静态翻译的 2 倍。

## P5 案例库(持续)

- 梯度:椅子(简单挤出)→ 书桌(对称/阵列)→ 货架(参数化重复)→ 小屋(建筑构件,接 `skills/parametric-building`)
- 每个案例:草图 + hip + 断言集 + 映射表增量 + 踩坑记录,进 `eval/cases/` 作回归
- 每个案例必须回答:映射表新覆盖了什么?skill 哪条被验证/推翻?

## P6 接入 agent 主循环(3–5 天)

- P2 skill 挂进 `eee_agent` runtime 的技能加载链路
- 视觉反馈闭环进主循环:构建后自动截图 → 视觉模型 critique → 修正轮,替代"盲目构建"
- eval 驱动:用 P5 案例库跑 `eval/run_eval.py`,跟踪翻译成功率 / 参数化覆盖率 / 断言通过率三个指标

**完成标准**:agent 端到端从一句话需求产出参数化资产 + 验证报告,eval 案例通过率 ≥ 基线(先建基线再谈提升)。

---

## 2. 优先级与取舍

- **先做 P1+P2**:投入最小、卡位最准——没有它们,后面都是空中楼阁
- **P3 与 P2 同期**:没有验证器的 skill 是盲飞;断言库先做自行车回归就有价值
- **P4 是最硬的骨头**,单独排期,别和 P2 混做;先做"组件拆分+分类引导"的半自动,全自动后置
- 细分策略已定(节点固有分辨率),不再投入

## 3. 风险

| 风险 | 缓解 |
|---|---|
| 复杂草图(嵌套循环/递归)翻译率骤降 | P1 质量门限制草图结构;映射表只承诺已验证条目 |
| 参数化分类错误(设计量被判成常量) | P4 联动扫描报告 + 人在 min/max 档位抽查 |
| 视觉模型 critique 不稳定 | P3 数值断言为底线,视觉判断只做补充 |
| 双执行通道分裂 | P0 一次性决策,skill 只写一条路径 |

## 4. 立即行动项

1. P0 通道决策记录(半天)
2. 起草 `skills/html-to-houdini/SKILL.md` 骨架:强制流程 + 已验证映射表(从自行车案例抄)+ 检查清单
3. 自行车断言集进 `eval/cases/`,作为 P3 的第一个回归
