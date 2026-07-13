# resource 模块文档

## 需求

管道实例的配额管理和活跃计数。需要：

1. 按管道类型定义配额（最大实例数 + 最大迭代次数）
2. 创建前检查是否超过配额（`can_create`）
3. 维护活跃管道计数（`register`/`release`）
4. 纯内存检查，不依赖外部存储

## 逻辑

### 资源管理流程

```
ResourceManager(quotas)
  can_create(pipeline_type) → bool  # 活跃数 < max_pipelines
  register(pipeline_type)           # 活跃数 +1
  release(pipeline_type)            # 活跃数 -1（不低于 0）
```

- 按管道类型分别计数，未配置的类型使用 `"default"` 配额
- `release` 有下界保护：`max(0, count - 1)`，防止负数
- `can_create` 对未知的 `pipeline_type` 自动回退到 `"default"` 配额

### 精简原则

| 旧代码 | 新代码 | 理由 |
|--------|--------|------|
| psutil CPU/内存检查 | 纯配额数检查 | 避免外部依赖，配额足够 |
| 数据库查询 | 内存 dict | 框架级不需要持久化 |

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `resource.py` | `ResourceQuota` | 资源配额数据类 |
| `resource.py` | `ResourceManager` | 配额检查与活跃计数管理器 |

### 类关系

```
ResourceQuota        — 被ResourceManager持有
ResourceManager      — 管理多个ResourceQuota
```

### 依赖

- 无外部依赖
- 无内部依赖
