# 编排中心模块

## 需求

### 职责
负责任务调度、资源管理和执行协调的统一入口，支持 Agent、Workflow 和 Tool 三种目标类型的任务调度，提供依赖解析、并发控制和资源分配功能。

### 对外接口
- 输入：任务请求、资源配额
- 输出：任务结果、调度状态、资源使用情况

### 依赖
- 依赖模块：core.exceptions, core.event_bus, db.session_manager
- 外部依赖：sqlalchemy, pydantic

## 逻辑

### 流程设计
```
任务提交 → 依赖解析 → 资源分配 → 调度执行 → 结果返回
              ↓           ↓           ↓
         TaskOrchestrator ResourceManager Scheduler
```

### 数据流向
1. 任务调度：任务请求 → 调度队列 → 执行器 → 结果
2. 资源管理：资源请求 → 配额检查 → 分配 → 释放
3. 并发控制：请求类型 → 信号量获取 → 执行 → 释放
4. 依赖解析：任务依赖 → 验证 → 状态检查 → 调度就绪

### 数据模型
#### Agent 层级
| 层级 | 说明 |
|------|------|
| L1 | 主 Agent，负责整体任务规划 |
| L2 | SubAgent，负责子任务执行 |
| L3 | 执行 Agent，不能再创建子任务 |

#### 任务优先级
| 优先级 | 值 | 说明 |
|--------|-----|------|
| LOW | 1 | 低优先级 |
| NORMAL | 5 | 普通优先级 |
| HIGH | 8 | 高优先级 |
| URGENT | 10 | 紧急优先级 |

#### 任务状态
| 状态 | 说明 |
|------|------|
| PENDING | 待处理 |
| SCHEDULED | 已调度 |
| RUNNING | 运行中 |
| COMPLETED | 已完成 |
| FAILED | 失败 |
| CANCELLED | 已取消 |

#### 目标类型
| 类型 | 说明 |
|------|------|
| AGENT | Agent 任务 |
| WORKFLOW | 工作流任务 |
| TOOL | 工具任务 |

#### 资源配额
| 字段 | 类型 | 说明 |
|------|------|------|
| max_l1_agents | int | L1 Agent 最大并发数 |
| max_l2_agents | int | L2 Agent 最大并发数 |
| max_l3_agents | int | L3 Agent 最大并发数 |
| max_total_agents | int | 总 Agent 最大并发数 |
| max_cpu_percent | float | CPU 使用上限百分比 |
| max_memory_percent | float | 内存使用上限百分比 |
| priority_weights | dict | 优先级权重配置 |

#### 任务请求
| 字段 | 类型 | 说明 |
|------|------|------|
| task_id | str | 任务唯一标识 |
| agent_level | AgentLevel | Agent 层级 |
| priority | TaskPriority | 任务优先级 |
| target_type | TargetType | 目标类型 |
| parent_task_id | str | None | 父任务 ID |
| session_id | str | None | 会话 ID |
| description | str | 任务描述 |
| prompt | str | 执行提示 |
| config | dict | 任务配置 |
| status | ExecutionStatus | 任务状态 |
| result | Any | None | 执行结果 |
| error | str | None | 错误信息 |

#### 资源分配
| 字段 | 类型 | 说明 |
|------|------|------|
| task_id | str | 任务 ID |
| agent_level | AgentLevel | Agent 层级 |
| allocated_at | float | 分配时间戳 |
| expected_release_at | float | 预计释放时间戳 |

