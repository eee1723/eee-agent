# Houdini 文档知识图谱（按需查询）设计

> 实现注记（2026-07-24）：本文是设计稿，模块名以实际代码为准 —— 构建器为
> `eee_agent/knowledge/build.py`（CLI `python -m eee_agent.knowledge.build`），
> 运行时出口为 `eee_agent/runtime/knowledge.py` + `runtime/agent_tools.py`
> （`search_houdini_knowledge` / `get_houdini_knowledge`），文中 `build_kb.py`、
> `eee_agent/tools/knowledge.py` 均为设计期名称。

- 状态：设计稿（待实现）
- 日期：2026-07-14
- 范围：为 Agent 增加一个离线构建、本地查询的 Houdini 文档知识图谱工具
- 来源需求：用户提出"搜索 Houdini 相关文档、做成知识图谱、让 Agent 在操作过程中按需查询"
- Houdini 基线：21.0.440（两台机器均为同一 build：`C:\Program Files\Side Effects Software\Houdini 21.0.440` 与 `D:\houdini`）
- 相关文档：`docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`、`CLAUDE.md`

## 1. 摘要

Agent 反复出现的失败模式是**猜测 Houdini 事实**——节点 internal 名、VEX 函数签名、`hou.*` 方法名。本仓库的金科玉律是"verify, don't guess"，而历史上最严重的问题是**上下文膨胀**（prompt 从 15k 涨到 42k；`work_status` 在 148 次工具调用中被调用 64 次）。

目前 Agent 只有两个知识来源：4 个手写 skill 文件（约 239 行，始终注入 system prompt）和实时工具 `describe_node_type()`（读取**本机** Houdini 中节点的真实 parm）。没有任何手段能在不猜测的前提下查询"节点 X 是否存在、它是干什么的、有哪些相关项、VEX 函数 Y 的签名是什么、`hou.Z` 的结构如何"。

关键事实是：**Houdini 把官方文档随安装包发到本地，而且这些文档本身就是一张图**——带类型的交叉引用（`[Node:...]` / `[Vex:...]` / `[Hom:...]`）、`#tags`、`#context`、`#superclass` 继承关系。因此"知识图谱"在这里不是修辞，而是对源数据结构的如实描述。

本设计把这些文档离线索引成一个**知识图谱**，Agent 通过一个新的按需工具 `query_kb`（以及按需取全文的 `get_kb_text`）查询，**在猜测之前先核实**。

**用户已确认的决策：**
- 表示形式 = **知识图谱**（非扁平索引、非向量 RAG）。
- 索引来源 = **全部四类**：SOP 节点、VEX 函数、`hou.*` API、仓库内 skill 文件。

## 2. 背景与现有基线

### 2.1 现有工具/检索机制（已通过代码核实）

- **工具机制**（`eee_agent/tools/`）：`from langchain_core.tools import tool`；工具返回纯 dict，**永不抛异常**；失败统一为 `{"ok": False, "error": str}`。涉及 Houdini 的工具走 `res, err = hou_client.run(_fn)`（rpyc bridge）。一个查询本地文档的 KB 工具**不走 bridge**、不 import `serialize`——它是一条干净独立的路径（参考 `scene.hou_status` 的地板模式）。注册方式：在 `eee_agent/tools/registry.py` 的手维护列表 `ALL_TOOLS` 里加一行。
- **System prompt 装配**（`eee_agent/system_prompt.py`）：`build_system_prompt()` 把 BASE_PROMPT + 4 个 skill + `memory/AGENTS.md` 纯字符串拼接。BASE_PROMPT 的 `MODELING DISCIPLINE` 第 5 条已经写了"不熟悉的节点类型先用 `describe_node_type()` 读真实 parm，不要猜"——这正是 `query_kb`（离线/廉价：这是啥、相关啥）作为 `describe_node_type`（实时：本机真实 parm）**离线伙伴**的天然挂载点。
- **Trim 中间件**（`eee_agent/context_trim.py`）：`READBACK_TOOLS` 是工具名字符串的 frozenset（l.38）；它按**工具名**保留每个工具的最新一条结果，其余打桩成 `STUB`（l.42）。
- **配置**（`eee_agent/config.py`）：已有 `_env_bool(name, default)`（l.36）、`repo_root()`（l.78）、`resolve_path()`（l.91）可复用。
- **依赖**：`pyproject.toml` / `uv.lock` 里**没有任何**检索/图/向量库（networkx/faiss/chromadb/sentence-transformers 全无）。Foundation venv 有意保持精简（`CLAUDE.md` gotcha #7）。本设计**不新增任何运行时依赖**。

