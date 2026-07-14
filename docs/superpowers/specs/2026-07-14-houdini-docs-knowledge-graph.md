# Houdini 文档知识图谱（按需查询）设计

- 状态：设计稿（待用户审阅）
- 日期：2026-07-14
- 开发分支：`feature/houdini-knowledge-graph`
- 开发基线：`feature/foundation`（不依赖 `feature/runtime`）
- 范围：为 Agent 增加离线构建、本地只读查询、可追溯的 Houdini 文档知识图谱
- Houdini 基线：21.0.440
- 相关文档：`docs/superpowers/specs/2026-07-13-houdini-general-agent-architecture-design.md`、`CLAUDE.md`

## 1. 摘要

Agent 当前反复出现的失败模式是猜测 Houdini 事实：节点类型名和版本、VEX 函数签名、`hou.*` 类/函数/方法以及节点参数名。本项目的原则是“verify, don't guess”，但目前只有 4 个始终注入 system prompt 的项目 skill，以及实时创建临时节点并读取参数的 `describe_node_type()`；Agent 无法低成本查询本机 Houdini 官方文档。

Houdini 21.0.440 在安装目录中随附结构化文档 zip。文档包含实体类型、命名空间、版本、标签、上下文、继承、related、include 和带类型交叉引用，因此适合构建为一个小型属性图。系统不引入 Neo4j、NetworkX、向量数据库或 embedding 模型，而使用 Python 标准库 `sqlite3` 构建单文件、版本化的 SQLite 知识缓存：普通表保存实体、别名和边，FTS5 负责全文候选召回。

两个只读工具向 Agent 暴露按需查询：

- `search_houdini_knowledge`：精确 symbol 查询、自由文本搜索和结构过滤。
- `get_houdini_knowledge`：按实体和 section 读取受限长度正文。

知识库证明的是“本机官方文档记录了什么”。对于节点是否能在当前进程中创建、实际参数是什么，Agent 仍必须使用实时 Houdini introspection；文档事实不能覆盖真实 Houdini 结果。

## 2. 已批准的关键决策

1. 表示形式是知识图谱，不退化为仅有全文索引的文档搜索。
2. 来源覆盖 SOP 节点、VEX 函数、HOM `hou.*` API 和仓库内 4 个 skill。
3. 存储使用 SQLite 属性图 + FTS5，不使用一次性加载的 JSON，也不新增第三方运行时依赖。
4. 文档页面身份、文档中的 `#internal` 和 Houdini 可执行 symbol 是三个不同概念。
5. HOM 方法是一等实体，不截断为 class 的前 15 个摘要项。
6. 构建时使用本机 HFS 文档，并通过 `<HFS>/bin/hython.exe` 生成真实 SOP NodeType inventory；不需要 Houdini GUI 或 RPC。
7. 构建产物是 `%LOCALAPPDATA%\EEEAgent` 下可重建的共享 cache，不写入源码包、不提交 git、也不作为某个 Run 的业务 artifact。
8. 默认不在首次工具调用中同步自动构建。显式构建成功后工具才可查询。
9. 当前独立分支实现核心构建、存储、查询服务和 legacy CLI 工具接线；Runtime 的 read-only allowlist、事件和 Run snapshot 接线在分支集成时完成。
10. skill 可以被图谱发现和引用，但知识工具不能替代未来 Deep Agents 原生 skill 激活与 Run snapshot 记录。

## 3. 目标与非目标

### 3.1 目标

- 查询精确的 SOP、VEX、HOM class/function/method/module 和项目 skill。
- 正确处理 namespace、当前/历史节点文档、同名 symbol、大小写差异和重载。
- 返回来源路径、anchor、Houdini build、KB manifest hash 和匹配原因。
- 支持 outgoing/incoming 一跳关系、继承、类成员和 related 查询。
- 把单次工具响应控制在稳定上限内，避免重新制造上下文膨胀。
- 构建结果可复现、可校验、可原子替换、可检测过期。
- 查询路径完全只读，不依赖 Houdini RPC，不扩大 Agent 权限。
- 为后续 Runtime 的受限 Research Capability 提供稳定 service 接口。