#### 依赖解析结果
| 字段 | 类型 | 说明 |
|------|------|------|
| task_id | str | 任务 ID |
| is_resolved | bool | 依赖是否已解析 |
| pending_dependencies | list[str] | 未完成的依赖任务 ID |
| completed_dependencies | list[str] | 已完成的依赖任务 ID |
| failed_dependencies | list[str] | 失败的依赖任务 ID |
| is_executable | bool | 任务是否可执行 |
| block_reason | str | None | 阻塞原因 |

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `Scheduler` | 统一任务调度器类 |
| `GlobalAgentScheduler` | 全局调度器别名（向后兼容） |
| `get_global_scheduler() -> Scheduler` | 获取全局调度器实例 |
| `start_global_scheduler() -> None` | 启动全局调度器 |
| `stop_global_scheduler() -> None` | 停止全局调度器 |
| `TaskOrchestrator` | 任务编排器类 |
| `get_task_orchestrator(event_bus: EventBusBase) -> TaskOrchestrator` | 获取任务编排器实例 |
| `stop_task_orchestrator() -> None` | 停止任务编排器 |
| `ExecutorFactory` | 执行器工厂类 |
| `AgentExecutor` | Agent 执行器类 |
| `get_global_executor() -> AgentExecutor` | 获取全局执行器实例 |
| `ResourceManager` | 资源管理器类 |
| `ConcurrencyManager` | 并发管理器类 |

#### Scheduler API
| 接口 | 职责 |
|------|------|
| `Scheduler.__init__(quota: ResourceQuota | None) -> None` | 初始化调度器 |
| `Scheduler.start() -> None` | 启动调度器 |
| `Scheduler.stop() -> None` | 停止调度器 |
| `Scheduler.submit_task(agent_level: AgentLevel, description: str, prompt: str, priority: TaskPriority, target_type: TargetType, parent_task_id: str | None, parent_record_id: str | None, session_id: str | None, config: dict | None) -> str` | 提交任务 |
| `Scheduler.wait_for_completion(task_id: str, timeout: float | None) -> TaskRequest` | 等待任务完成 |
| `Scheduler.get_task_status(task_id: str) -> TaskRequest | None` | 获取任务状态 |
| `Scheduler.cancel_task(task_id: str) -> bool` | 取消任务 |
| `Scheduler.get_next_task() -> TaskRequest | None` | 获取下一个待执行任务 |
| `Scheduler.report_completion(task_id: str, result: dict) -> None` | 报告任务完成 |
| `Scheduler.get_statistics() -> dict` | 获取统计信息 |
| `Scheduler.register_completion_callback(callback: Callable) -> None` | 注册完成回调 |

#### TaskOrchestrator API
| 接口 | 职责 |
|------|------|
| `TaskOrchestrator.__init__(event_bus: EventBusBase)` | 初始化编排器 |
| `TaskOrchestrator.start() -> None` | 启动编排器 |
| `TaskOrchestrator.stop() -> None` | 停止编排器 |
| `TaskOrchestrator.get_pending_tasks() -> dict[str, dict]` | 获取等待中的任务 |
| `TaskOrchestrator.get_statistics() -> dict` | 获取统计信息 |

#### ResourceManager API
| 接口 | 职责 |
|------|------|
| `ResourceManager.__init__(quota: ResourceQuota | None) -> None` | 初始化管理器 |
| `ResourceManager.can_allocate(level: AgentLevel) -> bool` | 检查是否可以分配资源 |
| `ResourceManager.allocate(task_id: str, level: AgentLevel, expected_duration: float) -> ResourceAllocation` | 分配资源 |
| `ResourceManager.release(task_id: str) -> None` | 释放资源 |
| `ResourceManager.get_allocation(task_id: str) -> ResourceAllocation | None` | 获取资源分配记录 |
| `ResourceManager.get_usage() -> dict` | 获取资源使用情况 |
| `ResourceManager.wait_for_resource(level: AgentLevel, timeout: float | None, check_interval: float) -> bool` | 等待资源可用 |