### 2.2 文档来源（已通过直接读取 zip 核实）

本地 Houdini 安装随附大量结构化文档（`<HFS>\houdini\help\*.zip`，内部时间戳 2025-08-12，与 sidefx.com 网页是同一份快照，**无需爬网**）：

| 来源 | 路径 | 规模 | 用途 |
|---|---|---|---|
| 节点文档 | `help\nodes.zip` → `sop\*.txt` 等 | 4,730 个 `.txt`，其中 SOP 1,145 个 | 节点主参考 |
| HOM Python API | `help\hom.zip` → `hou\*.txt` | 891 个 | `hou.*` 类/方法/模块参考 |
| VEX 语言 | `help\vex.zip` → `functions\*.txt` | 1,116 个函数 | VEX 函数签名/参数 |
| Hscript 表达式/命令 | `help\expressions.zip`、`commands.zip` | ~430 KB | 表达式语言（v1 暂不索引） |
| 仓库内 skill | `skills\{parametric-building,vex-patterns,sop-cookbook,procedural-components}\SKILL.md` | 4 个，约 239 行 | 高保真、已针对 21 验证的配方 |

## 3. 已核实的文档格式（设计所依赖的事实）

直接从 zip 读取（WikiCreole 风格），并非猜测：

- **元数据头**：`#type: #context: #internal: #tags: #version: #since: #group: #cppname: #superclass: #status:`（任意子集；某个 `#key:` 可在后续缩进行续写）。
- 一行摘要写在 `"""..."""`。
- **VEX**：`:usage: <签名>` 行 = 签名；`:arg:name:` 块 = 参数说明；`#context: all|sop|cvex|...`。
- **HOM 有 6 种 `#type:`**（已统计）：`homclass` 339 / `homfunction` 342 / `hommodule` 203 / `pypackage` 5 / `hompackage` 1 / `include` 1。
  - `homclass`：类页内含 `::`签名` -> [Hom:hou.X]:` 形式的方法块，后接缩进正文与每方法的 `#cppname:`（已在 `hou/Node.txt`、`hou/ParmTemplate.txt` 核实）。
  - `homfunction`：单签名页（文件名以 `_` 结尾，如 `hou/node_.txt` → 标题 `hou.node`），`:usage:` 即签名。
  - `hommodule`/`pypackage`/`hompackage`：命名空间容器页。
  - `include`（1 个）与 vex 根目录的 `_common.txt`、`*_suite.txt`：是 include 片段，**不作为实体**。
- **交叉引用**：`[KIND:name]`，KIND ∈ `Node|Vex|Hom|Icon|...`，可选 `#anchor`（如 `[Hom:hou.parmData#Float]`）。`:include file#anchor:` 指令。
- 文件为 UTF-8 **带 BOM** → 解码用 `utf-8-sig`。
- 旧版节点文件以 `-` 结尾（`.txt` 前），如 `polyextrude-.txt`；`#internal` 有约 332 次碰撞，全是"旧版兄弟"模式。

## 4. 架构

**离线构建**（`eee_agent/knowledge/build_kb.py`，在 venv 中运行，不需要 Houdini）以只读 `zipfile` 方式读取 3 个 zip + 4 个 skill `.md`，逐页解析，写出单个 JSON artifact。**运行时**（`eee_agent/knowledge/store.py`）一次性加载 artifact（模块级缓存），服务于新的 `@tool query_kb`（及 `@tool get_kb_text`）。无 bridge、无网络、无新增运行时依赖。