### 3.2 非目标

- 不构建通用互联网搜索或在线 SideFX 爬虫。
- 不索引图片、视频字节或 `images.zip`。
- v1 不索引 HScript expression/command 文档。
- 不提供向量语义检索、多语言翻译或自然语言问答模型。
- 不执行多跳推理、中心性分析或图算法。
- 不用知识库替代 `describe_node_type()` 或其他实时 Houdini 验证。
- 不在本分支实现 Runtime、WebSocket、Session/Run、ArtifactService 或 capability graph。
- 不在本分支移除 system prompt 当前注入的 skill。
- 不把 SideFX 原始文档或派生知识缓存提交到 git 或随产品分发。

## 4. 当前项目基线与集成边界

### 4.1 当前工具契约

- LangChain 工具使用 `@tool`。
- 工具返回纯 Python dict，预期失败返回 `{"ok": False, "code": ..., "error": ...}`，不得把异常传播给 Agent。
- 知识查询不走 rpyc bridge，也不 import `eee_agent.bridge`。
- Foundation 的 `build_agent()` 默认使用 `all_tools()`，因此本分支可把两个知识工具接入 legacy CLI 做端到端验证。
- `describe_node_type()` 仍是节点实时参数和可创建性的最终检查。

### 4.2 与 Runtime 分支的关系

本分支从 `feature/foundation` 独立开发，不合入 `feature/runtime` 的未完成工作。核心模块不得 import Runtime、SQLite async repository、WebSocket 或 Qt。

Runtime 集成时需要单独完成：

- 把两个工具加入 `read_only_tools()` 精确 allowlist。
- 把 KB cache 目录纳入 RuntimePaths，但保持其“共享可重建 cache”语义。
- Runtime 启动 preflight 检查 KB 状态，缺失或过期时发送结构化状态，不阻止 Runtime 启动。
- Run snapshot 记录 `kb_manifest_sha256`、`kb_schema_version` 和 `houdini_build`。
- Supervisor/Modeling Capability 通过受限 Research Capability 使用 KnowledgeService；Executor 不直接读取任意正文。

这些是合并契约，不是本独立分支对 Runtime 代码的依赖。

### 4.3 Skill 权威边界

官方 Houdini 文档和项目 skill 使用同一个图查询入口，但结果必须携带 `authority`：

- `official_houdini_docs`
- `project_verified_skill`

skill 实体保存 path、内容 hash、标题、摘要、sections 和相关 Houdini symbol。知识工具可以返回 skill 参考内容，但不能声称已经激活 skill；未来正式激活仍通过 Deep Agents 原生 skill loader，并由 Run snapshot 记录内容 hash。

## 5. 已核实的本机语料

数据来自 `D:\houdini\houdini\help`，另一台机器的等价根目录是 `C:\Program Files\Side Effects Software\Houdini 21.0.440\houdini\help`。

| 来源 | zip/路径 | 原始 `.txt` | v1 页面实体 |
|---|---|---:|---:|
| SOP 节点 | `nodes.zip` → `sop/*.txt` | 1,145 | 1,115 个 `#type: node` |
| VEX | `vex.zip` | 1,161 | 1,099 个 `#type: vex` |
| HOM | `hom.zip` | 936 | 890 个非 `include` typed 页面 |
| 项目 skill | `skills/*/SKILL.md` | 4 | 4 |

HOM typed 页面共 891 个：

- `homclass` 339
- `homfunction` 342
- `hommodule` 203
- `pypackage` 5
- `hompackage` 1
- `include` 1（不作为实体）

339 个 class 中共解析出 5,476 个方法；111 个 class 超过 15 个方法，最大 class 有 453 个方法。方法必须成为独立实体。

SOP 文档还验证出：