#### ConcurrencyManager API
| 接口 | 职责 |
|------|------|
| `ConcurrencyManager.__init__() -> None` | 初始化并发管理器 |
| `ConcurrencyManager.get_llm_semaphore(provider: str, model: str | None, request_type: str | None) -> asyncio.Semaphore` | 获取 LLM 信号量 |
| `ConcurrencyManager.acquire_llm(provider: str, model: str | None, request_type: str | None, timeout: float | None) -> bool` | 获取 LLM 许可 |
| `ConcurrencyManager.release_llm(provider: str, model: str | None, request_type: str | None) -> None` | 释放 LLM 许可 |
| `ConcurrencyManager.acquire_agent(level: AgentLevel, timeout: float | None) -> bool` | 获取 Agent 许可 |
| `ConcurrencyManager.release_agent(level: AgentLevel) -> None` | 释放 Agent 许可 |
| `ConcurrencyManager.acquire_workflow(timeout: float | None) -> bool` | 获取工作流许可 |
| `ConcurrencyManager.release_workflow() -> None` | 释放工作流许可 |
| `ConcurrencyManager.get_stats() -> dict` | 获取统计信息 |
| `ConcurrencyManager.get_config() -> dict` | 获取配置信息 |

#### ExecutorFactory API
| 接口 | 职责 |
|------|------|
| `ExecutorFactory.create_executor(target_type: str) -> Any` | 创建执行器 |
| `ExecutorFactory.clear_cache() -> None` | 清除执行器缓存 |
| `ExecutorFactory.get_cached_executors() -> dict` | 获取已缓存的执行器 |

#### TaskClient API
| 接口 | 职责 |
|------|------|
| `TaskClient.__init__(current_agent_level: AgentLevel, session_id: str | None, parent_record_id: str | None)` | 初始化客户端 |
| `TaskClient.can_create_subtask() -> bool` | 检查是否可以创建子任务 |
| `TaskClient.submit_agent_task(description: str, prompt: str, priority: TaskPriority, target_id: str | None, agent_config: dict | None, is_subagent_context: bool, timeout: float | None) -> str` | 提交 Agent 任务 |
| `TaskClient.submit_workflow_task(description: str, workflow: Any, inputs: dict | None, priority: TaskPriority, is_subagent_context: bool, timeout: float | None) -> str` | 提交 Workflow 任务 |
| `TaskClient.submit_task_async(description: str, prompt: str, priority: TaskPriority, target_type: TargetType, target_id: str | None, config: dict | None) -> str` | 异步提交任务 |
| `TaskClient.get_task_status(task_id: str) -> TaskRequest | None` | 获取任务状态 |
| `TaskClient.cancel_task(task_id: str) -> bool` | 取消任务 |

### 配置设计
#### 资源配额默认值
| 配置项 | 默认值 |
|--------|--------|
| max_l1_agents | 2 |
| max_l2_agents | 10 |
| max_l3_agents | 50 |
| max_total_agents | 60 |
| max_cpu_percent | 80.0 |
| max_memory_percent | 80.0 |

#### 并发控制默认值
| 配置项 | 默认值 |
|--------|--------|
| provider_limits.zhipu | 2 |
| provider_limits.openai | 10 |
| provider_limits.anthropic | 5 |
| model_limits.gpt-4 | 3 |
| model_limits.glm-4 | 2 |
| workflow_limit | 20 |
| default_limit | 2 |

### 错误处理
- 任务不存在：抛出 TaskNotFoundError
- 资源不足：抛出 ResourceExhaustedError
- 任务执行失败：抛出 TaskExecutionError
- 子代理嵌套超限：抛出 SubAgentNestingError

### 安全设计
- Agent 层级控制：L3 不能创建子任务
- 资源配额限制：防止资源耗尽
- 并发控制：防止系统过载

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `AgentLevel`：Agent 层级枚举
- `TaskPriority`：任务优先级枚举
- `ExecutionStatus`：执行状态枚举（统一状态定义，来自 `core.states`）
- `TargetType`：目标类型枚举
- `ResourceQuota`：资源配额数据类
- `TaskRequest`：任务请求数据类
- `TaskResult`：任务结果数据类
- `ResourceAllocation`：资源分配数据类
- `OrchestrationError`：编排错误异常
- `TaskNotFoundError`：任务不存在异常
- `ResourceExhaustedError`：资源耗尽异常
- `TaskExecutionError`：任务执行错误异常
- `SubAgentNestingError`：子代理嵌套错误异常
- `Scheduler`：调度器类
- `GlobalAgentScheduler`：全局调度器别名
- `ExecutorFactory`：执行器工厂类
- `AgentExecutor`：Agent 执行器类
- `get_global_executor`：获取全局执行器实例
- `ResourceManager`：资源管理器类
- `ConcurrencyManager`：并发管理器类
- `TaskOrchestrator`：任务编排器类
- `DependencyResolution`：依赖解析结果数据类
- `get_scheduler`：获取调度器实例
- `get_global_scheduler`：获取全局调度器实例
- `start_global_scheduler`：启动全局调度器
- `stop_global_scheduler`：停止全局调度器
- `get_task_orchestrator`：获取任务编排器实例
- `stop_task_orchestrator`：停止任务编排器

