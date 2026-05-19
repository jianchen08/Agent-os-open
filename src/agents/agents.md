# Agent 模块

## 需求

### 职责

Agent 模块是系统的执行引擎，基于 LangGraph StateGraph 实现单个 Agent 的完整执行生命周期管理。

### 对外接口

- 输入：用户消息 + Agent 配置 + 工具注册表
- 输出：执行结果（AgentResult）或流式事件

### 依赖

- 依赖模块：tools（工具注册和执行）、llm（LLM 客户端）、memory（记忆检索）、orchestration（任务调度）
- 外部依赖：langgraph、langchain-core

## 逻辑

### 流程设计

```
用户输入 → AgentLoop.run()/stream()
    │
    ▼
初始化组件（协调器、检查点、TaskClient）
    │
    ▼
创建初始状态（AgentState）
    │
    ▼
LangGraph StateGraph 执行
    │
    ├─► call_model_node: LLM 决策
    │       │
    │       ▼
    │   should_continue: 条件判断
    │       │
    │       ├─► 继续 → execute_tools_node
    │       │               │
    │       │               ▼
    │       │           工具执行（ToolCoordinator）
    │       │               │
    │       │               └─► 返回 call_model_node
    │       │
    │       └─► 结束 → 输出结果
    │
    ▼
返回 AgentResult 或流式事件
```

### 数据流向

```
用户消息 → AgentState.layered_context_store
    ↓
LLM 决策 → 工具调用请求
    ↓
ToolCoordinator → 工具执行
    ↓
工具结果 → AgentState.layered_context_store
    ↓
循环直到完成 → AgentResult
```

### 数据模型

#### AgentState（LangGraph 状态）

| 字段 | 类型 | 说明 |
|------|------|------|
| iteration | int | 当前迭代次数 |
| pending_tool_calls | list[dict] | 待执行的工具调用 |
| tool_calls | list[dict] | 已执行的工具调用记录 |
| llm_client | Any \| None | LLM 客户端（运行时注入，不序列化） |
| tools | list[Any] | 可用工具列表 |
| tool_executor | Any \| None | 工具执行器（运行时注入，不序列化） |
| requires_approval | bool | 是否需要人工审批 |
| context | dict | 执行上下文 |
| final_output | str \| None | 最终输出 |
| error | str \| None | 错误信息 |
| should_stop | bool | 是否停止 |
| enable_thinking | bool | 是否启用思考模式 |
| output_schema | dict \| None | 结构化输出 Schema |
| agent_config | Any \| None | Agent 配置（运行时注入，不序列化） |
| layered_context_store | Any \| None | 分层上下文存储（运行时注入，不序列化），消息存储在此 |
| thinking_callback | Callable \| None | 思考内容回调函数（运行时注入，不序列化） |
| evaluate_reminder_count | int | 评估提醒次数（防止无限循环） |
| state_version | int | 状态版本控制 |
| last_updated | str | 状态变更时间戳 |
| last_updated_by | str \| None | 状态变更来源 |
| last_updated_reason | str \| None | 状态变更原因 |
| consistency_hash | str \| None | 状态一致性标记 |

#### AgentConfig（Agent 配置）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | str | Agent 名称 |
| model_name | str | LLM 模型名称 |
| system_prompt | str | 系统提示词 |
| tool_ids | list[str] | 可用工具 ID 列表 |
| timeout_seconds | int | 超时时间（秒） |
| agent_type | AgentType | Agent 类型 |
| model_params | dict | 模型参数 |
| output_schema | dict | 结构化输出 Schema |

#### AgentResult（执行结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| success | bool | 是否成功 |
| output | str | 输出内容 |
| error | str | 错误信息 |
| error_code | str | 错误代码 |
| iterations | int | 迭代次数 |
| tool_calls | list[ToolCallRecord] | 工具调用记录 |

### API 设计

#### 模块 API

| 方法 | 职责 |
|------|------|
| `AgentLoop.run(user_input: str) -> AgentResult` | 同步执行 Agent |
| `AgentLoop.stream(user_input: str) -> AsyncIterator` | 流式执行 Agent |
| `AgentLoop.resume_from_interrupt(response: dict) -> AgentResult` | 从中断恢复执行 |
| `AgentLoop.stop()` | 停止执行 |
| `AgentLoop.cleanup()` | 清理资源 |

