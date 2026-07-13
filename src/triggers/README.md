# 触发器系统（triggers）

## 需求

Agent OS 需要支持多种触发机制来驱动任务执行，包括延迟触发、定时触发、事件触发和条件触发。旧文件中已有触发器模块（`旧文件/src/triggers/`），需将其核心模式迁移为新架构的触发器管理器。

触发器用于：
- 在特定事件发生时自动执行动作
- 在满足条件时自动推进任务
- 在指定时间点触发检查或执行

## 逻辑

### 触发器类型

| 类型 | 枚举 | 触发条件 |
|------|------|----------|
| 延迟触发 | DELAY | 从注册时刻起经过 delay_seconds |
| 定时触发 | SCHEDULED | 到达 scheduled_at 时间或匹配 cron 表达式 |
| 事件触发 | EVENT | 监听的事件名称匹配且数据通过过滤条件 |
| 条件触发 | CONDITION | Python 布尔表达式求值为 True |

### 触发器状态

| 状态 | 枚举 | 含义 |
|------|------|------|
| PENDING | 初始状态 | 已创建，未激活 |
| ACTIVE | 活跃 | 可被触发 |
| FIRED | 已触发 | 达到最大触发次数 |
| CANCELLED | 已取消 | 手动取消 |
| EXPIRED | 已过期 | 超出有效期 |

### 事件过滤

事件触发器支持 `event_filter` 字典进行数据过滤：
- 简单匹配：`{"key": value}` → 事件数据中 key 必须等于 value
- 操作符匹配：`{"key": {"op": "gt", "value": 80}}` → 支持 eq/ne/gt/lt/gte/lte/contains

### 条件评估

条件触发器使用 `condition_expression` 字符串，在 `context` 命名空间中 `eval()` 求值。
安全措施：禁止 import/exec/eval/open 和赋值操作。

### max_fires 语义

- `max_fires=1`：默认，触发 1 次后状态变为 FIRED
- `max_fires=0`：无限触发
- `max_fires=N`：触发 N 次后变为 FIRED

## 结构

### 文件清单

| 文件 | 用途 |
|------|------|
| `types.py` | 数据类型定义（TriggerType, TriggerStatus, TriggerConfig） |
| `manager.py` | TriggerManager：注册/评估/查询/取消 |
| `__init__.py` | 公共 API 导出 |
| `README.md` | 本文档 |

### 数据流

```
TriggerConfig → TriggerManager.register() → 注册表
事件数据 → TriggerManager.evaluate_event() → 被触发的 trigger_id 列表
上下文 → TriggerManager.evaluate_condition() → 被触发的 trigger_id 列表
当前时间 → TriggerManager.check_scheduled() → 被触发的 trigger_id 列表
```

### 依赖

- 仅使用 Python 标准库（datetime, logging, re）
- 不依赖其他模块
