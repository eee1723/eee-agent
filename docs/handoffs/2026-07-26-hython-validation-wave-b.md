# Secure Bridge 确定性验证 Wave B 证据

- 日期：2026-07-26
- 分支：`feature/html-to-houdini-pipeline`
- 新增入口：`tests/runtime/scratch_bridge_houdini_journey.py`
- 依赖：真实 Houdini 21.0.440 hython worker
- LLM/Provider：未使用

## 运行

```powershell
python tests/runtime/scratch_bridge_houdini_journey.py `
  --hython "C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe" `
  --evidence <temporary-evidence-path>
```

worker 使用 RN-013 已批准的进程级 clean package boundary：

```text
HOUDINI_PACKAGE_SKIP=1
HOUDINI_NO_ENV_FILE=1
HOUDINI_PATH=<HFS>/packages/apex;<HFS>/packages/kinefx;&
```

## 结果

```text
SCRATCH BRIDGE JOURNEY OK
```

bounded evidence：

```json
{
  "build_ok": true,
  "capability": "scratch.v1",
  "commit_ok": true,
  "final_query_ok": true,
  "geometry_ok": true,
  "orientation_refusal_ok": true,
  "refused_sandbox_destroyed": true,
  "worker_cleanup_ok": true,
  "worker_removed_count": 1
}
```

## 已证明

- Bridge discovery 和 token handshake 可用；
- `scratch.v1` capability 被真实 worker 宣告；
- 生产 `BridgeChangeSetProvider` 可调用 `scratch.exec`；
- 几何统计经 Secure Bridge 回读；
- `scratch.commit` 可从 sandbox 提升到真实 `/obj` 容器；
- final container 和其输出子节点均可查询；
- orientation checks 在缺少 `component_id` 时经 Bridge fail-closed；
- 拒绝的 sandbox 可通过 `scratch_destroy` 清理；
- worker 停止后只清理本次测试创建的节点。

## 尚未证明

- RuntimeService/LangGraph/真实 Provider 的工具选择；
- `render_sketch` 审核门；
- 真实 Agent 连续三次成功；
- 参数表达式和多案例 eval。
