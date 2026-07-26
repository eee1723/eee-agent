# 案例:Three.js → Houdini 程序化资产转换(自行车)

日期:2026-07-24 · 场景:`output/bicycle.hip` · 状态:全链路跑通

## 1. 背景假设

通用编程 agent(OpenCode / Pi 类)之所以稳定,靠四点:**文本原生对象、廉价确定的验证器、深度对齐的训练数据、高单步杠杆**。Houdini 建模这四点全缺:对象是有状态节点图、没有"好不好看"的测试、SOP 训练数据稀缺、单步只改一个参数。

但"HTML + Three.js 做三维场景"重新占全了这四点(场景即代码 + 浏览器报错 + 视觉截图闭环 + 海量 web 训练数据)。由此产生核心思路:

> **Three.js 作草图层(LLM 的主场),Houdini 作资产层(生产目标),LLM 自己当翻译器。**

关键洞察:不需要机械转换器。Three.js 代码是 LLM 自己写的,它理解意图,可以把意图重新表达为 SOP 网络——这是"再创作"而非"编译"。

## 2. 实验过程(三轮闭环)

### 第 1 轮:生成 Three.js 草图
- 产出 `output/bicycle.html`(约 200 行,参数化:轮径/车架关键点全部显式数字)
- 核心抽象:`tube(p1, p2, r)` —— 两点间拉圆柱,覆盖车架管/辐条/前叉/链条等全部杆件
- 验证:无头 Chrome `--headless=new --screenshot` 截图,模型自查后修正光照

### 第 2 轮:翻译成 Houdini SOP 网络
- 先用 `get_node_card` 核实每个节点类型的真实参数名(cylinder SOP 不存在 → 改用 line+polywire)
- `build_network` 一次原子构建 **75 节点,零报错**,几何 4625 点 / 4430 面
- 视口截图与 Three.js 版对比,结构一致(包围盒高度 1.073 两端吻合)

映射表(验证有效):

| Three.js | Houdini SOP |
|---|---|
| `tube(p1,p2,r)` | line(origin/dir/dist)+ sweep(surfaceshape=tube, radius, cols=截面分段) |
| 循环辐条 ×N | 1 根 line + copyxform(ncy=N, rz=360/N, pivot=轮心)+ 一次 sweep(多 backbone 自动逐条扫掠) |
| `TorusGeometry` | torus(orient=z, rad=[外径, 管径], rows/cols 控制细分) |
| 缩放球体坐垫 | sphere + xform(pivot 必须设在物体中心) |
| 材质颜色分组 | 按材质 merge 成组 + color SOP(Cd) |
| `CircleGeometry` 地面 | grid |

### 第 3 轮:参数化为 Procedural Asset
- `/obj/bicycle` 上挂 9 个 spare 参数(Bicycle Controls 折叠页):wheel_radius / wheelbase / tire_thickness / spoke_count / handlebar_width / saddle_lift / frame_rgb
- 约 80 条 channel reference 联动;后叉/前叉/链条用 `sqrt(dx²+dy²+dz²)` 实时重算方向与长度
- 验证:改轮径 0.42 + 轴距 1.25 + 36 辐条 + 蓝色 → 辐条精确 216 面(36×6)、轮心跟随、车架不脱节 → 恢复默认值保存

### 第 4 轮:sweep 替换 polywire + 原生分辨率细分(不依赖 subdivide)
- 18 个 polywire 全部替换为 sweep(surfaceshape=tube):不需要第二输入,radius/cols/endcaptype 直接控形;网络 75 → 77 节点
- 细分原则:**提高生成节点的固有分辨率,不加 subdivide SOP** —— torus rows/cols(轮胎 64×24)、sweep cols(车架管 24、叉管 16、辐条 6)、sphere rows/cols(32×24)
- 末端加 normal SOP(默认 cusp 60°)获得平滑着色;box 直角边(90°>60°)不受影响
- 重构手法:先建 sweep(临时名)→ `connect_nodes_batch` 按 input_index 原位替换 merge 输入 → 删除旧节点,下游引用不断线
- 几何量 4625 → 8193 点,包围盒不变,`verify_network` 零错误

## 3. 经验教训

