# plugins 模块文档

## 需求

Agent OS 的插件体系，按层级分为三大类：

1. **Input 插件**（7 个）：准备管道输入数据，如上下文构建、知识注入、参数注入、提示词构建等
2. **Output 插件**（12 个）：处理管道输出，如停止检查、错误分析、任务评估、委派等待策略等
3. **Core 插件**（2 个）：执行核心逻辑，LLM 调用和工具执行

所有插件遵循 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin` 接口，通过 `PluginContext.state` 读写数据、通过 `ctx.get_service()` 获取服务。

## 逻辑

### 迁移策略（M6）

**合并原则**：关注点相同的插件合并，关注点不同的插件分离。
- 合并：`duplicate_call` + `repetitive_output` → `DuplicateCheckPlugin`（共享重复计数状态）
- 合并：`stop_requested` + `stop_check_strategy` + `task_status` → `StopCheckPlugin`（共享 should_stop/iteration 状态）
- 分离：`stop_check` 与 `task_evaluation` 不合并（前者是"执行安全判断"，后者是"任务完成判断"）

**错误策略选择**：
- `ABORT`：安全检查、停止判断、错误检查、参数注入 — 不确定就不能继续
- `FALLBACK`：上下文构建、工具 Schema — 降级也能跑
- `SKIP`：记忆写入、追踪统计、结果格式化 — 失败不影响当轮结果

**State 命名空间约定**：
- Input 插件写 `context.*`、`knowledge.*`、`prompt.*`、`tool.*`、`security.*`、`reasoning.*` 命名空间
- Output 插件写 `router.*`、`track.*`、`memory.*`、`evaluation.*`、`error_analysis` 命名空间

### 委派等待策略（M11a）

跨管道路由后，管道间平权，等待策略由 Output 插件决定：
- **FireAndForgetPlugin**：不等待，适合不关心子管道结果的场景
- **EventCallbackPlugin**：事件驱动挂起，适合异步事件恢复场景

## 结构

### 子文件夹

| 子文件夹 | 文档 | 说明 |
|---------|------|------|
| `core/` | [CORE.md](core/CORE.md) | LLM 调用 + 工具执行核心插件 |
| `input/` | 无独立文档（7 个简单插件） | Input 插件目录 |
| `output/` | [OUTPUT.md](output/OUTPUT.md) | Output 插件目录（含 M11a 委派策略） |

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `__init__.py` | — | 模块入口 |

### Input 插件概览

| 插件 | 文件 | 优先级 | 错误策略 | State 命名空间 |
|------|------|--------|---------|---------------|
| ContextBuildPlugin | `context_build.py` | 10 | FALLBACK | `context.*` |
| ParamInjectPlugin | `param_inject.py` | 20 | ABORT | `tool.params_injected` |
| KnowledgeInjectPlugin | `knowledge_inject.py` | 30 | FALLBACK | `knowledge.context` |
| PromptBuildPlugin | `prompt_build.py` | 50 | ABORT | `prompt.*`, `messages` |
| ToolSchemaPlugin | `tool_schema.py` | 50 | FALLBACK | `tool_schemas`, `prompt.tool_descriptions` |
| SecurityCheckPlugin | `security_check.py` | 70 | ABORT | `security.decision` |
| ReasoningCheckPlugin | `reasoning_check.py` | 75 | SKIP | `reasoning.check_result` |

### 测试覆盖

测试文件：`src/agent_os/tests/test_plugins.py`（51 个测试）
M11a 委派策略测试：`src/agent_os/tests/test_delegation_plugins.py`

**测试策略**：所有测试使用 Mock 的 `PluginContext`，不依赖真实 LLM 调用。
