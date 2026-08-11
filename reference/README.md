# reference/ — 0.1 代码参考归档

本目录归档了 **灵汐 AgentOS 0.1** 的源代码与脚本，仅供**参考**，不参与 0.2 的运行时、构建、测试或 import 解析。

> **重要**：本目录下的代码**不是**活代码。不要 import、不要执行、不要作为依赖。
> 0.2 的活代码在 `plugins/`、`kernel/`、`frontend/`、`plugins/sdk/`。

## 为什么保留？

0.2 架构迁移（Rust 内核 + MCP 插件化）从 0.1 的纯 Python 实现演进而来。
保留 0.1 代码是为了：

1. **迁移溯源**：查看某段 0.2 逻辑对应的 0.1 原始实现（git blame 跨 rename 可追溯）
2. **参考实现**：0.2 某些功能尚未完整迁移时，可参考 0.1 的实现思路
3. **回归对比**：0.2 行为与 0.1 不一致时，可对照排查

## 目录结构

| 子目录 | 内容 | 来源 |
|--------|------|------|
| `0.1_src/` | 0.1 Python 后端源码（735 文件，34 顶层模块） | 原 `src/` |
| `0.1_scripts/` | 0.1 启动与验证脚本（6 文件） | 原 `start_web.sh`、`start_web_cn.bat`、`verify_*.py` |
| `0.1_verify_scripts/` | 0.1 时代 docs/working 下的验证/调试脚本（39 文件） | 原 `docs/working/*.py` |

## 0.1 与 0.2 的架构对应

| 0.1 (`reference/0.1_src/`) | 0.2 对应位置 | 说明 |
|------|------|------|
| `core/logging/` | `plugins/sdk/src/agentos_plugin_sdk/logging/` | 下沉到 SDK |
| `core/exceptions/` | `plugins/sdk/src/agentos_plugin_sdk/exceptions/` | 下沉到 SDK |
| `config/settings.py` | `plugins/sdk/src/agentos_plugin_sdk/settings.py` | 下沉到 SDK |
| `auth/` | `plugins/shared/system/channel_api/`(runtime) | RBAC 部分未迁移(0.2 用 Rust tenant crate) |
| `tasks/` | `plugins/shared/system/tasks/` | 平铺迁移 |
| `isolation/` | `plugins/shared/system/isolation/` | 平铺迁移 |
| `llm/` | `plugins/shared/system/llm/` | 平铺迁移 |
| `pipeline/`(引擎) | `kernel/crates/engine/`(Rust) | **重写为 Rust 内核** |
| `channels/websocket/`、`api/` | `kernel/crates/api/`(Rust) | **重写为 Rust 内核** |
| `core/event_bus/` | `kernel/crates/`(Rust) | **重写为 Rust 内核** |
| `config/loader.py` | `kernel/crates/config/`(Rust) + SDK settings | 0.1 ConfigLoader 已废弃 |
| `plugins/shared/`(管道插件源) | `plugins/shared/pipeline/` | 已迁移为 sidecar 副本 |
| `tools/builtin/` | `plugins/shared/tools/` | 已迁移(MCP 工具) |

## 不要做的事

- ❌ `import` 本目录任何模块（0.2 运行时不依赖此处）
- ❌ 执行 `0.1_scripts/` 下的启动脚本（0.2 用 `start_web_02.sh`）
- ❌ 把本目录加入 `PYTHONPATH`（会让 0.1 的旧模块污染 0.2 命名空间）
- ❌ 在本目录写测试或修复 bug（这里是冻结的参考）

## 如果需要调试对照

临时把某段 0.1 实现拉回研究是可以的，但请：

```bash
# 仅查看，不执行
git log --follow reference/0.1_src/pipeline/engine.py

# 或在工作区外另开目录对比，避免污染 0.2 的 import 解析
```

---

归档时间：2026-08-11
对应迁移：0.2 架构（src/ 清理 + 测试整理）完成后