### 配置设计

#### 模块配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| enable_learning | 是否启用经验学习 | True |
| enable_checkpointing | 是否启用检查点 | True |
| enable_approval | 是否启用人工审批 | False |
| enable_monitoring | 是否启用监控 | True |

### 错误处理

#### 模块错误码

| 错误码 | 说明 |
|--------|------|
| EXECUTION_ERROR | 执行错误 |
| GRAPH_ERROR | 图执行错误 |
| RESUME_ERROR | 恢复错误 |

#### 异常类型

| 异常 | 说明 |
|------|------|
| AgentException | Agent 基础异常 |
| AgentNotFoundError | Agent 未找到 |
| AgentAlreadyExistsError | Agent 已存在 |
| AgentExecutionError | Agent 执行错误 |
| SubAgentNestingError | SubAgent 嵌套错误 |

### 安全设计

#### 模块安全

- 层级控制：L1/L2/L3 层级限制，防止无限嵌套
- 超时保护：执行超时自动终止
- 资源隔离：每个 AgentLoop 独立的状态和资源

## 结构

### 组件清单（文件夹 - 抽象说明）

| 组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| coordinators/ | 协调器模块，管理 LLM、工具、记忆、监控协调 | 输入：Agent 配置 → 输出：协调器实例 | - |
| nodes/ | LangGraph 节点函数 | 输入：AgentState → 输出：AgentState | - |
| lifecycle/ | 生命周期管理 | 输入：Agent 实例 → 输出：生命周期状态 | - |
| builtin/ | 内置原子 Agent 定义（YAML） | 输入：Agent ID → 输出：Agent 配置 | - |
| formatters/ | 消息格式化 | 输入：原始消息 → 输出：格式化消息 | - |
| utils/ | 工具函数 | 输入：各种数据 → 输出：处理结果 | - |

### 文件清单（代码文件 - 具体接口）

#### state.py
职责：Agent 状态定义和管理
暴露接口：
- `AgentState`：LangGraph 状态 TypedDict
- `create_initial_state(...) -> AgentState`：创建初始状态
- `update_state(state: AgentState, updates: dict, ...) -> AgentState`：更新状态
- `validate_state_consistency(state: AgentState) -> bool`：验证状态一致性

#### types.py
职责：Agent 类型定义
暴露接口：
- `AgentConfig`：Agent 配置类
- `AgentResult`：执行结果类
- `AgentType`：Agent 类型枚举
- `AgentLifecycleState`：生命周期状态枚举
- `ToolCallRecord`：工具调用记录类

#### graph.py
职责：LangGraph StateGraph 构建
暴露接口：
- `AgentGraphBuilder`：图构建器类
- `create_agent_graph(...) -> StateGraph`：创建 Agent 图

#### builder.py
职责：Agent 工厂和构建器
暴露接口：
- `AgentLoopBuilder`：构建器类
- `create_agent_loop(...) -> AgentLoop`：创建 AgentLoop
- `create_agent_loop_with_defaults(...) -> AgentLoop`：使用默认配置创建
- `create_agent_loop_minimal(...) -> AgentLoop`：最小配置创建

#### context.py
职责：Agent 上下文管理
暴露接口：
- `AgentContext`：上下文类，封装所有依赖

#### registry.py
职责：Agent 注册表
暴露接口：
- `AgentLoopRegistry`：注册表类
- `get_agent_loop_registry() -> AgentLoopRegistry`：获取全局注册表

#### stuck_detector.py
职责：卡死检测
暴露接口：
- `StuckDetector`：卡死检测器类
- `StuckResult`：检测结果类
- `StuckType`：卡死类型枚举
- `RecoveryAction`：恢复动作枚举

### 测试策略

#### 模块测试

- 单元测试：核心函数和类方法
- 集成测试：AgentLoop 完整执行流程
- 测试覆盖：核心逻辑 ≥90%

## 实现

→ 见各组件代码文件