- `#internal` 碰撞 key 66 个。
- 文件名以 `-` 结尾但没有相同 internal 当前兄弟的文档 15 个。
- 同一 internal 同时出现 legacy、2.0、3.0 三份文档。
- `#internal` 可能与真实 operator type 不同，例如 `loadslices.txt` 写的是 `file`。
- namespace 可能只可靠地体现在来源文件名中，例如 `apex--buildfkgraph.txt` → `apex::buildfkgraph`。

SOP typed 页面中至少有约 3,078 个直接 typed reference，以及约 2,396 个 `[Label|Kind:target]` 引用。解析器必须覆盖两种形式。

页面实体基础数量约为 3,108；加入 5,476 个 HOM method 后，预期 v1 实体数量约为 8,584。验收不硬编码单个总数，而按 kind 记录 manifest 并使用与 Houdini build 对应的合理范围和 invariants。

本机 venv 的 SQLite 3.50.4 和 Houdini Python 的 SQLite 3.44.2 均已验证 `ENABLE_FTS5=1`。

## 6. 来源解析与规范化

### 6.1 通用文档格式

- UTF-8 BOM 使用 `utf-8-sig` 解码。
- `#key: value` 元数据允许缩进行续写。
- `= Title =` 是页面标题。
- 首个 `"""..."""` 是摘要候选。
- `== Heading ==` 和带 anchor 的 heading 形成 section。
- 原始 zip entry 使用 POSIX 路径，数据库中不得保存机器绝对 HFS 路径。

### 6.2 引用解析

第一阶段只抽取统一 Reference DTO，不决定最终目标：

```text
Reference
- display_text
- target_kind
- raw_target
- normalized_target
- anchor
- source_span
```

必须覆盖：

```text
[Node:sop/boolean]
[Boolean SOP|Node:sop/boolean]
[Hom:hou.Node#createNode]
[node()|#node]
[Vex:intersect]
:include _common#geometry:
```

第二阶段在所有实体和 alias 建立后解析边。未命中或多候选引用保留 `target_raw`、`target_anchor` 和状态，不得猜测目标。

`Icon`、图片和视频引用不创建知识实体；必要时作为页面属性保留。`include` 保存边，正文展开仅在来源和 anchor 都能唯一解析时进行。

### 6.3 SOP 节点身份

每份 typed node 页面都有唯一 `document_id`，以 context、逻辑 zip entry 和文档版本生成。`#internal` 只是 alias；不得成为主键或直接成为 `create_node` 参数。

节点页面规范化得到：

```text
NodeDocument
- entity_id
- context
- source_path
- namespace
- internal_metadata
- document_version
- is_current_document
- operator_type_candidates[]
- operator_type_status
```

`operator_type_status` 取值：

- `verified_at_build`
- `documented_unverified`
- `ambiguous`
- `historical_only`
- `unresolved`

构建器从来源 basename、`#namespace`、`#version`、`#internal` 生成候选，再与 hython NodeType inventory 对账。只有 inventory 中存在且唯一匹配的候选才能标为 `verified_at_build`。

查询结果可以返回 `operator_type`，但同时必须返回状态。Agent 使用节点前仍调用 `describe_node_type(operator_type)`；如果实时结果与 KB 不同，以实时结果为准并报告 stale KB。

### 6.4 HOM

HOM 页面实体分为 class、function、module、package。class 内的每个 `::` 方法块解析为独立 method entity：

```text
hom:class:hou.Node
hom:method:hou.Node#createNode
hom:function:hou.node
hom:module:hou.qt
```

method 保存 owner、method name、qualified name、signature、returns、summary、cppname、source anchor 和正文。重载使用同一逻辑 method entity 的 `signatures[]`；如果真实语料需要区分同名独立块，则增加稳定 overload ordinal，而不是覆盖。

class 到 method 使用 `declares_method` 边；`#superclass` 使用 `inherits_from` 边。大小写保持原样，精确索引大小写敏感，同时可增加低优先级 casefold alias；casefold alias 多候选时必须返回歧义。

