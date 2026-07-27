# Hython 分层验证 Wave A 证据

- 日期：2026-07-26
- 分支：`feature/html-to-houdini-pipeline`
- 代码提交：
  - `e108db6` — HTML→Houdini 基础能力与 catalog 扩展
  - `9bcf5c7` — orientation/bake 缺属性时 fail-closed
- Houdini：21.0.440
- Hython：`C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe`

## 已通过

### 离线

```text
3509 passed, 12 skipped
Ruff: All checks passed
compileall: passed
```

### HFS 知识库契约

默认用户 Houdini package 会由 KeeTools 向 stdout 写入非 JSON 文本。按照
RN-013 的既有处置，验证使用进程级 clean package boundary：

```powershell
$env:HOUDINI_PACKAGE_SKIP = '1'
$env:HOUDINI_NO_ENV_FILE = '1'
$env:HOUDINI_PATH = "$hfs/packages/apex;$hfs/packages/kinefx;&"
```

结果：

```text
11 passed
```

### Catalog probe

`tests/modeling/catalog_probe_houdini.py` 在真实 Houdini 21.0.440 下通过，确认
新增 `torus`、`sphere`、`copyxform` 和 `sweep::2.0` 管状参数可被探测。

### L1 scratch smoke

运行：

```powershell
& "$hfs/bin/hython.exe" -u tests/runtime/scratch_houdini_smoke.py
```

结果：

```text
SCRATCH SMOKE OK
```

已覆盖：

- catalog box 的 literal parm 设置、cook 几何统计和 commit；
- 非法 parm 失败后保留 sandbox；
- Add SOP 产生 orphan points，health gate 拒绝并保留 sandbox；
- orientation checks 缺少 `component_id` 时 bake/orientation fail-closed；
- committed target 存在且 sandbox 清理；
- finally 只清理本 smoke 使用的唯一前缀节点，不保存/加载/清空/导出 HIP。

## 尚未通过 / 未执行

- L2：真实 Houdini + Secure Bridge + Coordinator 的无 LLM journey；
- L3：真实 Provider + Runtime + Bridge + Houdini 的 scratch-native journey；
- L4：HTML 草图两轮会话；
- catalog attribute-writing 节点尚未加入，当前 orientation 正例仍需专门的
  属性写入能力或受限测试 fixture；
- 旧 `provider_journey.py` 尚未迁移到 scratch-native evidence。

本证据只证明 Wave A 的 L0/L1 以及 catalog/HFS 子门，不代表 Agent 全链路
已经验收通过。
