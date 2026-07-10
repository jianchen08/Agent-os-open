# output 插件模块文档

## 需求

管道输出阶段插件集合，负责：

1. **系统级判断**：停止检查、错误分析、任务评估、重复检查
2. **副作用操作**：持久化存储、追踪统计、结果格式化、记忆写入
3. **委派等待策略（M11a）**：跨管道路由后，决定父管道如何等待子管道结果

核心设计：管道间平权，路由只是状态转移（A → B），等待策略由 Output 插件决定，不在框架硬编码。

## 逻辑

### 插件执行顺序

Output 插件按 priority 升序执行：

| 优先级 | 插件 | 类型 | 路由信号 | 说明 |
|--------|------|------|---------|------|
| 1 | StopCheckPlugin | 系统级 | `end` | 停止检查（用户停止/迭代上限/超时/任务取消） |
| 2 | ErrorCheckPlugin | 系统级 | `end`, `next_llm` | Core 错误分析 + 可重试判断 |
| 3 | TaskEvaluationPlugin | 系统级 | `end`, `next_llm` | 任务评估（完成指示/工具结果/评估指标） |
| 4 | DuplicateCheckPlugin | 系统级 | `end` | 工具调用重复 + 输出内容重复 |
| 5 | FireAndForgetPlugin | 系统级（M11a） | — | 即发即忘，不等待子管道结果 |
| 5 | EventCallbackPlugin | 系统级（M11a） | — | 事件驱动挂起等待 |
| 6 | PendingToolsOutput | 系统级 | `next_tool` | 待执行工具（M3 已有） |
| 10 | PersistPlugin | 副作用型 | — | 执行记录 JSON 文件存储 |
| 15 | TrackPlugin | 副作用型 | — | token 用量/执行耗时/迭代统计 |
| 20 | ResultFormatPlugin | 副作用型 | — | 工具结果转 LLM 消息格式 |
| 25 | MemoryWritePlugin | 副作用型 | — | 对话内容写入记忆系统 |

### 委派等待策略（M11a）

跨管道路由后，父管道需要决定如何处理子管道的结果。两种策略互斥，由管道配置决定使用哪个：

| 策略 | 等待方式 | State 影响 | 适用场景 |
|------|---------|-----------|---------|
| FireAndForgetPlugin | 不等待 | 无状态更新 | 不关心子管道结果 |
| EventCallbackPlugin | 设 ENDED=True + WAIT_FOR | 管道挂起，外部恢复 | 异步事件恢复场景 |

### EventCallbackPlugin 执行流程

```
execute(ctx)
  → 检查 state[ROUTED_TO]
  → 无 → 返回空 OutputResult
  → 有 → 设 ENDED=True, WAIT_FOR=routed_to
       → 管道循环退出（ENDED=True）
       → 外部系统通过 EventBus 恢复管道
```

- ENDED + WAIT_FOR 语义：管道挂起，等待事件恢复
- 恢复时清除 WAIT_FOR，设 DELEGATION_RESULT，重新启动管道循环

### State 命名空间

| 插件 | State 命名空间 | 服务依赖 |
|------|---------------|---------|
| StopCheckPlugin | `router.stop_reason` | 无 |
| ErrorCheckPlugin | `execution_status`, `error_analysis` | 无 |
| TaskEvaluationPlugin | `evaluation.*` | 无 |
| DuplicateCheckPlugin | `router.duplicate_count`, `router.repetitive_count` | 无 |
| FireAndForgetPlugin | — | 无 |
| EventCallbackPlugin | ENDED, WAIT_FOR | EventBus |
| PendingToolsOutput | — | 无 |
| PersistPlugin | `track.execution_record` | 无 |
| TrackPlugin | `track.llm_usage`, `track.execution_stats` | 无 |
| ResultFormatPlugin | `tool.formatted_results` | 无 |
| MemoryWritePlugin | `memory.written` | `memory_service` |

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `stop_check.py` | `StopCheckPlugin` | 停止检查 — 用户停止/迭代上限/超时/任务取消 |
| `error_check.py` | `ErrorCheckPlugin` | 错误检查 — Core 错误分析 + 可重试判断 |
| `task_evaluation.py` | `TaskEvaluationPlugin` | 任务评估 — 完成指示/工具结果/评估指标 |
| `duplicate_check.py` | `DuplicateCheckPlugin` | 重复检查 — 工具调用重复 + 输出内容重复 |
| `fire_and_forget.py` | `FireAndForgetPlugin` | 即发即忘策略（M11a） |
| `event_callback.py` | `EventCallbackPlugin` | 事件驱动挂起策略（M11a） |
| `pending_tools.py` | `PendingToolsOutput` | 待执行工具（M3 已有，next_tool 信号） |
| `persist.py` | `PersistPlugin` | 持久化 — 执行记录 JSON 文件存储 |
| `track.py` | `TrackPlugin` | 追踪统计 — token 用量/执行耗时/迭代统计 |
| `result_format.py` | `ResultFormatPlugin` | 结果格式化 — 工具结果转 LLM 消息格式 |
| `memory_write.py` | `MemoryWritePlugin` | 记忆写入 — 对话内容写入记忆系统 |
| `__init__.py` | — | 模块入口 |

### 委派策略插件接口详情

#### FireAndForgetPlugin

| 属性/方法 | 类型 | 说明 |
|----------|------|------|
| `name` | `str` | "fire_and_forget" |
| `priority` | `int` | 5 |
| `route_signals` | `list[str]` | []（关注所有信号） |
| `execute(ctx)` | `async → OutputResult` | 空操作 |

#### EventCallbackPlugin

| 属性/方法 | 类型 | 说明 |
|----------|------|------|
| `name` | `str` | "event_callback" |
| `priority` | `int` | 5 |
| `route_signals` | `list[str]` | []（关注所有信号） |
| `__init__(event_bus)` | — | 构造函数 |
| `execute(ctx)` | `async → OutputResult` | 事件回调挂起逻辑 |

### 依赖

- `pipeline.plugin`（IOutputPlugin, OutputResult, PluginContext）
- `pipeline.types`（StateKeys）— EventCallbackPlugin
- `pipeline.event_bus`（EventBus）— EventCallbackPlugin