### 6.5 VEX

- `#type: vex` 页面形成函数实体。
- 所有 `:usage:` 形成 `signatures[]`。
- `:returns:` 形成 returns 文本。
- `@related` 形成 `related_to` 边。
- `#context`、`#group`、`#tags` 形成结构字段。
- `_common.txt`、suite、context 和 include 页面不作为 v1 函数实体，但可以作为 include target。

### 6.6 Skill

四个 `SKILL.md` 解析 frontmatter、标题、heading sections 和正文。实体 ID 使用 slug，属性包含仓库相对路径和 SHA-256。skill 文本中的 Houdini symbol 只在语法明确时建立边；普通自然语言提及不自动推断关系。

## 7. 图 Schema

### 7.1 实体类型

- `node_document`
- `vex_function`
- `hom_class`
- `hom_method`
- `hom_function`
- `hom_module`
- `hom_package`
- `skill_reference`

公共字段：

```text
entity_id
kind
subtype
canonical_name
title
summary
authority
source_path
source_anchor
is_current
attributes_json
```

### 7.2 Alias

alias 是多值关系，不是 `name → 单个 id`。alias type 包括：

- `qualified_name`
- `short_name`
- `operator_type`
- `document_slug`
- `internal_metadata`
- `legacy_name`
- `title`
- `filename_alias`
- `casefold_alias`

每条 alias 有 priority。精确 qualified name 和已验证 operator type 优先级最高；casefold 和 internal metadata 最低。查询存在并列最高候选时返回 `AMBIGUOUS_SYMBOL`，不得静默选择。

### 7.3 边类型

- `references`
- `related_to`
- `inherits_from`
- `declares_method`
- `member_of`
- `includes`
- `supersedes`
- `documents_operator`
- `routes_to_skill`

边保存 source、predicate、可空 target entity、raw target、anchor、resolved 状态和来源位置。为 incoming 查询建立 `(target_id, predicate)` 索引。

## 8. SQLite 知识缓存

### 8.1 表

SQLite schema version 1 包含：

```text
kb_metadata(key PRIMARY KEY, value_json)
entities(entity_id PRIMARY KEY, kind, subtype, canonical_name, title,
         summary, authority, source_path, source_anchor, is_current,
         attributes_json)
aliases(alias, entity_id, alias_type, priority)
edges(edge_id PRIMARY KEY, source_id, predicate, target_id, target_raw,
      target_anchor, resolved, source_location)
documents(entity_id PRIMARY KEY, body)
sections(entity_id, section_key, heading, body, ordinal)
entities_fts(entity_id UNINDEXED, canonical_name, title, summary, tags, body)
```

需要的普通索引至少包括：

- alias 精确值和 type
- entity kind/subtype/current
- edge source/predicate
- edge target/predicate
- section entity/key

FTS5 只用于自由文本候选召回；symbol、tag、context、superclass 和关系查询使用结构索引。图语义不依赖 FTS。

### 8.2 位置

默认 Windows 路径：

```text
%LOCALAPPDATA%\EEEAgent\cache\knowledge\houdini\21.0.440\knowledge.sqlite3
```

测试通过显式临时路径隔离。兼容配置允许 `EEE_KB_PATH` 覆盖，但相对路径不得隐式相对 Houdini cwd；统一通过配置层解析为绝对路径。

### 8.3 Manifest

`kb_metadata` 至少保存：

```text
kb_schema_version
builder_version
parser_version
created_at_utc
houdini_version
houdini_build
manifest_sha256
source_archives[{logical_name,size_bytes,sha256}]
skill_sources[{path,sha256}]
node_inventory_sha256
entity_count_by_kind
edge_count_by_predicate
unresolved_reference_count
ambiguous_alias_count
```

manifest hash 只覆盖确定性字段，不包含创建时间和绝对机器路径。相同源码、HFS build 和 parser version 应得到相同 manifest hash。

