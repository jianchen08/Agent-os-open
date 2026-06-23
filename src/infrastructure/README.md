# Infrastructure 模块文档

## 需求

基础设施层，为管道执行提供通用的运行时支持服务：

1. **并发控制**：运行时三层并发控制（LLM Key 级 / 硬件任务级 / 配置级）
2. **资源管理**：管道实例配额与活跃计数
3. **错误策略**：统一的插件错误处理
4. **统计收集**：轻量级运行统计
5. **数据存储**：数据库访问与执行记录持久化
6. **服务注册**：ServiceProvider 全局服务查找
7. **会话管理**：SessionService 会话生命周期
8. **消息队列**：异步消息队列
9. **恢复机制**：Recovery 异常恢复

## 逻辑

### 模块关系

```
ResourceManager (配额管理) → 被管道持有
StatsCollector (统计收集) → 被管道持有
apply_error_policy (错误策略) → 被 PluginChain 调用
ServiceProvider (服务注册) → 全局单例
SessionService (会话管理) → 被 Channel 调用
MessageQueue (消息队列) → 异步通信
```

### 各子模块逻辑

详细逻辑请参阅各子模块独立文档：

- [concurrency.md](concurrency.md) — 并发控制（运行时三层并发机制）
- [resource.md](resource.md) — 资源管理
- [error_policy.md](error_policy.md) — 错误策略
- [stats.md](stats.md) — 统计收集

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `resource.py` | ResourceQuota, ResourceManager | 资源配额管理 |
| `error_policy.py` | apply_error_policy | 错误策略处理函数 |
| `stats.py` | StatsCollector | 统计信息收集器 |
| `db.py` | — | 数据库访问层 |
| `models.py` | — | 数据模型 |
| `execution_record_storage.py` | — | 执行记录存储 |
| `pipeline_checkpoint.py` | PipelineCheckpointManager | 管道检查点管理 |
| `message_queue.py` | MessageQueue | 异步消息队列 |
| `recovery.py` | — | 异常恢复机制 |
| `protocols.py` | MemoryStoreProtocol | 跨层依赖抽象协议（解耦 infrastructure↔channels） |
| `service_provider.py` | ServiceProvider, get_service_provider | 全局服务注册表 |
| `session_service.py` | SessionService | 会话管理服务 |
| `task_worker.py` | TaskWorker | 任务工作器 |

### 文档清单

| 文档 | 说明 |
|------|------|
| `README.md` | 本文档（模块总览） |
| `concurrency.md` | 并发控制文档（运行时三层并发机制） |
| `resource.md` | 资源管理器文档 |
| `error_policy.md` | 错误策略文档 |
| `stats.md` | 统计收集器文档 |

### 依赖

- asyncio（标准库）
- dataclasses, abc, typing, logging（标准库）
- `pipeline.types` — ErrorPolicy, StateKeys
- `pipeline.plugin` — PluginResult