**图 = 标准库 dict，不用 networkx。** 每个查询要么是 O(1) 的倒排索引查表，要么是 1 跳邻接扫描。networkx 的价值（多跳遍历/中心性）超出本范围，且会违反精简 venv 原则（`CLAUDE.md` gotcha #7）。纯标准库让 `uv.lock` 保持干净、构建在两台机器间确定一致。（仅当未来出现真实的多跳查询需求时再 revisit。）

**自由文本排序 = 复合打分器（非 BM25/TF-IDF）。** Agent 的查询是名字形或标签形，不是散文。排序：精确名字（1000）> 名字前缀·idf（500）> 精确 tag（300）> idf 加权正文 token 重叠（兜底）。约 50 行，标准库。

**Artifact 策略：gitignore + 每台机器重建。** 它索引的是本机 HFS；提交进 git 会在另一台机器上喂给 Agent 过期/错误事实（与"不要在机器间复制 `.venv`"同理）。体积约 10–25 MB。提交构建脚本；JSON 入 `.gitignore`。store 在 artifact 缺失时**惰性构建**（`EEE_KB_AUTOBUILD`，默认开），让全新 checkout 也能直接用。

## 5. 知识图谱 Schema

- **id**（kind 前缀，无碰撞）：`node:{ctx}:{internal}`（当前）/ `node:{ctx}:{internal}:legacy` / `vex:{func}` / `hom:hou.{Class|func|ns}` / `skill:{slug}`。节点的规范名 = `#internal`（即 Agent 传给 `create_node` 的串）。
- **属性**（公共：`id, kind, name, title, summary(≤240), tags[], source`）：
  - node → `context, internal, version, since, legacy(bool), parm_summary_count`
  - vex → `context, group, signatures[]（:usage:）, returns`
  - homclass → `superclass, cppname, methods[]（上限 15；{name,signature,summary_short}）, method_count_total`
  - homfunction → `signature, returns, group`
  - hommodule/pypackage/hompackage → `members[]（id 列表）`
  - skill → `path, title`
- **边**（按源 id 索引的邻接表）：
  - `cross_ref`（细分为 node/vex/hom/icon；目标 id 存在则解析，否则存原始串 + `resolved:false`）。
  - `superclass`（hom 的 `#superclass: hou.X`）——这条边让继承查询变成一次索引查表，是"图"表示最有力的理由。
  - `related`（VEX `@related` 段，是 cross_ref 的策展子集）。
  - `includes`（`:include file#anchor:`，v1 不解析）。
- **倒排索引**：`name→id`（裸名解析到**当前**节点，绝不到 legacy）、`title→id`、`tag→[id]`、`context→[id]`、`group→[id]`、`superclass→[id]`、`token→{id:tf}`（供打分器用）。

## 6. 工具 API

两个工具均在 `eee_agent/tools/knowledge.py`，`@tool` 装饰，返回 dict，永不抛异常：

```python
query_kb(query=None, name=None, kind=None, tag=None, context=None,
         superclass=None, k=5) -> dict
```
- `name` → `{ok, match:{<摘要条目, related[]≤8 个 id+name+kind>}}`，否则 `{ok:false, error:"no match", suggestions:[≤5]}`；`kind` 可过滤（node/vex/hom/skill）。
- `tag`/`context`/`superclass` → `{ok, count, results:[{id,kind,name,title,summary≤120}]}`，上限 k（最大 25）。
- `query` → `{ok, results:[{id,kind,name,title,snippet≤140,score}]}`，按分排序，上限 k。
- 组合参数 → 先过滤再打分。沿 `related` 的 id 前进 = 再调一次 `query_kb(name=)`（1 跳）。

```python
get_kb_text(id, max_chars=8000, section=None) -> {ok, id, name, body, length_chars,
                                                   truncated, section?}
```
正文轻度清洗（去掉 `:fig:`、合并空行、保留签名/参数/分节）；`section` 可分页取单个 `== Block ==`。未命中返回 `{ok:false, error:"unknown id"}`。**artifact 缺失**时两个工具都返回 `{ok:false, error:"KB not built — run python -m eee_agent.knowledge.build_kb"}`（不抛、不阻塞其它工具）。