### 8.4 读取

运行时以 SQLite URI `mode=ro` 打开，设置 `query_only=ON`。查询服务不执行 migration；schema 不兼容、integrity check 失败或 source manifest 过期时返回结构化 unavailable/stale 状态。

## 9. 构建生命周期

### 9.1 HFS 定位

优先级：

1. CLI `--hfs`
2. `EEE_HFS`
3. `HFS`
4. 已验证的两台机器路径

定位后验证 `houdini/help/{nodes,hom,vex}.zip`、`bin/hython.exe` 和版本。路径只用于本次构建，不写入数据库。

### 9.2 构建流程

1. 获取目标 cache 的独占构建锁；锁包含 PID、开始时间和 nonce。
2. 读取并 hash 三个 zip 和四个 skill。
3. 通过 hython 子进程导出 SOP NodeType inventory JSON 到内存或受控临时文件。
4. 解析页面、sections、aliases 和 unresolved references。
5. 完成引用 resolution、节点 candidate 对账和图 invariants。
6. 生成确定性 metadata、计算 manifest hash，并在同一目录创建 `knowledge.sqlite3.tmp.<pid>.<nonce>`。
7. 写入 metadata、图表和 FTS，运行 foreign key 检查、`integrity_check` 和 build self-check。
8. 关闭并刷新临时数据库。
9. 使用 `os.replace` 原子替换正式 cache。
10. 释放锁；任何失败保留上一份有效 cache，并删除属于本次 nonce 的临时文件。

构建不得覆盖仍然有效的旧 cache 后再验证。stale lock 只能在 PID 不存在且超过明确超时后回收。

### 9.3 构建触发策略

- v1 不提供自动构建配置，也不在模型工具调用中同步构建。
- legacy CLI 在 cache 缺失时返回带构建命令的 `KB_NOT_BUILT`。
- 显式 CLI：`python -m eee_agent.knowledge.build --hfs <path>`。
- `--selftest` 只使用合成语料和临时 SQLite，不要求 HFS/hython。
- 未来 Runtime 可以在后台构建并发送进度事件，但该行为不属于本分支。

## 10. KnowledgeService

核心 service 不依赖 LangChain：

```python
class KnowledgeService:
    def status(self) -> KnowledgeStatus: ...
    def search(self, request: SearchRequest) -> SearchResponse: ...
    def get(self, request: GetRequest) -> GetResponse: ...
    def neighbors(self, entity_id: str, predicate: str | None,
                  direction: str, limit: int) -> NeighborResponse: ...
```

service 负责：

- schema/manifest 检查
- symbol resolution
- 结构过滤
- FTS 查询
- 一跳邻接
- section 读取
- 结果和字符上限
- provenance

LangChain tool adapter 只负责输入校验、DTO 转 dict 和异常转结构错误。

## 11. 工具 API

### 11.1 `search_houdini_knowledge`

```python
search_houdini_knowledge(
    symbol: str | None = None,
    query: str | None = None,
    kinds: list[str] | None = None,
    context: str | None = None,
    tag: str | None = None,
    superclass: str | None = None,
    include_historical: bool = False,
    limit: int = 5,
) -> dict
```

规则：

- `symbol` 做精确/alias resolution。
- `query` 使用 FTS5，必要时与结构过滤组合。
- 至少提供一个查询条件；空请求返回 `INVALID_ARGUMENT`。
- `limit` 默认 5，硬上限 25。
- 默认排除历史 node 文档。
- 多个最高优先级 symbol 候选返回 `AMBIGUOUS_SYMBOL` 和最多 10 个候选。
- 结果不返回全文。

单个结果最多包含：

```text
entity_id
kind/subtype
canonical_name
title
summary <= 240 chars
authority
is_current
operator_type/status（仅 node）
match_reasons
neighbors <= 8
source{path,anchor}
kb{manifest_sha256,houdini_build}
```

### 11.2 `get_houdini_knowledge`

