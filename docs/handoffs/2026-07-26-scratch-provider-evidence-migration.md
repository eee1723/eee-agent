# Scratch-native Provider 验收迁移证据

- 日期：2026-07-26
- 分支：`feature/html-to-houdini-pipeline`
- 验收入口：`tests/runtime/runtime_mvp_provider_e2e.py`
- Provider adapter：`tests/runtime/provider_journey.py`
- 状态：迁移与离线验证通过；真实 Provider 未运行

## 已迁移内容

真实 Agent 提示词现在要求确定执行：

```text
scratch_build(tabletop box)
→ verify_geometry(8 points / 6 faces)
→ scratch_commit(/obj/eee_provider_scratch_table)
```

旧 `AwaitingApproval`、proposal digest、approval、post-Apply artifact/vision
字段不再作为 scratch 主链路的通过条件。旧 ChangeSet 内核及其独立测试保留。

新的严格 bounded evidence 字段为：

```text
run_status
scratch_build_seen
scratch_build_ok
geometry_verified
scratch_commit_seen
commit_status
commit_receipt_present
final_path
final_geometry_ok
sandbox_absent
restart_replay_last_seq
scene_cleanup
```

adapter 同时要求：

- 三个工具均有持久化 `tool.started` 和 `tool.completed`；
- build 与 verify 的完整有界结果结构合法；
- commit 结果为 committed，目标路径精确匹配；
- Secure Bridge 回读最终容器和 `tabletop` 的 8 点/6 面几何；
- run-scoped sandbox 已不存在；
- Runtime 重启后三个工具事件仍可 replay；
- hython worker 正常清理并写出 cleanup receipt。

## 迁移中发现并修复的生产缺口

真实 `BridgeReadOnlyProvider.geometry_stats()` 返回：

```text
{ok, path, node_type, is_locked, geometry_stats:{points, primitives, bbox}}
```

而 `verify_geometry` 原先只兼容测试中的扁平
`{points, prims, bbox}`，导致真实面数被读成 0。现已：

- 解包 production `geometry_stats` envelope；
- 接受 Bridge 冻结后的只读 Mapping；
- 使用 `primitives`，并保留 `prims` 兼容回退；
- 在 L2 真实 Houdini Bridge journey 中直接调用生产 `verify_geometry`，
  8 点/6 面断言通过。

## 当前分层结果

- offline：passed（`3514 passed, 12 skipped`）
- hython L1：passed
- deterministic Bridge L2：passed（`verify_geometry_ok=true`）
- real Provider L3：`not_run`
- 原因：当前进程未提供已批准 Provider API 凭据
- GUI/manual：`not_run`

真实 L3 必须在凭据可用时显式 opt-in，连续运行三次；不得用 L0/L1/L2
结果替代该层。