## 7. 上下文膨胀防护（schema 本身就是防线）

- `query_kb` **不返回 parm、不返回全文**——节点查询只返回 `parm_summary_count`；parm 是经由现有实时工具 `describe_node_type(type)` 的刻意"第二跳"。（polyextrude 约 131 个 parm，全量返回会重新触发团队曾经打的膨胀问题。）
- 硬上限在 store 里强制（便于测试断言）：summary 240、signatures 5、tags 12、related 8、methods 15、自由文本 k=25。
- **只 trim `get_kb_text`**（每条 8 KB、可廉价重算）→ 加入 `READBACK_TOOLS`。**不 trim `query_kb`**：它的结果小且**互不相同**（每次查不同实体）——trim 掉会丢失有用的不同文档、并迫使反复重查（= 过度迭代，本项目的另一个已知问题）。这是刻意取舍：trim 那个大而可重算的，保留那个小而互异的。

## 8. 文档来源与范围（用户确认：全部四类）

- SOP 节点（1,145）——建模主力层。
- VEX 函数（1,116）——签名幻觉的另一主战场。
- `hou.*` Python API（891）——bridge 调用的 HOM 方法。
- 仓库内 skill（4）——高保真配方，索引后可从"始终注入"改为按需拉取（未来可瘦身 prompt）。

预计实体总数约 3,250（≈1,145 sop + 1,116 vex + ~890 hou + 4 skill − include/suite 片段）。

## 9. 待创建 / 修改的文件（实现清单）

**创建：**
1. `eee_agent/knowledge/__init__.py` —— 包标记；惰性导出 `load_kb`。
2. `eee_agent/knowledge/parser.py` —— **纯函数、无 I/O，测试覆盖最重的模块**。`parse_node/parse_vex/parse_hom(text)` → `(entity, [edges])`。处理 BOM、`#meta`+缩进续行、docstring、`:usage:`/`:arg:`、homclass `::` 方法块、`= Title =`、交叉引用正则 `\[(Node|Vex|Hom|Icon|Parm):([^\]\[#]+)(?:#(\w+))?\]`、legacy 判定（文件名以 `-` 结尾）。
3. `eee_agent/knowledge/build_kb.py` —— 定位 HFS（`EEE_HFS` → 探测 `C:\Program Files\Side Effects Software\Houdini 21.0.440` → `D:\houdini`，仿 `scripts/env_probe.sh`）；只读打开 zip；按路径/type 分派；解析 4 个 skill `.md`（去 frontmatter）；装配图；dump JSON。CLI `python -m eee_agent.knowledge.build_kb [--hfs] [--out] [--selftest]`；`--selftest` 用合成内存语料（无 HFS 也可跑）。
4. `eee_agent/knowledge/store.py` —— `load_kb()`（模块缓存）；`KBStore` 提供 `by_name/by_tag/by_context/by_superclass/free_text/neighbors/get_text`；`EEE_KB_ENABLED`；缺失则惰性构建或返回 `None`。
5. `eee_agent/tools/knowledge.py` —— `query_kb` + `get_kb_text`（不 import bridge）。
6. `tests/test_kb_build.py` —— 合成 `.txt`（当前节点、legacy `-` 节点、vex、含 2 方法的 homclass、homfunction、skill `.md`）→ 断言 kind/context/签名/tags、legacy 标志、BOM 去除、cross_ref 边解析、多行 `#replaces` 值捕获。
7. `tests/test_kb_tool.py` —— 微型内存 KB，monkeypatch `store.load_kb`；断言名字查询、自由文本排序、未命中建议、`get_kb_text` 正文+截断、`None`→`{ok:false}` 不抛。