```python
get_houdini_knowledge(
    entity_id: str,
    section: str | None = None,
    max_chars: int = 4000,
) -> dict
```

规则：

- `entity_id` 只能查库内主键，不能解释为文件路径。
- 默认 4,000 字符，硬上限 8,000。
- 有 section 时返回唯一 section；多候选返回可选 section 列表。
- 截断发生在段落边界；响应标记 `truncated` 和可用 section。
- 返回 authority、source 和 KB provenance。

### 11.3 错误码

- `KB_DISABLED`
- `KB_NOT_BUILT`
- `KB_BUILDING`
- `KB_STALE`
- `KB_SCHEMA_MISMATCH`
- `KB_CORRUPT`
- `INVALID_ARGUMENT`
- `UNKNOWN_ENTITY`
- `NO_MATCH`
- `AMBIGUOUS_SYMBOL`
- `INTERNAL_ERROR`

所有错误都返回 `ok: false`；不向 Agent 返回 traceback、绝对 HFS 路径或原始 SQL。

## 12. 上下文与 Agent 行为

### 12.1 响应预算

- 搜索 summary 240 字符。
- 搜索结果最多 25，默认 5。
- neighbors 最多 8。
- VEX signatures 默认最多 5；更多签名通过 get/section 获取。
- get 默认 4 KB、最大 8 KB。
- tags 默认最多 12。

### 12.2 Trim

不把 `get_houdini_knowledge` 直接加入当前按工具名保留最后一条的 `READBACK_TOOLS`。不同实体的正文互不替代，按工具名删除会丢失仍然有效的证据。

v1 依靠响应硬上限和 Agent 纪律控制上下文。未来 Runtime 使用以下策略：

- 相同 `(entity_id, section)` 结果去重。
- 每个 Run 设置知识正文总预算。
- Run 结束时只保留 evidence refs 和 DecisionSummary，不保留整段正文。

### 12.3 Prompt 规则

system prompt 只增加简短规则：

> 首次使用不熟悉的精确 Houdini 节点、VEX 函数或 HOM API 前查询一次；同一 Run 复用已有证据。知识库说明文档记录，节点实际可创建性和参数仍由实时 Houdini introspection 确认。实时结果与知识库冲突时，以实时结果为准并报告知识库可能过期。

不得使用“一次错误猜测比一次查询代价更大”之类可能诱发每步查询的绝对措辞。

## 13. 配置

- `EEE_KB_ENABLED`：默认 `true`，仅控制查询工具。
- `EEE_KB_PATH`：覆盖 SQLite cache 绝对位置。
- `EEE_HFS`：覆盖构建来源 HFS。

布尔配置复用严格的 `_env_bool` 语义；非法值必须在配置边界报错，不能静默当成 false。

## 14. 安全、隐私与来源约束

- 只读取明确的三个 HFS zip 和仓库内四个 skill。
- zip entry 必须拒绝绝对路径和 `..`，即使当前实现不解压到磁盘。
- SQL 查询全部参数化；FTS query 通过受控 tokenizer/query builder 生成。
- 查询工具只读打开数据库，不提供任意 SQL、文件读取或路径参数。
- 不把 cache、原始 SideFX 文档正文或 hython inventory 提交 git。
- 不自动把 cache 复制到 Run artifact 或外部观测系统。
- tool error、event 和日志只记录逻辑 source path、hash、状态和计数，不记录机器用户名或绝对安装路径。

## 15. 测试策略

### 15.1 纯单元测试

- BOM、metadata 续行、标题、摘要、section。
- direct/labeled/local/include reference。
- namespace、版本、legacy、孤立历史页、错误 internal。
- HOM 六种 page type、method、overload、anchor 和方法块边界。
- VEX 多签名、returns、related 和 include。
- skill frontmatter、section 和 hash。
- alias priority、大小写冲突和歧义结果。

测试 fixture 使用合成或最小重写语料，不提交大段官方文档。

### 15.2 SQLite 集成测试

