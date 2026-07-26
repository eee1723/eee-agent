# P0 决策:执行通道统一为 eee_agent 原生 scratch_build

日期:2026-07-24 · 分支:feature/html-to-houdini-pipeline · 状态:已决策

## 决策

html-to-houdini 流水线的翻译产物**只走 eee_agent 原生通道**(`scratch_build` op 序列 + `eee_agent/modeling/catalog.py` 的 NodeCatalog),不走 fxhoudinimcp MCP 直建。

## 背景

自行车案例(`threejs-to-houdini-bicycle-case.md`)是用 fxhoudinimcp MCP 工具(build_network / node card / render_viewport)完成的,功能完整但**在仓库 agent 体系之外**:

- 无法被 `skills/` 加载链路消费(repo 的 skill 全部面向 scratch_build 目录)
- 无法被 `eval/run_eval.py` 驱动(eval 通过 export obj + geometry_assertions 判定)
- 与 `tool_call_guard` / `loop_guard` / 编译器校验器链无关,错误没有护栏

两条通道并存 = skill 写两遍、验证做两遍、agent 行为分裂。所以收敛到一条。

## 代价:catalog 当前表达不了自行车

`houdini_21_minimal_catalog()`(Houdini 21.0.440 验证版)有两个硬限制:

1. **缺节点**:无 torus / sphere / copyxform / circle;sweep2 只暴露 `surfacetype/scale/roll`,没有 `surfaceshape/radius/cols/endcaptype`,也没有可用的截面节点
2. **字面量参数**:catalog docstring 明确 "Only literal numeric parameters are included" —— 自行车的 80 条 channel reference 表达式无法通过此通道下发,**参数化(P4)需要编译器层面支持表达式,是架构级工作**

## 后续行动

- **catalog 扩展(已于 2026-07-24 落地)**:以下条目经 live Houdini 21.0.440 探针验证后并入 `houdini_21_minimal_catalog()`:
  - `torus`:radx/rady, tx/ty/tz, orient(menu 整数:x=0/y=1/z=2), rows, cols
  - `sphere`:radx/rady/radz, tx/ty/tz, rows(默认 13), cols
  - `copyxform`:ncy, rx/ry/rz, px/py/pz(pivot 必须显式,默认在原点)
  - `sweep2` 增补:surfaceshape(input=0/tube=1)、radius、cols、endcaptype(none=0/single=1)—— tube 模式免截面节点
- **表达式支持**留到 P4,在 compiler/contracts 层做,不在 catalog 里开洞
- fxhoudinimcp 仍可用于**人工探索/原型验证**(如本案例),但产物必须翻译回 scratch_build 才算交付

## 验证方式

catalog 扩展条目按现有惯例用 hython 验证脚本核对(参照 catalog.py 顶部 "verified with Houdini 21.0.440 hython" 注释);验证通过后自行车案例翻译为 scratch_build 版本,进 `eval/cases/bicycle.yaml` 回归。
