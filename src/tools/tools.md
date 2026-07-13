# tools 模块

## 需求

Agent 管道需要工具执行能力——当 LLM 返回工具调用时，管道需查找并执行对应工具，将结果反馈给 LLM 继续推理。

核心需求：
1. **工具注册**：提供统一的工具注册机制，支持按名称注册工具函数及其输入 Schema
2. **工具查找**：ToolCore 能按名称快速定位已注册工具
3. **LLM 格式输出**：将注册的工具描述转换为 OpenAI function calling 格式，供 LLM 选择调用
4. **简化设计**：去掉旧代码的 LRU 卸载、DB 同步、按需加载等复杂功能

## 逻辑

### 数据模型

```
ToolDefinition
  ├── name: str              # 工具唯一名称
  ├── description: str       # 功能描述
  ├── input_schema: dict     # JSON Schema（OpenAI function calling 格式）
  └── handler: Callable      # 工具执行函数（同步或异步）
```

### 注册表操作

```
ToolRegistry
  ├── register(name, func, schema, description)  # 注册工具
  ├── get(name) → ToolDefinition                  # 获取（不存在抛 KeyError）
  ├── has(name) → bool                            # 是否存在
  ├── list_tools() → list[ToolDefinition]          # 列出全部
  └── get_tools_for_llm() → list[dict]            # OpenAI 格式输出
```

### ToolCore 执行流程

```
state["raw_tool_calls"] → 逐个查找工具 → asyncio.wait_for 执行 → 收集结果
                                              ↓
                              超时 → 返回 timeout 错误信息
                              异常 → 返回 error 信息
                              成功 → 返回 data
                                                    ↓
                    写入 state:
                      tool_results — 结果列表
                      raw_result — 最后一个工具的结果文本
                      raw_tool_calls — 清空（已处理）
```

### PendingToolsOutput 信号流程

```
state["raw_tool_calls"] 非空 → RouteSignal("next_tool", target="tool_execute")
state["raw_tool_calls"] 为空 → 无信号（返回空 OutputResult）
```

优先级 6，与架构文档输出路由表中 next_tool 条目一致。

### 从旧代码精简的对照

| 旧组件 | 处理 | 理由 |
|--------|------|------|
| `ToolRegistry.register_with_handler()` | ❌ 去掉 | 合并为 `register(name, func, schema)` |
| `ToolRegistry.register_with_sync()` | ❌ 去掉 | 无 DB 同步需求 |
| `ToolRegistry.register_runnable()` | ❌ 去掉 | 无 MCP/Runnable 概念 |
| `ToolRegistry._try_load_tool_on_demand()` | ❌ 去掉 | 简化，工具显式注册 |
| `ToolRegistry._check_and_unload_if_needed()` | ❌ 去掉 | 无 LRU 卸载需求 |
| `ToolRegistry.get_tools_for_llm_yaml()` | ❌ 去掉 | 只保留 JSON 格式 |
| `ToolRegistry.get_tools_for_mcp()` | ❌ 去掉 | 无 MCP 需求 |
| `ToolRegistry.get/has/list_tools()` | ✅ 保留 | 核心查询能力 |
| `ToolRegistry.get_tools_for_llm()` | ✅ 保留 | LLM 工具描述格式 |

## 结构

### 文件清单

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `__init__.py` | 模块导出 | ToolRegistry, ToolDefinition |
| `types.py` | ToolDefinition dataclass | ToolDefinition |
| `registry.py` | ToolRegistry 简化版注册表 | ToolRegistry |
| `tool_context.py` | pipeline 类型桥接层（解耦 builtin/ 对 pipeline 的直接依赖） | PipelineMessage, MessageType, emit, HotSwapManager, PluginRegistry, RollbackManager, PipelineConfig, PipelineConfigStore, PipelineEngine, get_engine_registry |
| `README.md` | 模块文档 | — |
| `tools.md` | 模块文档（规范化副本） | — |

### 外部关联文件

| 文件 | 职责 | 暴露接口 |
|------|------|----------|
| `plugins/core/tool_core.py` | ToolCore 工具执行 Core 插件 | ToolCore |
| `plugins/output/pending_tools.py` | PendingToolsOutput 输出插件 | PendingToolsOutput |
| `tests/test_tool_core.py` | 单元测试（23 个） | — |

### 依赖关系

```
tools/types.py ← tools/registry.py ← plugins/core/tool_core.py
                                    ← plugins/output/pending_tools.py

tools/tool_context.py ← tools/builtin/*/tool.py   (pipeline 类型桥接)
```

- `tools/` 不依赖 `pipeline/`，是独立的数据层
- `tool_core.py` 和 `pending_tools.py` 依赖 `pipeline/plugin.py`（插件接口）和 `pipeline/types.py`（StateKeys, RouteSignal）
- `tool_core.py` 依赖 `tools/registry.py`（批量注册）
- `tools/builtin/` 下的工具通过 `tools/tool_context.py` 间接访问 pipeline 类型，不直接 `from pipeline import`

### 相关文档索引

- [项目章程](../../docs/project/charter.md) — 项目整体目标和里程碑
- [项目逻辑](../../docs/project/logic.md) — 全局架构设计决策
- [项目结构](../../docs/project/structure.md) — 完整目录结构说明
