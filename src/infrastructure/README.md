# Infrastructure 模块文档

## 需求

基础设施层，为管道执行提供通用的运行时支持服务：

1. **调度器**：按优先级异步调度任务
2. **并发控制**：三级信号量限流（Provider/Model/Agent）
3. **资源管理**：管道实例配额与活跃计数
4. **错误策略**：统一的插件错误处理
5. **统计收集**：轻量级运行统计
6. **数据存储**：数据库访问与执行记录持久化
7. **管道任务**：PipelineTask 调度单元
8. **服务注册**：ServiceProvider 全局服务查找
9. **会话管理**：SessionService 会话生命周期
10. **消息队列**：异步消息队列
11. **恢复机制**：Recovery 异常恢复

## 逻辑

### 模块关系

```
Scheduler (调度) → PipelineTask (调度单元)
ConcurrencyController (并发限流) → 被管道持有
ResourceManager (配额管理) → 被管道持有
StatsCollector (统计收集) → 被管道持有
apply_error_policy (错误策略) → 被 PluginChain 调用
ServiceProvider (服务注册) → 全局单例
SessionService (会话管理) → 被 Channel 调用
MessageQueue (消息队列) → 异步通信
```

### 各子模块逻辑

详细逻辑请参阅各子模块独立文档：

- [scheduler.md](scheduler.md) — 调度器
- [concurrency.md](concurrency.md) — 并发控制
- [resource.md](resource.md) — 资源管理
- [error_policy.md](error_policy.md) — 错误策略
- [stats.md](stats.md) — 统计收集

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `scheduler.py` | PriorityItem, SchedulerStrategy, DefaultSchedulerStrategy, Scheduler | 优先级调度器 |
| `concurrency.py` | ConcurrencyController | 三级并发控制器 |
| `resource.py` | ResourceQuota, ResourceManager | 资源配额管理 |
| `error_policy.py` | apply_error_policy | 错误策略处理函数 |
| `stats.py` | StatsCollector | 统计信息收集器 |
| `db.py` | — | 数据库访问层 |
| `models.py` | — | 数据模型 |
| `execution_record_storage.py` | — | 执行记录存储 |
| `pipeline_task.py` | PipelineTask | 管道调度任务单元 |
| `pipeline_checkpoint.py` | PipelineCheckpointManager | 管道检查点管理 |
| `message_queue.py` | MessageQueue | 异步消息队列 |
| `recovery.py` | — | 异常恢复机制 |
| `service_provider.py` | ServiceProvider, get_service_provider | 全局服务注册表 |
| `session_service.py` | SessionService | 会话管理服务 |
| `task_worker.py` | TaskWorker | 任务工作器 |

### 文档清单

| 文档 | 说明 |
|------|------|
| `README.md` | 本文档（模块总览） |
| `scheduler.md` | 调度器文档 |
| `concurrency.md` | 并发控制器文档 |
| `resource.md` | 资源管理器文档 |
| `error_policy.md` | 错误策略文档 |
| `stats.md` | 统计收集器文档 |

### 依赖

- asyncio（标准库）
- dataclasses, abc, typing, logging（标准库）
- `pipeline.types` — ErrorPolicy, StateKeys
- `pipeline.plugin` — PluginResult
