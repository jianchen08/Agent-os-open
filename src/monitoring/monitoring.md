# 监控模块

## 需求

### 职责
提供系统性能监控、用量监控、执行监控和任务进度管理功能，包括性能瓶颈检测、告警机制、执行状态跟踪和任务进度管理。

### 对外接口
- 输入：系统指标、用量记录、任务状态
- 输出：性能报告、告警信息、执行统计、进度信息

### 依赖
- 依赖模块：无内部依赖
- 外部依赖：psutil, pydantic, sqlalchemy

## 逻辑

### 流程设计
```
监控启动 → 收集指标 → 检测瓶颈 → 触发告警 → 记录日志
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  系统      LLM       任务
  指标      指标      指标
```

### 数据流向
1. 性能监控：系统资源 → 指标收集 → 瓶颈检测 → 告警
2. 用量监控：API 调用 → 用量记录 → 配额检查 → 告警
3. 执行监控：任务状态 → 状态查询 → 统计报告
4. 任务进度：任务创建 → 状态更新 → 进度计算 → 持久化

### 数据模型
#### 性能指标
| 字段 | 类型 | 说明 |
|------|------|------|
| name | str | 指标名称 |
| value | float | 指标值 |
| unit | str | 指标单位 |
| timestamp | float | 时间戳 |
| tags | dict | 标签 |

#### 系统指标
| 字段 | 类型 | 说明 |
|------|------|------|
| cpu_usage | float | CPU使用率 |
| memory_usage | float | 内存使用率 |
| disk_usage | float | 磁盘使用率 |
| network_sent | float | 网络发送速率 |
| network_recv | float | 网络接收速率 |
| timestamp | float | 时间戳 |

#### 数据库指标
| 字段 | 类型 | 说明 |
|------|------|------|
| active_connections | int | 活跃连接数 |
| connection_pool_size | int | 连接池大小 |
| connection_wait_time | float | 连接等待时间 |
| query_execution_time | float | 查询执行时间 |
| timestamp | float | 时间戳 |

#### LLM 指标
| 字段 | 类型 | 说明 |
|------|------|------|
| active_requests | int | 活跃请求数 |
| request_rate | float | 请求速率 |
| average_response_time | float | 平均响应时间 |
| error_rate | float | 错误率 |
| timestamp | float | 时间戳 |

#### 工具执行指标
| 字段 | 类型 | 说明 |
|------|------|------|
| execution_count | int | 执行次数 |
| average_execution_time | float | 平均执行时间 |
| cache_hit_rate | float | 缓存命中率 |
| error_count | int | 错误次数 |
| timestamp | float | 时间戳 |

#### 任务执行指标
| 字段 | 类型 | 说明 |
|------|------|------|
| pending_tasks | int | 待处理任务数 |
| running_tasks | int | 运行中任务数 |
| completed_tasks | int | 已完成任务数 |
| average_task_time | float | 平均任务执行时间 |
| timestamp | float | 时间戳 |

#### 性能告警
| 字段 | 类型 | 说明 |
|------|------|------|
| level | str | 告警级别 |
| message | str | 告警消息 |
| metrics | dict | 相关指标 |
| timestamp | float | 时间戳 |

#### 用量记录
| 字段 | 类型 | 说明 |
|------|------|------|
| timestamp | datetime | 时间戳 |
| prompt_tokens | int | 提示词 token 数 |
| completion_tokens | int | 补全 token 数 |
| total_tokens | int | 总 token 数 |
| model | str | 模型名称 |
| request_id | str | None | 请求 ID |

#### 用量统计
| 字段 | 类型 | 说明 |
|------|------|------|
| today_tokens | int | 今日 token 数 |
| today_requests | int | 今日请求数 |
| month_tokens | int | 本月 token 数 |
| month_requests | int | 本月请求数 |
| total_tokens | int | 总 token 数 |
| total_requests | int | 总请求数 |
| daily_token_usage_percent | float | 每日 token 使用率 |
| monthly_token_usage_percent | float | 每月 token 使用率 |

#### 任务状态
| 状态 | 说明 |
|------|------|
| PENDING | 待执行 |
| IN_PROGRESS | 执行中 |
| COMPLETED | 已完成 |
| FAILED | 失败 |
| CANCELLED | 已取消 |
| PAUSED | 已暂停 |

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `PerformanceMonitor` | 性能监控器类 |
| `get_performance_monitor() -> PerformanceMonitor` | 获取全局性能监控器实例 |
| `start_performance_monitor() -> None` | 启动性能监控器 |
| `stop_performance_monitor() -> None` | 停止性能监控器 |