**修改：**
8. `eee_agent/tools/registry.py` —— `from eee_agent.tools import knowledge`；在 `ALL_TOOLS` 加 `knowledge.query_kb, knowledge.get_kb_text`。
9. `eee_agent/system_prompt.py` —— 在 `MODELING DISCIPLINE`（第 5 条之后，约 l.41）加一条：把 `query_kb(name=, kind=)` 定位为 `describe_node_type` 的**离线**伙伴（后者仍是实时 parm 检查）。措辞大致："在不确定节点/VEX/hou 名字或签名时，先 `query_kb`；需要全文再 `get_kb_text(id)`。一次错误猜测比一次查询代价更大。" 控制在约 3 行（避免 prompt 膨胀）。
10. `eee_agent/context_trim.py` —— 仅把 `"get_kb_text"` 加入 `READBACK_TOOLS`（见第 7 节）。
11. `.gitignore` —— 加 `eee_agent/knowledge/houdini_kb.json`。
12. `.env.example` —— 新增注释段：`EEE_KB_ENABLED` / `EEE_KB_AUTOBUILD` / `EEE_KB_PATH` / `EEE_HFS`。
13. `CLAUDE.md` —— Layout：在 `eee_agent/` 列表加 `knowledge/`；**新增 gotcha #8**（KB artifact gitignore + 每机重建；HOM 6 种 `#type:` 在 parser 分支；legacy `-` 文件 → `legacy:true` + `:legacy` id，name 查询绝不被其覆盖）；工具数 25 → **27**。

## 10. 配置项（环境变量）

- `EEE_KB_ENABLED`（默认 true）：总开关。
- `EEE_KB_AUTOBUILD`（默认 true）：artifact 缺失时是否在首次查询惰性构建。
- `EEE_KB_PATH`：覆盖 artifact 位置（默认 `eee_agent/knowledge/houdini_kb.json`）。
- `EEE_HFS`：覆盖 Houdini 安装根（否则探测 C:\ 与 D:\）。

## 11. 验证（多数无需 Houdini / 无需 RPC）

- 构建：`uv run python -m eee_agent.knowledge.build_kb` → 打印实体/边数，写 JSON（预期约 3,250 实体）。
- 构建自检（无 HFS）：`uv run python -m eee_agent.knowledge.build_kb --selftest`。
- 单测：`uv run --extra eval pytest tests/test_kb_build.py tests/test_kb_tool.py -q`。
- 无新增依赖：`uv run python -m eee_agent.cli versions` 的 JSON 不变；`uv lock --check` 通过。
- 已接线：`uv run python -c "from eee_agent.tools.registry import all_tools; n=sorted(t.name for t in all_tools()); assert 'query_kb' in n and 'get_kb_text' in n"`。
- 实时（无 RPC）：`uv run python -c "from eee_agent.tools.knowledge import query_kb; print(query_kb.invoke({'name':'polyextrude','kind':'node'}))"` 及 `...{'query':'ray intersect geometry'})`——输出精简、无全文、无异常。
- Agent 端到端：`uv run python -m eee_agent.cli prompt "哪个 VEX 函数投射射线并返回所有命中？再说出 2 个相关函数"` → 确认 Agent 在作答前先调用 `query_kb`。
- Trim 接线：连续调用两次 `get_kb_text` 后，较早那条结果应等于 `context_trim.STUB`。

## 12. 实现期需核实的事项（不要假设——廉价 zip 探测即可）

1. 为 HOM **每种** `#type:` 读一个真实样例（hommodule 如 `hou/qt*`；homfunction 已读 `hou/node_.txt`；homclass 已读 `hou/Node.txt`+`hou/ParmTemplate.txt`；pypackage；那 1 个 `include`）。
2. 在 `hou/ParmTemplate.txt`、`hou/ObjNode.txt`、一个枚举页上确认 homclass `::` 方法块边界（块在下一个 `::`、`@section` 或 `==` 处结束）。
3. legacy 误报：每个 `-` 文件都应有非 `-` 兄弟；任何孤儿 `-` 文件都要查清。
4. legacy 之外的 `#internal` 碰撞：抽查 5–10 个非 apex 碰撞——若存在真实的跨 context 同名，则 `name→[ids]` 并按 `context` 消歧。
5. skill `.md` 的交叉链接语法（逐个读 SKILL.md）再决定如何解析成边。
6. `#tags` 缺失率 → 缺失 = 空列表，绝不报错。
