# stats 模块文档

## 需求

轻量级统计信息收集器，合并调度器和并发控制器的运行统计。需要：

1. 支持记录任意键值对（`record`）
2. 支持数值递增（`increment`）
3. 支持查询（`get`）
4. 支持快照（`snapshot`），返回浅拷贝

纯内存，不做持久化和分类聚合。

## 逻辑

### 统计收集流程

```
StatsCollector
  record(key, value)    → 覆盖写入
  increment(key, delta) → 递增（不存在从 0 开始）
  get(key, default)     → 读取
  snapshot()            → 浅拷贝 dict
```

- 使用 `dataclass` 定义，`_stats` 字段通过 `field(default_factory=dict)` 初始化
- `increment` 对不存在的 key 从 0 开始递增
- `snapshot` 返回 `dict(self._stats)` 浅拷贝，外部修改不影响内部状态

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `stats.py` | `StatsCollector` | 统计收集器（dataclass） |

### 依赖

- dataclasses, typing（标准库）
- 无内部依赖
