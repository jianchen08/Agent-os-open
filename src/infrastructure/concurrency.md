# concurrency 模块文档

## 需求

三级并发控制，为管道执行提供不同粒度的限流能力：

1. **provider 级**：限制同一 LLM Provider 的并发请求数（如 OpenAI 3 并发）
2. **model 级**：限制同一模型的并发请求数（如 GPT-4 5 并发）
3. **agent 级**：限制同一 Agent 管道的并发数（如主 Agent 10 并发）

需要异步上下文管理器接口（`async with acquire`），自动获取和释放信号量。

## 逻辑

### 并发控制流程

```
ConcurrencyController(config)
  ├── provider 信号量（默认 3）
  ├── model    信号量（默认 5）
  └── agent    信号量（默认 10）

async with controller.acquire("agent"):
    # 受信号量保护的临界区
    # 超出并发限制时自动等待
```

- 初始化时通过 `config` 字典配置各级别最大并发数
- `acquire(level)` 使用 `@asynccontextmanager`，自动管理信号量的获取和释放
- `available` 属性返回各级别当前可用许可数（读取 `Semaphore._value`）
- 无效级别抛出 `ValueError`

### 精简原则

| 旧代码 | 新代码 | 理由 |
|--------|--------|------|
| 单例模式 + 线程锁 | 普通实例 + asyncio | 纯异步框架不需要线程安全 |
| 全局共享实例 | 由管道持有 | 生命周期由管道管理 |

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `concurrency.py` | `ConcurrencyController` | 三级信号量并发控制器 |

### 依赖

- asyncio（标准库）
- contextlib.asynccontextmanager（标准库）
- 无内部依赖