#### PerformanceMonitor API
| 接口 | 职责 |
|------|------|
| `PerformanceMonitor.__init__(alert_callback: Callable | None)` | 初始化监控器 |
| `PerformanceMonitor.start() -> None` | 启动监控 |
| `PerformanceMonitor.stop() -> None` | 停止监控 |
| `PerformanceMonitor.get_system_metrics() -> SystemMetrics` | 获取系统指标 |
| `PerformanceMonitor.get_database_metrics() -> DatabaseMetrics` | 获取数据库指标 |
| `PerformanceMonitor.get_llm_metrics() -> LLMMetrics` | 获取 LLM 指标 |
| `PerformanceMonitor.get_tool_metrics() -> ToolMetrics` | 获取工具执行指标 |
| `PerformanceMonitor.get_task_metrics() -> TaskMetrics` | 获取任务执行指标 |
| `PerformanceMonitor.detect_bottlenecks() -> None` | 检测性能瓶颈 |
| `PerformanceMonitor.get_current_metrics() -> dict` | 获取当前指标 |
| `PerformanceMonitor.get_health_status() -> dict` | 获取健康状态 |
| `PerformanceMonitor.record_database_connection(connection_time: float) -> None` | 记录数据库连接 |
| `PerformanceMonitor.record_query_execution(execution_time: float) -> None` | 记录查询执行 |
| `PerformanceMonitor.record_llm_request(response_time: float, error: bool) -> None` | 记录 LLM 请求 |
| `PerformanceMonitor.record_tool_execution(execution_time: float, cache_hit: bool, error: bool) -> None` | 记录工具执行 |
| `PerformanceMonitor.update_task_status(pending: int, running: int, completed: int, task_time: float) -> None` | 更新任务状态 |

#### UsageMonitor API
| 接口 | 职责 |
|------|------|
| `UsageMonitor.__init__(config: QuotaConfig, alert_callback: Callable | None)` | 初始化监控器 |
| `UsageMonitor.record_usage(usage: TokenUsage, model: str, request_id: str | None) -> UsageAlert | None` | 记录 API 使用 |
| `UsageMonitor.get_statistics() -> UsageStatistics` | 获取当前统计 |
| `UsageMonitor.get_recent_records(limit: int) -> list[UsageRecord]` | 获取最近用量记录 |
| `UsageMonitor.export_usage_report() -> dict` | 导出用量报告 |
| `UsageMonitor.reset_statistics() -> None` | 重置统计 |

#### ExecutionMonitor API
| 接口 | 职责 |
|------|------|
| `ExecutionMonitor.__init__(session: AsyncSession)` | 初始化监控器 |
| `ExecutionMonitor.get_task_execution_status(task_id: str) -> dict` | 获取任务执行状态 |
| `ExecutionMonitor.get_execution_statistics(user_id: str | None, time_range: int | None) -> dict` | 获取执行统计 |
| `ExecutionMonitor.get_active_executions() -> list[dict]` | 获取活跃执行任务 |
| `ExecutionMonitor.check_execution_health() -> dict` | 检查执行健康状态 |

#### ExecutionLogger API
| 接口 | 职责 |
|------|------|
| `ExecutionLogger.__init__(session: AsyncSession)` | 初始化日志记录器 |
| `ExecutionLogger.log_event(event_type: ExecutionEventType, project_id: str | None, task_id: str | None, user_id: str | None, message: str, details: dict | None, level: str) -> None` | 记录执行事件 |
| `ExecutionLogger.log_project_created(project_id: str, user_id: str, goal: str, auto_execute: bool) -> None` | 记录项目创建 |
| `ExecutionLogger.log_task_started(task_id: str, project_id: str, user_id: str, auto_triggered: bool) -> None` | 记录任务开始 |
| `ExecutionLogger.log_task_completed(task_id: str, project_id: str, user_id: str, duration: float, retry_count: int) -> None` | 记录任务完成 |
| `ExecutionLogger.log_task_failed(task_id: str, project_id: str, user_id: str, error: str, retry_count: int, max_retries: int) -> None` | 记录任务失败 |