#### types.py
职责：编排中心类型定义
暴露接口：
- `AgentLevel`：Agent 层级枚举
- `TaskPriority`：任务优先级枚举
- `ExecutionStatus`：执行状态枚举（统一状态定义，来自 `core.states`）
- `TargetType`：目标类型枚举
- `ResourceQuota`：资源配额数据类
- `TaskRequest`：任务请求数据类
- `TaskResult`：任务结果数据类
- `ResourceAllocation`：资源分配数据类

#### exceptions.py
职责：编排模块异常定义
暴露接口：
- `OrchestrationError`：通用编排错误
- `TaskNotFoundError`：任务不存在错误
- `ResourceExhaustedError`：资源耗尽错误
- `TaskExecutionError`：任务执行错误
- `SubAgentNestingError`：子代理嵌套错误

#### scheduler.py
职责：统一任务调度器
暴露接口：
- `Scheduler`：调度器类
- `GlobalAgentScheduler`：全局调度器别名
- `get_global_scheduler() -> Scheduler`：获取全局调度器实例
- `start_global_scheduler() -> None`：启动全局调度器
- `stop_global_scheduler() -> None`：停止全局调度器

#### task_orchestrator.py
职责：任务编排器
暴露接口：
- `TaskOrchestrator`：任务编排器类
- `DependencyResolution`：依赖解析结果数据类
- `get_task_orchestrator(event_bus: EventBusBase) -> TaskOrchestrator`：获取编排器实例
- `stop_task_orchestrator() -> None`：停止编排器

#### resource_manager.py
职责：资源管理器
暴露接口：
- `ResourceManager`：资源管理器类

#### concurrency_manager.py
职责：统一并发管理器
暴露接口：
- `ConcurrencyManager`：并发管理器类
- `ConcurrencyConfig`：并发控制配置数据类
- `ConcurrencyStats`：并发统计信息数据类
- `RequestType`：LLM 请求类型枚举

#### executor_factory.py
职责：执行器工厂
暴露接口：
- `ExecutorFactory`：执行器工厂类
- `execute_with_factory(task: TaskRequest) -> dict`：使用工厂执行任务

#### agent_executor.py
职责：Agent 任务执行器
暴露接口：
- `AgentExecutor`：Agent 执行器类
- `AgentExecutor.execute_task(task: TaskRequest) -> dict[str, Any]`：执行任务
- `AgentExecutor.cancel_task(task_id: str) -> bool`：取消任务
- `AgentExecutor.get_running_tasks() -> dict[str, str]`：获取运行中的任务
- `get_global_executor() -> AgentExecutor`：获取全局执行器实例

#### task_client.py
职责：任务客户端
暴露接口：
- `TaskClient`：任务客户端类
- `TaskClientFactory`：任务客户端工厂类
- `SubAgentManager`：SubAgent 管理器（向后兼容）
- `SubAgentManagerFactory`：SubAgent 管理器工厂（向后兼容）
- `SubAgentConfig`：SubAgent 配置数据类

### 测试策略
#### 模块测试
- 单元测试：调度逻辑、资源分配、并发控制
- 集成测试：任务执行流程、依赖解析
- Mock 策略：Mock 执行器、数据库会话

## 实现
→ 见代码文件
