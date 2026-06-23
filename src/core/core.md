# Core 模块文档

## 需求

核心基础模块，提供 Agent OS 各子系统共用的基础抽象：

1. **统一状态管理**：ExecutionStatus 枚举 + 通用状态机 + 状态转换事件
2. **统一结果类型**：Agent/Tool/Evaluation 执行结果基类
3. **注册表基类**：SingletonMixin、BaseRegistry、SimpleRegistry、CachedFactory
4. **Runnable 抽象**：统一工具/Agent/Workflow 的执行接口
5. **事件总线**：进程内事件发布/订阅
6. **错误处理**：统一异常类型、错误报告器、常量定义

## 逻辑

### 状态管理

```
ExecutionStatus（10 种状态）
  ├── PENDING    — 待执行
  ├── SCHEDULED  — 已调度
  ├── RUNNING    — 执行中
  ├── EVALUATING — 评估中
  ├── SUSPENDED  — 暂停
  ├── BLOCKED    — 阻塞
  ├── COMPLETED  — 已完成（终态）
  ├── FAILED     — 失败（终态）
  ├── CANCELLED  — 已取消（终态）
  └── TIMEOUT    — 超时（终态）

状态属性判断：
  is_terminal → COMPLETED / FAILED / CANCELLED / TIMEOUT
  is_active   → RUNNING / EVALUATING
  is_waiting  → PENDING / SCHEDULED / SUSPENDED / BLOCKED
  is_success  → COMPLETED
  is_failure  → FAILED / CANCELLED / TIMEOUT
```

### 状态机

```
StateMachine[T]
  → 泛型状态机，支持任意状态枚举类型
  → 配置：StateMachineConfig（转换规则 + 生命周期钩子）
  → 转换验证：can_transition(from, to)
  → 生命周期钩子：on_enter / on_exit / on_transition
  → 事件通知：StateEvent
```

### 注册表层次

```
SingletonMixin       — 单例混入（get_instance / reset_instance / has_instance）
BaseRegistry[K,V]    — 注册表基类（register / unregister / get / has / list_all）
  ├── SimpleRegistry     — 简单字典注册表
  ├── CachedFactory      — 带缓存的工厂注册表
  ├── SingletonRegistry  — 单例注册表
  └── SingletonCachedFactory — 单例 + 缓存工厂
```

### Runnable 抽象

```
RunnableType 枚举：TOOL / AGENT / WORKFLOW
RunnableStatus 枚举：ACTIVE / INACTIVE / ERROR

Runnable (ABC)
  ├── ToolRunnable    — 工具执行封装（input_schema + handler）
  ├── AgentRunnable   — Agent 执行封装（agent_config + agent_loop）
  ├── WorkflowRunnable — 工作流执行封装（workflow_id + executor）
  └── CompositeRunnable — 组合执行（sequence / parallel）

格式输出：
  to_mcp_format()   → MCP 协议格式
  to_llm_format()   → LLM function calling JSON 格式
  to_llm_yaml_format() → LLM YAML 格式
```

### 结果类型

```
BaseResult (ABC)
  ├── AgentResult    — Agent 执行结果
  ├── ToolResult     — 工具执行结果
  ├── ToolCallResult — 工具调用结果
  └── EvaluationResult — 评估结果
```

### 事件总线

```
EventBus
  → 进程内发布/订阅
  → 事件类型：StateEvent
  → 支持同步和异步回调
```

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `__init__.py` | — | 模块入口 |
| `constants.py` | — | 常量定义 |
| `errors.py` | — | 错误类型定义（含 ErrorSeverity、ErrorCode、StandardError） |
| `exceptions.py` | — | 统一异常类型 |
| `registry_base.py` | SingletonMixin, BaseRegistry, SimpleRegistry, CachedFactory, SingletonRegistry, SingletonCachedFactory | 注册表和工厂基类 |
| `runnable.py` | RunnableType, RunnableStatus, RunnableMetadata, ToolRunnable, AgentRunnable, WorkflowRunnable, CompositeRunnable | 统一 Runnable 抽象 |
| `interfaces/__init__.py` | — | 接口子模块入口 |
| `interfaces/event_bus.py` | EventBus | 事件总线接口 |
| `results/__init__.py` | — | 结果子模块入口 |
| `results/base.py` | BaseResult | 结果基类 |
| `results/agent.py` | AgentResult | Agent 执行结果 |
| `results/evaluation.py` | EvaluationResult | 评估结果 |
| `results/tool.py` | ToolResult | 工具执行结果 |
| `results/tool_call.py` | ToolCallResult | 工具调用结果 |
| `states/__init__.py` | — | 状态子模块入口 |
| `states/base.py` | StateTransition | 状态转换基础类型 |
| `states/events.py` | StateEvent | 状态事件类型 |
| `states/execution.py` | ExecutionStatus | 统一执行状态枚举（10 种） |
| `states/lifecycle.py` | — | 生命周期管理 |
| `states/machine.py` | StateMachineConfig, StateMachine | 通用状态机 |

| `logging/__init__.py` | get_logger, setup_logging, ContextFilter | 统一日志入口 |
| `logging/config.py` | LoggingConfig | 日志配置数据类 |
| `logging/formatters.py` | StructuredFormatter, JsonFormatter | 结构化/JSON 格式化器 |
| `logging/context.py` | LogContext | 请求ID/任务ID上下文管理 |
| `logging/filters.py` | ContextFilter | LogRecord 上下文注入过滤器 |

### 子目录

| 目录 | 说明 |
|------|------|
| `interfaces/` | 接口定义（事件总线等） |
| `logging/` | 统一日志系统 |
| `results/` | 执行结果类型 |
| `states/` | 状态管理与状态机 |

### 依赖

- `pydantic` — 数据模型（Runnable 元数据）
- `langchain_core.runnables` — Runnable 抽象基类
- Python 标准库：enum, dataclasses, abc, logging, threading
