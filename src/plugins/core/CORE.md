# core 插件模块文档

## 需求

管道核心执行插件，负责实际调用大模型和工具：

1. **LLMCore**：通过 LiteLLM 统一调用各厂商大模型，内置重试和流式回调
2. **ToolCore**：工具执行核心，检测工具调用并执行注册工具

## 逻辑

### LLMCore

- 使用 LiteLLM 的 `completion()` 接口，支持 `provider/model` 格式的模型字符串
- 内置重试机制：`_is_retryable_error()` 通过类名匹配判断可重试错误
- 重试用完返回 raw_error 不抛异常（Core 尽力而为）
- 支持流式回调：`stream_callback` 参数接收流式 token

### ToolCore

- 同步/异步兼容：`inspect.iscoroutinefunction` + `asyncio.to_thread`
- 工具执行错误不抛异常，返回 `{success: False, error: ...}`
- 通过 `ctx.get_service("tool_registry")` 获取 ToolRegistry

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `llm_core.py` | `LLMCore` | LLM 调用核心，LiteLLM 统一接口 |
| `tool_core.py` | `ToolCore` | 工具执行核心，同步/异步兼容 |
| `__init__.py` | `LLMCore`, `ToolCore` | 模块入口 |

### 依赖关系

```
LLMCore  ──→ ICorePlugin (pipeline/plugin.py)
         ──→ ErrorPolicy, StateKeys (pipeline/types.py)
         ──→ litellm (外部依赖)

ToolCore ──→ ICorePlugin (pipeline/plugin.py)
         ──→ ErrorPolicy, StateKeys (pipeline/types.py)
```
