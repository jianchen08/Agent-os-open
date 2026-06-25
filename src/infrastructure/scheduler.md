# scheduler 模块文档

## 需求

管道调度器，按优先级异步调度任务。需要：

1. 优先级队列管理待调度项，数值越小优先级越高
2. 策略可插拔，默认按优先级排序、同优先级 FIFO
3. 异步接口（submit/pick_next），与 PipelineEngine 协作
4. 轻量级，不依赖外部存储

## 逻辑

### 调度流程

```
submit(item, priority) → PriorityQueue.put(PriorityItem)
pick_next()           → PriorityQueue.get_nowait() → item | None
```

- PriorityItem 使用 `dataclass(order=True)`，priority 参与排序，item 不参与
- SchedulerStrategy(ABC) 定义策略接口，DefaultSchedulerStrategy 实现默认策略
- 当前简化版 Scheduler 的 pick_next 直接用 PriorityQueue，策略预留扩展点

### 精简原则

| 旧代码 | 新代码 | 理由 |
|--------|--------|------|
| `priority^1.5` 公式 | 直接用 priority 数值 | 过度设计，整数优先级足够 |
| 事件驱动 + 0.5s 轮询 | asyncio.PriorityQueue | 简洁可靠 |
| 公平调度算法 | 简单优先级排序 | 当前规模不需要 |

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `scheduler.py` | `PriorityItem` | 优先级队列数据项（dataclass order=True） |
| `scheduler.py` | `SchedulerStrategy` | 调度策略抽象基类 |
| `scheduler.py` | `DefaultSchedulerStrategy` | 默认策略：按优先级排序 + FIFO |
| `scheduler.py` | `Scheduler` | 调度器主类，asyncio.PriorityQueue |

### 类继承关系

```
SchedulerStrategy (ABC)
└── DefaultSchedulerStrategy
```

### 依赖

- asyncio（标准库）
- abc, dataclasses, typing（标准库）
- 无内部依赖