- schema、索引、FTS5 和只读连接。
- symbol lookup、结构过滤、FTS 排序、incoming/outgoing neighbor。
- section、截断和 provenance。
- schema mismatch、corrupt/stale/not-built 错误。
- 两次相同输入构建得到相同确定性 manifest hash。
- 临时构建失败不破坏上一份有效 cache。
- 并发锁和 stale lock 回收。

### 15.3 本机语料 contract test

显式标记为需要 HFS，不进入无 Houdini 机器的默认 unit suite。对 21.0.440 验证：

- SOP typed node 数量处于审核范围，且关键 namespace/错误 internal fixture 对账正确。
- VEX function、HOM page 和 HOM method 数量处于审核范围。
- `apex::buildfkgraph`、`loadslices`、`kinefx::rigpython` 与 hython inventory 一致。
- 所有 `verified_at_build` operator type 存在于 inventory。
- resolved edge target 存在。
- unresolved、ambiguous、historical 数量被 manifest 记录。

### 15.4 检索质量评估

提交 30–50 条不包含受版权正文的 golden query YAML，覆盖：

- 精确 node、namespace node 和历史版本。
- 节点用途搜索。
- VEX 名称、签名和用途。
- HOM class/function/method、继承和同名歧义。
- related、无结果和错误 symbol。
- skill 路由。

报告 top-1、recall@5、MRR、歧义正确率、平均/最大响应字符和 p95 本地查询耗时。v1 验收门槛：精确 symbol top-1 100%（无歧义集）、golden recall@5 ≥ 95%、歧义用例 100% 返回候选而非静默选择。

### 15.5 Agent 行为测试

- 回答精确 Houdini 事实前调用查询工具。
- 同一 Run 不重复查询相同实体。
- 节点使用前仍调用实时 introspection。
- 实时结果与 KB 冲突时采用实时结果。
- KB 缺失/损坏时不循环重试。
- 两个知识工具只读且不引入隐式 `task` subagent。
- legacy CLI 端到端输出受限且带 provenance。

## 16. 验收标准

1. 默认测试套件全部通过且无新增第三方运行时依赖。
2. 构建器从 Houdini 21.0.440 本地文档和四个 skill 原子生成 SQLite cache。
3. cache 通过 integrity、schema、manifest 和 graph invariants。
4. `#internal` 不作为节点主键或未经验证的可执行类型。
5. 所有解析出的 HOM 方法均可按 qualified symbol 查询，不受 class 摘要上限影响。
6. direct、labeled、anchor 和 include 引用均被解析或显式记录为 unresolved。
7. 精确 symbol 歧义不会静默选错实体。
8. 两个工具在 cache 缺失、过期、损坏和正常状态下都返回稳定结构且不抛异常。
9. 工具响应遵守字符和数量硬上限。
10. 查询结果携带 authority、逻辑 source、manifest hash 和 Houdini build。
11. Agent 对节点实际可用性仍执行实时 introspection。
12. cache 不进入 git，构建失败不破坏旧 cache。
13. Runtime 集成契约有独立测试入口，不要求本分支 import Runtime。
14. golden query 指标达到第 15.4 节门槛。

## 17. 独立开发与合并策略

本功能在 `feature/houdini-knowledge-graph` 分支逐阶段开发。每个阶段必须：

- 从失败测试开始。
- 只实现该阶段批准的范围。
- 运行阶段测试和完整回归。
- 形成独立、可审核提交。
- 由独立审核者检查 spec 合规、代码质量、测试证据和范围漂移。

核心功能完成后不直接覆盖 Runtime 分支。先把 `feature/runtime` 更新到稳定状态，再选择 rebase/cherry-pick 或集成分支，解决 `registry.py`、配置路径、read-only allowlist 和 Run snapshot 的明确交点。合并前必须同时通过 Knowledge 和 Runtime 两套 contract tests。

详细阶段、Claude Code 开发提示词、审核提示词、命令和提交边界由后续实施计划定义。