1. **先查 node card 再动手**。"cylinder SOP 不存在"这种坑只有查了才知道;参数名猜错是翻译失败的最大来源。
2. **翻译率高的前提是源代码结构规整**。`tube()` 抽象恰好对齐 line+polywire 范式,映射近乎机械。杂乱的三.js 代码翻译质量会骤降。
3. **成本分布:静态翻译容易,参数化难**。难点不是搭节点,而是识别哪些尺寸是"设计意图"(应暴露为参数)、哪些是"派生量"(应写成表达式),以及维护依赖结构(改轮径时后叉必须跟随)。
4. **双端截图是天然交叉验证**。两个独立实现同一意图,互为测试集。
5. **pivot 是隐蔽 bug 源**:xform 缩放、copyxform 旋转,默认 pivot 在原点,必须显式设为物体/轮心。
6. 组件级参数名用 `parmTuple("t")[idx]` 解析,不要猜 `tx`/`originx` 命名(实测 torus 是 `radx/rady`、line 是 `originx` 这套,但并不统一)。
7. **细分用节点固有分辨率,不用 subdivide**:sweep 的 cols、torus 的 rows/cols 直接决定平滑度,几何轻、参数化友好;sweep 的 `surfaceshape=tube` 模式连 circle 截面节点都省了。末端 normal SOP(cusp 60°)补平滑着色。
8. **截图验证前先确认没人正在操作**:第 4 轮截图"颜色丢失"实为人在拖滑块——共享会话里 agent 的观察可能抓到人的中间状态,诊断前先把控制器参数值读一遍。

## 4. 已知缺口(下轮迭代目标)

- [x] ~~细分~~ → 第 4 轮完成:sweep cols + torus rows/cols + normal SOP,不依赖 subdivide
- [ ] **组件化组织**:模型部件装进 netbox/subnet(wheel / frame / cockpit / drivetrain),参数按组件拆成独立选项卡,而不是全部堆在一个文件夹
- [ ] **更细的参数拆分**:座管角度、头管角度、把立长度、牙盘半径等目前烘焙在表达式常量里的设计量;分辨率(cols/rows)也可提为全局 detail 参数
- [ ] **HTML 生成质量门**:提示词必须强调**静态、高细节、结构准确**(见 §5)

## 5. 后续开发方向

### 流水线定型

```
需求 → [1] 生成 Three.js 草图(强调:静态/高细节/结构准确)
     → [2] 截图视觉验证,LLM 自修
     → [3] html-to-houdini 翻译(skill 辅助)
     → [4] Houdini 截图 + 几何断言交叉验证
     → [5] 参数拆分 + 组件分类(netbox + 参数选项卡)
     → [6] 细分等后处理
```

### HTML 草图阶段的提示词要求(第 1 步的质量决定全链路)
- **静态**:不要动画、不要交互逻辑污染结构;场景是一次性可求值的纯声明
- **高细节**:倒角、材质区分、合理比例,宁多勿少
- **结构准确**:部件命名语义化(`tube_down` 而非 `mesh3`),尺寸常量在文件头部集中定义,`tube()` 这类可映射的辅助函数优先于散落的 BufferGeometry

### html-to-houdini skill(待建)
skill 应包含:
1. **映射表**(§2 表为种子,随案例扩充:extrude→polyextrude、lathe→revolve、CSG→boolean、instancing→copytopoints)
2. **强制流程**:先 `get_node_card` 验证参数名 → `build_network` 原子构建 → `verify_network` → 截图对比 → bbox/计数断言
3. **pivot / 坐标系检查清单**(Y-up 一致;torus orient=z 对齐 XY 平面)
4. **参数提升清单**:指导 LLM 识别设计意图 vs 派生量,常量集中在表达式里要标注来源

### 参数化阶段的规则
- 组件先行:先分 netbox/subnet,再在每个组件上挂参数,跨组件引用用相对通道 `ch("../../ctrl/...")`
- 派生量永远写成表达式,不许烘焙;设计量才暴露为参数
- 每个参数改动必须有一次几何断言或截图验证(参考 `eval/geometry_assertions.py` 的思路)

## 6. 产物索引

- `output/bicycle.html` — Three.js 草图(可直接浏览器打开)
- `output/bicycle_preview.png` — Three.js 渲染验证图
- `output/bicycle_houdini.png` — Houdini 默认参数视口截图
- `output/bicycle_houdini_parametric.png` — 参数联动验证截图(大轮/蓝架/36辐条)
- `output/bicycle.hip` — 最终程序化资产(`/obj/bicycle`,75 节点,9 控制参数)

## 7. 产品化(2026-07-24)

本案例的流程已固化为仓库能力:编排 skill `skills/procedural-modeling`(六阶段,含 HTML 质量门与对话审核门)、翻译参考 skill `skills/html-to-houdini`(映射表/检查清单)、有界工具 `render_sketch`(headless Chrome 出图)与 `verify_geometry`(loop 内几何断言)、catalog 扩展(torus/sphere/copyxform + sweep2 tube 参数)。开发计划见 `html-to-houdini-dev-plan.md`。