#### TaskProgressManager API
| 接口 | 职责 |
|------|------|
| `TaskProgressManager.__init__(session_id: str, user_id: str | None, auto_save: bool, save_interval: int)` | 初始化管理器 |
| `TaskProgressManager.create_task(title: str, description: str | None, subtasks: list[dict] | None) -> TaskProgress` | 创建任务 |
| `TaskProgressManager.update_subtask(task_id: str, subtask_id: str, status: ExecutionStatus, progress_percent: float | None, error_message: str | None, metadata: dict | None) -> None` | 更新子任务 |
| `TaskProgressManager.update_task_status(task_id: str, status: ExecutionStatus, error_message: str | None) -> None` | 更新任务状态 |
| `TaskProgressManager.save_checkpoint(task_id: str, checkpoint_data: dict) -> None` | 保存检查点 |
| `TaskProgressManager.get_task(task_id: str) -> TaskProgress | None` | 获取任务 |
| `TaskProgressManager.get_current_task() -> TaskProgress | None` | 获取当前任务 |
| `TaskProgressManager.list_tasks() -> list[TaskProgress]` | 列出所有任务 |
| `TaskProgressManager.resume_task(task_id: str) -> TaskProgress | None` | 从数据库恢复任务 |
| `TaskProgressManager.cleanup() -> None` | 清理资源 |

### 配置设计
#### 性能监控配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| max_history_size | 最大历史记录数 | 1000 |
| monitor_interval | 监控间隔（秒） | 5 |

#### 用量配额配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| daily_token_limit | 每日 token 限制 | None |
| daily_request_limit | 每日请求次数限制 | None |
| monthly_token_limit | 每月 token 限制 | None |
| warning_threshold | 警告阈值 | 0.8 |
| critical_threshold | 严重阈值 | 0.9 |
| auto_save_at_warning | 达到警告阈值自动保存 | True |
| auto_pause_at_critical | 达到严重阈值自动暂停 | True |
| auto_stop_at_exhausted | 达到配额自动停止 | True |

### 错误处理
- 性能监控异常：记录日志，继续运行
- 用量记录失败：记录日志，不影响正常流程
- 数据库操作失败：记录日志，返回空结果

### 安全设计
- 敏感数据不记录到日志
- 用量统计支持用户隔离

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `PerformanceMonitor`：性能监控器类
- `get_performance_monitor`：获取性能监控器实例
- `start_performance_monitor`：启动性能监控器
- `stop_performance_monitor`：停止性能监控器
- `PerformanceMetric`：性能指标模型
- `SystemMetrics`：系统指标模型
- `DatabaseMetrics`：数据库指标模型
- `LLMMetrics`：LLM 指标模型
- `ToolMetrics`：工具执行指标模型
- `TaskMetrics`：任务执行指标模型
- `PerformanceAlert`：性能告警模型

#### performance_monitor.py
职责：系统性能监控和瓶颈检测
暴露接口：
- `PerformanceMonitor`：性能监控器类
- `PerformanceMetric`：性能指标模型
- `SystemMetrics`：系统指标模型
- `DatabaseMetrics`：数据库指标模型
- `LLMMetrics`：LLM 指标模型
- `ToolMetrics`：工具执行指标模型
- `TaskMetrics`：任务执行指标模型
- `PerformanceAlert`：性能告警模型
- `get_performance_monitor() -> PerformanceMonitor`：获取全局实例
- `start_performance_monitor() -> None`：启动监控
- `stop_performance_monitor() -> None`：停止监控

#### usage_monitor.py
职责：用量监控与告警
暴露接口：
- `UsageMonitor`：用量监控器类
- `QuotaConfig`：配额配置模型
- `UsageRecord`：用量记录模型
- `UsageStatistics`：用量统计模型
- `UsageAlert`：用量告警数据类
- `AlertLevel`：告警级别枚举
- `AlertAction`：告警动作枚举

#### execution_monitor.py
职责：执行状态监控
暴露接口：
- `ExecutionMonitor`：执行状态监控器类

#### execution_logger.py
职责：执行日志记录
暴露接口：
- `ExecutionLogger`：执行日志记录器类
- `ExecutionEventType`：执行事件类型枚举

#### task_progress.py
职责：任务进度管理
暴露接口：
- `TaskProgressManager`：任务进度管理器类
- `TaskProgress`：任务进度模型
- `SubTask`：子任务模型
- `ExecutionStatus`：执行状态枚举（统一状态定义，来自 `core.states`）

### 测试策略
#### 模块测试
- 单元测试：指标收集、告警触发、用量统计
- 集成测试：监控循环、数据库持久化
- Mock 策略：Mock 系统资源 API

## 实现
→ 见代码文件
